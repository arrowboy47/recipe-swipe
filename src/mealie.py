"""Thin Mealie API client - only what the ingestion pipeline needs.

Credentials come from ``~/.hermes/scripts/.mealie_automation.env`` (chmod 600),
matching the existing ``.nextcloud_automation.env`` convention. Never inline a
token here.
"""

import os
from pathlib import Path

import requests

from .canonical import canonicalize

ENV_FILE = Path.home() / ".hermes/scripts/.mealie_automation.env"
TIMEOUT = 60


class MealieError(RuntimeError):
    pass


def load_env() -> dict:
    """Read MEALIE_URL / MEALIE_TOKEN, preferring the process environment."""
    env = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                env[key.strip()] = value.strip().strip("'\"")
    for key in ("MEALIE_URL", "MEALIE_TOKEN"):
        if os.environ.get(key):
            env[key] = os.environ[key]
    missing = {"MEALIE_URL", "MEALIE_TOKEN"} - env.keys()
    if missing:
        raise MealieError(f"{ENV_FILE} missing {', '.join(sorted(missing))}")
    return env


def _session(env) -> requests.Session:
    sess = requests.Session()
    sess.headers["Authorization"] = f"Bearer {env['MEALIE_TOKEN']}"
    return sess


def existing_org_urls(env=None, session=None) -> set[str]:
    """Canonical source URLs of every recipe already in Mealie.

    Mealie stores the URL a recipe was imported from as ``orgURL``, which makes
    it the natural dedup key: a recipe already in the library should never be
    staged again.

    One request with ``perPage=-1``. That is fine for a library of hundreds; if
    it ever grows into the tens of thousands this should page instead, or filter
    with ``queryFilter=orgURL IN [...]`` over just the candidates in hand.
    """
    env = env or load_env()
    sess = session or _session(env)
    try:
        resp = sess.get(f"{env['MEALIE_URL']}/api/recipes",
                        params={"perPage": -1}, timeout=TIMEOUT)
    except requests.RequestException as e:
        raise MealieError(f"could not reach Mealie: {e}") from e
    if not resp.ok:
        raise MealieError(f"GET /api/recipes returned {resp.status_code}")

    urls = set()
    for item in resp.json().get("items", []):
        raw = item.get("orgURL")
        if not raw:
            continue
        try:
            urls.add(canonicalize(raw))
        except ValueError:
            continue
    return urls


PROMOTE_TIMEOUT = 300  # Mealie fetches and processes the hero image server-side
                       # during import; observed exceeding 60s on a real recipe.


def promote(record: dict, env=None, session=None) -> str:
    """Import a staged record into Mealie and return its slug.

    Sends the stored JSON-LD rather than the URL, so Mealie parses our durable
    copy and the import still works if the source page has since disappeared.
    """
    import json

    env = env or load_env()
    sess = session or _session(env)

    data = record.get("jsonld")
    payload = {
        "url": record["canonical_url"],
        "data": json.dumps(data) if data else (record.get("html") or ""),
        "includeTags": True,
        "includeCategories": True,
    }
    resp = sess.post(f"{env['MEALIE_URL']}/api/recipes/create/html-or-json",
                     json=payload, timeout=PROMOTE_TIMEOUT)
    if not resp.ok:
        raise MealieError(f"promote failed for {record['canonical_url']}: "
                          f"{resp.status_code} {resp.text[:200]}")
    return resp.json()


def tag_recipes(slugs: list[str], tag_name: str, env=None, session=None) -> None:
    """Apply one tag across recipes, for the Vegan/Vegetarian diet label.

    Promotion carries over the *site's* tags but not our diet decision, so the
    caller has to apply it explicitly.
    """
    if not slugs:
        return
    env = env or load_env()
    sess = session or _session(env)

    resp = sess.get(f"{env['MEALIE_URL']}/api/organizers/tags",
                    params={"search": tag_name, "perPage": 100}, timeout=TIMEOUT)
    tag = None
    if resp.ok:
        for item in resp.json().get("items", []):
            if item["name"].lower() == tag_name.lower():
                tag = item
                break
    if tag is None:
        created = sess.post(f"{env['MEALIE_URL']}/api/organizers/tags",
                            json={"name": tag_name}, timeout=TIMEOUT)
        if not created.ok:
            raise MealieError(f"could not create tag {tag_name}: {created.status_code}")
        tag = created.json()

    resp = sess.post(f"{env['MEALIE_URL']}/api/recipes/bulk-actions/tag",
                     json={"recipes": slugs, "tags": [tag]}, timeout=TIMEOUT)
    if not resp.ok:
        raise MealieError(f"bulk tag failed: {resp.status_code} {resp.text[:200]}")
