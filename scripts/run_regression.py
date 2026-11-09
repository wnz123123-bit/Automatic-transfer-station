"""Run every pytest module with an isolated Qt application/process on Windows.

One native QApplication hosts many legacy widget tests within each module. A
fresh process between modules avoids carrying Qt platform/font/deferred-delete
state across otherwise unrelated modules. No tests are skipped or auto-retried;
any failure, native crash or timeout fails this command and CI.
"""
from pathlib import Path
import os
import re
import subprocess
import sys


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    modules = sorted((root / "tests").rglob("test_*.py"))
    if not modules:
        print("No test modules found", file=sys.stderr)
        return 1
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    env.setdefault("QT_QPA_PLATFORM", "offscreen")
    failures = []
    passed = subtests = 0
    for module in modules:
        relative = module.relative_to(root)
        try:
            result = subprocess.run(
                [sys.executable, "-m", "pytest", "-q", str(relative), "--tb=short"],
                cwd=root, env=env, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=180,
            )
        except subprocess.TimeoutExpired:
            failures.append(str(relative))
            print(f"FAIL {relative}: exceeded 180 seconds", flush=True)
            continue
        if result.returncode:
            failures.append(str(relative))
            print(f"FAIL {relative} (exit {result.returncode})", flush=True)
            print(result.stdout + result.stderr, flush=True)
        else:
            summary = result.stdout.rstrip().splitlines()[-1]
            count = re.search(r"\b(\d+) passed\b", summary)
            sub_count = re.search(r"\b(\d+) subtests passed\b", summary)
            passed += int(count.group(1)) if count else 0
            subtests += int(sub_count.group(1)) if sub_count else 0
            print(f"PASS {relative}: {summary}", flush=True)
    print(f"Regression: {len(modules) - len(failures)}/{len(modules)} modules passed; "
          f"{passed} tests, {subtests} subtests passed", flush=True)
    return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(main())
