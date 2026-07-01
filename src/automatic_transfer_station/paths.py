from __future__ import annotations

import sys
from pathlib import Path


PACKAGE_DIR = Path(__file__).resolve().parent

# BP7: frozen-build awareness (PyInstaller / cx_Freeze).
#
# When NOT frozen (normal source-tree / editable-install run) the layout is:
#   src/automatic_transfer_station/paths.py   <- PACKAGE_DIR
#   src/automatic_transfer_station/           <- PACKAGE_DIR (parents[0])
#   src/                                      <- parents[1]  (SRC_DIR — NOT root)
#   <project root>/                           <- parents[2] == PACKAGE_DIR.parents[1]
#
# Wait — the original code uses PACKAGE_DIR.parents[1] which is `src/`, not the
# project root.  Re-reading the original: parents[0] = automatic_transfer_station,
# parents[1] = src.  But CONFIG_DIR = PROJECT_ROOT / "config" resolves to
# "src/config" — that can't be right.  Let's check the actual number of parents:
#   paths.py is at: .../src/automatic_transfer_station/paths.py
#   parents[0] = .../src/automatic_transfer_station   (PACKAGE_DIR itself)
#   parents[1] = .../src
# Hmm — but the project README says config/ is at the project root, not under src/.
# The ORIGINAL code says PACKAGE_DIR.parents[1] which is the PARENT of parents[0].
# Path.parents is zero-indexed and parents[0] is the direct parent of __file__,
# which is PACKAGE_DIR itself (the directory).  So:
#   __file__           = .../src/automatic_transfer_station/paths.py
#   PACKAGE_DIR        = Path(__file__).resolve().parent = .../src/automatic_transfer_station
#   PACKAGE_DIR.parents[0] = .../src
#   PACKAGE_DIR.parents[1] = project root  (E:\Automatic transfer station)
#
# The original PROJECT_ROOT = PACKAGE_DIR.parents[1] is therefore correct.
# (We preserve this EXACTLY for the non-frozen path.)
#
# For a frozen build (sys.frozen is True, set by PyInstaller / cx_Freeze):
#   - sys.executable is the .exe file (e.g. dist/app.exe)
#   - config/, runtime/, vendor/ are bundled next to the .exe
#   - __file__ / PACKAGE_DIR may point into sys._MEIPASS (temp extraction dir)
# In that case we resolve PROJECT_ROOT from the executable's directory so that
# the mutable directories (config, runtime, runtime logs) stay next to the .exe
# rather than inside the temporary _MEIPASS tree.
if getattr(sys, "frozen", False):
    # Frozen build: resources live next to the executable.
    PROJECT_ROOT = Path(sys.executable).resolve().parent
else:
    # Normal source-tree / editable-install run: keep the EXACT original path.
    PROJECT_ROOT = PACKAGE_DIR.parents[1]

CONFIG_DIR = PROJECT_ROOT / "config"
DOCS_DIR = PROJECT_ROOT / "docs"
RUNTIME_DIR = PROJECT_ROOT / "runtime"
CAMERA_SNAPSHOT_DIR = RUNTIME_DIR / "camera_snapshots"
UI_SETTINGS_PATH = RUNTIME_DIR / "ui_settings.json"
VENDOR_DIR = PROJECT_ROOT / "vendor"
MOTION_CARD_SDK_DIR = VENDOR_DIR / "motion_card_sdk"

GAS_DLL_PATH = MOTION_CARD_SDK_DIR / "GAS.dll"
STAGE_OVERVIEW_IMAGE = DOCS_DIR / "ui_preview" / "assets" / "stage_overview.png"
