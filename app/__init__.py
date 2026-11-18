"""Ofertão. Ao importar o pacote, as variáveis do arquivo .env (fora do git) são carregadas."""

import os
from pathlib import Path


def _carregar_env(caminho: Path) -> None:
    """Lê CHAVE=valor do arquivo .env (fora do git). Variáveis já definidas no sistema têm prioridade."""
    if not caminho.is_file():
        return
    for linha in caminho.read_text(encoding="utf-8-sig").splitlines():
        linha = linha.strip()
        if linha and not linha.startswith("#") and "=" in linha:
            chave, valor = linha.split("=", 1)
            os.environ.setdefault(chave.strip(), valor.strip().strip('"').strip("'"))


_carregar_env(Path(__file__).resolve().parent.parent / ".env")

USER_AGENT = "OfertaoBot/0.1 (comparador de precos da Grande Natal)"
