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
"""

import base64
import json
import os
import secrets
import sys
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
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

if not AUTH_PASS:
    raise RuntimeError("SWIPE_AUTH_PASS must be set - refusing to start unauthenticated")

app = FastAPI(title="Recipe Swipe")


def _require_auth(authorization: str | None = Header(default=None)) -> None:
    if not authorization or not authorization.startswith("Basic "):
        raise HTTPException(401, detail="auth required", headers={"WWW-Authenticate": "Basic"})
    try:
        user, password = base64.b64decode(authorization[6:]).decode().split(":", 1)
    except Exception:
        raise HTTPException(401, detail="bad auth header", headers={"WWW-Authenticate": "Basic"})
    if not (secrets.compare_digest(user, AUTH_USER) and secrets.compare_digest(password, AUTH_PASS)):
        raise HTTPException(401, detail="wrong credentials", headers={"WWW-Authenticate": "Basic"})


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
def get_deck(authorization: str | None = Header(default=None)):
    """Every pending record's preview - small enough (a few hundred recipes at
    most) to just return in full; the frontend handles paging through them."""
    _require_auth(authorization)
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


@app.get("/api/deck/{record_id}/image")
def get_image(record_id: str, authorization: str | None = Header(default=None)):
    _require_auth(authorization)
    _, image_path = _record_paths(record_id)
    if not image_path.exists():
        raise HTTPException(404, detail="no image")
    return FileResponse(image_path, media_type="image/jpeg")


@app.post("/api/swipe/{record_id}")
def swipe(record_id: str, body: SwipeRequest, authorization: str | None = Header(default=None)):
    _require_auth(authorization)
    if body.direction not in ("left", "right"):
        raise HTTPException(400, detail="direction must be 'left' or 'right'")

    json_path, image_path = _record_paths(record_id)
    if not json_path.exists():
        raise HTTPException(404, detail="not in the pending deck (already swiped, or never staged)")
    record = json.loads(json_path.read_text())

    with Stager(host=None, root=STAGING_ROOT) as st:
        if body.direction == "left":
            st.blacklist[record["canonical_url"]] = "rejected via swipe app"
            _move(json_path, image_path, STAGING_ROOT / "rejected")
            return {"result": "rejected"}

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
        return {"result": "approved", "slug": slug}


def _move(json_path: Path, image_path: Path, dest_dir: Path) -> None:
    dest_dir.mkdir(parents=True, exist_ok=True)
    json_path.rename(dest_dir / json_path.name)
    if image_path.exists():
        image_path.rename(dest_dir / image_path.name)


@app.post("/api/pull")
def pull_more(body: PullRequest, authorization: str | None = Header(default=None)):
    """Runs the exact same harvester the weekly cron does, on demand.

    host=None: staging is a local bind mount in this container, so this reads
    and writes it directly rather than over SSH the way the Z4 G4-hosted cron
    has to.
    """
    _require_auth(authorization)
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
    return {"status": "ok", "pending": len(list((STAGING_ROOT / "pending").glob("*.json")))}


# Serve the PWA frontend for everything else.
app.mount("/", StaticFiles(directory=str(REPO / "webapp/frontend"), html=True), name="frontend")
