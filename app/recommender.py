"""Motor de recomendação: qual loja real compensa mais para o carrinho do usuário?

custo_total = soma(preço x quantidade) + deslocamento (ida e volta x R$/km)

- Item genérico ("arroz 5kg"): usa o produto mais barato daquele tipo em cada loja.
- Produto específico: usa o preço exato daquele produto.
- Só entram no ranking lojas que têm preço para pelo menos um item do carrinho.
- Item faltante numa loja é estimado pela mediana das outras lojas + 20%.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import combinations
from statistics import median

from sqlmodel import Session, col, or_, select

from app.geo import distancia_rua_km
from app.models import ItemGenerico, Mercado, Preco, Produto
from app.schemas import (
    LinhaItem,
    ParteDivisao,
    PedidoRecomendacao,
    RespostaRecomendacao,
    ResultadoDivisao,
    ResultadoMercado,
)

PENALIDADE_FALTANTE = 1.20
MAX_MERCADOS_DIVISAO = 12
ECONOMIA_MINIMA_DIVISAO = 3.00

Chave = tuple[str, int]


@dataclass
class _Ctx:
    pedido: PedidoRecomendacao
    rotulos: dict[Chave, str]
    quantidades: dict[Chave, float]
    # mercado_id -> chave do carrinho -> melhor (Preco, Produto)
    ofertas: dict[int, dict[Chave, tuple[Preco, Produto]]]
    medianas: dict[Chave, float]


def recomendar(session: Session, pedido: PedidoRecomendacao) -> RespostaRecomendacao:
    quantidades: dict[Chave, float] = {}
    for item in pedido.itens:
        quantidades[item.chave] = quantidades.get(item.chave, 0) + item.quantidade

    grupo_ids = [i for t, i in quantidades if t == "g"]
    produto_ids = [i for t, i in quantidades if t == "p"]
    rotulos: dict[Chave, str] = {}
    for g in session.exec(select(ItemGenerico).where(col(ItemGenerico.id).in_(grupo_ids))):
        rotulos[("g", g.id)] = g.nome
    for p in session.exec(select(Produto).where(col(Produto.id).in_(produto_ids))):
        rotulos[("p", p.id)] = p.nome
    quantidades = {k: q for k, q in quantidades.items() if k in rotulos}

    vazio = dict(melhor=None, ranking=[], divisao=None, lojas_no_raio=0, lojas_sem_dados=0)
    if not quantidades:
        return RespostaRecomendacao(**vazio, explicacao="Nenhum item válido no carrinho.")

    mercados = {
        m.id: (m, d)
        for m in session.exec(select(Mercado))
        if (d := distancia_rua_km(pedido.lat, pedido.lon, m.lat, m.lon)) <= pedido.raio_km
    }
    if not mercados:
        return RespostaRecomendacao(
            **vazio, explicacao=f"Nenhuma loja num raio de {pedido.raio_km:.0f} km."
        )

    limite = datetime.now(UTC) - timedelta(days=pedido.max_idade_dias)
    linhas = session.exec(
        select(Preco, Produto)
        .join(Produto, Produto.id == Preco.produto_id)
        .where(
            col(Preco.mercado_id).in_(mercados),
            Preco.coletado_em >= limite,
            or_(col(Preco.valido_ate).is_(None), Preco.valido_ate >= datetime.now(UTC)),
            or_(col(Produto.id).in_(produto_ids), col(Produto.grupo_id).in_(grupo_ids)),
        )
    ).all()

    ofertas: dict[int, dict[Chave, tuple[Preco, Produto]]] = {}
    for preco, prod in linhas:
        chaves = [k for k in (("p", prod.id), ("g", prod.grupo_id)) if k in quantidades]
        por_loja = ofertas.setdefault(preco.mercado_id, {})
        for k in chaves:
            atual = por_loja.get(k)
            if atual is None or preco.preco < atual[0].preco:
                por_loja[k] = (preco, prod)

    medianas = {}
    for k in quantidades:
        valores = [o[k][0].preco for o in ofertas.values() if k in o]
        medianas[k] = median(valores) if valores else 0.0

    ctx = _Ctx(pedido, rotulos, quantidades, ofertas, medianas)
    com_dados = [(mercados[mid][0], mercados[mid][1]) for mid in ofertas]
    if not com_dados:
        return RespostaRecomendacao(
            **{**vazio, "lojas_no_raio": len(mercados), "lojas_sem_dados": len(mercados)},
            explicacao=(
                f"Há {len(mercados)} lojas no raio, mas ainda nenhuma tem preço para esses itens. "
                "Envie um cupom fiscal de uma delas para começar!"
            ),
        )

    ranking = sorted(
        (_avaliar(ctx, m, d) for m, d in com_dados),
        key=lambda r: (_custo_comparavel(ctx, r), r.distancia_km),
    )
    divisao = None
    if pedido.permitir_divisao and len(com_dados) > 1:
        divisao = _melhor_divisao(ctx, com_dados, _custo_comparavel(ctx, ranking[0]))

    return RespostaRecomendacao(
        melhor=ranking[0],
        ranking=ranking,
        divisao=divisao,
        lojas_no_raio=len(mercados),
        lojas_sem_dados=len(mercados) - len(com_dados),
        explicacao=_explicar(ranking, divisao, len(mercados) - len(com_dados)),
    )


def _linha(ctx: _Ctx, chave: Chave, preco: Preco, prod: Produto) -> LinhaItem:
    q = ctx.quantidades[chave]
    return LinhaItem(
        pedido=ctx.rotulos[chave],
        produto_id=prod.id,
        produto=prod.nome,
        quantidade=q,
        preco_unitario=preco.preco,
        subtotal=round(preco.preco * q, 2),
        promocao=preco.promocao,
        fonte=preco.fonte,
        coletado_em=preco.coletado_em,
    )


def _avaliar(ctx: _Ctx, m: Mercado, distancia: float) -> ResultadoMercado:
    tabela = ctx.ofertas[m.id]
    itens = [_linha(ctx, k, *tabela[k]) for k in ctx.quantidades if k in tabela]
    faltantes = [ctx.rotulos[k] for k in ctx.quantidades if k not in tabela]
    subtotal = round(sum(i.subtotal for i in itens), 2)
    deslocamento = round(distancia * 2 * ctx.pedido.custo_km, 2)
    return ResultadoMercado(
        mercado_id=m.id, nome=m.nome, rede=m.rede, endereco=m.endereco, cidade=m.cidade,
        lat=m.lat, lon=m.lon, distancia_km=round(distancia, 2),
        subtotal_carrinho=subtotal, custo_deslocamento=deslocamento,
        custo_total=round(subtotal + deslocamento, 2),
        itens=itens, faltantes=faltantes, completo=not faltantes,
    )


def _custo_comparavel(ctx: _Ctx, r: ResultadoMercado) -> float:
    tabela = ctx.ofertas[r.mercado_id]
    penalidade = sum(
        ctx.medianas[k] * q * PENALIDADE_FALTANTE
        for k, q in ctx.quantidades.items() if k not in tabela
    )
    return r.custo_total + penalidade


def _melhor_divisao(ctx: _Ctx, mercados: list[tuple[Mercado, float]],
                    custo_unico: float) -> ResultadoDivisao | None:
    candidatos = sorted(mercados, key=lambda md: md[1])[:MAX_MERCADOS_DIVISAO]
    melhor: ResultadoDivisao | None = None
    for (a, da), (b, db) in combinations(candidatos, 2):
        ta, tb = ctx.ofertas[a.id], ctx.ofertas[b.id]
        if any(k not in ta and k not in tb for k in ctx.quantidades):
            continue
        itens_a, itens_b = [], []
        for k in ctx.quantidades:
            oa, ob = ta.get(k), tb.get(k)
            if ob is None or (oa is not None and oa[0].preco <= ob[0].preco):
                itens_a.append(_linha(ctx, k, *oa))
            else:
                itens_b.append(_linha(ctx, k, *ob))
        if not itens_a or not itens_b:
            continue
        rota = da + distancia_rua_km(a.lat, a.lon, b.lat, b.lon) + db
        desloc = round(rota * ctx.pedido.custo_km, 2)
        sub_a = round(sum(i.subtotal for i in itens_a), 2)
        sub_b = round(sum(i.subtotal for i in itens_b), 2)
        total = round(sub_a + sub_b + desloc, 2)
        if melhor is None or total < melhor.custo_total:
            melhor = ResultadoDivisao(
                partes=[ParteDivisao(mercado_id=a.id, nome=a.nome, itens=itens_a, subtotal=sub_a),
                        ParteDivisao(mercado_id=b.id, nome=b.nome, itens=itens_b, subtotal=sub_b)],
                distancia_rota_km=round(rota, 2), custo_deslocamento=desloc, custo_total=total,
                economia_vs_melhor_unico=round(custo_unico - total, 2),
            )
    if melhor and melhor.economia_vs_melhor_unico >= ECONOMIA_MINIMA_DIVISAO:
        return melhor
    return None


def _brl(v: float) -> str:
    return f"R$ {v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


NOMES_FONTE = {
    "atacadao_online": "API pública Atacadão",
    "paodeacucar_online": "API pública Pão de Açúcar",
    "samsclub_online": "API pública Sam's Club",
    "consulta_publica": "consulta de referência pública local",
    "cupom_fiscal": "cupom fiscal enviado por usuário",
    "dono": "informado pela loja",
    "encarte": "encarte de ofertas da rede",
}


def _explicar(ranking: list[ResultadoMercado], divisao: ResultadoDivisao | None,
              sem_dados: int) -> str:
    m = ranking[0]
    partes = [
        f"A melhor opção é {m.nome}{f' ({m.cidade})' if m.cidade else ''}, a {m.distancia_km:.1f} km: "
        f"{_brl(m.subtotal_carrinho)} na feira + {_brl(m.custo_deslocamento)} de deslocamento "
        f"= {_brl(m.custo_total)}."
    ]
    if m.faltantes:
        partes.append(f"Lá não encontramos preço para: {', '.join(m.faltantes)}.")
    completos = [r for r in ranking if r.completo]
    if len(completos) > 1 and m.completo:
        mais_caro = max(completos, key=lambda r: r.custo_total)
        if mais_caro.custo_total > m.custo_total:
            partes.append(
                f"Você economiza {_brl(mais_caro.custo_total - m.custo_total)} em relação ao "
                f"mais caro ({mais_caro.nome})."
            )
    fontes = sorted({NOMES_FONTE.get(i.fonte, i.fonte) for i in m.itens})
    partes.append(f"Fonte dos preços: {', '.join(fontes)}.")
    if divisao:
        a, b = divisao.partes
        partes.append(
            f"Dica: dividindo a feira entre {a.nome} e {b.nome} você paga "
            f"{_brl(divisao.custo_total)} (economia de {_brl(divisao.economia_vs_melhor_unico)})."
        )
    if sem_dados:
        partes.append(f"{sem_dados} lojas próximas ainda não têm preços no Ofertão.")
    return " ".join(partes)
