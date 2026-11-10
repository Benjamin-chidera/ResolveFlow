from pathlib import Path
from typing import Generator
from sqlmodel import Session, SQLModel, create_engine
from app.core.config import settings

# Ensure data directory exists for SQLite database file
if settings.DATABASE_URL.startswith("sqlite"):
    clean_path = settings.DATABASE_URL.replace("sqlite:////", "/").replace("sqlite:///", "")
    Path(clean_path).parent.mkdir(parents=True, exist_ok=True)
    connect_args = {"check_same_thread": False}
else:
    connect_args = {}

engine = create_engine(
    settings.DATABASE_URL,
    echo=False,
    connect_args=connect_args,
)


def init_db() -> None:
    """Create all tables defined in SQLModel metadata."""
    # Ensure all models are imported so their tables register in metadata
    import app.models  # noqa: F401
    SQLModel.metadata.create_all(engine)


def get_session() -> Generator[Session, None, None]:
    """Dependency that yields a database session."""
    with Session(engine) as session:
        yield session

