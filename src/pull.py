"""The pull driver - shared by the weekly cron and the `pull-recipes` skill.

Round-robins across enabled sources so no single prolific blog fills a batch,
skips anything already seen or blacklisted, and stops once ``count`` recipes
have actually been staged.

Diet comes from the source registry or the site's own category id. There is no
meat-word title regex: the old one silently discarded "Vegan Butter Chicken".

    python3 -m src.pull --count 20
    python3 -m src.pull --count 5 --source rainbowplantlife.com --course dinner
    python3 -m src.pull --count 3 --dry-run
"""

import argparse
import sys
from concurrent.futures import ThreadPoolExecutor

import requests

from .canonical import canonicalize
from .extract import ExtractError, extract
from .harvest import HarvestError, harvest
from .mealie import MealieError, existing_org_urls
from .sources import diet_tag, load_sources
from .stage import HOST, ROOT, Stager, build_record

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0 Safari/537.36 (+homelab recipe indexer, contact via site owner)")
IMAGE_TIMEOUT = 30
MAX_IMAGE_BYTES = 8 * 1024 * 1024


def _fetch_image(sess, url):
    """Best effort. A missing hero image is not a reason to drop a recipe."""
    if not url:
        return None
    try:
        resp = sess.get(url, headers={"User-Agent": UA}, timeout=IMAGE_TIMEOUT, stream=True)
        if not resp.ok:
            return None
        data = resp.raw.read(MAX_IMAGE_BYTES + 1, decode_content=True)
        return data if data and len(data) <= MAX_IMAGE_BYTES else None
    except requests.RequestException:
        return None


def _harvest_all(entries, per_source, after, search, course):
    """Enumerate candidates from every source in parallel.

    Listing pages are cheap and this is the only place parallelism pays; the
    per-recipe fetches below stay sequential so we stay a polite guest.
    """
    results, errors = {}, []

    def one(entry):
        try:
            return entry["domain"], harvest(entry, limit=per_source, after=after,
                                            search=search, course=course), None
        except (HarvestError, Exception) as e:      # noqa: BLE001 - reported, not raised
            return entry["domain"], [], f"{entry['domain']}: {type(e).__name__}: {e}"

    with ThreadPoolExecutor(max_workers=6) as pool:
        for domain, candidates, err in pool.map(one, entries):
            results[domain] = candidates
            if err:
                errors.append(err)
    return results, errors


def _round_robin(queues, order):
    """Yield one candidate at a time, cycling sources until all are empty."""
    while any(queues[d] for d in order):
        for domain in order:
            if queues[domain]:
                yield queues[domain].pop(0)


def pull(count=20, sources=None, course=None, search=None, after=None,
         dry_run=False, host=HOST, root=ROOT):
    """Stage up to ``count`` new recipes. Returns a summary dict."""
    entries = load_sources(enabled_only=True, names=sources)
    by_domain = {e["domain"]: e for e in entries}

    # Over-fetch: most candidates on a normal week are already seen.
    per_source = max(5, count)
    queues, errors = _harvest_all(entries, per_source, after, search, course)
    order = [e["domain"] for e in entries]

    staged, skipped = [], []
    newest = {}
    sess = requests.Session()

    # Anything already in Mealie must never be staged again, or the Phase 3 app
    # would promote a duplicate. Mealie keeps the source URL as `orgURL`.
    # A pull is still useful if Mealie is down, so this is a warning, not a stop.
    try:
        in_mealie = existing_org_urls()
    except MealieError as e:
        in_mealie = set()
        errors.append(f"Mealie dedup unavailable ({e}); staged recipes may duplicate the library")

    with Stager(host=host, root=root) as st:
        for cand in _round_robin(queues, order):
            if len(staged) >= count:
                break

            entry = by_domain[cand.source]
            if cand.date:
                prev = newest.get(cand.source, "")
                newest[cand.source] = max(prev, cand.date) if prev else cand.date

            if st.is_known(cand.url):
                continue

            if canonicalize(cand.url) in in_mealie:
                if not dry_run:
                    st.note_skip(cand.url, "already in Mealie", cand.source)
                continue

            try:
                got = extract(cand.url, session=sess)
            except ExtractError as e:
                skipped.append(f"{cand.source}: {cand.url} — {e}")
                if not dry_run:
                    st.note_skip(cand.url, "fetch failed", cand.source)
                continue

            if not got["jsonld"] and not got["html"]:
                skipped.append(f"{cand.source}: {cand.url} — nothing extractable")
                if not dry_run:
                    st.note_skip(cand.url, "no recipe data", cand.source)
                continue

            tag = diet_tag(entry, matched_vegan_category=cand.matched_vegan)
            record = build_record(cand, entry, got, tag)

            if dry_run:
                staged.append(record)
                continue

            image = _fetch_image(sess, got.get("image_url"))
            if st.stage(record, image):
                staged.append(record)

        if not dry_run:
            for domain, value in newest.items():
                st.set_cursor(domain, value)
        else:
            # Nothing buffered and no state touched, so make the flush a no-op.
            st._staged.clear()

    return {"staged": staged, "skipped": skipped, "errors": errors,
            "dry_run": dry_run, "requested": count}


def format_summary(result) -> str:
    verb = "would stage" if result["dry_run"] else "staged"
    lines = [f"Recipe pull: {verb} {len(result['staged'])} of {result['requested']} requested"]
    for rec in result["staged"]:
        p = rec["preview"]
        bits = [f"{p.get('ingredient_count', '?')} ing"]
        if p.get("total_minutes"):
            bits.append(f"{p['total_minutes']}m")
        lines.append(f"  + [{rec['diet']}] {p.get('title') or rec['canonical_url']} "
                     f"({rec['source']}, {', '.join(bits)})")
    if result["skipped"]:
        lines.append(f"{len(result['skipped'])} skipped:")
        lines += [f"  - {s}" for s in result["skipped"][:5]]
    if result["errors"]:
        lines.append("Source errors:")
        lines += [f"  ! {e}" for e in result["errors"][:5]]
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="pull-recipes",
                                 description="Stage new vegan/vegetarian recipes.")
    ap.add_argument("--count", type=int, default=20, help="recipes to stage (default 20)")
    ap.add_argument("--source", help="comma-separated domains from sources.json")
    ap.add_argument("--course", help="e.g. dinner, breakfast, dessert, soup")
    ap.add_argument("--search", help="keyword filter")
    ap.add_argument("--after", help="only posts after this date, YYYY-MM-DD")
    ap.add_argument("--dry-run", action="store_true", help="report without writing")
    ap.add_argument("--root", default=ROOT, help="staging root")
    ap.add_argument("--local", action="store_true",
                    help="write to --root on this machine instead of over SSH")
    args = ap.parse_args(argv)

    try:
        result = pull(
            count=args.count,
            sources=[s.strip() for s in args.source.split(",")] if args.source else None,
            course=args.course, search=args.search, after=args.after,
            dry_run=args.dry_run, host=None if args.local else HOST, root=args.root,
        )
    except Exception as e:                       # noqa: BLE001 - top level reporting
        print(f"Recipe pull failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    print(format_summary(result))
    # "Nothing new" is a normal, quiet outcome. Only a total source failure is an error.
    return 1 if result["errors"] and not result["staged"] else 0


if __name__ == "__main__":
    sys.exit(main())
