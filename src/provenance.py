"""Code fingerprint excludes source PBF and all external runtime artifacts."""

import hashlib
from pathlib import Path
from .storage.files import digest


def source_fingerprint(root: Path):
    files = {}
    candidates=[p for folder in ('src','scripts') for p in (root/folder).rglob('*.py')]
    candidates += [root/n for n in ('main.py','pyproject.toml','requirements.txt','README.md','NOTICE')]
    candidates += [p for p in (root/'config').glob('*') if p.is_file() and p.suffix in ('.yaml','.json')]
    for path in sorted(candidates):
        if (
            path.is_file()
            and path.suffix != ".pbf"
            and ".git" not in path.parts
            and '__pycache__' not in path.parts
        ):
            files[str(path.relative_to(root)).replace("\\", "/")] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
    return digest(files)
