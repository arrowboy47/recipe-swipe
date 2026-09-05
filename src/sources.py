"""Loader and validator for ``sources.json``.

The registry is data, not code: a site changing its REST surface should be a
one-line edit there, never a code change. This module's job is to fail loudly
when an entry is malformed, so a typo surfaces at load time instead of halfway
through a weekly run.
"""

import json
from pathlib import Path

REGISTRY = Path(__file__).resolve().parent.parent / "sources.json"

METHODS = {"wprm", "wp_category", "wp_posts", "sitemap"}
DIETS = {"vegan", "vegetarian", "mixed"}

# Every key an entry is allowed to carry. Anything else is a typo or a field
# somebody added without telling the loader, and both should be errors.
ALLOWED = {
    "domain", "base", "name", "diet", "method", "count", "enabled", "note",
    "vegan_category_id", "vegetarian_category_id", "vegan_count",
    "vegetarian_count", "url_pattern", "diet_taxonomy",
}
REQUIRED = {"domain", "base", "name", "diet", "method", "count", "enabled"}


class RegistryError(ValueError):
    """Raised when sources.json does not describe a usable source."""


def _validate(entry: dict) -> None:
    where = entry.get("domain", "<no domain>")

    missing = REQUIRED - entry.keys()
    if missing:
        raise RegistryError(f"{where}: missing {', '.join(sorted(missing))}")

    unknown = entry.keys() - ALLOWED
    if unknown:
        raise RegistryError(f"{where}: unknown field(s) {', '.join(sorted(unknown))}")

    if entry["method"] not in METHODS:
        raise RegistryError(f"{where}: method {entry['method']!r} not in {sorted(METHODS)}")

    if entry["diet"] not in DIETS:
        raise RegistryError(f"{where}: diet {entry['diet']!r} not in {sorted(DIETS)}")

    if not str(entry["base"]).startswith("https://"):
        raise RegistryError(f"{where}: base must be an https URL, got {entry['base']!r}")

    # Harvest-readiness. A disabled entry is allowed to be incomplete - that is
    # usually *why* it is disabled - so these only apply once it is switched on.
    if not entry["enabled"]:
        return

    # A mixed site is only harvestable if we know which category is the veg one.
    if entry["diet"] == "mixed" and "vegan_category_id" not in entry:
        raise RegistryError(f"{where}: diet 'mixed' requires vegan_category_id")

    if entry["method"] == "wp_category" and "vegan_category_id" not in entry:
        raise RegistryError(f"{where}: method 'wp_category' requires vegan_category_id")

    # Sitemaps list every page on the site, so we need a way to tell recipes
    # apart from everything else.
    if entry["method"] == "sitemap" and "url_pattern" not in entry:
        raise RegistryError(f"{where}: method 'sitemap' requires url_pattern")


def load_sources(enabled_only: bool = True, names: list[str] | None = None) -> list[dict]:
    """Return validated source entries.

    ``names`` filters by domain, and is what ``--source a,b`` on the CLI maps
    onto. An unknown name is an error rather than an empty result, so a typo in
    a manual pull does not look like "that site had nothing new".
    """
    raw = json.loads(REGISTRY.read_text())
    if raw.get("schema") != 1:
        raise RegistryError(f"unsupported registry schema {raw.get('schema')!r}")

    entries = raw["sources"]
    for entry in entries:
        _validate(entry)

    domains = {e["domain"] for e in entries}
    if names:
        unknown = set(names) - domains
        if unknown:
            raise RegistryError(f"unknown source(s): {', '.join(sorted(unknown))}")
        entries = [e for e in entries if e["domain"] in set(names)]

    if enabled_only:
        entries = [e for e in entries if e["enabled"]]

    return entries


def diet_tag(entry: dict, matched_vegan_category: bool = False) -> str:
    """The Mealie tag a recipe from this source should carry.

    All-vegan sites tag everything ``Vegan``. Vegetarian sites tag everything
    ``Vegetarian``, upgrading to ``Vegan`` for the subset that matched the
    site's own vegan category. Mixed sites depend entirely on which category
    the recipe came out of.
    """
    if entry["diet"] == "vegan":
        return "Vegan"
    if matched_vegan_category:
        return "Vegan"
    return "Vegetarian"
