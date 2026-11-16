import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

import anthropic
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlmodel import Session, col, func, or_, select

from app.catalogo import REGRAS_POR_SLUG, sincronizar_catalogo
from app.coletores import cupom, encarte
from app.coletores.vtex import salvar_preco, salvar_produto
from app.database import criar_tabelas, engine, get_session
from app.models import Encarte, ItemGenerico, Mercado, Preco, Produto
from app.recommender import recomendar
from app.schemas import PedidoRecomendacao, PrecosDono, RespostaRecomendacao

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
TOKEN_ADMIN = os.getenv("OFERTAO_ADMIN_TOKEN")


@asynccontextmanager
async def lifespan(_: FastAPI):
    criar_tabelas()
    with Session(engine) as session:
        sincronizar_catalogo(session)
    yield


app = FastAPI(title="Ofertão API", version="0.2.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/status")
def status(session: Session = Depends(get_session)):
    """Quantos dados reais o sistema tem hoje."""
    por_fonte = session.exec(select(Preco.fonte, func.count()).group_by(Preco.fonte)).all()
    return {
        "lojas": session.exec(select(func.count()).select_from(Mercado)).one(),
        "lojas_com_preco": session.exec(select(func.count(func.distinct(Preco.mercado_id)))).one(),
        "produtos": session.exec(select(func.count()).select_from(Produto)).one(),
        "precos_por_fonte": dict(por_fonte),
    }


@app.get("/api/itens")
def listar_itens(q: str | None = None, session: Session = Depends(get_session)):
    """Itens genéricos da feira + produtos específicos (por nome ou EAN)."""
    genericos = session.exec(select(ItemGenerico).order_by(ItemGenerico.categoria, ItemGenerico.nome)).all()
    stmt = select(Produto).order_by(Produto.nome).limit(30)
    if q:
        genericos = [g for g in genericos if q.lower() in g.nome.lower()]
        stmt = stmt.where(or_(col(Produto.nome).ilike(f"%{q}%"), Produto.ean == q))
    produtos = session.exec(stmt).all() if q else []
    return {"genericos": genericos, "produtos": produtos}


@app.get("/api/mercados", response_model=list[Mercado])
def listar_mercados(session: Session = Depends(get_session)):
    return session.exec(select(Mercado).order_by(Mercado.nome)).all()


@app.post("/api/recomendar", response_model=RespostaRecomendacao)
def recomendar_mercado(pedido: PedidoRecomendacao, session: Session = Depends(get_session)):
    return recomendar(session, pedido)


@app.post("/api/mercados/{mercado_id}/cupom")
async def enviar_cupom(
    mercado_id: int,
    foto: UploadFile | None = File(default=None),
    texto: str | None = Form(default=None),
    session: Session = Depends(get_session),
):
    """Usuário envia a foto (ou o texto) do cupom fiscal de uma compra naquela loja."""
    mercado = session.get(Mercado, mercado_id)
    if not mercado:
        raise HTTPException(404, "Loja não encontrada")
    imagem = await foto.read() if foto else None
    if imagem and len(imagem) > 8 * 1024 * 1024:
        raise HTTPException(413, "Foto muito grande (máx. 8 MB)")
    try:
        dados = cupom.extrair(imagem=imagem, media_type=(foto.content_type if foto else "image/jpeg"),
                              texto=texto)
    except ValueError as e:
        raise HTTPException(422, str(e))
    except (encarte.SemCredencial, anthropic.AuthenticationError):
        raise HTTPException(503, "Leitura de cupons indisponível: configure ANTHROPIC_API_KEY")
    except anthropic.APIError as e:
        raise HTTPException(502, f"Falha ao ler o cupom: {e.message}")
    gravados = cupom.importar(session, mercado, dados)
    return {"itens_gravados": gravados, "cupom": dados}


def _exigir_token(x_token: str = Header(default="")) -> None:
    if not TOKEN_ADMIN or not secrets.compare_digest(x_token, TOKEN_ADMIN):
        raise HTTPException(401, "Token inválido")


@app.get("/admin", include_in_schema=False)
def pagina_admin() -> FileResponse:
    return FileResponse(STATIC_DIR / "admin.html")


@app.get("/api/redes")
def listar_redes(session: Session = Depends(get_session)):
    """Redes com lojas na Grande Natal (para associar encartes)."""
    linhas = session.exec(
        select(Mercado.rede, func.count()).where(Mercado.rede.is_not(None)).group_by(Mercado.rede)
    ).all()
    return [{"rede": r, "lojas": n} for r, n in sorted(linhas)]


@app.get("/api/encartes", response_model=list[Encarte])
def listar_encartes(session: Session = Depends(get_session)):
    return session.exec(select(Encarte).order_by(col(Encarte.processado_em).desc()).limit(50)).all()


@app.post("/api/encartes", dependencies=[Depends(_exigir_token)])
def enviar_encarte(
    rede: str = Form(...),
    arquivo: UploadFile = File(...),
    url_origem: str | None = Form(default=None),
    session: Session = Depends(get_session),
):
    """Envia um encarte (PDF ou imagem) de uma rede; a IA extrai as ofertas."""
    conteudo = arquivo.file.read(encarte.MAX_BYTES + 1)
    if len(conteudo) > encarte.MAX_BYTES:
        raise HTTPException(413, "Arquivo muito grande")
    tipo = arquivo.content_type or "application/octet-stream"
    if tipo != "application/pdf" and not tipo.startswith("image/"):
        raise HTTPException(415, "Envie PDF ou imagem")
    try:
        r = encarte.processar(session, rede, conteudo, tipo, url_origem)
    except ValueError as e:
        raise HTTPException(422, str(e))
    except (encarte.SemCredencial, anthropic.AuthenticationError):
        raise HTTPException(503, "Leitura indisponível: configure ANTHROPIC_API_KEY")
    except anthropic.APIError as e:
        raise HTTPException(502, f"Falha ao ler o encarte: {e.message}")
    return r


def _gravar_precos_dono(session: Session, mercado_ids: list[int], corpo: PrecosDono) -> dict:
    grupos = sincronizar_catalogo(session)
    for item in corpo.itens:
        prod = salvar_produto(session, item.ean, item.nome, None, None, grupos)
        if prod.grupo_id is None and item.item_generico in REGRAS_POR_SLUG:
            prod.grupo_id = grupos[item.item_generico]
        for mercado_id in mercado_ids:
            salvar_preco(session, mercado_id, prod.id, item.preco, "dono", item.promocao)
    session.commit()
    return {"itens_gravados": len(corpo.itens), "lojas": len(mercado_ids)}


@app.post("/api/mercados/{mercado_id}/precos")
def precos_do_dono(
    mercado_id: int,
    corpo: PrecosDono,
    session: Session = Depends(get_session),
    _: None = Depends(_exigir_token),
):
    """Painel do dono: a loja informa seus preços. Protegido por OFERTAO_ADMIN_TOKEN."""
    if not session.get(Mercado, mercado_id):
        raise HTTPException(404, "Loja não encontrada")
    return _gravar_precos_dono(session, [mercado_id], corpo)


@app.post("/api/redes/{rede}/precos")
def precos_da_rede(
    rede: str,
    corpo: PrecosDono,
    session: Session = Depends(get_session),
    _: None = Depends(_exigir_token),
):
    """Cadastro manual de preços válidos em todas as lojas de uma rede (ex.: copiados do encarte)."""
    ids = session.exec(select(Mercado.id).where(Mercado.rede == rede)).all()
    if not ids:
        raise HTTPException(404, "Nenhuma loja dessa rede")
    return _gravar_precos_dono(session, list(ids), corpo)
