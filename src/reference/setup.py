"""Offline setup of reproducible reference artifacts from the configured PBF."""

from ..processing.inputs import network_off
from ..storage.files import read_json
from .extract import extract_reference
from .graph import build_reference


def setup_reference(config) -> dict:
    """Extract then build; each stage validates its content-addressed cache.

    Return the graph selected by the actual inputs, never the graph_cache hint.
    Config, candidate paths and capture plans are not rewritten by setup.
    """
    source = config.study.osm_pbf
    if not source.is_file():
        raise FileNotFoundError(f"Configured PBF not found: {source}")
    with network_off():
        extract = extract_reference(config)
        graph = build_reference(extract, config)
    metadata = read_json(graph / "graph.json")
    return {
        "status": "complete",
        "network": "off",
        "pbf": str(source.resolve()),
        "extract": str(extract.resolve()),
        "graph": str(graph.resolve()),
        "graph_version": metadata["version"],
        "nodes": metadata["nodes"],
        "edges": metadata["edges"],
        "graph_cache_matches_config": (
            config.study.graph_cache is not None
            and config.study.graph_cache.resolve() == graph.resolve()
        ),
    }
