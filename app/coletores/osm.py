"""Importa supermercados reais da Grande Natal a partir do OpenStreetMap (Overpass API).

Dados © colaboradores do OpenStreetMap, licença ODbL.
"""

import logging
import re
import time

import httpx
from sqlmodel import Session, select

from app.coletores import USER_AGENT
from app.models import Mercado, agora

log = logging.getLogger(__name__)

# Códigos IBGE dos municípios da Região Metropolitana de Natal (evita homônimos, ex.: Parnamirim-PE)
MUNICIPIOS = {
    "2408102": "Natal",
    "2403251": "Parnamirim",
    "2412005": "São Gonçalo do Amarante",
    "2407104": "Macaíba",
    "2403608": "Extremoz",
    "2402600": "Ceará-Mirim",
    "2412203": "São José de Mipibu",
    "2408201": "Nísia Floresta",
}

SERVIDORES = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]

# Redes conhecidas em Natal: regex sobre nome/brand -> nome canônico da rede
REDES = [
    (r"queiroz", "Queiroz Atacadão"),  # rede local diferente do Atacadão (Carrefour)
    (r"atacad[aã]o", "Atacadão"),
    (r"nordest[aã]o", "Nordestão"),
    (r"superf[aá]cil", "SuperFácil"),
    (r"carrefour", "Carrefour"),
    (r"assa[ií]", "Assaí"),
    (r"rede mais", "Rede Mais"),
    (r"super ?show", "Super Show"),
    (r"p[aã]o de a[cç][uú]car", "Pão de Açúcar"),
    (r"favorito", "Favorito"),
    (r"bompre[cç]o", "Bompreço"),
    (r"mineir[aã]o", "Mineirão"),
    (r"comjol", "Comjol"),
    (r"mar vermelho", "Mar Vermelho"),
    (r"boa esperan[cç]a", "Boa Esperança"),
]

# Coisas marcadas como supermercado no OSM que não são mercado de feira
IGNORAR = re.compile(r"suplemento|pet ?shop|farm[aá]cia|propaganda|cosm[eé]tico", re.I)


def _consulta() -> str:
    codigos = "|".join(MUNICIPIOS)
    return f"""
[out:json][timeout:120];
area["IBGE:GEOCODIGO"~"^({codigos})$"]->.gn;
(
  nwr["shop"~"^(supermarket|wholesale)$"](area.gn);
  nwr["shop"="convenience"]["name"~"[Mm]ercad|[Ss]uper"](area.gn);
);
out center tags;
"""


def baixar_elementos(tentativas: int = 6) -> list[dict]:
    espera = 5.0
    ultimo_erro: Exception | None = None
    for i in range(tentativas):
        url = SERVIDORES[i % len(SERVIDORES)]
        try:
            r = httpx.post(url, data={"data": _consulta()}, timeout=150,
                           headers={"User-Agent": USER_AGENT})
            r.raise_for_status()
            return r.json()["elements"]
        except (httpx.HTTPError, ValueError, KeyError) as e:
            ultimo_erro = e
            log.warning("Overpass falhou em %s (%s); nova tentativa em %.0fs", url, e, espera)
            time.sleep(espera)
            espera *= 2
    raise RuntimeError(f"Overpass indisponível: {ultimo_erro}")


def identificar_rede(*textos: str | None) -> str | None:
    alvo = " ".join(t for t in textos if t)
    for padrao, rede in REDES:
        if re.search(padrao, alvo, re.I):
            return rede
    return None


def importar_mercados(session: Session, elementos: list[dict] | None = None) -> int:
    """Insere/atualiza as lojas. Retorna quantas lojas foram gravadas."""
    if elementos is None:
        elementos = baixar_elementos()

    existentes = {m.osm_id: m for m in session.exec(select(Mercado)) if m.osm_id}
    gravados = 0
    for e in elementos:
        tags = e.get("tags", {})
        nome = tags.get("name") or tags.get("brand")
        lat = e.get("lat") or e.get("center", {}).get("lat")
        lon = e.get("lon") or e.get("center", {}).get("lon")
        if not nome or lat is None or IGNORAR.search(nome):
            continue

        osm_id = f"{e['type']}/{e['id']}"
        rua = " ".join(filter(None, [tags.get("addr:street"), tags.get("addr:housenumber")]))
        dados = dict(
            nome=nome.strip(),
            rede=identificar_rede(tags.get("brand"), nome),
            tipo=tags.get("shop", "supermarket"),
            endereco=rua,
            bairro=tags.get("addr:suburb", ""),
            cidade=tags.get("addr:city", ""),
            lat=lat,
            lon=lon,
            atualizado_em=agora(),
        )
        m = existentes.get(osm_id)
        if m:
            for k, v in dados.items():
                setattr(m, k, v)
        else:
            session.add(Mercado(osm_id=osm_id, **dados))
        gravados += 1

    session.commit()
    return gravados
