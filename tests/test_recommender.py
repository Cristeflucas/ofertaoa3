import pytest
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from app.catalogo import encaixar, normalizar, sincronizar_catalogo
from app.coletores.osm import identificar_rede, importar_mercados
from app.models import Mercado, Preco, Produto
from app.recommender import recomendar
from app.schemas import ItemCarrinho, PedidoRecomendacao


@pytest.fixture
def session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _loja(s, nome, lat, lon=-35.20):
    m = Mercado(nome=nome, lat=lat, lon=lon, cidade="Natal")
    s.add(m)
    s.flush()
    return m


def _produto(s, nome, grupo_id=None, ean=None):
    p = Produto(nome=nome, grupo_id=grupo_id, ean=ean)
    s.add(p)
    s.flush()
    return p


def _preco(s, m, p, v, fonte="cupom_fiscal"):
    s.add(Preco(mercado_id=m.id, produto_id=p.id, preco=v, fonte=fonte))


def _pedido(*itens, lat=-5.80, lon=-35.20, **kw):
    return PedidoRecomendacao(lat=lat, lon=lon, itens=list(itens), **kw)


def test_encaixe_de_produtos_reais():
    assert encaixar("Arroz Kicaldo Agulhinha Tipo 1 5kg").slug == "arroz-5kg"
    assert encaixar("Arroz Integral Camil 5 kg") is None
    assert encaixar("Leite Longa Vida Aurora Integral 1L").slug == "leite-uht-1l"
    assert encaixar("Ovos Brancos Grandes 30 unidades").slug == "ovos-30"
    assert encaixar("Flocão de Arroz Urbano 500g") is None
    assert normalizar("Açúcar 1,5 KG") == "acucar 1 5kg"  # não vira "15kg"


def test_generico_usa_o_produto_mais_barato_de_cada_loja(session):
    grupos = sincronizar_catalogo(session)
    g = grupos["arroz-5kg"]
    a, b = _loja(session, "A", -5.80), _loja(session, "B", -5.801)
    x, y = _produto(session, "Arroz X 5kg", g), _produto(session, "Arroz Y 5kg", g)
    _preco(session, a, x, 30.0); _preco(session, a, y, 21.0)
    _preco(session, b, x, 25.0)
    session.commit()

    r = recomendar(session, _pedido(ItemCarrinho(grupo_id=g), permitir_divisao=False))
    assert r.melhor.nome == "A"
    assert r.melhor.itens[0].produto == "Arroz Y 5kg"


def test_deslocamento_pesa_na_decisao(session):
    p = _produto(session, "Café Z 250g", ean="789")
    perto, longe = _loja(session, "Perto", -5.80), _loja(session, "Longe", -5.90)
    _preco(session, perto, p, 15.0); _preco(session, longe, p, 12.0)
    session.commit()
    item = ItemCarrinho(produto_id=p.id)
    assert recomendar(session, _pedido(item, permitir_divisao=False)).melhor.nome == "Perto"
    assert recomendar(session, _pedido(item, custo_km=0, permitir_divisao=False)).melhor.nome == "Longe"


def test_lojas_sem_preco_ficam_fora_do_ranking(session):
    p = _produto(session, "Óleo 900ml")
    com = _loja(session, "Com preço", -5.80)
    _loja(session, "Sem preço", -5.80)
    _preco(session, com, p, 7.0)
    session.commit()
    r = recomendar(session, _pedido(ItemCarrinho(produto_id=p.id)))
    assert [m.nome for m in r.ranking] == ["Com preço"]
    assert r.lojas_sem_dados == 1


def test_importacao_osm(session):
    elementos = [
        {"type": "node", "id": 1, "lat": -5.8, "lon": -35.2,
         "tags": {"shop": "supermarket", "name": "Atacadão Natal Sul", "addr:city": "Natal"}},
        {"type": "way", "id": 2, "center": {"lat": -5.81, "lon": -35.21},
         "tags": {"shop": "supermarket", "name": "Quartel dos Suplementos"}},
        {"type": "node", "id": 3, "lat": -5.82, "lon": -35.22, "tags": {"shop": "supermarket"}},
    ]
    assert importar_mercados(session, elementos) == 1
    assert importar_mercados(session, elementos) == 1  # idempotente
    assert identificar_rede("Supermercado Nordestão Tirol") == "Nordestão"
