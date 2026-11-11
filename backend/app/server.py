"""Production entry point: one origin serves the React build at / and the API at /api.

    uvicorn app.server:root

In development you don't need this: Vite serves the frontend and proxies /api to
`uvicorn app.main:app`.
"""
import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .main import app as api

STATIC_DIR = Path(os.getenv("STATIC_DIR", Path(__file__).resolve().parents[1] / "static"))

# Mounted apps don't run their own startup, so the root app runs the API's
# (create tables, seed, start the NetSuite sync worker).
root = FastAPI(lifespan=api.router.lifespan_context, docs_url=None, redoc_url=None, openapi_url=None)
root.mount("/api", api)  # API docs at /api/docs
if STATIC_DIR.is_dir():
    root.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="frontend")
