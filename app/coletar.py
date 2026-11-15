"""Roda os coletores de dados reais.

    uv run python -m app.coletar osm        # lojas reais da Grande Natal (OpenStreetMap)
    uv run python -m app.coletar atacadao   # preços da loja online do Atacadão
    uv run python -m app.coletar encartes   # encartes de ofertas lidos por IA (precisa ANTHROPIC_API_KEY)
    uv run python -m app.coletar tudo
"""

import logging
import sys

from sqlmodel import Session

from app.catalogo import sincronizar_catalogo
from app.coletores import encarte, osm, vtex
from app.database import criar_tabelas, engine


def main(alvo: str) -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    criar_tabelas()
    with Session(engine) as s:
        sincronizar_catalogo(s)
        if alvo in ("osm", "tudo"):
            print(f"OSM: {osm.importar_mercados(s)} lojas gravadas")
        if alvo in ("atacadao", "tudo"):
            print(f"Atacadão: {vtex.coletar(s, vtex.ATACADAO)}")
        if alvo in ("encartes", "tudo"):
            for r in encarte.coletar(s):
                print(f"Encarte {r.encarte_id}: {r.situacao}, {r.itens} itens, {r.lojas} lojas")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "tudo")
