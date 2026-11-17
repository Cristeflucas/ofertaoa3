import os
from collections.abc import Iterator
import tempfile
from pathlib import Path

from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine


def _url_padrao() -> str:
    if os.getenv("VERCEL") or not os.access(".", os.W_OK):
        caminho = (Path(tempfile.gettempdir()) / "ofertao.db").as_posix()
        return f"sqlite:///{caminho}"
    return "sqlite:///./ofertao.db"


DATABASE_URL = os.getenv("OFERTAO_DB_URL", _url_padrao())

SQLITE = DATABASE_URL.startswith("sqlite")

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False, "timeout": 30} if SQLITE else {},
)

if SQLITE:
    @event.listens_for(engine, "connect")
    def _sqlite_wal(conexao, _):
        cur = conexao.cursor()
        try:
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
        except Exception:
            pass
        finally:
            cur.close()


_inicializado = False


def criar_tabelas() -> None:
    from app import models  # noqa: F401  (registra as tabelas no metadata)

    SQLModel.metadata.create_all(engine)


def garantir_inicializado() -> None:
    global _inicializado
    if _inicializado:
        return
    criar_tabelas()
    from app.catalogo import sincronizar_catalogo

    with Session(engine) as session:
        sincronizar_catalogo(session)
    _inicializado = True


def get_session() -> Iterator[Session]:
    garantir_inicializado()
    with Session(engine) as session:
        yield session
