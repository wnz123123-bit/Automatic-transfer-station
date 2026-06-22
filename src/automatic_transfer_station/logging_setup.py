"""BP3 — centralised logging bootstrap for the Automatic Transfer Station.

Call ``configure_logging()`` once at application start (in ``main()``).
Subsequent calls are idempotent: no duplicate handlers are created.

Logger conventions
------------------
* ``ats.audit``  — append-only safety-event trail (over-temp, watchdog, e-stop …)
* ``ats.rd105``  — RD105 temperature-controller events
* ``ats.motion`` — motion-safety events (auto-stop, open/close/reset, e-stop)
* ``ats.calibration`` — calibration overwrite / load-rejection events
"""
from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

_LOG_FILE_NAME = "transfer_station.log"
# Dedicated append-only safety/audit trail. Kept in its OWN rotating file so a
# chatty DEBUG session on the main log can never rotate away the forensic record
# of an over-temp trip, watchdog forced-disable, e-stop, or calibration overwrite.
_AUDIT_LOG_FILE_NAME = "transfer_station.audit.log"
_LOG_FORMAT = "%(asctime)s.%(msecs)03d %(levelname)-8s %(name)s %(message)s"
_DATE_FORMAT = "%Y-%m-%dT%H:%M:%S"   # ISO-8601 (local time); sub-second ms appended by format

# Guard flag: set to True once the root/app logger has been configured so that
# repeated calls from tests + main() are safe.
_configured = False

# Tracks every handler this module added (tagged with _ats_managed=True).
# On re-configure we remove + close these before adding the new ones, so a
# second configure_logging(runtime_dir=other) repoints cleanly and releases
# the old file handle (no Windows file-handle leak).
_managed_handlers: list[logging.Handler] = []


def configure_logging(runtime_dir: "str | Path | None" = None, console: bool = True) -> None:
    """Set up the application-level logging stack (idempotent).

    Parameters
    ----------
    runtime_dir:
        Directory where ``transfer_station.log`` is written.  Defaults to
        ``paths.RUNTIME_DIR``.  If the directory cannot be created or written,
        file logging is silently skipped and only the console handler (if
        enabled) is kept.
    console:
        When True a ``StreamHandler`` at DEBUG level is added to the root logger
        so messages appear on stderr during development/testing.

    Re-configuration (tests that reset ``_configured``): any handler previously
    added by this module (tagged ``_ats_managed=True``) is removed and closed
    before the new handlers are added, so a re-configure cleanly repoints the
    file handler and never leaks the old file handle.
    """
    global _configured, _managed_handlers
    if _configured:
        return

    if runtime_dir is None:
        try:
            from .paths import RUNTIME_DIR
            runtime_dir = RUNTIME_DIR
        except Exception:
            runtime_dir = None

    formatter = logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT)

    root = logging.getLogger()

    # Remove + close any handlers this module added in a previous call so that
    # re-configuration (e.g. tests that reset _configured) repoints cleanly and
    # does not leak the old RotatingFileHandler's file handle.
    audit = logging.getLogger("ats.audit")
    for old_handler in list(_managed_handlers):
        root.removeHandler(old_handler)
        audit.removeHandler(old_handler)  # the dedicated audit handler lives here, not on root
        try:
            old_handler.close()
        except Exception:  # pragma: no cover - defensive; close must not crash
            pass
    _managed_handlers.clear()

    # Avoid adding a StreamHandler if a non-managed one already exists (e.g.
    # pytest's log capture handler) — check by type excluding managed ones.
    existing_unmanaged_types = {
        type(h) for h in root.handlers
        if not getattr(h, "_ats_managed", False)
    }

    if console and logging.StreamHandler not in existing_unmanaged_types:
        ch = logging.StreamHandler()
        ch.setLevel(logging.DEBUG)
        ch.setFormatter(formatter)
        ch._ats_managed = True  # type: ignore[attr-defined]
        root.addHandler(ch)
        _managed_handlers.append(ch)

    if runtime_dir is not None:
        try:
            log_path = Path(runtime_dir)
            log_path.mkdir(parents=True, exist_ok=True)
            fh = logging.handlers.RotatingFileHandler(
                log_path / _LOG_FILE_NAME,
                maxBytes=5 * 1024 * 1024,   # 5 MB per file
                backupCount=5,
                encoding="utf-8",
            )
            fh.setLevel(logging.DEBUG)
            fh.setFormatter(formatter)
            fh._ats_managed = True  # type: ignore[attr-defined]
            root.addHandler(fh)
            _managed_handlers.append(fh)
        except (OSError, PermissionError, ValueError):
            # Degraded mode: file logging unavailable (bad path, permissions,
            # null bytes, etc.); console still works.
            pass

        # Dedicated audit handler on the ats.audit logger. Safety events (INFO+)
        # land in their OWN rotating file with independent retention so the main
        # DEBUG stream's rotation can never discard them; they still propagate to
        # the main log/console as well (redundant, not exclusive).
        try:
            ah = logging.handlers.RotatingFileHandler(
                Path(runtime_dir) / _AUDIT_LOG_FILE_NAME,
                maxBytes=2 * 1024 * 1024,   # 2 MB per file
                backupCount=10,             # ~20 MB of audit history, independent of debug churn
                encoding="utf-8",
            )
            ah.setLevel(logging.INFO)
            ah.setFormatter(formatter)
            ah._ats_managed = True  # type: ignore[attr-defined]
            audit.addHandler(ah)
            _managed_handlers.append(ah)
        except (OSError, PermissionError, ValueError):
            pass

    # Set root level permissive; individual loggers can tighten as needed.
    if root.level == logging.WARNING or root.level == logging.NOTSET:
        root.setLevel(logging.DEBUG)

    _configured = True


def audit_logger() -> logging.Logger:
    """Return the dedicated audit logger (``ats.audit``)."""
    return logging.getLogger("ats.audit")
