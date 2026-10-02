"""A Sparrow with only its HTTP routers mounted, for test_fleet_e2e.

The real routes.router (so the real /peer/ask dispatch) plus the fleet
router, without app.main's startup (models, capture, workers). Run under
uvicorn in a subprocess with its own QUILL_DATA_DIR.
"""
from __future__ import annotations

from fastapi import FastAPI

from app.api.fleet_routes import router as fleet_router
from app.api.routes import router as main_router

app = FastAPI()
app.include_router(main_router)
app.include_router(fleet_router)
