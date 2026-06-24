import unittest
import tempfile
import os
from pathlib import Path


class TranslatorTests(unittest.TestCase):
    def setUp(self):
        from automatic_transfer_station.i18n import Translator
        self.Translator = Translator

    def test_zh_returns_key_unchanged(self):
        t = self.Translator(lang="zh")
        t._table = {"温度控制": "Temperature Control"}
        self.assertEqual("温度控制", t.tr("温度控制"))

    def test_en_returns_translation(self):
        t = self.Translator(lang="en")
        t._table = {"温度控制": "Temperature Control"}
        self.assertEqual("Temperature Control", t.tr("温度控制"))

    def test_missing_key_passthrough(self):
        t = self.Translator(lang="en")
        self.assertEqual("未登记", t.tr("未登记"))

    def test_set_language_notifies_observers(self):
        t = self.Translator(lang="en")
        hits = []
        t.add_observer(lambda: hits.append(t.lang))
        t.set_language("zh")
        self.assertEqual(["zh"], hits)

    def test_initialize_does_not_notify(self):
        t = self.Translator(lang="en")
        hits = []
        t.add_observer(lambda: hits.append(1))
        t.initialize("zh")
        self.assertEqual([], hits)
        self.assertEqual("zh", t.lang)

    def test_saver_called_on_set_language_only(self):
        t = self.Translator(lang="en")
        saved = []
        t.set_saver(saved.append)
        t.initialize("zh")
        self.assertEqual([], saved)
        t.set_language("en")
        self.assertEqual(["en"], saved)

    def test_context_disambiguation_en(self):
        """Context key wins over the plain table when context is given."""
        t = self.Translator(
            lang="en",
            table={"连接": "Connection"},
            ctx_table={("objective", "连接"): "Connect"},
        )
        # Without context → plain table
        self.assertEqual("Connection", t.tr("连接"))
        # With context → context override
        self.assertEqual("Connect", t.tr("连接", "objective"))

    def test_context_disambiguation_zh_passthrough(self):
        """ZH mode always returns the source string regardless of context."""
        t = self.Translator(
            lang="zh",
            table={"连接": "Connection"},
            ctx_table={("objective", "连接"): "Connect"},
        )
        self.assertEqual("连接", t.tr("连接"))
        self.assertEqual("连接", t.tr("连接", "objective"))


class LanguagePersistenceTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = Path(self.dir) / "ui_settings.json"

    def tearDown(self):
        for p in Path(self.dir).glob("*"):
            try:
                p.unlink()
            except OSError:
                pass
        os.rmdir(self.dir)

    def test_default_when_missing(self):
        from automatic_transfer_station.i18n import load_language
        self.assertEqual("en", load_language(self.path))

    def test_round_trip(self):
        from automatic_transfer_station.i18n import load_language, save_language
        save_language("zh", self.path)
        self.assertEqual("zh", load_language(self.path))

    def test_corrupt_file_falls_back_to_default(self):
        from automatic_transfer_station.i18n import load_language
        self.path.write_text("{not json", encoding="utf-8")
        self.assertEqual("en", load_language(self.path))

    def test_invalid_value_falls_back(self):
        from automatic_transfer_station.i18n import load_language
        self.path.write_text('{"language": "fr"}', encoding="utf-8")
        self.assertEqual("en", load_language(self.path))


class RetranslatorTests(unittest.TestCase):
    def _trans(self):
        from automatic_transfer_station.i18n import Translator
        return Translator(lang="en", table={"温度控制": "Temperature Control"})

    def test_text_applies_now_and_on_switch(self):
        from automatic_transfer_station.i18n import Retranslator
        t = self._trans()
        r = Retranslator(t)

        class W:
            def __init__(self): self.t = None
            def setText(self, v): self.t = v
        w = W()
        r.text(w, "温度控制")
        self.assertEqual("Temperature Control", w.t)   # applied now (EN)
        t.set_language("zh")
        self.assertEqual("温度控制", w.t)               # live re-applied (ZH)

    def test_items_preserve_index(self):
        from automatic_transfer_station.i18n import Retranslator
        t = self._trans()
        r = Retranslator(t)

        class Combo:
            def __init__(self): self.texts = ["a", "b"]; self.idx = 1
            def setItemText(self, i, v): self.texts[i] = v
            def currentIndex(self): return self.idx
            def count(self): return len(self.texts)
        c = Combo()
        r.items(c, ["温度控制", "温度控制"])
        self.assertEqual(["Temperature Control", "Temperature Control"], c.texts)
        self.assertEqual(1, c.currentIndex())


if __name__ == "__main__":
    unittest.main()
