"""Itens genéricos da feira e regras para encaixar produtos reais neles.

Cada item tem um termo de busca (usado nos coletores) e palavras que o nome do
produto precisa ter / não pode ter. Tamanhos são normalizados ("5 kg" -> "5kg").
"""

import re
import unicodedata
from dataclasses import dataclass, field

from sqlmodel import Session, select

from app.models import ItemGenerico


@dataclass(frozen=True)
class Regra:
    slug: str
    nome: str
    categoria: str
    busca: str
    incluir: tuple[str, ...]
    excluir: tuple[str, ...] = field(default=())


CESTA: list[Regra] = [
    Regra("arroz-5kg", "Arroz branco tipo 1 5kg", "Grãos", "arroz tipo 1 5kg",
          ("arroz", "5kg"), ("integral", "parboilizado", "arboreo", "japones", "basmati", "7 graos")),
    Regra("feijao-carioca-1kg", "Feijão carioca 1kg", "Grãos", "feijao carioca 1kg",
          ("feijao", "carioca", "1kg")),
    Regra("feijao-preto-1kg", "Feijão preto 1kg", "Grãos", "feijao preto 1kg", ("feijao", "preto", "1kg")),
    Regra("feijao-macassar-1kg", "Feijão macassar/verde 1kg", "Grãos", "feijao macassar 1kg",
          ("feijao", "1kg"), ("carioca", "preto", "branco", "fradinho", "rajado")),
    Regra("acucar-cristal-1kg", "Açúcar cristal 1kg", "Mercearia", "acucar cristal 1kg",
          ("acucar", "cristal", "1kg")),
    Regra("acucar-cristal-2kg", "Açúcar cristal 2kg", "Mercearia", "acucar cristal 2kg",
          ("acucar", "cristal", "2kg")),
    Regra("acucar-cristal-5kg", "Açúcar cristal 5kg", "Mercearia", "acucar cristal 5kg",
          ("acucar", "cristal", "5kg")),
    Regra("cafe-250g", "Café torrado e moído 250g", "Mercearia", "cafe 250g",
          ("cafe", "250g"), ("capsula", "soluvel", "cappuccino", "graos")),
    Regra("cafe-500g", "Café torrado e moído 500g", "Mercearia", "cafe 500g",
          ("cafe", "500g"), ("capsula", "soluvel", "cappuccino", "graos")),
    Regra("oleo-soja-900ml", "Óleo de soja 900ml", "Mercearia", "oleo soja",
          ("oleo", "soja", "900ml")),
    Regra("flocao-500g", "Flocão de milho 500g", "Mercearia", "flocao de milho 500g",
          ("flocao", "milho", "500g")),
    Regra("farinha-mandioca-1kg", "Farinha de mandioca 1kg", "Mercearia", "farinha de mandioca 1kg",
          ("farinha", "mandioca", "1kg"), ("tapioca",)),
    Regra("macarrao-espaguete-500g", "Macarrão espaguete 500g", "Mercearia", "macarrao espaguete 500g",
          ("macarrao", "espaguete", "500g"), ("integral",)),
    Regra("sal-1kg", "Sal refinado 1kg", "Mercearia", "sal refinado 1kg", ("sal", "refinado", "1kg")),
    Regra("leite-uht-1l", "Leite integral UHT 1L", "Laticínios", "leite integral 1l",
          ("leite", "integral", "1l"), ("po", "condensado", "fermentado", "zero lactose", "coco")),
    Regra("leite-po-400g", "Leite em pó integral 400g", "Laticínios", "leite em po integral 400g",
          ("leite", "po", "400g"), ("composto", "desnatado")),
    Regra("manteiga-200g", "Manteiga 200g", "Laticínios", "manteiga 200g", ("manteiga", "200g"), ("cacau",)),
    Regra("margarina-500g", "Margarina 500g", "Laticínios", "margarina 500g", ("margarina", "500g")),
    Regra("ovos-30", "Ovos brancos 30 unidades", "Proteínas", "ovos brancos 30 unidades",
          ("ovo", "30un"), ("pascoa", "chocolate", "codorna")),
    Regra("ovos-12", "Ovos brancos 12 unidades", "Proteínas", "ovos brancos 12 unidades",
          ("ovo", "12un"), ("pascoa", "chocolate", "codorna")),
    Regra("frango-inteiro-kg", "Frango inteiro congelado (kg)", "Proteínas", "frango inteiro congelado",
          ("frango", "inteiro")),
    Regra("sardinha-lata", "Sardinha em lata 125g", "Proteínas", "sardinha em oleo 125g",
          ("sardinha", "125g")),
    Regra("pao-forma", "Pão de forma tradicional", "Padaria", "pao de forma tradicional",
          ("pao", "forma"), ("integral", "sem gluten", "brioche")),
    Regra("biscoito-cream-cracker", "Biscoito cream cracker", "Padaria", "biscoito cream cracker",
          ("cream cracker",)),
    Regra("molho-tomate-340g", "Molho de tomate sachê 340g", "Mercearia", "molho de tomate 340g",
          ("molho", "tomate", "340g")),
    Regra("detergente-500ml", "Detergente líquido 500ml", "Limpeza", "detergente liquido 500ml",
          ("detergente", "500ml")),
    Regra("sabao-po-1kg", "Sabão em pó 1kg", "Limpeza", "sabao em po 1kg", ("sabao", "po", "1kg")),
    Regra("agua-sanitaria-1l", "Água sanitária 1L", "Limpeza", "agua sanitaria 1l",
          ("agua sanitaria", "1l")),
    Regra("papel-higienico-12", "Papel higiênico 12 rolos", "Higiene", "papel higienico 12 rolos",
          ("papel higienico", "12")),
    Regra("creme-dental-90g", "Creme dental 90g", "Higiene", "creme dental 90g",
          ("creme dental", "90g")),
    Regra("sabonete-85g", "Sabonete em barra 85g", "Higiene", "sabonete 85g", ("sabonete", "85g"),
          ("liquido",)),
]

REGRAS_POR_SLUG = {r.slug: r for r in CESTA}


def normalizar(texto: str) -> str:
    """minúsculas, sem acento, tamanhos colados ("5 KG" -> "5kg", "1 litro" -> "1l")."""
    t = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode().lower()
    t = re.sub(r"(\d)\s*(unidades|unid|un)\b", r"\1un", t)
    t = re.sub(r"(\d)\s*(kg|g|ml|l)\b", r"\1\2", t)
    t = re.sub(r"(\d)\s*litros?\b", r"\1l", t)
    t = re.sub(r"[^a-z0-9 ]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _tem(nome_norm: str, termo: str) -> bool:
    # palavra inteira, aceitando plural simples ("ovo" casa com "ovos")
    return re.search(rf"(?<![a-z0-9]){re.escape(termo)}(s|es)?(?![a-z0-9])", nome_norm) is not None


def encaixar(nome_produto: str) -> Regra | None:
    """Retorna o item genérico em que o produto se encaixa (ou None)."""
    n = normalizar(nome_produto)
    for regra in CESTA:
        if all(_tem(n, t) for t in regra.incluir) and not any(_tem(n, t) for t in regra.excluir):
            return regra
    return None


def sincronizar_catalogo(session: Session) -> dict[str, int]:
    """Garante que todos os itens genéricos existem no banco. Retorna slug -> id."""
    existentes = {g.slug: g for g in session.exec(select(ItemGenerico))}
    for r in CESTA:
        if r.slug not in existentes:
            g = ItemGenerico(slug=r.slug, nome=r.nome, categoria=r.categoria)
            session.add(g)
            existentes[r.slug] = g
        else:
            existentes[r.slug].nome, existentes[r.slug].categoria = r.nome, r.categoria
    session.flush()
    ids = {slug: g.id for slug, g in existentes.items()}
    session.commit()  # ids lidos antes do commit: não deixa transação de leitura aberta
    return ids
