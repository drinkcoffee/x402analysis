"""The entire x402-analysis-api server, deployed as ONE Vercel serverless
function.

Vercel's "FastAPI" framework detection builds the whole project as a single
consolidated serverless function, not one function per api/*.py file -- so
every route lives in this one module, using plain, standard FastAPI paths.
See the sibling x402-analysis-gui project for a longer account of that
gotcha.

Routes:
    GET  /         unauthenticated -- a one-line pointer to API.md
    GET  /status   unauthenticated -- checks the Neon connection and
                   reports whether the server and database are online

Everything else requires an API key sent as an `X-API-Key` header (see
lib/auth.py: require_read_access / require_read_write_access) -- but there
are no other endpoints yet. `/` and `/status` are deliberately exempt from
that requirement (so uptime checks and the landing message don't need a
key).

See API.md for the machine-readable request/response contract, and
README.md for how to run and deploy this server.

Logging: configured (via lib.app_setup.configure_app) so every logger.info()/
logger.exception() call in this module reaches stderr, which Vercel captures
as Function Logs.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("x402_api")

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import JSONResponse, PlainTextResponse

load_dotenv()

import lib.db as db  # noqa: E402
from lib.app_setup import configure_app  # noqa: E402

app = FastAPI(title="x402 Analysis API")
configure_app(app, logger)

HOME_MESSAGE = (
    "x402 Analysis API Server. See "
    "https://github.com/drinkcoffee/x402analysis/x402-analysis-api/API.md "
    "for instructions on how to connect to this API server."
)


@app.get("/")
def home() -> PlainTextResponse:
    return PlainTextResponse(HOME_MESSAGE)


@app.get("/status")
def status_check() -> JSONResponse:
    try:
        db.check_connection()
    except Exception:
        logger.exception("status: database connection check failed")
        return JSONResponse({"server": "online", "database": "offline"}, status_code=503)
    return JSONResponse({"server": "online", "database": "online"})
