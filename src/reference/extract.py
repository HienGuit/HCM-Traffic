"""Geometry-intersection selection with backward-reference-complete output."""

from pathlib import Path
import osmium
from shapely.geometry import LineString, box, mapping
from ..geo.crs import project
from ..storage.files import digest, write_json, sha256
from ..storage.cache import validate_cache, seal_cache
from .inspect import inspect_reference
from .dependencies import extract_dependencies


def extract_reference(config) -> Path:
    study = config.study
    source = inspect_reference(study.osm_pbf)
    region = project(box(*study.bbox), study.metric_crs)
    buffered = project(region.buffer(study.graph_buffer_m), "EPSG:4326", study.metric_crs)
    deps = extract_dependencies(config, source.source_sha256)
    folder = config.output("reference/" + digest(deps)[:20])
    if validate_cache(folder, deps):
        return folder / "region.osm.pbf"
    if folder.exists():
        import uuid

        folder.rename(folder.with_name(folder.name + ".failed." + uuid.uuid4().hex[:8]))
    folder.mkdir(parents=True)
    selected = set()
    index_path = folder / "locations.idx"
    bounds = buffered.bounds

    class Select(osmium.SimpleHandler):
        def way(self, way):
            if "highway" not in way.tags or len(way.nodes) < 2:
                return
            coords = [(n.lon, n.lat) for n in way.nodes if n.location.valid()]
            if len(coords) != len(way.nodes):
                return
            xs, ys = zip(*coords)
            if (
                max(xs) < bounds[0]
                or min(xs) > bounds[2]
                or max(ys) < bounds[1]
                or min(ys) > bounds[3]
            ):
                return
            if LineString(coords).intersects(buffered):
                selected.add(way.id)

    try:
        Select().apply_file(
            str(study.osm_pbf), locations=True, idx=f"sparse_file_array,{index_path}"
        )
    finally:
        import gc

        gc.collect()
        try:
            index_path.unlink(missing_ok=True)
        except PermissionError:
            # Some Windows libosmium builds keep the mmap until process exit.
            write_json(
                folder / "cleanup_pending.json",
                {
                    "owned_temporary_paths": [str(index_path)],
                    "reason": "WINDOWS_MMAP_UNTIL_PROCESS_EXIT",
                },
            )
    retained_relations = []
    destination = folder / "region.osm.pbf"
    with osmium.BackReferenceWriter(
        str(destination), str(study.osm_pbf), remove_tags=False, relation_depth=1
    ) as writer:

        class Export(osmium.SimpleHandler):
            def way(self, way):
                if way.id in selected:
                    writer.add_way(way)

            def relation(self, relation):
                if relation.tags.get("type") == "restriction" and any(
                    m.type == "w" and m.ref in selected for m in relation.members
                ):
                    writer.add_relation(relation)
                    retained_relations.append(relation.id)

        Export().apply_file(str(study.osm_pbf))
    write_json(
        folder / "reference.json",
        {
            **source.model_dump(mode="json"),
            "selected_ways": len(selected),
            "retained_restrictions": retained_relations,
            "study_geometry": mapping(box(*study.bbox)),
            "buffer_geometry": mapping(buffered),
            "extract_sha256": sha256(destination),
            "completeness": {
                "complete_way_references": True,
                "all_relations": False,
                "boundary_degree": "unknown_at_extract_boundary",
            },
        },
    )
    seal_cache(folder, deps, ["region.osm.pbf", "reference.json"])
    return destination
