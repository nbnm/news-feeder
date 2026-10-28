from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from .models import Base


def database(url: str):
    if url.startswith("sqlite:///./"):
        Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
    options = {"check_same_thread": False} if url.startswith("sqlite") else {}
    engine = create_engine(url, connect_args=options, pool_pre_ping=True)
    Base.metadata.create_all(engine)
    return engine, sessionmaker(engine, expire_on_commit=False)
