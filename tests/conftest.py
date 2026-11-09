import pytest


# Keep failed teardown objects alive rather than destroying a running QThread.
_UNFINISHED_TEST_WINDOWS = []


def _window_workers(roots):
    """Include detached Qt workers, Python workers, and asynchronous close jobs."""
    from concurrent.futures import Future
    from threading import Thread
    from PySide6.QtCore import QObject, QThread
    from shiboken6 import isValid

    pending = set()
    for root in roots:
        if not isValid(root):
            continue
        for obj in (root, *root.findChildren(QObject)):
            # Detached workers remain in their owner's lists; they no longer
            # appear in findChildren(QThread). Only inspect direct attributes.
            for name, value in list(vars(obj).items()):
                values = tuple(value) if isinstance(value, (list, tuple, set)) else (value,)
                for worker in values:
                    if isinstance(worker, QThread):
                        running = isValid(worker) and worker.isRunning()
                    elif isinstance(worker, Thread):
                        running = worker.is_alive()
                    elif isinstance(worker, Future):
                        running = not worker.done()
                    else:
                        continue
                    if running:
                        pending.add(f"{type(obj).__name__}.{name}:{type(worker).__name__}")
            if isinstance(obj, QThread) and obj.isRunning():
                pending.add(type(obj).__name__)
    return sorted(pending)


@pytest.fixture(autouse=True)
def _close_test_main_windows(monkeypatch, _pin_language_zh):
    """Close each test's windows before releasing wrappers or language observers.

    Normal close retains its existing shutdown deadlines. Any remaining worker
    gets at most two additional seconds of event processing; timeout fails the
    test and retains the widgets, never terminates/deletes an active worker.
    """
    import time
    from functools import wraps
    from PySide6.QtCore import QCoreApplication, QEvent
    from PySide6.QtWidgets import QApplication, QWidget
    from shiboken6 import isValid
    from automatic_transfer_station.main_window import MainWindow

    windows = []
    original_init = MainWindow.__init__

    @wraps(original_init)
    def track(window, *args, **kwargs):
        original_init(window, *args, **kwargs)
        windows.append(window)

    monkeypatch.setattr(MainWindow, "__init__", track)
    yield
    if not windows:
        return

    app = QApplication.instance()
    roots = []
    for window in windows:
        if not isValid(window):
            continue
        for root in (window, window.camera_window, window.cutting_window):
            # Auxiliary windows are sometimes Mock/SimpleNamespace test doubles.
            if isinstance(root, QWidget) and isValid(root) and root not in roots:
                roots.append(root)
        window.close()

    deadline = time.monotonic() + 2.0
    while True:
        app.processEvents()
        pending = _window_workers(roots)
        if not pending:
            refused = [type(root).__name__ for root in roots
                       if isValid(root) and not root.close()]
            pending = _window_workers(roots) + refused
            if not pending:
                break
        if time.monotonic() >= deadline:
            _UNFINISHED_TEST_WINDOWS.extend(roots)
            pytest.fail("Qt teardown retained unfinished windows/workers: " + ", ".join(pending))
        time.sleep(0.01)

    for root in roots:
        if isValid(root):
            root.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


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
