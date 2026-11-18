"""Importa preços reais a partir do cupom fiscal (NFC-e) que o próprio usuário enviou.

O usuário manda a FOTO do cupom (ou cola o texto). A IA (Gemini grátis ou Claude) extrai os itens,
o CNPJ da loja e, quando possível, encaixa cada item num item genérico da feira.
A consulta automática no portal da SEFAZ-RN não é usada porque ela exige captcha.
"""

from pydantic import BaseModel, Field
from sqlmodel import Session

from app.catalogo import CESTA, REGRAS_POR_SLUG, encaixar, sincronizar_catalogo
from app.coletores.vtex import salvar_preco, salvar_produto
from app.models import Mercado
from app.visao import SemCredencial, como_leitor  # noqa: F401 (SemCredencial é reexportado)


class ItemCupom(BaseModel):
    codigo: str | None = Field(description="Código do item como impresso (EAN de 8/13/14 dígitos, se houver)")
    descricao: str = Field(description="Descrição do item como impressa no cupom")
    descricao_legivel: str = Field(description="Descrição expandida e legível, ex.: 'Arroz Tio João Tipo 1 5kg'")
    quantidade: float
    unidade: str | None = Field(description="UN, KG, etc.")
    preco_unitario: float
    item_generico: str | None = Field(description="slug do item genérico da feira, ou null se nenhum servir")


class Cupom(BaseModel):
    cnpj: str | None = Field(description="CNPJ do estabelecimento, só dígitos")
    nome_estabelecimento: str | None
    data_emissao: str | None = Field(description="Data no formato AAAA-MM-DD")
    itens: list[ItemCupom]


def _instrucoes() -> str:
    lista = "\n".join(f"- {r.slug}: {r.nome}" for r in CESTA)
    return (
        "Você lê cupons fiscais (NFC-e) de supermercados do Rio Grande do Norte e extrai os dados "
        "exatamente como impressos. Não invente itens nem preços; se um campo estiver ilegível, use null. "
        "Ignore descontos gerais, troco e formas de pagamento. Preço unitário é o valor de uma unidade "
        "(ou de 1 kg, para itens pesados).\n\n"
        "Para cada item, preencha item_generico com o slug abaixo somente se o produto for claramente "
        f"aquele item, inclusive o tamanho:\n{lista}"
    )


def extrair(imagem: bytes | None = None, media_type: str = "image/jpeg",
            texto: str | None = None, cliente=None) -> Cupom:
    """`cliente`: um Leitor (app.visao), um cliente da Anthropic ou None (usa o provedor configurado)."""
    if not imagem and not texto:
        raise ValueError("Envie a foto ou o texto do cupom.")
    leitor = como_leitor(cliente)
    try:
        return leitor.ler(_instrucoes(), imagem, media_type,
                          texto or "Extraia os dados deste cupom fiscal.", Cupom)
    except ValueError as e:
        raise ValueError("Não foi possível ler este cupom.") from e


def _ean(codigo: str | None) -> str | None:
    c = (codigo or "").strip()
    return c if c.isdigit() and len(c) in (8, 12, 13, 14) else None


def importar(session: Session, mercado: Mercado, cupom: Cupom) -> int:
    """Grava os preços do cupom na loja escolhida. Retorna quantos itens foram gravados."""
    grupos = sincronizar_catalogo(session)
    if cupom.cnpj and not mercado.cnpj:
        mercado.cnpj = "".join(ch for ch in cupom.cnpj if ch.isdigit())

    gravados = 0
    for item in cupom.itens:
        if item.preco_unitario <= 0:
            continue
        nome = item.descricao_legivel or item.descricao
        prod = salvar_produto(session, _ean(item.codigo), nome, None, None, grupos)
        if prod.grupo_id is None:
            # a regra local tem prioridade; se ela não encaixou, usa a sugestão da IA
            slug = item.item_generico if item.item_generico in REGRAS_POR_SLUG else None
            if slug is None and (regra := encaixar(item.descricao)):
                slug = regra.slug
            if slug:
                prod.grupo_id = grupos[slug]
        salvar_preco(session, mercado.id, prod.id, item.preco_unitario, "cupom_fiscal")
        gravados += 1
    session.commit()
    return gravados
