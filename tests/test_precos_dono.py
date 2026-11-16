"""Cadastro manual de preços (sem IA): por loja e para a rede inteira."""

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from app import main
from app.database import get_session
from app.models import ItemGenerico, Mercado, Preco, Produto


@pytest.fixture
def session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        s.add_all([
            Mercado(nome="Rede Mais Tirol", rede="Rede Mais", lat=-5.80, lon=-35.20),
            Mercado(nome="Rede Mais Alecrim", rede="Rede Mais", lat=-5.81, lon=-35.22),
            Mercado(nome="Outro", rede="Nordestão", lat=-5.80, lon=-35.20),
        ])
        s.commit()
        yield s


@pytest.fixture
def cliente(session, monkeypatch):
    monkeypatch.setattr(main, "TOKEN_ADMIN", "segredo")
    main.app.dependency_overrides[get_session] = lambda: session
    yield TestClient(main.app)
    main.app.dependency_overrides.clear()


def test_rede_inteira_grava_em_todas_as_lojas_e_encaixa_item(cliente, session):
    slug = "arroz-5kg"
    corpo = {"itens": [{"nome": "Arroz da casa 5 quilos", "preco": 21.9, "item_generico": slug}]}
    r = cliente.post("/api/redes/Rede Mais/precos", json=corpo, headers={"X-Token": "segredo"})
    assert r.status_code == 200, r.text
    assert r.json() == {"itens_gravados": 1, "lojas": 2}

    precos = session.exec(select(Preco)).all()
    assert len(precos) == 2 and all(p.fonte == "dono" and p.preco == 21.9 for p in precos)
    prod = session.exec(select(Produto)).one()
    grupo = session.exec(select(ItemGenerico).where(ItemGenerico.slug == slug)).one()
    assert prod.grupo_id == grupo.id


def test_token_obrigatorio_e_rede_desconhecida(cliente):
    corpo = {"itens": [{"nome": "X", "preco": 1}]}
    assert cliente.post("/api/redes/Rede Mais/precos", json=corpo).status_code == 401
    r = cliente.post("/api/redes/Nao Existe/precos", json=corpo, headers={"X-Token": "segredo"})
    assert r.status_code == 404
