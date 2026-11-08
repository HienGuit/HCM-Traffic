import math
from shapely.geometry import box


def tile_xy(lon: float, lat: float, zoom: int) -> tuple[int, int]:
    n = 2**zoom
    lat = max(-85.05112878, min(85.05112878, lat))
    return (
        max(0, min(n - 1, int((lon + 180) / 360 * n))),
        max(0, min(n - 1, int((1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n))),
    )


def tile_lonlat(x: float, y: float, zoom: int) -> tuple[float, float]:
    n = 2**zoom
    return x / n * 360 - 180, math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))


def cover(geometry, zoom: int) -> list[tuple[int, int, int]]:
    """Cover EPSG:4326 corridor with intersecting slippy tiles, including boundaries."""
    w, s, e, n = geometry.bounds
    x0, y0 = tile_xy(w, n, zoom)
    x1, y1 = tile_xy(e, s, zoom)
    out = []
    for x in range(x0, x1 + 1):
        for y in range(y0, y1 + 1):
            left, top = tile_lonlat(x, y, zoom)
            right, bottom = tile_lonlat(x + 1, y + 1, zoom)
            if geometry.intersects(box(left, bottom, right, top)):
                out.append((zoom, x, y))
    return out
