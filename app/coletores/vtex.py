"""Coletor de preços de lojas na plataforma VTEX (catálogo público "Intelligent Search").

Hoje: Atacadão. O preço é o da LOJA ONLINE da rede; ele é atribuído às lojas
físicas da rede na Grande Natal com a fonte "atacadao_online", para o usuário
saber que o preço de gôndola pode ser diferente.
"""

import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime

import httpx
from sqlmodel import Session, select

from app.catalogo import CESTA, encaixar, sincronizar_catalogo
from app.coletores import USER_AGENT
from app.models import Mercado, Preco, Produto, agora

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class LojaVtex:
    rede: str
    base_url: str
    fonte: str


ATACADAO = LojaVtex("Atacadão", "https://www.atacadao.com.br", "atacadao_online")
LOJAS = [ATACADAO]


@dataclass
class Oferta:
    ean: str
    nome: str
    marca: str | None
    imagem_url: str | None
    preco: float
    preco_lista: float


def termo_amplo(busca: str) -> str:
    """Tira o tamanho da busca ("arroz tipo 1 5kg" -> "arroz tipo 1"): o filtro de tamanho
    é feito depois, pelas regras do catálogo."""
    return re.sub(r"\b\d+\s*(kg|g|ml|l|unidades|rolos)\b", "", busca).strip()


def buscar(cliente: httpx.Client, loja: LojaVtex, termo: str, paginas: int = 2) -> list[Oferta]:
    produtos = []
    for pagina in range(1, paginas + 1):
        for tentativa in range(4):
            r = cliente.get(
                f"{loja.base_url}/api/io/_v/api/intelligent-search/product_search/",
                params={"query": termo, "count": 50, "page": pagina},
            )
            if r.status_code != 429:
                break
            espera = float(r.headers.get("Retry-After") or 30 * (tentativa + 1))
            log.info("Atacadão pediu para esperar (429); aguardando %.0fs", espera)
            time.sleep(espera)
        r.raise_for_status()
        lote = r.json().get("products", [])
        produtos += lote
        if len(lote) < 50:
            break
    ofertas = []
    for p in produtos:
        for item in p.get("items", [])[:1]:
            ean = (item.get("ean") or "").strip()
            vendedores = [
                s for s in item.get("sellers", [])
                if s["commertialOffer"].get("AvailableQuantity", 0) > 0
                and s["commertialOffer"].get("Price")
            ]
            if not ean or not vendedores:
                continue
            oferta = min(vendedores, key=lambda s: s["commertialOffer"]["Price"])["commertialOffer"]
            imagens = item.get("images") or []
            ofertas.append(Oferta(
                ean=ean,
                nome=p["productName"],
                marca=p.get("brand"),
                imagem_url=imagens[0]["imageUrl"] if imagens else None,
                preco=float(oferta["Price"]),
                preco_lista=float(oferta.get("ListPrice") or oferta["Price"]),
            ))
    return ofertas


def salvar_produto(session: Session, ean: str | None, nome: str, marca: str | None,
                   imagem_url: str | None, grupos: dict[str, int]) -> Produto:
    """Upsert por EAN; sem EAN, procura pelo nome exato."""
    prod = None
    if ean:
        prod = session.exec(select(Produto).where(Produto.ean == ean)).first()
    if prod is None and not ean:
        prod = session.exec(select(Produto).where(Produto.nome == nome, Produto.ean.is_(None))).first()
    regra = encaixar(nome)
    if prod is None:
        prod = Produto(ean=ean or None, nome=nome, marca=marca, imagem_url=imagem_url)
        session.add(prod)
    prod.imagem_url = prod.imagem_url or imagem_url
    prod.marca = prod.marca or marca
    if regra and prod.grupo_id is None:
        prod.grupo_id = grupos[regra.slug]
    session.flush()
    return prod


def salvar_preco(session: Session, mercado_id: int, produto_id: int, preco: float,
                 fonte: str, promocao: bool = False, valido_ate: datetime | None = None,
                 encarte_id: int | None = None) -> None:
    p = session.exec(
        select(Preco).where(Preco.mercado_id == mercado_id, Preco.produto_id == produto_id)
    ).first()
    if p is None:
        p = Preco(mercado_id=mercado_id, produto_id=produto_id, preco=preco, fonte=fonte)
        session.add(p)
    p.preco, p.fonte, p.promocao, p.coletado_em = round(preco, 2), fonte, promocao, agora()
    p.valido_ate, p.encarte_id = valido_ate, encarte_id


def coletar(session: Session, loja: LojaVtex = ATACADAO, pausa: float = 3.0) -> dict[str, int]:
    grupos = sincronizar_catalogo(session)
    mercados = session.exec(select(Mercado).where(Mercado.rede == loja.rede)).all()
    if not mercados:
        log.warning("Nenhuma loja física de %s cadastrada; rode a importação do OSM antes.", loja.rede)

    stats = {"buscas": 0, "produtos": 0, "precos": 0}
    vistos: set[str] = set()
    with httpx.Client(timeout=30, headers={"User-Agent": USER_AGENT}, follow_redirects=True) as cliente:
        for regra in CESTA:
            try:
                ofertas = buscar(cliente, loja, termo_amplo(regra.busca))
            except httpx.HTTPError as e:
                log.warning("Falha buscando %r em %s: %s", regra.busca, loja.rede, e)
                continue
            stats["buscas"] += 1
            for o in ofertas:
                if o.ean in vistos or encaixar(o.nome) is None:
                    continue  # só guarda o que encaixa em algum item da feira
                vistos.add(o.ean)
                prod = salvar_produto(session, o.ean, o.nome, o.marca, o.imagem_url, grupos)
                stats["produtos"] += 1
                for m in mercados:
                    salvar_preco(session, m.id, prod.id, o.preco, loja.fonte,
                                 promocao=o.preco < o.preco_lista)
                    stats["precos"] += 1
            session.commit()
            time.sleep(pausa)
    return stats
