"""FastAPI app: static frontend + the parse/resolve/import HTTP surface.

Nothing here talks to Cronometer directly. The write path goes through
app.importer -> app.resolve -> app.cronometer, and app/cronometer.py is the
only module that imports the upstream client.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app import db
from app.constants import BREAKFAST, MACRO_TOLERANCE
from app.cronometer import CronometerAdapter, CronometerUnavailable
from app.importer import collect_dishes, run_import
from app.parser import normalize_dish_name, parse_text
from app.resolve import resolve_all

load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("cronohelper")

STATIC_DIR = Path(__file__).parent / "static"

@asynccontextmanager
async def lifespan(_: FastAPI):
    """Check the data directory at boot.

    Without this the container starts, serves the page, and only falls over
    when someone pastes a week — a slow and confusing way to discover that a
    bind mount is owned by the wrong user. Logged rather than raised so the
    container still comes up and can explain itself over HTTP.
    """
    try:
        db.check_writable()
        logger.info("data directory ok: %s", db.DATA_DIR)
    except db.DataDirNotWritable as exc:
        logger.error("STARTUP CHECK FAILED\n%s", exc)
    yield


app = FastAPI(
    lifespan=lifespan,
    title="cronohelper",
    description="Parse a Hungarian food-delivery weekly summary into Cronometer.",
    # No public docs: this is a LAN tool and the schema is not the product.
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


@app.exception_handler(db.DataDirNotWritable)
async def data_dir_not_writable(
    request: Request, exc: db.DataDirNotWritable
) -> JSONResponse:
    """A uid/permissions problem, not a bug. Return the whole explanation --
    it contains the exact commands that fix it and no sensitive data."""
    logger.error("data directory not writable: %s", exc)
    return JSONResponse(status_code=503, content={"detail": str(exc)})


@app.exception_handler(Exception)
async def unhandled_exception(request: Request, exc: Exception) -> JSONResponse:
    """Never let an unhandled error leave as a plain-text 500.

    Uvicorn's default 500 body is not JSON, which means the browser cannot
    read it and reports a useless "could not reach the server". Returning
    structured JSON keeps the real cause visible in the UI.

    Only the exception *type* goes in the response. The full traceback goes to
    the log, where it cannot end up on a screen or in a screenshot.
    """
    logger.exception("unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={
            "detail": (
                f"Internal error ({type(exc).__name__}) in {request.url.path}. "
                "Nothing further was written. See the container logs "
                "(`docker logs cronohelper`) for the traceback."
            )
        },
    )


@lru_cache(maxsize=1)
def get_adapter() -> CronometerAdapter:
    """One adapter (and therefore one session token) per process.

    Cronometer rate-limits login aggressively, so the client is built once and
    reused; it re-authenticates only on a 401/403, never per request.
    """
    return CronometerAdapter()


# --- request models --------------------------------------------------------


class RowIn(BaseModel):
    name: str
    normalized_name: str | None = None
    code: str | None = None
    kcal: float = 0
    carbs_g: float = 0
    protein_g: float = 0
    fat_g: float = 0


class DayIn(BaseModel):
    date: str
    breakfast: bool = True
    rows: list[RowIn] = Field(default_factory=list)


class ParseRequest(BaseModel):
    text: str = Field(default="", description="The pasted weekly summary.")


class PayloadRequest(BaseModel):
    """The edited draft. Used by both /api/resolve and /api/import."""

    days: list[DayIn] = Field(default_factory=list)
    # normalized dish name -> "use_existing" | "create_new_version"
    decisions: dict[str, str] = Field(default_factory=dict)


class LinkRequest(BaseModel):
    name: str
    food_id: int


def _payload_dict(req: PayloadRequest) -> dict:
    return {
        "days": [
            {
                "date": d.date,
                "breakfast": d.breakfast,
                "rows": [
                    {
                        "name": r.name,
                        "normalized_name": r.normalized_name
                        or normalize_dish_name(r.name),
                        "kcal": r.kcal,
                        "carbs_g": r.carbs_g,
                        "protein_g": r.protein_g,
                        "fat_g": r.fat_g,
                    }
                    for r in d.rows
                ],
            }
            for d in req.days
        ]
    }


# --- routes ----------------------------------------------------------------


@app.get("/healthz")
def healthz() -> JSONResponse:
    """Container health.

    Deliberately does not touch Cronometer -- health must not depend on a
    third party, and must never trigger a login. It *does* check the data
    directory: a container that cannot write its own database is not healthy,
    and reporting ok here is what let a permissions problem masquerade as a
    working deployment.
    """
    try:
        db.check_writable()
    except db.DataDirNotWritable as exc:
        return JSONResponse(
            status_code=503, content={"status": "unhealthy", "detail": str(exc)}
        )
    return JSONResponse({"status": "ok", "database": "writable"})


@app.post("/api/parse")
def api_parse(req: ParseRequest) -> JSONResponse:
    """Text in, structured preview out.

    Pure: no side effects, no Cronometer auth, nothing written. Parse issues
    come back as data with line numbers rather than as an exception, so the UI
    can point at the offending line.
    """
    result = parse_text(req.text)
    logger.info(
        "parsed: %d rows across %d days, %d issues",
        len(result.rows),
        len(result.days),
        len(result.issues),
    )
    return JSONResponse(result.as_dict())


@app.post("/api/resolve")
def api_resolve(req: PayloadRequest) -> JSONResponse:
    """Resolve every dish in the draft to a Cronometer food. Read-only.

    Reads from Cronometer (search + get_food) but writes nothing to it: a
    dish that does not exist yet comes back as `will_create`, and is only
    created during /api/import. This is what lets a macro conflict be surfaced
    in the preview, before anything is committed.
    """
    payload = _payload_dict(req)
    try:
        with db.connect() as conn:
            resolutions = resolve_all(
                conn, get_adapter(), collect_dishes(payload), decisions=req.decisions
            )
    except CronometerUnavailable as exc:
        return JSONResponse(status_code=502, content={"detail": str(exc)})

    out = [r.as_dict() for r in resolutions.values()]
    logger.info(
        "resolved %d dishes: %s",
        len(out),
        ", ".join(sorted({r["status"] for r in out})) or "none",
    )
    return JSONResponse({"dishes": out})


@app.post("/api/import")
def api_import(req: PayloadRequest) -> JSONResponse:
    """Write the confirmed payload and return a per-entry result array.

    Writes exactly what it is handed; the original pasted text is never sent
    here and is never re-parsed. Partial failure is expected -- one bad entry
    does not abort the rest.
    """
    payload = _payload_dict(req)
    if not payload["days"]:
        return JSONResponse(status_code=400, content={"detail": "Nothing to import."})

    try:
        with db.connect() as conn:
            result = run_import(
                conn, get_adapter(), payload, decisions=req.decisions
            )
    except CronometerUnavailable as exc:
        return JSONResponse(status_code=502, content={"detail": str(exc)})

    return JSONResponse(result)


@app.post("/api/foods/link")
def api_link(req: LinkRequest) -> JSONResponse:
    """Bind a dish name to a Cronometer food id permanently.

    The spike landed on the searchable-and-identifiable branch, so this is not
    required for normal operation -- but it is the manual override for the
    cases search cannot handle: a dish whose Cronometer name differs from the
    delivery name, or a conflict you want pinned to a specific food id.

    The food is fetched first, so a typo'd id fails here rather than silently
    binding the cache to someone else's food.
    """
    try:
        food = get_adapter().get_food(req.food_id)
    except CronometerUnavailable as exc:
        return JSONResponse(status_code=502, content={"detail": str(exc)})

    key = normalize_dish_name(req.name)
    with db.connect() as conn:
        db.bind_food(
            conn,
            normalized_name=key,
            food_id=food.food_id,
            measure_id=food.measure_id,
            grams_per_serving=food.grams_per_serving,
            translation_id=food.translation_id,
            display_name=food.name,
            kcal=food.kcal,
            carbs_g=food.carbs_g,
            protein_g=food.protein_g,
            fat_g=food.fat_g,
            created_by_app=False,
        )
    logger.info("linked %r -> food %d", key, food.food_id)
    return JSONResponse({"normalized_name": key, "food": food.as_dict()})


@app.get("/api/config")
def api_config() -> JSONResponse:
    """The fixed breakfast block, served from the same constants the writer
    uses. The frontend renders these numbers rather than hardcoding its own
    copy, so `app/constants.py` stays the single place they are defined."""
    return JSONResponse(
        {
            "breakfast": [
                {
                    "name": f.name,
                    "serving": f.serving_name,
                    "kcal": f.kcal,
                    "carbs_g": f.carbs_g,
                    "protein_g": f.protein_g,
                    "fat_g": f.fat_g,
                }
                for f in BREAKFAST
            ],
            "macro_tolerance": MACRO_TOLERANCE,
        }
    )


@app.get("/api/history")
def api_history() -> JSONResponse:
    """Recent imports, plus every date already written to.

    Local read only -- never contacts Cronometer. The frontend uses
    `imported_days` to mark already-imported days in the grid before the user
    clicks anything, and to warn on a drop onto such a date.
    """
    with db.connect() as conn:
        return JSONResponse(
            {
                "runs": db.recent_runs(conn),
                "imported_days": db.days_with_entries(conn),
            }
        )


# --- static frontend -------------------------------------------------------


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
