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


if __name__ == "__main__":
    unittest.main()
