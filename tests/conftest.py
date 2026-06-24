import pytest


@pytest.fixture(autouse=True)
def _pin_language_zh():
    """Existing assertions expect Chinese; pin the translator to ZH (where tr()
    returns the key unchanged) and reset observers so closures from a prior test's
    destroyed widgets never fire."""
    from automatic_transfer_station.i18n import translator
    translator.clear_observers()
    translator.set_saver(None)
    translator.initialize("zh")
    yield
    translator.clear_observers()
