from app.api.approvals import router as approvals_router
from app.api.cases import router as cases_router
from app.api.chat import router as chat_router
from app.api.investigations import router as investigations_router
from app.api.provider_events import router as provider_events_router
from app.core.config import settings
from app.core.database import init_db
from contextlib import asynccontextmanager
from typing import Any, Dict, List
from fastapi import FastAPI



@asynccontextmanager
async def lifespan(app: FastAPI):
    # Initialize SQLite database and create tables on startup
    init_db()
    yield


app = FastAPI(
    # title=settings.APP_NAME,
    description="Agentic Payment Operations, Transaction Recovery & Fraud Evidence Investigation",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(provider_events_router)
app.include_router(cases_router)
app.include_router(investigations_router)
app.include_router(approvals_router)
app.include_router(chat_router)


@app.get("/")
def read_root():
    return {
        "service": settings.APP_NAME,
        "environment": settings.APP_ENV,
        "database": "sqlite",
        "status": "online",
        "docs_url": "/docs",
    }


@app.get("/health")
def health_check():
    return {
        "status": "healthy",
        "database": "sqlite",
        # "ollama_base_url": settings.OLLAMA_BASE_URL,
        # "ollama_model": settings.OLLAMA_MODEL,
    }


if __name__ == "__main__":
    from app.cli.terminal_app import run_terminal_mvp

    run_terminal_mvp()



