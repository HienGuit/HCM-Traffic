"""Reconcile interrupted state without deleting evidence or resuming live calls."""

from ..storage.files import read_json, write_json


def reconcile(state, config):
    recovered = []
    with state.connect() as db:
        rows = db.execute("SELECT * FROM slots WHERE status='collecting'").fetchall()
        for row in rows:
            found = list(config.output("raw").glob("*/*/" + row["snapshot"] + "/manifest.json"))
            status = read_json(found[0])["status"] if len(found) == 1 else "interrupted"
            db.execute(
                "UPDATE slots SET status=? WHERE study=? AND registry=? AND scheduled=?",
                (status, row["study"], row["registry"], row["scheduled"]),
            )
            recovered.append({"snapshot": row["snapshot"], "status": status})
    # Preserve orphan bodies/events; recording recovery does not rewrite immutable capture manifests.
    for collecting in config.output("raw").glob("*/*/*/collecting.json"):
        if not (collecting.parent / "manifest.json").exists():
            write_json(
                collecting.parent / "recovery.json",
                {
                    "status": "interrupted",
                    "live_resume": False,
                    "events": len(list((collecting.parent / "events").glob("*.json"))),
                },
            )
    return recovered
