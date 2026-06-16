"""Shared test fixtures."""
import os

# Use offscreen Qt platform plugin so tests run without a display.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest


@pytest.fixture(autouse=True)
def _no_store_icon_fetches(monkeypatch):
    """Stop the Store tab's IconLoader from doing real HTTP fetches.

    The bundled catalog has ``icon_url`` on every row. When the Store
    tab is built (in test_main_window or test_store_tab), the loader
    schedules a urllib fetch per URL on a QThreadPool. Those threads
    keep running across pytest test boundaries and, on race, swallow
    the response bodies that other tests' ``patch.object(<mod>.urllib
    .request, "urlopen")`` mocks set up — making test_sources_*
    flaky.

    Replacing the QThreadPool used by IconLoader with a no-op makes
    icon fetching a fixed-no-op for the whole suite, which is what we
    want anyway: the tests don't need real icons.
    """
    from ivi_installer.ui import store_tab as _store_tab

    class _NullPool:
        def setMaxThreadCount(self, _n):
            pass

        def start(self, _runnable):
            pass

    monkeypatch.setattr(_store_tab, "QThreadPool",
                        lambda *_a, **_kw: _NullPool())
