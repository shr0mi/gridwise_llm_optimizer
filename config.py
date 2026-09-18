"""Load ``.env`` into the process environment, with no extra dependency.

Imported for its side effect by :mod:`main` and :mod:`llm` before either reads a
setting, so a local ``.env`` works the same way as real environment variables.

Precedence: a variable already present in the environment always wins. Hosting
platforms (Render, Railway, Docker ``-e``) inject secrets that way, so a stale
``.env`` accidentally shipped in an image can never override them.
"""
from __future__ import annotations

import os
from pathlib import Path

_LOADED = False


def load_env(path: "str | os.PathLike[str] | None" = None) -> int:
    """Read ``KEY=value`` lines from ``.env``. Returns how many were applied."""
    global _LOADED
    if _LOADED and path is None:
        return 0

    target = Path(path) if path is not None else Path(__file__).with_name(".env")
    if path is None:
        _LOADED = True
    if not target.is_file():
        return 0

    applied = 0
    try:
        raw = target.read_text(encoding="utf-8")
    except OSError:
        return 0

    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("export "):
            line = line[7:].lstrip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if not key or key in os.environ:
            continue          # a real environment variable always wins
        os.environ[key] = value
        applied += 1
    return applied


load_env()
