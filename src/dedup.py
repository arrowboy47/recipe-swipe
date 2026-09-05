"""Duplicate detection for both ends of the pipeline.

Two places can put a recipe into Mealie, and until now they guarded themselves
differently: `pull()` checked a set of source URLs before staging, while the
approval path checked nothing at all. That asymmetry is how four duplicate
pairs reached the library - a promotion that succeeded but failed to file its
record left the record in `pending/`, and the next swipe imported it again.

So both ends now ask the same object the same question.

**Two keys, deliberately not treated alike.**

`orgURL` is the strong key: Mealie stores the URL a recipe was imported from,
and a match there is proof of the same import. It is also incomplete - a recipe
created by the AI importer or typed in by hand has no `orgURL` at all (4 of 48
in this library), and is invisible to a URL check no matter how careful.

Title is the key that covers those, and it is weak on its own: "Gazpacho" from
two different blogs is two legitimate recipes, and silently dropping the second
would lose real work.

So the two keys carry different authority, and callers are expected to treat
them differently:

- `certain` (same source URL) - safe to block outright.
- `likely` (same normalized title) - **must not silently discard anything.**
  A pull skips these, because staging is cheap to redo and the point of a deck
  is not to show you the same dish twice; the summary names every one it
  passed over. An approval does *not* block on them - you chose that card, so
  it gets imported and the app tells you what it resembles.

A first attempt keyed titles on `(title, ingredient_count)` to make the weak
key strong. That was dead code: `GET /api/recipes` returns `RecipeSummary`
objects with no `recipeIngredient` field at all (INTEGRATION.md section 3, "the
recipe list endpoint is a trap"), so every count was -1 and no title ever
matched. Paying 48 extra requests to fix that is not worth it - the confidence
split above handles the weakness honestly instead of hiding it.

Nothing here deletes anything. The strongest action is to decline to create.
"""

import re
import unicodedata

import requests

from .canonical import canonicalize
from .mealie import MealieError, TIMEOUT, _session, load_env

# Words that appear in a title without changing which dish it is. Stripping
# them stops "Easy Vegan Gazpacho (1 Bowl!)" and "Gazpacho" from reading as two
# different recipes when they are the same import twice.
_NOISE = {
    "easy", "best", "simple", "quick", "the", "a", "an", "and", "or", "with",
    "vegan", "vegetarian", "plant", "based", "recipe", "recipes", "homemade",
    "healthy", "creamy", "crispy", "perfect", "ultimate", "classic", "minute",
    "minutes", "bowl", "pot", "pan", "ingredient", "ingredients", "copycat",
}


def normalize_title(title: str) -> str:
    """A title reduced to the words that identify the dish.

    Accents folded, punctuation dropped, noise words removed, remainder sorted -
    so word order and marketing adjectives can't disguise a repeat.
    """
    if not title:
        return ""
    folded = unicodedata.normalize("NFKD", str(title))
    folded = "".join(c for c in folded if not unicodedata.combining(c))
    words = re.findall(r"[a-z0-9]+", folded.lower())
    kept = [w for w in words if w not in _NOISE and not w.isdigit()]
    return " ".join(sorted(kept or words))


class Duplicate:
    """Why something was judged a duplicate, and of what."""

    def __init__(self, slug: str, reason: str, confidence: str):
        self.slug = slug
        self.reason = reason
        self.confidence = confidence      # "certain" | "likely"

    def __repr__(self):
        return f"<Duplicate {self.slug} ({self.confidence}: {self.reason})>"

    def __str__(self):
        return f"{self.slug} ({self.reason})"


class RecipeIndex:
    """Everything already in Mealie, indexed both ways.

    Built once and reused: a pull asks it hundreds of times, and it is the same
    answer each time. The approval path builds one per promotion, which is a
    single request against a library this size.
    """

    def __init__(self, by_url: dict, by_title: dict, total: int):
        self.by_url = by_url
        self.by_title = by_title
        self.total = total

    @classmethod
    def build(cls, env=None, session=None) -> "RecipeIndex":
        env = env or load_env()
        sess = session or _session(env)
        try:
            resp = sess.get(f"{env['MEALIE_URL']}/api/recipes",
                            params={"perPage": -1}, timeout=TIMEOUT)
        except requests.RequestException as e:
            raise MealieError(f"could not reach Mealie: {e}") from e
        if not resp.ok:
            raise MealieError(f"GET /api/recipes returned {resp.status_code}")

        items = resp.json().get("items", [])
        # Oldest first, so `setdefault` below keeps the original rather than a
        # later duplicate. The API does not promise creation order, and without
        # this the index happily pointed at `gazpacho-1` instead of `gazpacho`.
        items.sort(key=lambda i: i.get("createdAt") or "")
        by_url, by_title = {}, {}
        for item in items:
            slug = item.get("slug")
            if not slug:
                continue
            raw = item.get("orgURL")
            if raw:
                try:
                    by_url.setdefault(canonicalize(raw), slug)
                except ValueError:
                    pass
            norm = normalize_title(item.get("name"))
            if norm:
                by_title.setdefault(norm, slug)
        return cls(by_url, by_title, len(items))

    def find(self, url=None, title=None) -> Duplicate | None:
        """The existing recipe this would duplicate, or None.

        URL first: it is the only key that proves the same import. Title is
        consulted only when the URL says nothing, and its verdict is `likely` -
        see the module docstring for what each caller owes that distinction.
        """
        if url:
            try:
                slug = self.by_url.get(canonicalize(url))
            except ValueError:
                slug = None
            if slug:
                return Duplicate(slug, "same source URL", "certain")

        if title:
            norm = normalize_title(title)
            slug = self.by_title.get(norm) if norm else None
            if slug:
                return Duplicate(slug, f"same title ({norm!r})", "likely")
        return None


def find_duplicate_groups(env=None, session=None) -> list[dict]:
    """Duplicates that already exist in the library, for reporting.

    Read-only and deliberately so: this reports, a human decides. Groups are
    ordered oldest-first, because the oldest is the one anything else in the
    system will have linked to.
    """
    env = env or load_env()
    sess = session or _session(env)
    resp = sess.get(f"{env['MEALIE_URL']}/api/recipes",
                    params={"perPage": -1}, timeout=TIMEOUT)
    if not resp.ok:
        raise MealieError(f"GET /api/recipes returned {resp.status_code}")

    buckets: dict = {}
    for item in resp.json().get("items", []):
        raw = item.get("orgURL")
        key = None
        if raw:
            try:
                key = ("url", canonicalize(raw))
            except ValueError:
                key = None
        if key is None:
            norm = normalize_title(item.get("name"))
            if not norm:
                continue
            key = ("title", norm)
        buckets.setdefault(key, []).append(item)

    groups = []
    for key, items in buckets.items():
        if len(items) < 2:
            continue
        items.sort(key=lambda i: i.get("createdAt") or "")
        groups.append({
            "key": key[0],
            "value": key[1],
            "keep": items[0]["slug"],
            "duplicates": [i["slug"] for i in items[1:]],
            "items": items,
        })
    return groups
