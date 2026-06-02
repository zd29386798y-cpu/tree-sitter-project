from __future__ import annotations

from pathlib import Path
from typing import Iterable

from config import EXCLUDE_DIRS, SOURCE_EXTENSIONS


def scan_sources(
    src: str | Path,
    extensions: Iterable[str] = SOURCE_EXTENSIONS,
    exclude_dirs: Iterable[str] = EXCLUDE_DIRS,
) -> list[Path]:
    root = Path(src).resolve()
    allowed = {ext.lower() for ext in extensions}
    excluded = {name.lower() for name in exclude_dirs}

    if root.is_file():
        return [root] if root.suffix.lower() in allowed else []

    results: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if any(part.lower() in excluded for part in path.parts):
            continue
        if path.suffix.lower() in allowed:
            results.append(path)

    return sorted(results, key=lambda p: str(p).lower())
