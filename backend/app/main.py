from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy import text

from app.db import get_engine
from app.settings import get_settings


class Health(BaseModel):
    status: str
    database: str


def create_app() -> FastAPI:
    app = FastAPI(title="MORPH backend", version="0.1.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=get_settings().cors_origins,
        allow_methods=["GET"],
        allow_headers=["*"],
    )

    @app.get("/health", response_model=Health)
    def health() -> Health:
        try:
            with get_engine().connect() as conn:
                conn.execute(text("SELECT 1"))
            database = "ok"
        except Exception:
            database = "unavailable"
        return Health(status="ok" if database == "ok" else "degraded", database=database)

    return app


app = create_app()
