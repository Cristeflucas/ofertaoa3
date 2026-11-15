import os
from collections.abc import Iterator

from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine

DATABASE_URL = os.getenv("OFERTAO_DB_URL", "sqlite:///./ofertao.db")

SQLITE = DATABASE_URL.startswith("sqlite")

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False, "timeout": 30} if SQLITE else {},
)

if SQLITE:
    @event.listens_for(engine, "connect")
    def _sqlite_wal(conexao, _):
        # WAL: o site continua lendo enquanto os coletores gravam
        cur = conexao.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()


def criar_tabelas() -> None:
    from app import models  # noqa: F401  (registra as tabelas no metadata)

    SQLModel.metadata.create_all(engine)


def get_session() -> Iterator[Session]:
    with Session(engine) as session:
        yield session
