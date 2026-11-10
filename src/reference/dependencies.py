"""Versioned reference recipes, independent of traffic processing policy."""

from importlib.metadata import version
import pyproj
from .classification import MAPPER_VERSION

EXTRACT_CONTRACT = "reference_extract_v2"
GRAPH_CONTRACT = "reference_graph_v2"


def libraries():
    return {name: version(name) for name in ("osmium", "shapely", "pyproj")} | {
        "proj": pyproj.proj_version_str
    }


def extract_dependencies(config, source_hash):
    study = config.study
    return {
        "contract": EXTRACT_CONTRACT,
        "source": source_hash,
        "bbox": list(study.bbox),
        "buffer_m": study.graph_buffer_m,
        "metric_crs": study.metric_crs,
        "extractor": "complete_ways_restrictions_v1",
        "libraries": libraries(),
    }


def graph_dependencies(config, extract_hash):
    study = config.study
    return {
        "contract": GRAPH_CONTRACT,
        "extract_sha256": extract_hash,
        "crs": study.metric_crs,
        "bbox": list(study.bbox),
        "buffer_m": study.graph_buffer_m,
        "graph": "directed_junction_v1",
        "importer": "normalized_osm_v1",
        "classification": "osm_direction_access_v1",
        "mapper": MAPPER_VERSION,
        "libraries": libraries(),
    }
