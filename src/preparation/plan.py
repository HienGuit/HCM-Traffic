"""Resource identity deduplicates exact endpoint/version/parameters only."""

from ..domain.capture import CapturePlan, ResourceRequest
from ..geo.crs import project
from ..geo.linear import path_geometry
from ..geo.tiles import cover
from ..storage.files import digest
from ..domain.reference import RegistryPath
from ..reference.graph import Graph
from ..config import ResolvedConfig


def build_capture_plan(
    paths: list[RegistryPath], graph: Graph, config: ResolvedConfig
) -> CapturePlan:
    """Exact tile/parameter union; weather point is the metric along-path midpoint."""
    resources = {}
    weather_points = {}
    for path in paths:
        metric = path_geometry(graph, path.edges)
        corridor = project(metric.buffer(config.policy.tile_buffer_m), "EPSG:4326", graph.crs)
        for z, x, y in cover(corridor, config.policy.tile_zoom):
            url = config.sources.traffic_url.format(z=z, x=x, y=y)
            params = {"attributes": config.sources.attributes}
            key = digest([url, config.sources.api_version, params])
            if key not in resources:
                resources[key] = ResourceRequest(
                    resource_id=key,
                    provider="traffic",
                    url=url,
                    params=params,
                    segments=[],
                    tile=(z, x, y),
                )
            if path.segment_id not in resources[key].segments:
                resources[key].segments.append(path.segment_id)
            resources[key].segment_directions.append((path.segment_id, path.direction_id))
        middle = project(metric.interpolate(metric.length / 2), "EPSG:4326", graph.crs)
        point = weather_points.setdefault(path.segment_id, (round(middle.x, 7), round(middle.y, 7)))
        params = {
            "latitude": point[1],
            "longitude": point[0],
            "current": config.sources.weather_current,
            "precipitation_unit": "mm",
            "timezone": "UTC",
        }
        key = digest([config.sources.weather_url, params])
        if key not in resources:
            resources[key] = ResourceRequest(
                resource_id=key,
                provider="weather",
                url=config.sources.weather_url,
                params=params,
                segments=[],
                point=point,
            )
        if path.segment_id not in resources[key].segments:
            resources[key].segments.append(path.segment_id)
        resources[key].segment_directions.append((path.segment_id, path.direction_id))
    return CapturePlan(
        registry_version=paths[0].registry_version if paths else "draft_v1",
        registry_sha256=digest([p.model_dump(mode="json") for p in paths]),
        graph_version=graph.version,
        config_sha256=config.hash,
        resources=sorted(resources.values(), key=lambda r: r.resource_id),
        expected_attempts=len(resources) * config.budget.max_attempts,
        collection_kind="scheduled"
        if config.study.operational_selection_file is not None or not config.study.provisional
        else "pilot",
    )
