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
    selected = config.study.selected_paths_file
    candidates = {'status':'not_configured', 'ready':False, 'reasons':['CANDIDATES_NOT_CONFIGURED']}
    if selected is not None:
        if not selected.is_file():
            candidates = {'status':'missing', 'ready':False, 'reasons':['CANDIDATE_PATHS_MISSING']}
        else:
            versions = sorted({p['graph_version'] for p in read_json(selected)})
            matches = versions == [metadata['version']]
            candidates = {'status':'compatible' if matches else 'requires_rebind',
                          'ready':matches, 'path_graph_versions':versions,
                          'reasons':[] if matches else ['PATH_GRAPH_VERSION_MISMATCH']}
            if matches:
                from ..domain.reference import RegistryPath
                from .compatibility import candidate_bundle, descriptor_edges, small_graph, verify_paths
                try:
                    paths = [RegistryPath.model_validate(p) for p in read_json(selected)]
                    if config.study.operational_selection_file is not None:
                        if config.study.operational_selection_file.resolve() != (selected.parent/'manifest.json').resolve():
                            raise ValueError('CANDIDATE_MANIFEST_LOCATION_MISMATCH')
                        candidate_bundle(selected.parent)
                    edges = descriptor_edges(graph, {e.edge_id for p in paths for e in p.edges})
                    verify_paths(paths, small_graph(metadata, edges))
                except (OSError, ValueError, KeyError, TypeError) as exc:
                    candidates = {'status':'invalid','ready':False,'reasons':[str(exc)]}
    return {
        "status": "complete",
        "reference_status": "complete",
        "candidate_compatibility": candidates,
        "setup_ready": candidates['ready'] and config.study.graph_cache is not None and config.study.graph_cache.resolve() == graph.resolve(),
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
