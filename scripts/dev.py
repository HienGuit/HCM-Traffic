"""Single standard-library launcher; development artifacts stay outside checkout."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]


def environment(data):
    data = data.resolve()
    if data.is_relative_to(ROOT) or ROOT.is_relative_to(data):
        raise ValueError("External data root must be disjoint from repo")
    env = os.environ.copy()
    for name, rel in {
        "RUFF_CACHE_DIR": "dev/cache/ruff",
        "TMP": "dev/tmp",
        "TEMP": "dev/tmp",
        "TMPDIR": "dev/tmp",
    }.items():
        p = data / rel
        p.mkdir(parents=True, exist_ok=True)
        env[name] = str(p)
    env.update(PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")
    env.pop("PYTHONPATH", None)
    return env


def inventory():
    return {
        str(p.relative_to(ROOT)): {
            "bytes": p.stat().st_size,
            "sha256": hashlib.sha256(p.read_bytes()).hexdigest() if p.suffix != ".pbf" else None,
        }
        for p in ROOT.rglob("*")
        if p.is_file() and ".git" not in p.parts and not p.is_relative_to(ROOT / "data")
        and not p.is_relative_to(ROOT/'logs') and not p.is_relative_to(ROOT/'config/local')
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=["bootstrap", "run", "check", "verify", "package", "clean"]
    )
    parser.add_argument("--data-root", required=True, type=Path)
    parser.add_argument("--qa-root", type=Path)
    parser.add_argument("--scope", default="offline")
    parser.add_argument("--heavy", action="store_true")
    parser.add_argument("--run-id")
    parser.add_argument("--apply", action="store_true")
    args, rest = parser.parse_known_args()
    if args.run_id and (Path(args.run_id).name != args.run_id or args.run_id in (".", "..")):
        raise ValueError("Run ID must be a single name")
    if rest[:1] == ["--"]:
        rest = rest[1:]
    data = args.data_root.resolve()
    env = environment(data)
    python = data / "dev/env/Scripts/python.exe" if os.name == "nt" else data / "dev/env/bin/python"
    run_id = args.run_id or time.strftime("%Y%m%dT%H%M%S") + "_" + uuid.uuid4().hex[:8]
    run = data / "dev/runs" / run_id
    run.mkdir(parents=True, exist_ok=False)
    before = inventory()
    started = time.monotonic()
    (run / "before.json").write_text(json.dumps(before), encoding="utf-8")
    command = []
    cwd = run
    if args.command == "bootstrap":
        if python.exists():
            raise RuntimeError("Environment already exists; not replacing a collector environment")
        subprocess.run(
            [sys.executable, "-m", "venv", str(python.parent.parent)], env=env, check=True
        )
        subprocess.run(
            [str(python), "-m", "pip", "install", "-r", str(ROOT / "requirements.txt")],
            env=env,
            check=True,
        )
        command = [
            str(python),
            "-m",
            "pip",
            "install",
            "--no-deps",
            "--no-build-isolation",
            str(ROOT),
        ]
    elif args.command == "run":
        command = [str(python), "-B", "-m", "src", *rest]
    elif args.command == "verify":
        if not args.qa_root:
            raise ValueError("verify requires --qa-root")
        qa = args.qa_root.resolve()
        if qa.is_relative_to(ROOT) or not (qa / "runner.py").is_file():
            raise ValueError("QA bundle absent/invalid: NOT_RUN")
        command = [
            str(python),
            "-B",
            str(qa / "runner.py"),
            "--repo-root",
            str(ROOT),
            "--data-root",
            str(data),
            "--scope",
            args.scope,
            *rest,
        ]
    elif args.command == "check":
        commands = [
            [str(python), "-B", "-m", "ruff", "check", str(ROOT), "--no-cache"],
            [str(python), "-B", "-m", "ruff", "format", "--check", str(ROOT), "--no-cache"],
            [str(python), "-B", "-m", "src", "--help"],
        ]
        for command in commands:
            subprocess.run(command, cwd=cwd, env=env, check=True)
        code = (
            "from pathlib import Path; import yaml; from src.config import Policy,Sources,Budget; "
            "root=Path(" + repr(str(ROOT)) + "); "
            "[cls.model_validate(yaml.safe_load((root/'config'/name).read_text(encoding='utf-8'))) "
            "for cls,name in [(Policy,'policy.yaml'),(Sources,'source_contracts.yaml'),(Budget,'budget.example.yaml')]]; "
            "print('Config schemas valid; study example requires external local roots')"
        )
        subprocess.run([str(python), "-B", "-c", code], cwd=cwd, env=env, check=True)
        command = []
    elif args.command == "package":
        stage = data / "dev/packages" / run_id / "source"
        stage.mkdir(parents=True)
        allowed = [
            "src",
            "main.py",
            "config",
            "scripts",
            "pyproject.toml",
            "requirements.txt",
            "README.md",
            "NOTICE",
            ".python-version",
            ".gitignore",
        ]
        for name in allowed:
            source = ROOT / name
            if source.is_dir():
                shutil.copytree(
                    source, stage / name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "local", "candidates")
                )
            elif source.is_file():
                shutil.copy2(source, stage / name)
        out = stage.parent / "artifacts"
        command = [
            str(python),
            "-m",
            "pip",
            "wheel",
            "--no-deps",
            "--no-build-isolation",
            "--wheel-dir",
            str(out),
            str(stage),
        ]
        (run / "package.json").write_text(
            json.dumps({"staging": str(stage), "artifacts": str(out)})
        )
    elif args.command == "clean":
        if not args.run_id or not args.qa_root:
            raise ValueError("clean requires run ID and QA root ownership manifest")
        qa = args.qa_root.resolve()
        scratch = qa / "scratch" / args.run_id
        owner = qa / "runs" / args.run_id / "ownership.json"
        if not owner.is_file():
            raise ValueError("Missing ownership: report only, no deletion")
        owned = json.loads(owner.read_text())
        if not (qa / "runs" / args.run_id / "report.json").is_file():
            raise ValueError("Cannot clean a run without a finalized report")
        if Path(
            owned["scratch"]
        ).resolve() != scratch.resolve() or not scratch.resolve().is_relative_to(qa):
            raise ValueError("Invalid ownership target")
        if args.apply and scratch.exists():
            shutil.rmtree(scratch)
        print(json.dumps({"target": str(scratch), "applied": args.apply}))
    peak = 0
    interrupted = False
    result = 0
    if command:
        with (
            (run / "stdout.log").open("w", encoding="utf-8") as out,
            (run / "stderr.log").open("w", encoding="utf-8") as err,
        ):
            process = subprocess.Popen(
                command, cwd=cwd, env=env, stdout=out, stderr=err, shell=False
            )
            if args.heavy:
                import psutil

                root = psutil.Process(process.pid)
                while process.poll() is None:
                    try:
                        family = [root] + root.children(recursive=True)
                        peak = max(peak, sum(p.memory_info().rss for p in family if p.is_running()))
                        if time.monotonic() - started > 1200 or peak > 12 * 1024**3:
                            for p in reversed(family):
                                try:
                                    p.kill()
                                except psutil.NoSuchProcess:
                                    continue
                            interrupted = True
                            break
                    except psutil.NoSuchProcess:
                        break
                    time.sleep(0.25)
            result = process.wait()
        print((run / "stdout.log").read_text(encoding="utf-8"))
        print((run / "stderr.log").read_text(encoding="utf-8"), file=sys.stderr)
    after = inventory()
    delta = sorted(k for k in before.keys() | after.keys() if before.get(k) != after.get(k))
    candidate_artifact_delta = []
    if args.command == "run" and "candidates" in rest and "survey" in rest:
        candidate_artifact_delta = [
            k for k in delta if Path(k).is_relative_to(Path("data/reference/candidates"))
        ]
        delta = [k for k in delta if k not in candidate_artifact_delta]
    report = {
        "candidate_artifact_delta": candidate_artifact_delta,
        "run_id": run_id,
        "returncode": result,
        "elapsed_s": time.monotonic() - started,
        "peak_rss_bytes": peak,
        "peak_rss_measured": args.heavy,
        "interrupted": interrupted,
        "source_delta": delta,
        "clean_workspace": not delta,
    }
    (run / "after.json").write_text(json.dumps(after), encoding="utf-8")
    (run / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({**report, "evidence": str(run)}))
    return result or (2 if delta else 0)


if __name__ == "__main__":
    raise SystemExit(main())
