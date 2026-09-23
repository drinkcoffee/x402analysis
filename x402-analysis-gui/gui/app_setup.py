"""Shared FastAPI setup (session cookie support + request logging).

See api/app.py's module docstring for why this whole site is one FastAPI app
deployed as a single Vercel serverless function.
"""

from __future__ import annotations

import logging
import time

from fastapi import FastAPI, Request

from gui.session import add_session_middleware


def configure_app(app: FastAPI, logger: logging.Logger) -> None:
    """Add session-cookie support and request logging to `app`.

    The logging middleware logs every request that actually reaches this
    app (method, path, status, duration) -- if a request doesn't show up
    here at all, it never reached the Python function (a routing/rewrite
    problem, not an app-code problem).
    """
    add_session_middleware(app)

    @app.middleware("http")
    async def log_requests(request: Request, call_next):
        start = time.monotonic()
        logger.info("request start method=%s path=%s", request.method, request.url.path)
        try:
            response = await call_next(request)
        except Exception:
            logger.exception(
                "request raised an unhandled exception method=%s path=%s", request.method, request.url.path
            )
            raise
        duration_ms = (time.monotonic() - start) * 1000
        logger.info(
            "request end method=%s path=%s status=%s duration_ms=%.1f",
            request.method,
            request.url.path,
            response.status_code,
            duration_ms,
        )
        return response
