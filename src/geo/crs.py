from functools import lru_cache
from pyproj import Transformer
from shapely import transform


@lru_cache(maxsize=16)
def transformer(source: str, target: str):
    return Transformer.from_crs(source, target, always_xy=True)


def project(geometry, target: str, source: str = "EPSG:4326"):
    """Transform geometry with longitude/easting first; units follow target CRS."""
    return transform(geometry, transformer(source, target).transform, interleaved=False)
