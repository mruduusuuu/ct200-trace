from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI

from .api import router
from .config import Settings, get_settings
from .db import Base, make_engine, make_session_factory
from .llm import make_llm
from .store import make_store


def create_app(settings: Settings | None = None, llm=None, store=None, engine=None) -> FastAPI:
    settings = settings or get_settings()
    if engine is None:
        if settings.database_url.startswith("sqlite:///") and ":memory:" not in settings.database_url:
            Path(settings.database_url.replace("sqlite:///", "")).parent.mkdir(parents=True, exist_ok=True)
        engine = make_engine(settings.database_url)
    Base.metadata.create_all(engine)
    app = FastAPI(title="CT-200 traceable test-case generator", version="1.0")
    app.state.settings = settings
    app.state.session_factory = make_session_factory(engine)
    app.state.store = store or make_store(settings)
    app.state.llm = llm if llm is not None else make_llm(settings)
    app.include_router(router)
    return app
