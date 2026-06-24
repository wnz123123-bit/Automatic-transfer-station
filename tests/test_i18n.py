import unittest


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


if __name__ == "__main__":
    unittest.main()
