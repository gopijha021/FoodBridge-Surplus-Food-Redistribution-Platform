import math

from sqlalchemy import and_, cast, func
from sqlalchemy.engine import Dialect
from sqlalchemy.types import UserDefinedType

from app.core.config import settings

EARTH_KM = 6371.0088


class Geography(UserDefinedType):
    cache_ok = True

    def get_col_spec(self, **kw):
        return "geography"


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi, dlmb = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * EARTH_KM * math.asin(min(1.0, math.sqrt(a)))


def radius_clause(lat_col, lon_col, lat: float, lon: float, km: float, dialect_name: str):
    """SQL prefilter. PostGIS ST_DWithin (GiST-indexed expression) when available,
    otherwise a bounding box. Exact distance is always re-checked with haversine in Python."""
    if settings.use_postgis and dialect_name == "postgresql":
        here = cast(func.ST_SetSRID(func.ST_MakePoint(lon, lat), 4326), Geography())
        there = cast(func.ST_SetSRID(func.ST_MakePoint(lon_col, lat_col), 4326), Geography())
        return func.ST_DWithin(there, here, km * 1000.0)
    dlat = km / 111.32
    dlon = km / (111.32 * max(math.cos(math.radians(lat)), 0.01))
    return and_(
        lat_col.between(lat - dlat, lat + dlat),
        lon_col.between(lon - dlon, lon + dlon),
    )
