"""Resolved-path boundaries, including existing Windows junction parents."""

from pathlib import Path


def within(path: Path, root: Path) -> bool:
    """Test resolved containment, never string prefixes."""
    return path.resolve().is_relative_to(root.resolve())


def distinct_roots(roots: list[Path]) -> None:
    """Reject equal or nested code/data/QA/project roots before writes."""
    for i, root in enumerate(roots):
        for other in roots[i + 1 :]:
            if within(root, other) or within(other, root):
                raise ValueError(f"Roots must be disjoint: {root}, {other}")


def safe_output(path: Path, allowed: Path, protected: tuple[Path, ...] = ()) -> Path:
    """Resolve prospective write and reject containment/alias with protected inputs."""
    target = path.resolve()
    if not within(target, allowed) or target == allowed.resolve():
        raise ValueError(f"Output outside permitted artifact subtree: {target}")
    for source in protected:
        source = source.resolve()
        if within(target, source) or within(source, target):
            raise ValueError(f"Output intersects protected source: {source}")
        if target.exists() and source.exists() and target.samefile(source):
            raise ValueError("Output aliases protected source")
    return target
