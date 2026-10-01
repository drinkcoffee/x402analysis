"""The entire x402-analysis-api server, deployed as ONE Vercel serverless
function.

Vercel's "FastAPI" framework detection builds the whole project as a single
consolidated serverless function, not one function per api/*.py file -- so
every route lives in this one module, using plain, standard FastAPI paths.
See the sibling x402-analysis-gui project for a longer account of that
gotcha.

Shared code (apilib/) lives *inside* this api/ directory rather than at the
project root, and is deliberately NOT named "lib": the repo's root
.gitignore has a bare `lib/` rule (standard Python-venv boilerplate, which
matches a directory of that name at any depth), so a package called `lib/`
-- anywhere in this repo -- never actually gets committed, and Vercel only
ever deploys what's in git. That's exactly what happened here: it ran fine
locally, then broke in production with `ModuleNotFoundError: No module
named 'lib'`, because the module had silently never been pushed at all.
Keeping it under api/ (rather than the project root) is a good idea
regardless, since Vercel's Python build only reliably bundles files under
the entrypoint's own directory tree -- but the name change is what actually
fixed this particular failure.

Routes:
    GET  /             unauthenticated -- a one-line pointer to API.md. A
                       minimal HTML page (not plain text) so its
                       `<link rel="icon">` actually gets the browser to show
                       the tab favicon -- browsers only reliably probe
                       /favicon.ico by convention for real HTML documents,
                       not e.g. a text/plain response, and that fallback
                       probing isn't a web standard to begin with.
    GET  /status       unauthenticated -- checks the Neon connection and
                       reports whether the server and database are online
    GET  /favicon.ico  unauthenticated -- the browser-tab icon
                       (api/assets/favicon.ico, generated from
                       api/assets/icon.png)
    GET  /facilitators          requires a "read" (or "read_write") API key
                                -- name/risk/active for every facilitator
    GET  /facilitators/{name}   requires a "read" (or "read_write") API key
                                -- everything known about one facilitator
                                (matched case-insensitively), 404 if none
                                match
    GET  /servers                requires a "read" (or "read_write") API key
                                 -- name/risk/active for every server,
                                 paged (?limit=&offset=; there can be
                                 thousands)
    GET  /servers/{name}         requires a "read" (or "read_write") API key
                                 -- everything known about one server
                                 (matched case-insensitively), including its
                                 services, 404 if none match
    GET  /services               requires a "read" (or "read_write") API key
                                 -- a summary of every service across every
                                 server, paged (?limit=&offset=; there can
                                 be thousands)
    GET  /services/{id}          requires a "read" (or "read_write") API key
                                 -- everything known about one service, 404
                                 if no service has that id

Everything except `/`, `/status`, and `/favicon.ico` requires an API key
sent as an `X-API-Key` header (see api/apilib/auth.py: require_read_access /
require_read_write_access).

See API.md for the machine-readable request/response contract, and
README.md for how to run and deploy this server.

Logging: configured (via apilib.app_setup.configure_app) so every
logger.info()/logger.exception() call in this module reaches stderr, which
Vercel captures as Function Logs.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("x402_api")

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

load_dotenv()

import apilib.db as db  # noqa: E402
from apilib.app_setup import configure_app  # noqa: E402
from apilib.auth import require_read_access  # noqa: E402

app = FastAPI(title="x402 Analysis API")
configure_app(app, logger)

# GET /servers and GET /services can each have thousands of rows, so both
# are paged (?limit=&offset=) rather than returned whole the way
# GET /facilitators is -- there are only ever a few dozen facilitators.
DEFAULT_PAGE_LIMIT = 50
MAX_PAGE_LIMIT = 500

ASSETS_DIR = Path(__file__).resolve().parent / "assets"

HOME_MESSAGE = (
    "x402 Analysis API Server. See "
    "https://github.com/drinkcoffee/x402analysis/x402-analysis-api/API.md "
    "for instructions on how to connect to this API server."
)

HOME_HTML = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<title>x402 Analysis API</title>
<link rel="icon" href="/favicon.ico" />
</head>
<body>{HOME_MESSAGE}</body>
</html>
"""


@app.get("/")
def home() -> HTMLResponse:
    return HTMLResponse(HOME_HTML)


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> FileResponse:
    return FileResponse(ASSETS_DIR / "favicon.ico", media_type="image/vnd.microsoft.icon")


@app.get("/status")
def status_check() -> JSONResponse:
    try:
        db.check_connection()
    except Exception:
        logger.exception("status: database connection check failed")
        return JSONResponse({"server": "online", "database": "offline"}, status_code=503)
    return JSONResponse({"server": "online", "database": "online"})


@app.get("/facilitators")
def list_facilitators(_=Depends(require_read_access)) -> list[dict]:
    # A plain dict/list return (rather than manually building a JSONResponse,
    # as the routes above do) goes through FastAPI's own jsonable_encoder,
    # which -- unlike a bare JSONResponse -- knows how to serialize the
    # datetime.date values psycopg2 hands back for `updated` columns.
    return db.list_facilitators()


@app.get("/facilitators/{name}")
def get_facilitator(name: str, _=Depends(require_read_access)) -> dict:
    facilitator = db.find_facilitator(name)
    if facilitator is None:
        raise HTTPException(status_code=404, detail=f"No facilitator named {name!r}.")
    return facilitator


@app.get("/servers")
def list_servers(
    limit: int = Query(DEFAULT_PAGE_LIMIT, ge=1, le=MAX_PAGE_LIMIT),
    offset: int = Query(0, ge=0),
    _=Depends(require_read_access),
) -> dict:
    items, total = db.list_servers(limit, offset)
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@app.get("/servers/{name}")
def get_server(name: str, _=Depends(require_read_access)) -> dict:
    server = db.find_server(name)
    if server is None:
        raise HTTPException(status_code=404, detail=f"No server named {name!r}.")
    return server


@app.get("/services")
def list_services(
    limit: int = Query(DEFAULT_PAGE_LIMIT, ge=1, le=MAX_PAGE_LIMIT),
    offset: int = Query(0, ge=0),
    _=Depends(require_read_access),
) -> dict:
    items, total = db.list_services(limit, offset)
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@app.get("/services/{service_id}")
def get_service(service_id: int, _=Depends(require_read_access)) -> dict:
    service = db.find_service(service_id)
    if service is None:
        raise HTTPException(status_code=404, detail=f"No service with id {service_id}.")
    return service
