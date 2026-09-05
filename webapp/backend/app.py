"""Recipe swipe app backend.

Reuses the Phase 1/2 pipeline directly (`src.stage`, `src.pull`, `src.mealie`,
`src.canonical`) rather than reimplementing any of it - this container just
adds a thin HTTP surface + a browser UI on top of the same staging directory
and the same Mealie API the cron jobs already use.

Runs *on thebigbox*, where staging physically lives (`/data/staging`, bind-
mounted from `/media/containers/recipe-staging`), so file access is direct -
no SSH hop the way the Z4 G4-hosted cron needs. Reaches Mealie over the docker
network by container name.

Auth: HTTP Basic, single shared username/password from environment - same
posture as Hermes Dashboard (LAN + WireGuard only, password-gated, no public
DNS). Nothing here is exposed beyond the LAN.

Enforced by middleware covering *every* request, including the static
frontend - not per-route. An earlier version only gated /api/*, which let the
page shell load with no prompt at all; the first thing the page does is
fetch("/api/deck"), and a fetch()-triggered 401 doesn't reliably produce the
browser's native credential prompt the way a full-page navigation does. That
left the deck silently empty with no login ever offered. Gating the initial
document load itself guarantees the native prompt fires before any JS runs,
and the browser then attaches those cached credentials to every fetch() on
the same origin.
"""

import base64
import json
import os
import secrets
import sys
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from src.mealie import MealieError, promote, tag_recipes  # noqa: E402
from src.pull import pull                        # noqa: E402
from src.stage import Stager                     # noqa: E402

STAGING_ROOT = Path(os.environ.get("STAGING_ROOT", "/data/staging"))
AUTH_USER = os.environ.get("SWIPE_AUTH_USER", "aiden")
AUTH_PASS = os.environ.get("SWIPE_AUTH_PASS")

# Dry run: swipes still file the record, but nothing is written to Mealie.
#
# This exists because a dev server pointed at the real MEALIE_URL will happily
# import test swipes into the real library - which is exactly what happened
# during the v2 rewrite, leaving a duplicate recipe behind. Any non-production
# instance should set SWIPE_DRY_RUN=1. It defaults off so the deployed
# container needs no extra configuration to behave normally, and the frontend
# shows a permanent badge when it's on, so the mode is never ambiguous.
DRY_RUN = os.environ.get("SWIPE_DRY_RUN", "").strip().lower() in ("1", "true", "yes", "on")

if not AUTH_PASS:
    raise RuntimeError("SWIPE_AUTH_PASS must be set - refusing to start unauthenticated")

app = FastAPI(title="Recipe Swipe")


def _authorized(authorization: str | None) -> bool:
    if not authorization or not authorization.startswith("Basic "):
        return False
    try:
        user, password = base64.b64decode(authorization[6:]).decode().split(":", 1)
    except Exception:
        return False
    return secrets.compare_digest(user, AUTH_USER) and secrets.compare_digest(password, AUTH_PASS)


@app.middleware("http")
async def require_basic_auth(request: Request, call_next):
    if not _authorized(request.headers.get("authorization")):
        return Response(status_code=401, headers={"WWW-Authenticate": "Basic"})
    return await call_next(request)


def _record_paths(record_id: str) -> tuple[Path, Path]:
    return (STAGING_ROOT / "pending" / f"{record_id}.json",
            STAGING_ROOT / "pending" / f"{record_id}.jpg")


class SwipeRequest(BaseModel):
    direction: str          # "left" or "right"


class PullRequest(BaseModel):
    count: int = 20
    source: str | None = None
    course: str | None = None
    search: str | None = None
    after: str | None = None


@app.get("/api/deck")
def get_deck():
    """Every pending record's preview - small enough (a few hundred recipes at
    most) to just return in full; the frontend handles paging through them."""
    pending = STAGING_ROOT / "pending"
    cards = []
    for path in sorted(pending.glob("*.json")):
        try:
            record = json.loads(path.read_text())
        except json.JSONDecodeError:
            continue
        preview = dict(record["preview"])
        preview["ingredients_peek"] = _ingredients_peek(record)
        cards.append({
            "id": record["id"],
            "source_name": record.get("source_name", record.get("source", "")),
            "diet": record.get("diet"),
            "canonical_url": record["canonical_url"],
            "preview": preview,
            "has_image": (pending / f"{record['id']}.jpg").exists(),
        })
    return {"cards": cards}


def _ingredients_peek(record: dict, limit: int = 6) -> list[str]:
    """A few raw ingredient lines for the card's partial preview - computed at
    request time from the stored JSON-LD rather than persisted, so this stays
    additive to the locked staging record shape (schema: 1) rather than a
    breaking change to it."""
    jsonld = record.get("jsonld")
    if not isinstance(jsonld, dict):
        return []
    ingredients = jsonld.get("recipeIngredient")
    if not isinstance(ingredients, list):
        return []
    return [str(i) for i in ingredients[:limit] if i]


@app.get("/api/deck/{record_id}/detail")
def get_detail(record_id: str):
    """The full staged record, flattened for the detail sheet.

    The deck payload deliberately carries only a short ingredient peek - it's
    sent for every card at once. This is per-card and only fetched when the
    sheet actually opens, so it can afford the whole ingredient list and the
    method, which is what you need to judge a recipe rather than a thumbnail.
    """
    json_path, _ = _record_paths(record_id)
    if not json_path.exists():
        raise HTTPException(404, detail="not in the pending deck")
    record = json.loads(json_path.read_text())
    jsonld = record.get("jsonld") or {}

    ingredients = [str(i).strip() for i in jsonld.get("recipeIngredient") or [] if i]
    return {
        "id": record["id"],
        "ingredients": ingredients,
        "steps": _steps(jsonld),
        "keywords": _keywords(jsonld),
        "yield": jsonld.get("recipeYield"),
        "author": _author(jsonld),
    }


def _steps(jsonld: dict, limit: int = 14) -> list[str]:
    """recipeInstructions is the messiest field in the wild: a bare string, a
    list of strings, a list of HowToStep objects, or HowToSections wrapping
    lists of those. Flatten whatever shape turned up rather than assuming one."""
    raw = jsonld.get("recipeInstructions")
    out: list[str] = []

    def walk(node):
        if len(out) >= limit or node is None:
            return
        if isinstance(node, str):
            text = node.strip()
            if text:
                out.append(text)
        elif isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, dict):
            if node.get("@type") == "HowToSection":
                walk(node.get("itemListElement"))
            else:
                walk(node.get("text") or node.get("name"))

    walk(raw)
    return out[:limit]


def _keywords(jsonld: dict, limit: int = 12) -> list[str]:
    raw = jsonld.get("keywords")
    if isinstance(raw, str):
        parts = [k.strip() for k in raw.split(",")]
    elif isinstance(raw, list):
        parts = [str(k).strip() for k in raw]
    else:
        parts = []
    return [k for k in parts if k][:limit]


def _author(jsonld: dict) -> str | None:
    raw = jsonld.get("author")
    if isinstance(raw, dict):
        return raw.get("name")
    if isinstance(raw, list) and raw:
        first = raw[0]
        return first.get("name") if isinstance(first, dict) else str(first)
    return str(raw) if raw else None


@app.get("/api/deck/{record_id}/image")
def get_image(record_id: str):
    _, image_path = _record_paths(record_id)
    if not image_path.exists():
        raise HTTPException(404, detail="no image")
    return FileResponse(image_path, media_type="image/jpeg")


@app.post("/api/swipe/{record_id}")
def swipe(record_id: str, body: SwipeRequest):
    """Idempotent on purpose.

    A repeated or late-arriving swipe for a record that's already been filed
    used to 500 (FileNotFoundError on the rename), and the frontend read any
    error as "put the card back" - so a double-submit resurrected a recipe you
    had already judged. Replaying a settled swipe is now a no-op 200, and the
    only errors left are ones that genuinely need the user's attention.
    """
    if body.direction not in ("left", "right"):
        raise HTTPException(400, detail="direction must be 'left' or 'right'")

    json_path, image_path = _record_paths(record_id)
    if not json_path.exists():
        for settled in ("approved", "rejected"):
            if (STAGING_ROOT / settled / f"{record_id}.json").exists():
                return {"result": "already", "was": settled}
        raise HTTPException(404, detail="not in the pending deck, and never staged")
    record = json.loads(json_path.read_text())

    with Stager(host=None, root=STAGING_ROOT) as st:
        if body.direction == "left":
            st.blacklist[record["canonical_url"]] = "rejected via swipe app"
            _move(json_path, image_path, STAGING_ROOT / "rejected")
            return {"result": "rejected"}

        if DRY_RUN:
            slug = None
        else:
            try:
                promotion = promote(record)
                slug = promotion if isinstance(promotion, str) else promotion.get("slug", promotion)
                if record.get("diet"):
                    tag_recipes([slug], record["diet"])
            except MealieError as e:
                raise HTTPException(502, detail=f"promotion failed: {e}")

        st.seen[record["canonical_url"]] = {
            "status": "imported", "first_seen": record.get("staged_at", ""),
            "source": record.get("source", ""),
        }
        _move(json_path, image_path, STAGING_ROOT / "approved")
        return {"result": "approved", "slug": slug, "dry_run": DRY_RUN}


def _move(json_path: Path, image_path: Path, dest_dir: Path) -> None:
    """Tolerant of a source that's already gone - two requests racing the same
    record must not raise, or the caller sees a 500 for work that did land."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    for src in (json_path, image_path):
        if src.exists():
            src.replace(dest_dir / src.name)


@app.post("/api/pull")
def pull_more(body: PullRequest):
    """Runs the exact same harvester the weekly cron does, on demand.

    host=None: staging is a local bind mount in this container, so this reads
    and writes it directly rather than over SSH the way the Z4 G4-hosted cron
    has to.
    """
    try:
        result = pull(
            count=body.count,
            sources=[s.strip() for s in body.source.split(",")] if body.source else None,
            course=body.course, search=body.search, after=body.after,
            host=None, root=STAGING_ROOT,
        )
    except Exception as e:                        # noqa: BLE001 - reported to the caller
        raise HTTPException(502, detail=f"pull failed: {type(e).__name__}: {e}")
    return {"staged": len(result["staged"]), "skipped": len(result["skipped"]),
            "errors": result["errors"]}


@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "pending": len(list((STAGING_ROOT / "pending").glob("*.json"))),
        "dry_run": DRY_RUN,
    }


class NoCacheStatic(StaticFiles):
    """Static files the browser must revalidate on every load.

    This app is redeployed constantly and `index.html`, `app.js` and
    `style.css` change together - a browser holding a cached `app.js` against a
    freshly-served `index.html` renders a blank deck, which looks exactly like
    a data bug and wasted a debugging round. `no-cache` still allows a 304, so
    the cost is one conditional request per asset on a LAN.

    Sounds are exempt: they're the only large files here and they never change.
    """

    LONG_LIVED = (".mp3", ".png")

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        if path.endswith(self.LONG_LIVED):
            response.headers["Cache-Control"] = "public, max-age=604800"
        else:
            response.headers["Cache-Control"] = "no-cache, must-revalidate"
        return response


# Serve the PWA frontend for everything else.
app.mount("/", NoCacheStatic(directory=str(REPO / "webapp/frontend"), html=True), name="frontend")
