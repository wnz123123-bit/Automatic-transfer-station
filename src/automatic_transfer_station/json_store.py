from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def now_utc() -> str:
    """UTC timestamp as a Zulu-suffixed ISO-8601 string (millisecond resolution).

    Milliseconds (not seconds) so rapid safety events — e.g. a watchdog trip
    immediately followed by a calibration overwrite — stay orderable from the
    persisted timestamp alone during incident reconstruction.
    """
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _tmp_path(path: Path) -> Path:
    return path.with_name(f"{path.name}.tmp")


def cleanup_tmp(path: Path) -> None:
    """Remove any crash-residue ``<store>.tmp`` sibling of ``path``.

    A power loss / crash between the temp write and ``os.replace`` can leave a
    stale ``.tmp`` file next to the store. It is never authoritative, so drop it
    on load/init. Missing file is fine; other OS errors are swallowed so a
    read-only or transient FS issue cannot break startup.
    """
    tmp_path = _tmp_path(Path(path))
    try:
        tmp_path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


def atomic_write_json(path: str | Path, data: Any) -> None:
    """Durably write ``data`` as JSON to ``path`` via a temp file + atomic rename.

    The sequence is write -> flush -> ``os.fsync`` -> close -> ``os.replace`` so
    the bytes are on disk before the rename makes them visible; this prevents a
    half-written or empty store after a crash/power loss. ``os.replace`` is
    atomic on the same filesystem.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = _tmp_path(path)
    text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    with open(tmp_path, "w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp_path, path)
