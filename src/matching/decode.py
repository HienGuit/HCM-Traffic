"""Bounded profile decoding; cached locations contain no speed measurements."""

from importlib.metadata import version
from pathlib import Path
import openlr
from openlr_dereferencer import decode, Config
from openlr_dereferencer.decoding import LRDecodeError
from ..domain.processing import DecodedLocation
from ..reference.classification import MAPPER_VERSION
from ..storage.files import digest, read_json, write_json
from .map_reader import OSMMapReader
from ..reference.graph import Graph

PROFILES = {
    "baseline": Config(),
    "stricter": Config(search_radius=35, min_score=0.45, max_bear_deviation=30),
}


def decode_location(
    code: str,
    graph: Graph,
    profile: str,
    cache_root: Path | None = None,
    reader: OSMMapReader | None = None,
) -> tuple[DecodedLocation, bool]:
    """Return ordered directed edges and resolved metric offsets, applied exactly once."""
    settings = PROFILES[profile]
    deps = {
        "graph": graph.version,
        "mapper": MAPPER_VERSION,
        "decoder": version("openlr-dereferencer"),
        "openlr": version("openlr"),
        "profile": profile,
        "settings": repr(settings),
        "adapter": "v1",
    }
    key = digest([code, deps])
    path = cache_root / (key + ".json") if cache_root else None
    if path and path.is_file():
        try:
            entry = read_json(path)
            if (
                entry.get("hash") == digest(entry.get("result"))
                and entry.get("dependencies") == deps
            ):
                return DecodedLocation.model_validate(entry["result"]), True
        except (OSError, ValueError, KeyError, TypeError):
            entry = None
        import uuid

        path.rename(path.with_suffix(".invalid." + uuid.uuid4().hex[:8] + ".json"))
    result = DecodedLocation(
        code=code, graph_version=graph.version, profile=profile, status="failed", dependencies=deps
    )
    try:
        reference = openlr.binary_decode(code)
        if not isinstance(reference, openlr.LineLocationReference):
            result.reasons = ["REFERENCE_TYPE_UNSUPPORTED"]
        else:
            location = decode(reference, reader or OSMMapReader(graph), config=settings)
            edges = [str(e.line_id) for e in location.lines]
            length = sum(graph.edges[e].length for e in edges)
            start, end = location.p_off, length - location.n_off
            if not 0 <= start < end <= length + 1e-6:
                raise ValueError("Offsets remove all support")
            result = result.model_copy(
                update={
                    "status": "decoded",
                    "edges": edges,
                    "source_positive_offset": reference.poffs,
                    "source_negative_offset": reference.noffs,
                    "start_m": start,
                    "end_m": end,
                }
            )
    except (ValueError, TypeError, IndexError, LRDecodeError) as exc:
        result.reasons = ["DECODE_FAILED"]
        result.dependencies = {
            **deps,
            "failure_type": type(exc).__name__,
            "failure_detail": str(exc)[:500],
        }
    if path:
        value = result.model_dump(mode="json")
        write_json(path, {"dependencies": deps, "hash": digest(value), "result": value})
    return result, False
