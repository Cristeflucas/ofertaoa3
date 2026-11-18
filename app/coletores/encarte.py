"""Encartes de ofertas (tabloides) lidos por IA.

Fluxo: PDF/imagem do encarte -> páginas em JPEG -> IA (Gemini grátis ou Claude) extrai itens, preços e
validade -> preços gravados em TODAS as lojas daquela rede na Grande Natal,
com fonte "encarte" e data de validade (oferta vencida não entra na comparação).

Fontes automáticas: sites oficiais das redes que publicam o encarte (FONTES).
Outras redes: envio manual pelo painel (/admin) com a imagem/PDF.
"""

import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time

import anthropic
import httpx
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from app.catalogo import CESTA, REGRAS_POR_SLUG, encaixar, sincronizar_catalogo
from app.coletores import USER_AGENT
from app.coletores.vtex import salvar_preco, salvar_produto
from app.models import Encarte, Mercado
from app.visao import FalhaLeitura, Leitor, SemCredencial, como_leitor

log = logging.getLogger(__name__)

MAX_PAGINAS = 12
MAX_BYTES = 60 * 1024 * 1024
LADO_MAX_PX = 1568  # bom equilíbrio entre legibilidade dos preços e tamanho do envio


@dataclass(frozen=True)
class FonteEncarte:
    rede: str  # precisa bater com Mercado.rede (ver coletores/osm.py)
    pagina: str  # página oficial onde o encarte é publicado
    padrao_arquivo: str  # regex dos links de encarte nessa página


FONTES = [
    FonteEncarte(
        "Rede Mais",
        "https://www.redemaissupermercados.com.br/encarte-rede-mais",
        r"https?://[^\"'\s]+/painel/dados/arquivos/encarte[^\"'\s]*\.pdf",
    ),
]


# ---------- extração por IA ----------

class ItemEncarte(BaseModel):
    nome: str = Field(description="Produto como no encarte, com marca e tamanho. Ex.: 'Margarina Qualy Cremosa com Sal 500g'")
    unidade: str = Field(description="'kg' se o preço é por quilo, senão 'cada'")
    preco_oferta: float = Field(description="Preço em destaque")
    preco_regular: float | None = Field(description="'Preço regular' impresso ao lado, se houver")
    so_no_app: bool = Field(description="true se o preço em destaque vale só pelo aplicativo/clube da rede")
    item_generico: str | None = Field(description="slug do item genérico da feira, ou null")


class PaginaEncarte(BaseModel):
    valido_de: str | None = Field(description="Início da validade, AAAA-MM-DD")
    valido_ate: str | None = Field(description="Fim da validade, AAAA-MM-DD")
    itens: list[ItemEncarte]


def _instrucoes() -> str:
    lista = "\n".join(f"- {r.slug}: {r.nome}" for r in CESTA)
    return (
        "Você lê encartes (tabloides) de ofertas de supermercados do Rio Grande do Norte e extrai "
        "cada produto com seu preço, exatamente como impresso. Não invente itens nem preços; pule "
        "produtos cujo preço esteja ilegível. Ignore logotipos de marcas patrocinadoras sem preço. "
        "Se o encarte não mostrar o ano da validade, use o ano da data de hoje informada.\n\n"
        "Preencha item_generico com um slug abaixo somente se o produto for claramente aquele "
        f"item, inclusive o tamanho:\n{lista}"
    )


def paginas_como_imagens(conteudo: bytes, media_type: str) -> list[tuple[bytes, str, str]]:
    """Retorna [(jpeg, 'image/jpeg', texto_da_pagina)]. Imagens passam direto."""
    if media_type != "application/pdf" and not conteudo.startswith(b"%PDF"):
        return [(conteudo, media_type, "")]
    import pymupdf
    doc = pymupdf.open(stream=conteudo, filetype="pdf")
    paginas = []
    for pagina in list(doc)[:MAX_PAGINAS]:
        zoom = LADO_MAX_PX / max(pagina.rect.width, pagina.rect.height)
        pix = pagina.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom))
        paginas.append((pix.tobytes("jpeg", jpg_quality=85), "image/jpeg", pagina.get_text()))
    return paginas


def ler_pagina(imagem: bytes, media_type: str, texto: str, hoje: date, leitor: Leitor) -> PaginaEncarte:
    pedido = f"Data de hoje: {hoje.isoformat()}. Extraia as ofertas desta página do encarte."
    if texto.strip():
        pedido += f"\n\nTexto embutido no PDF (pode ajudar a ler nomes e preços):\n{texto[:6000]}"
    return leitor.ler(_instrucoes(), imagem, media_type, pedido, PaginaEncarte)


# ---------- gravação ----------

def _data(texto: str | None, fim_do_dia: bool = False) -> datetime | None:
    try:
        d = date.fromisoformat(texto) if texto else None
    except ValueError:
        return None
    return datetime.combine(d, time.max if fim_do_dia else time.min, tzinfo=UTC) if d else None


@dataclass
class Resultado:
    encarte_id: int
    situacao: str
    itens: int
    lojas: int
    valido_ate: datetime | None


def processar(session: Session, rede: str, conteudo: bytes, media_type: str,
              url_origem: str | None = None, cliente=None,
              hoje: date | None = None) -> Resultado:
    """`cliente`: um Leitor (app.visao), um cliente da Anthropic ou None (usa o provedor configurado)."""
    hash_arquivo = hashlib.sha256(conteudo).hexdigest()
    ja = session.exec(select(Encarte).where(Encarte.hash_arquivo == hash_arquivo)).first()
    if ja:
        return Resultado(ja.id, "repetido", ja.itens_extraidos, 0, ja.valido_ate)

    hoje = hoje or datetime.now(UTC).date()
    leitor = como_leitor(cliente)
    imagens = paginas_como_imagens(conteudo, media_type)
    log.info("Encarte %s: lendo %d página(s) com %s", rede, len(imagens), leitor.nome)
    paginas = [ler_pagina(img, mt, txt, hoje, leitor) for img, mt, txt in imagens]

    valido_de = next((_data(p.valido_de) for p in paginas if p.valido_de), None)
    valido_ate = next((_data(p.valido_ate, fim_do_dia=True) for p in paginas if p.valido_ate), None)
    lojas = session.exec(select(Mercado).where(Mercado.rede == rede)).all()

    situacao = "processado"
    if valido_ate and valido_ate.date() < hoje:
        situacao = "expirado"
    elif not lojas:
        situacao = "sem_lojas"

    encarte = Encarte(rede=rede, hash_arquivo=hash_arquivo, url_origem=url_origem,
                      valido_de=valido_de, valido_ate=valido_ate, situacao=situacao)
    session.add(encarte)
    session.flush()

    itens = [i for p in paginas for i in p.itens if i.preco_oferta > 0]
    encarte.itens_extraidos = len(itens)
    if situacao == "processado":
        grupos = sincronizar_catalogo(session)
        for item in itens:
            prod = salvar_produto(session, None, item.nome, None, None, grupos)
            if prod.grupo_id is None:
                slug = item.item_generico if item.item_generico in REGRAS_POR_SLUG else None
                if slug is None and (regra := encaixar(item.nome)):
                    slug = regra.slug
                if slug:
                    prod.grupo_id = grupos[slug]
            # Preço só no app: quem compra no caixa paga o regular
            preco = item.preco_regular if item.so_no_app and item.preco_regular else item.preco_oferta
            for loja in lojas:
                salvar_preco(session, loja.id, prod.id, preco, "encarte", promocao=True,
                             valido_ate=valido_ate, encarte_id=encarte.id)
    session.commit()
    return Resultado(encarte.id, situacao, len(itens), len(lojas), valido_ate)


# ---------- busca automática nos sites das redes ----------

def baixar(cliente: httpx.Client, url: str) -> bytes:
    with cliente.stream("GET", url) as r:
        r.raise_for_status()
        dados = bytearray()
        for bloco in r.iter_bytes():
            dados += bloco
            if len(dados) > MAX_BYTES:
                raise ValueError(f"Encarte maior que {MAX_BYTES // 2**20} MB: {url}")
        return bytes(dados)


def coletar(session: Session, cliente_ia=None) -> list[Resultado]:
    resultados = []
    leitor = como_leitor(cliente_ia)  # falha cedo (SemCredencial) se não houver chave
    with httpx.Client(timeout=120, headers={"User-Agent": USER_AGENT}, follow_redirects=True) as http:
        for fonte in FONTES:
            try:
                html = http.get(fonte.pagina).text
            except httpx.HTTPError as e:
                log.warning("Não consegui abrir %s: %s", fonte.pagina, e)
                continue
            links = list(dict.fromkeys(re.findall(fonte.padrao_arquivo, html)))
            log.info("%s: %d encarte(s) encontrado(s)", fonte.rede, len(links))
            for url in links:
                try:
                    conteudo = baixar(http, url)
                    r = processar(session, fonte.rede, conteudo, "application/pdf", url, leitor)
                except SemCredencial as e:
                    log.error("%s", e)
                    return resultados
                except (httpx.HTTPError, ValueError, FalhaLeitura, anthropic.AnthropicError) as e:
                    log.warning("Falha no encarte %s: %s", url, e)
                    continue
                log.info("%s -> %s (%d itens, %d lojas)", url, r.situacao, r.itens, r.lojas)
                resultados.append(r)
    return resultados
