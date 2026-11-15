from math import asin, cos, radians, sin, sqrt

RAIO_TERRA_KM = 6371.0

# Fator médio para converter distância em linha reta em distância de rua.
# Ajustável depois com uma API de rotas (OSRM / OpenRouteService).
FATOR_RUA = 1.3


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    lat1, lon1, lat2, lon2 = map(radians, (lat1, lon1, lat2, lon2))
    dlat, dlon = lat2 - lat1, lon2 - lon1
    a = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    return 2 * RAIO_TERRA_KM * asin(sqrt(a))


def distancia_rua_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    return haversine_km(lat1, lon1, lat2, lon2) * FATOR_RUA
