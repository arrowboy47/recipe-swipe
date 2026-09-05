"""Turn a source registry entry into candidate recipe URLs, newest first.

Nothing here fetches a recipe page - it only enumerates URLs cheaply, so the
pull driver can decide what is worth fetching. ``X-WP-Total`` gives the size of
an archive before a single record is downloaded, which is how the weekly run
knows what is new without walking history.

Four methods, matching ``sources.json``:

``wprm``         the WP Recipe Maker custom post type; the canonical permalink
                 is in ``.link`` and the WPRM taxonomies come along for free
``wp_category``  ordinary posts filtered to the site's own vegan/vegetarian
                 category ids
``wp_posts``     ordinary posts, for all-vegan sites with no WPRM post type
``sitemap``      for sites whose REST API is absent or blocked
"""

import html
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

import requests

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0 Safari/537.36 (+homelab recipe indexer, contact via site owner)")
TIMEOUT = 30
PAGE_SIZE = 100
SITEMAP_NS = "{http://www.sitemaps.org/schemas/sitemap/0.9}"


@dataclass
class Candidate:
    """One recipe URL we might stage, plus what the listing told us about it."""
    url: str
    source: str
    title: str = ""
    date: str = ""
    matched_vegan: bool = False
    categories: list[int] = field(default_factory=list)


class HarvestError(RuntimeError):
    pass


def _session() -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = UA
    return s


def _get(sess, url, params=None, *, retries=1):
    """One GET with a single retry on 5xx. Never hammers: these are other
    people's blogs and we are a guest."""
    last = None
    for attempt in range(retries + 1):
        try:
            r = sess.get(url, params=params, timeout=TIMEOUT)
        except requests.RequestException as e:
            last = e
            if attempt < retries:
                time.sleep(2)
                continue
            raise HarvestError(f"{url}: {e}") from e
        if r.status_code >= 500 and attempt < retries:
            time.sleep(2)
            continue
        return r
    raise HarvestError(f"{url}: {last}")


def _wp_pages(sess, endpoint, params, limit):
    """Yield WP REST records across pages, newest first, stopping at ``limit``.

    WP answers a request past the end with 400 ``rest_post_invalid_page_number``
    rather than an empty list, so that is a normal stop condition, not an error.
    """
    got = 0
    page = 1
    while limit is None or got < limit:
        page_params = dict(params)
        page_params.update({"per_page": PAGE_SIZE, "page": page})
        r = _get(sess, endpoint, page_params)

        if r.status_code == 400 and "invalid_page_number" in r.text:
            return
        if r.status_code == 403:
            raise HarvestError(f"{endpoint}: 403 - REST API blocked, use the sitemap method")
        if not r.ok:
            raise HarvestError(f"{endpoint}: HTTP {r.status_code} {r.text[:160]}")

        batch = r.json()
        if not isinstance(batch, list) or not batch:
            return

        for rec in batch:
            yield rec
            got += 1
            if limit is not None and got >= limit:
                return

        if len(batch) < PAGE_SIZE:
            return
        page += 1


def archive_size(entry: dict) -> int | None:
    """Total records available, from ``X-WP-Total``. None when not applicable."""
    if entry["method"] == "sitemap":
        return None
    sess = _session()
    endpoint = _endpoint(entry)
    r = _get(sess, endpoint, {"per_page": 1, **_base_params(entry)})
    total = r.headers.get("X-WP-Total")
    return int(total) if total else None


def _endpoint(entry: dict) -> str:
    base = entry["base"].rstrip("/")
    path = "wprm_recipe" if entry["method"] == "wprm" else "posts"
    return f"{base}/wp-json/wp/v2/{path}"


def _base_params(entry: dict) -> dict:
    """Category filter that is inherent to the source, independent of the run."""
    params = {}
    if entry["method"] == "wp_category":
        # OR semantics: fetch the vegan and vegetarian categories in one stream
        # and decide each post's tag from its own `categories` array.
        ids = [entry["vegan_category_id"]]
        if "vegetarian_category_id" in entry:
            ids.append(entry["vegetarian_category_id"])
        params["categories"] = ",".join(str(i) for i in ids)
    return params


def _resolve_course(sess, entry: dict, course: str) -> str | None:
    """Map a course name like 'dinner' to its wprm_course term id."""
    base = entry["base"].rstrip("/")
    r = _get(sess, f"{base}/wp-json/wp/v2/wprm_course", {"per_page": 100})
    if not r.ok:
        return None
    for term in r.json():
        if term.get("name", "").strip().lower() == course.strip().lower():
            return str(term["id"])
    return None


def _harvest_wp(entry, limit, after, search, course) -> list[Candidate]:
    sess = _session()
    endpoint = _endpoint(entry)

    params = _base_params(entry)
    params["_fields"] = "link,date,title,categories"
    params["orderby"] = "date"
    params["order"] = "desc"
    if after:
        params["after"] = f"{after}T00:00:00"
    if search:
        params["search"] = search

    if course and entry["method"] == "wprm":
        term_id = _resolve_course(sess, entry, course)
        if term_id is None:
            return []          # site has no such course; nothing to offer
        params["wprm_course"] = term_id
    elif course:
        # Non-WPRM sites have no course taxonomy. Approximate with a search
        # rather than silently ignoring the filter.
        params["search"] = f"{search} {course}".strip() if search else course

    vegan_id = entry.get("vegan_category_id")
    out = []
    for rec in _wp_pages(sess, endpoint, params, limit):
        link = rec.get("link")
        if not link:
            continue
        cats = rec.get("categories") or []
        raw_title = rec.get("title")
        title = raw_title.get("rendered", "") if isinstance(raw_title, dict) else (raw_title or "")
        out.append(Candidate(
            url=link,
            source=entry["domain"],
            # WP returns rendered titles, so entities arrive escaped.
            title=html.unescape(title).strip(),
            date=rec.get("date", ""),
            matched_vegan=bool(vegan_id and vegan_id in cats),
            categories=cats,
        ))
    return out


def _sitemap_urls(sess, url, seen_maps=None) -> list[str]:
    """Flatten a sitemap, following a <sitemapindex> one level into its children."""
    seen_maps = seen_maps if seen_maps is not None else set()
    if url in seen_maps:
        return []
    seen_maps.add(url)

    r = _get(sess, url)
    if not r.ok:
        raise HarvestError(f"{url}: HTTP {r.status_code}")

    root = ET.fromstring(r.content)
    locs = [el.text.strip() for el in root.iter(f"{SITEMAP_NS}loc") if el.text]

    if root.tag.endswith("sitemapindex"):
        nested = []
        for child in locs:
            nested.extend(_sitemap_urls(sess, child, seen_maps))
        return nested
    return locs


def _harvest_sitemap(entry, limit, search) -> list[Candidate]:
    sess = _session()
    base = entry["base"].rstrip("/")
    pattern = entry["url_pattern"]

    urls = None
    for candidate_map in (f"{base}/sitemap_index.xml", f"{base}/sitemap.xml"):
        try:
            urls = _sitemap_urls(sess, candidate_map)
            break
        except HarvestError:
            continue
    if urls is None:
        raise HarvestError(f"{entry['domain']}: no readable sitemap")

    hits = [u for u in urls if pattern in u]
    if search:
        needle = search.lower()
        hits = [u for u in hits if needle in u.lower()]

    # Sitemaps carry no reliable ordering, so this is "some recipes", not
    # "the newest recipes". The pull driver's seen-state keeps runs moving.
    if limit is not None:
        hits = hits[:limit]

    return [Candidate(url=u, source=entry["domain"]) for u in hits]


def harvest(entry: dict, limit: int | None = None, after: str | None = None,
            search: str | None = None, course: str | None = None) -> list[Candidate]:
    """Candidate recipe URLs for one source, newest first where the site says so."""
    if entry["method"] == "sitemap":
        if after or course:
            # Neither is expressible against a bare sitemap; say so instead of
            # returning results that quietly ignore the filter.
            return []
        return _harvest_sitemap(entry, limit, search)
    return _harvest_wp(entry, limit, after, search, course)
