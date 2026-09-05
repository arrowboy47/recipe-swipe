"""Fetch a recipe page and lift out the schema.org Recipe block.

Storing the JSON-LD rather than the URL is what makes a staged recipe durable:
it is a few KB, it is exactly what Mealie's scraper reads, and a recipe staged
today still imports cleanly if the blog deletes the post next month. Promotion
is ``POST /api/recipes/create/html-or-json`` with this block as ``data``, so
Mealie does its own canonical parse at approval time.

Where a page carries no JSON-LD Recipe we keep the whole HTML instead and let
Mealie's other strategies deal with it.
"""

import html
import json
import re

import requests
from bs4 import BeautifulSoup

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0 Safari/537.36 (+homelab recipe indexer, contact via site owner)")
TIMEOUT = 30

ISO_DURATION = re.compile(
    r"^P(?:(?P<d>\d+)D)?(?:T(?:(?P<h>\d+)H)?(?:(?P<m>\d+)M)?(?:(?P<s>\d+)S)?)?$")


class ExtractError(RuntimeError):
    pass


def _types(node) -> set[str]:
    t = node.get("@type") if isinstance(node, dict) else None
    if isinstance(t, str):
        return {t}
    if isinstance(t, list):
        return {x for x in t if isinstance(x, str)}
    return set()


def _walk(node):
    """Yield every dict in an arbitrarily nested JSON-LD document."""
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item)


def find_recipe_node(document) -> dict | None:
    """The first node whose @type includes Recipe, wherever it is nested.

    Real pages put it at the top level, inside a list, or inside an @graph
    alongside Organization/WebPage/BreadcrumbList nodes.
    """
    for node in _walk(document):
        if "Recipe" in _types(node):
            return node
    return None


def _first_image_url(value) -> str | None:
    """schema.org lets `image` be a string, a list, or an ImageObject."""
    if isinstance(value, str):
        return value or None
    if isinstance(value, dict):
        return _first_image_url(value.get("url"))
    if isinstance(value, list):
        for item in value:
            got = _first_image_url(item)
            if got:
                return got
    return None


def _text_of(value) -> str:
    """Plain text from a string or a schema.org node.

    Sites routinely leave HTML entities inside JSON-LD strings, so unescape
    rather than carrying "Salads &amp; Sides" onto a card.
    """
    if isinstance(value, str):
        return html.unescape(value).strip()
    if isinstance(value, dict):
        return html.unescape(str(value.get("text") or value.get("name") or "")).strip()
    return ""


def _count_instructions(value) -> int:
    """Steps, counting through HowToSection wrappers rather than counting them."""
    if isinstance(value, str):
        # A single blob of prose. Treat it as one step; Mealie will re-split it.
        return 1 if value.strip() else 0
    if isinstance(value, dict):
        if "HowToSection" in _types(value):
            return _count_instructions(value.get("itemListElement") or [])
        return 1 if _text_of(value) else 0
    if isinstance(value, list):
        return sum(_count_instructions(v) for v in value)
    return 0


def _as_list(value) -> list[str]:
    if isinstance(value, str):
        # schema.org keywords are conventionally comma-separated.
        return [p.strip() for p in html.unescape(value).split(",") if p.strip()]
    if isinstance(value, list):
        return [_text_of(v) for v in value if _text_of(v)]
    if isinstance(value, dict):
        return [_text_of(value)] if _text_of(value) else []
    return []


def iso_to_minutes(value) -> int | None:
    """ISO-8601 duration to whole minutes, for sorting and display."""
    if not isinstance(value, str):
        return None
    m = ISO_DURATION.match(value.strip())
    if not m:
        return None
    d, h, mi, s = (int(m.group(k) or 0) for k in ("d", "h", "m", "s"))
    total = d * 1440 + h * 60 + mi + (1 if s >= 30 else 0)
    return total or None


def _servings(node) -> str | int | None:
    y = node.get("recipeYield")
    if isinstance(y, list):
        y = y[0] if y else None
    if isinstance(y, (int, float)):
        return int(y)
    if isinstance(y, str):
        m = re.search(r"\d+", y)
        return int(m.group()) if m else y.strip() or None
    return None


def build_preview(node: dict) -> dict:
    """The fields a swipe card needs, so the app never refetches the source."""
    total = node.get("totalTime")
    return {
        "title": _text_of(node.get("name")) or "",
        "description": _text_of(node.get("description"))[:600],
        "image": None,                       # filled in by the stager
        "total_time": total if isinstance(total, str) else None,
        "total_minutes": iso_to_minutes(total),
        "servings": _servings(node),
        "ingredient_count": len(_as_list(node.get("recipeIngredient"))),
        "instruction_count": _count_instructions(node.get("recipeInstructions")),
        "course": _as_list(node.get("recipeCategory")),
        "keywords": _as_list(node.get("keywords"))[:12],
    }


def extract(url: str, session: requests.Session | None = None) -> dict:
    """Fetch ``url`` and return ``{jsonld, html, preview, image_url}``.

    ``jsonld`` is None when the page has no schema.org Recipe, in which case
    ``html`` holds the full document instead.
    """
    sess = session or requests.Session()
    try:
        resp = sess.get(url, headers={"User-Agent": UA}, timeout=TIMEOUT)
    except requests.RequestException as e:
        raise ExtractError(f"{url}: {e}") from e

    if not resp.ok:
        raise ExtractError(f"{url}: HTTP {resp.status_code}")

    soup = BeautifulSoup(resp.text, "lxml")

    node = None
    for tag in soup.find_all("script", type="application/ld+json"):
        raw = tag.string or tag.get_text() or ""
        if not raw.strip():
            continue
        try:
            document = json.loads(raw)
        except json.JSONDecodeError:
            continue                     # a malformed block is not fatal
        node = find_recipe_node(document)
        if node:
            break

    if node is None:
        return {
            "jsonld": None,
            "html": resp.text,
            "preview": {"title": (soup.title.string or "").strip() if soup.title else ""},
            "image_url": None,
        }

    preview = build_preview(node)
    if not preview["title"] and soup.title:
        preview["title"] = (soup.title.string or "").strip()

    return {
        "jsonld": node,
        "html": None,
        "preview": preview,
        "image_url": _first_image_url(node.get("image")),
    }
