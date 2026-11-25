"""logfile.py — append a una línea de log en disco con rotación simple (archivo.1)."""

from __future__ import annotations

from pathlib import Path


def append_line(path: Path, line: str, max_bytes: int = 5_000_000) -> None:
    """Añade la línea; si el archivo pasa de max_bytes, lo mueve a <nombre>.1 (se guarda uno)."""
    try:
        if path.exists() and path.stat().st_size > max_bytes:
            path.replace(path.with_name(path.name + ".1"))
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass
