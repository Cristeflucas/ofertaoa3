"""Consulta supermercados reais em qualquer cidade/região do Brasil em tempo real (~0.5s)
combinando ArcGIS World Geocoding POI API (categoria Grocery + redes) e OpenStreetMap.
"""

from concurrent.futures import ThreadPoolExecutor
import hashlib
import logging
import math
import re

import httpx
from sqlmodel import Session, select

from app.coletores import USER_AGENT
from app.models import Mercado, agora

log = logging.getLogger(__name__)

ARCGIS_URL = "https://geocode.arcgis.com/arcgis/rest/services/World/GeocodeServer/findAddressCandidates"

# Códigos IBGE originais (mantido para compatibilidade com chamadas legadas sem coordenadas)
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

# Redes de supermercados e atacarejos de todo o Brasil
REDES = [
    (r"queiroz", "Queiroz Atacadão"),
    (r"atacad[aã]o", "Atacadão"),
    (r"sam'?s ?club", "Sam's Club"),
    (r"assa[ií]", "Assaí"),
    (r"carrefour", "Carrefour"),
    (r"p[aã]o de a[cç][uú]car", "Pão de Açúcar"),
    (r"minuto p[aã]o de a[cç][uú]car", "Pão de Açúcar"),
    (r"extra\b|mercado extra|clube extra|mini extra", "Extra"),
    (r"nordest[aã]o", "Nordestão"),
    (r"superf[aá]cil", "SuperFácil"),
    (r"rede ?mais", "Rede Mais"),
    (r"super ?show", "Super Show"),
    (r"favorito", "Favorito"),
    (r"bompre[cç]o", "Bompreço"),
    (r"todo ?dia", "TodoDia"),
    (r"super ?co+p", "Supercop"),
    (r"maxxi", "Maxxi Atacado"),
    (r"mineir[aã]o", "Mineirão"),
    (r"mateus|mix mateus", "Mateus"),
    (r"guanabara", "Guanabara"),
    (r"mundial", "Mundial"),
    (r"prezunic", "Prezunic"),
    (r"zona sul", "Zona Sul"),
    (r"supermercados? bh\b", "Supermercados BH"),
    (r"mart minas", "Mart Minas"),
    (r"epa\b", "EPA"),
    (r"verdemar", "Verdemar"),
    (r"muffato|max atacadista", "Muffato"),
    (r"condor", "Condor"),
    (r"angeloni", "Angeloni"),
    (r"giassi", "Giassi"),
    (r"bistek", "Bistek"),
    (r"kompr[aã]o|koch", "Koch"),
    (r"zaffari", "Zaffari"),
    (r"stok center", "Stok Center"),
    (r"tenda atacado", "Tenda Atacado"),
    (r"rold[aã]o", "Roldão"),
    (r"savegnago", "Savegnago"),
    (r"sonda", "Sonda"),
    (r"mambo", "Mambo"),
    (r"hirota", "Hirota"),
    (r"st ?marche", "St Marche"),
    (r"nagumo", "Nagumo"),
    (r"barbosa", "Barbosa"),
    (r"oba hortifruti", "Oba Hortifruti"),
    (r"\bdia\b|supermercado dia|dia %", "Dia"),
    (r"comper", "Comper"),
    (r"bretas", "Bretas"),
    (r"comjol", "Comjol"),
    (r"mar vermelho", "Mar Vermelho"),
    (r"boa esperan[cç]a", "Boa Esperança"),
]

IGNORAR = re.compile(r"suplemento|pet ?shop|farm[aá]cia|propaganda|cosm[eé]tico", re.I)


def _bbox(lat: float, lon: float, raio_km: float = 12.0) -> tuple[float, float, float, float]:
    """Retorna (south, west, north, east) em graus para o raio solicitado."""
    r = min(max(raio_km, 2.0), 25.0)
    dlat = r / 111.0
    dlon = r / max(20.0, 111.0 * math.cos(math.radians(lat)))
    return (lat - dlat, lon - dlon, lat + dlat, lon + dlon)


def identificar_rede(*textos: str | None) -> str | None:
    alvo = " ".join(t for t in textos if t)
    for padrao, rede in REDES:
        if re.search(padrao, alvo, re.I):
            return rede
    return None


def _buscar_arcgis_regiao(lat: float, lon: float, raio_km: float = 15.0) -> list[dict]:
    """Busca instantânea (~0.5s) de supermercados e atacadistas na região via ArcGIS POI API."""
    s, w, n, e = _bbox(lat, lon, raio_km)
    extent = f"{w:.4f},{s:.4f},{e:.4f},{n:.4f}"

    termos = [
        "Supermercado",
        "Mercado",
        "Hipermercado",
        "Atacadão",
        "Assaí",
        "Carrefour",
        "Pão de Açúcar",
        "Atacado",
    ]

    def _req(termo: str) -> list[dict]:
        try:
            with httpx.Client(timeout=3.5, headers={"User-Agent": USER_AGENT}) as c:
                params = {
                    "SingleLine": termo,
                    "location": f"{lon},{lat}",
                    "searchExtent": extent,
                    "countryCode": "BRA",
                    "maxLocations": 50,
                    "outFields": "PlaceName,Place_addr,StAddr,Nbrhd,City,Type",
                    "f": "json",
                }
                r = c.get(ARCGIS_URL, params=params)
                if r.status_code == 200:
                    return r.json().get("candidates", [])
        except Exception:
            pass
        return []

    elementos: list[dict] = []
    vistos: set[str] = set()
    with ThreadPoolExecutor(max_workers=8) as pool:
        for cands in pool.map(_req, termos):
            for c in cands:
                attr = c.get("attributes") or {}
                loc = c.get("location") or {}
                clat, clon = loc.get("y"), loc.get("x")
                nome = (attr.get("PlaceName") or "").strip()
                tipo_arcgis = (attr.get("Type") or "").strip()
                if not nome or clat is None or clon is None:
                    continue
                if tipo_arcgis in ("StreetName", "StreetAddress", "PointAddress", "DistanceMarker", "Locality", "Neighborhood", "City", "Postal", "SubAdmin", "Admin"):
                    continue
                # Ignora resultados fora do bounding box
                if not (s <= clat <= n and w <= clon <= e):
                    continue
                endereco = (attr.get("StAddr") or attr.get("Place_addr") or "").strip()
                bairro = (attr.get("Nbrhd") or "").strip()
                cidade = (attr.get("City") or "").strip()
                # Se o PlaceName for genérico ("Supermercado"), enriquece com o bairro/rua
                if nome.lower() in ("supermercado", "mercado", "mercearia") and (bairro or endereco):
                    complemento = bairro or endereco.split(",")[0]
                    nome = f"{nome.title()} {complemento}"

                chave_pos = f"{round(clat, 4)}:{round(clon, 4)}"
                if chave_pos in vistos:
                    continue
                vistos.add(chave_pos)

                uid = int(hashlib.md5(f"{nome}:{chave_pos}".encode()).hexdigest()[:12], 16)
                elementos.append({
                    "type": "node",
                    "id": uid,
                    "lat": float(clat),
                    "lon": float(clon),
                    "tags": {
                        "name": nome,
                        "shop": "wholesale" if re.search(r"atacad|assa[ií]|sam'?s|maxxi|rold[aã]o|tenda|mart minas|kompr[aã]o|stok", nome, re.I) else "supermarket",
                        "addr:street": endereco.split(",")[0] if endereco else "",
                        "addr:suburb": bairro,
                        "addr:city": cidade,
                    },
                })
    return elementos


def importar_mercados(session: Session, elementos: list[dict] | None = None) -> int:
    """Insere/atualiza as lojas. Retorna quantas lojas foram gravadas."""
    if elementos is None:
        elementos = _buscar_arcgis_regiao(-5.8275, -35.2050, 15.0)

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
        cidade = (
            tags.get("addr:city")
            or tags.get("addr:municipality")
            or tags.get("addr:town")
            or ""
        )
        dados = dict(
            nome=nome.strip(),
            rede=identificar_rede(tags.get("brand"), nome),
            tipo=tags.get("shop", "supermarket"),
            endereco=rua,
            bairro=tags.get("addr:suburb", ""),
            cidade=cidade,
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


def importar_mercados_regiao(session: Session, lat: float, lon: float, raio_km: float = 15.0) -> int:
    """Consulta instantaneamente os supermercados em torno de (lat, lon) em qualquer lugar do Brasil."""
    elementos = _buscar_arcgis_regiao(lat, lon, raio_km)
    return importar_mercados(session, elementos)


def buscar_localidade_brasil(termo: str) -> list[dict]:
    """Busca cidade, bairro, endereço ou CEP em todo o Brasil via ViaCEP + ArcGIS Geocoder (~0.3s)."""
    q = termo.strip()
    if not q:
        return []

    with httpx.Client(timeout=6.0, headers={"User-Agent": USER_AGENT}, follow_redirects=True) as client:
        apenas_digitos = re.sub(r"\D", "", q)
        if len(apenas_digitos) == 8:
            try:
                rv = client.get(f"https://viacep.com.br/ws/{apenas_digitos}/json/")
                if rv.status_code == 200:
                    dados_cep = rv.json()
                    if not dados_cep.get("erro"):
                        partes = [
                            dados_cep.get("logradouro"),
                            dados_cep.get("bairro"),
                            dados_cep.get("localidade"),
                            dados_cep.get("uf"),
                            "Brasil",
                        ]
                        q = ", ".join(p for p in partes if p)
            except httpx.HTTPError:
                pass

        r = client.get(
            ARCGIS_URL,
            params={
                "SingleLine": q,
                "countryCode": "BRA",
                "maxLocations": 6,
                "outFields": "Match_addr,City,Region",
                "f": "json",
            },
        )
        r.raise_for_status()
        resultados = []
        vistos = set()
        for item in r.json().get("candidates", []):
            loc = item.get("location") or {}
            attr = item.get("attributes") or {}
            nome = attr.get("Match_addr") or item.get("address") or q
            chave = f"{round(loc.get('y', 0), 3)}:{round(loc.get('x', 0), 3)}"
            if chave in vistos:
                continue
            vistos.add(chave)
            resultados.append({
                "nome": nome,
                "cidade": attr.get("City") or "",
                "estado": attr.get("Region") or "",
                "lat": float(loc["y"]),
                "lon": float(loc["x"]),
            })
        return resultados
