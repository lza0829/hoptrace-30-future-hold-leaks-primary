from fastapi import APIRouter

from app.routers import core, future_ts

api = APIRouter()
api.include_router(core.router)
api.include_router(future_ts.router)
