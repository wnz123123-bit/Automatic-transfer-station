import json
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
PYTHON_DIR = SRC_DIR / "automatic_transfer_station"
sys.path.insert(0, str(SRC_DIR))


class JsonStoreHelperTests(unittest.TestCase):
    def test_now_utc_is_zulu_iso8601(self):
        from automatic_transfer_station.json_store import now_utc

        value = now_utc()
        self.assertTrue(value.endswith("Z"))
        self.assertNotIn("+00:00", value)

    def test_atomic_write_round_trips_and_leaves_no_tmp(self):
        from automatic_transfer_station.json_store import atomic_write_json

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "data.json"
            payload = {"hello": "world", "n": 1}
            atomic_write_json(path, payload)

            self.assertEqual(payload, json.loads(path.read_text(encoding="utf-8")))
            # No residual temp file left behind by the atomic write.
            self.assertEqual([], list(path.parent.glob("*.tmp")))

    def test_atomic_write_creates_parent_directory(self):
        from automatic_transfer_station.json_store import atomic_write_json

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "nested" / "deeper" / "data.json"
            atomic_write_json(path, {"a": 1})
            self.assertTrue(path.exists())

    def test_cleanup_tmp_removes_sibling_tmp_files(self):
        from automatic_transfer_station.json_store import cleanup_tmp

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "data.json"
            stale = path.with_name(f"{path.name}.tmp")
            stale.write_text("garbage", encoding="utf-8")
            self.assertTrue(stale.exists())

            cleanup_tmp(path)

            self.assertFalse(stale.exists())

    def test_cleanup_tmp_is_safe_when_no_tmp_present(self):
        from automatic_transfer_station.json_store import cleanup_tmp

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "data.json"
            # Should not raise even though nothing exists yet.
            cleanup_tmp(path)


if __name__ == "__main__":
    unittest.main()
