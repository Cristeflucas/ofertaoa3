"""Testa o fluxo de encartes com o Claude simulado (sem chamar a API)."""

from datetime import date
from types import SimpleNamespace

import pymupdf
import pytest
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from app.catalogo import sincronizar_catalogo
from app.coletores import encarte
from app.models import Encarte, Mercado, Preco, Produto
from app.recommender import recomendar
from app.schemas import ItemCarrinho, PedidoRecomendacao


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


def _pdf(paginas: int = 2) -> bytes:
    doc = pymupdf.open()
    for i in range(paginas):
        doc.new_page().insert_text((72, 72), f"Margarina Qualy 500g 10,99 pagina {i}")
    return doc.tobytes()


class ClaudeFalso:
    """Imita client.beta.messages.parse devolvendo páginas prontas."""

    def __init__(self, paginas: list[encarte.PaginaEncarte]):
        self.paginas, self.chamadas = list(paginas), []
        self.beta = SimpleNamespace(messages=SimpleNamespace(parse=self._parse))

    def _parse(self, **kw):
        self.chamadas.append(kw)
        return SimpleNamespace(stop_reason="end_turn", parsed_output=self.paginas.pop(0))


def _item(nome, oferta, regular=None, app=False, generico=None):
    return encarte.ItemEncarte(nome=nome, unidade="cada", preco_oferta=oferta,
                               preco_regular=regular, so_no_app=app, item_generico=generico)


PAG1 = encarte.PaginaEncarte(valido_de="2026-11-10", valido_ate="2026-11-14", itens=[
    _item("Margarina Qualy Cremosa com Sal 500g", 10.99, 11.49, app=True),
    _item("Arroz Emoções Tipo 1 5kg", 22.90),
])
PAG2 = encarte.PaginaEncarte(valido_de=None, valido_ate=None, itens=[
    _item("Café Bom Jesus Tradicional 250g", 10.99, generico="cafe-250g"),
])


def test_encarte_aplica_ofertas_em_todas_as_lojas_da_rede(session):
    ia = ClaudeFalso([PAG1, PAG2])
    r = encarte.processar(session, "Rede Mais", _pdf(), "application/pdf", cliente=ia,
                          hoje=date(2026, 11, 10))
    assert (r.situacao, r.itens, r.lojas) == ("processado", 3, 2)
    assert len(ia.chamadas) == 2
    assert ia.chamadas[0]["messages"][0]["content"][0]["type"] == "image"

    precos = session.exec(select(Preco)).all()
    assert len(precos) == 3 * 2
    assert all(p.fonte == "encarte" and p.valido_ate.date() == date(2026, 11, 14) for p in precos)
    margarina = session.exec(select(Produto).where(Produto.nome.contains("Margarina"))).one()
    # preço só no app -> quem paga no caixa paga o regular
    assert {p.preco for p in precos if p.produto_id == margarina.id} == {11.49}

    grupos = sincronizar_catalogo(session)
    arroz = session.exec(select(Produto).where(Produto.nome.contains("Arroz"))).one()
    cafe = session.exec(select(Produto).where(Produto.nome.contains("Café"))).one()
    assert arroz.grupo_id == grupos["arroz-5kg"]  # regra local
    assert cafe.grupo_id == grupos["cafe-250g"]   # sugestão da IA


def test_encarte_vencido_nao_grava_precos(session):
    r = encarte.processar(session, "Rede Mais", _pdf(1), "application/pdf",
                          cliente=ClaudeFalso([PAG1]), hoje=date(2026, 11, 20))
    assert r.situacao == "expirado"
    assert session.exec(select(Preco)).all() == []


def test_encarte_repetido_nao_chama_ia_de_novo(session):
    pdf = _pdf(1)
    encarte.processar(session, "Rede Mais", pdf, "application/pdf",
                      cliente=ClaudeFalso([PAG1]), hoje=date(2026, 11, 10))
    ia = ClaudeFalso([])
    r = encarte.processar(session, "Rede Mais", pdf, "application/pdf", cliente=ia)
    assert r.situacao == "repetido" and ia.chamadas == []
    assert len(session.exec(select(Encarte)).all()) == 1


def test_recomendador_ignora_oferta_vencida(session):
    grupos = sincronizar_catalogo(session)
    encarte.processar(session, "Rede Mais", _pdf(1), "application/pdf",
                      cliente=ClaudeFalso([encarte.PaginaEncarte(
                          valido_de="2020-01-01", valido_ate="2099-12-31",
                          itens=[_item("Arroz Emoções Tipo 1 5kg", 22.90)])]),
                      hoje=date(2026, 11, 10))
    pedido = PedidoRecomendacao(lat=-5.80, lon=-35.20, itens=[ItemCarrinho(grupo_id=grupos["arroz-5kg"])])
    assert recomendar(session, pedido).melhor.rede == "Rede Mais"

    for p in session.exec(select(Preco)):
        p.valido_ate = p.valido_ate.replace(year=2020)
    session.commit()
    assert recomendar(session, pedido).melhor is None


# ---------- Gemini (HTTP simulado) ----------

import json  # noqa: E402

import httpx  # noqa: E402

from app import visao  # noqa: E402


def _gemini_falso(respostas):
    """Devolve as respostas em ordem; cada uma é (status, corpo) ou uma PaginaEncarte."""
    pedidos = []

    def tratar(req: httpx.Request):
        pedidos.append((str(req.url), json.loads(req.read())))
        r = respostas.pop(0)
        if isinstance(r, encarte.PaginaEncarte):
            texto = r.model_dump_json()
            return httpx.Response(200, json={"candidates": [{"finishReason": "STOP",
                                                             "content": {"parts": [{"text": texto}]}}]})
        return httpx.Response(r[0], json=r[1])

    http = httpx.Client(transport=httpx.MockTransport(tratar))
    return visao.LeitorGemini("chave", http=http, espera_s=0), pedidos


def test_encarte_com_gemini(session):
    leitor, pedidos = _gemini_falso([PAG1, PAG2])
    r = encarte.processar(session, "Rede Mais", _pdf(), "application/pdf", cliente=leitor,
                          hoje=date(2026, 11, 10))
    assert (r.situacao, r.itens, r.lojas) == ("processado", 3, 2)

    url, corpo = pedidos[0]
    assert "gemini-flash-latest:generateContent" in url
    partes = corpo["contents"][0]["parts"]
    assert partes[0]["inline_data"]["mime_type"] == "image/jpeg"
    assert "Margarina Qualy" in partes[1]["text"]  # texto embutido do PDF vai junto
    schema = corpo["generationConfig"]["responseJsonSchema"]
    assert "$defs" not in json.dumps(schema) and "$ref" not in json.dumps(schema)
    assert schema["properties"]["itens"]["items"]["properties"]["preco_oferta"]["type"] == "number"


def test_gemini_sobrecarregado_tenta_de_novo_e_troca_de_modelo(session):
    ocupado = (503, {"error": {"message": "high demand"}})
    leitor, pedidos = _gemini_falso([ocupado, ocupado, PAG1])
    r = encarte.processar(session, "Rede Mais", _pdf(1), "application/pdf", cliente=leitor,
                          hoje=date(2026, 11, 10))
    assert r.itens == 2
    modelos = [u.split("/models/")[1].split(":")[0] for u, _ in pedidos]
    assert modelos == ["gemini-flash-latest", "gemini-flash-latest", "gemini-flash-lite-latest"]


def test_gemini_fora_do_ar_nao_grava_nada(session):
    ocupado = (503, {"error": {"message": "high demand"}})
    leitor, _ = _gemini_falso([ocupado] * 4)
    with pytest.raises(visao.FalhaLeitura):
        encarte.processar(session, "Rede Mais", _pdf(1), "application/pdf", cliente=leitor)
    assert session.exec(select(Encarte)).all() == []


def test_gemini_chave_invalida(session):
    leitor, _ = _gemini_falso([(400, {"error": {"message": "API key not valid. Please pass a valid API key."}})])
    with pytest.raises(visao.SemCredencial):
        encarte.processar(session, "Rede Mais", _pdf(1), "application/pdf", cliente=leitor)


def test_leitor_padrao_prefere_gemini(monkeypatch):
    monkeypatch.delenv("OFERTAO_VISAO_PROVEDOR", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "g")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "a")
    assert visao.leitor_padrao().nome == "gemini"
    monkeypatch.delenv("GEMINI_API_KEY")
    assert visao.leitor_padrao().nome == "anthropic"
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    with pytest.raises(visao.SemCredencial):
        visao.leitor_padrao()
