"""The shared running-Anki harness itself, against a real `AnkiQt`.

The addon suites exercise `running_anki` constantly, but only by accident of timing: the
failures it exists to prevent are races, and a race that loses once in ten runs is not a
test. What is here forces each race to lose every time, so a harness fix that stops working
fails here rather than as a flake somewhere else.
"""

import time
from typing import Any, Iterator

import aqt.mediasrv
import aqt.webview
import pytest
from aqt.qt import QUrl

from anki_shared.testing import running_anki

# Long enough that Anki asks for the port while the server thread is still asleep; short
# enough to be cheap. Anki asks within milliseconds of starting the thread.
MEDIA_SERVER_START_DELAY = 0.5

# Any port a media server of an earlier test could have had.
A_PORT = 50334

# A page's alert() reaches Python within milliseconds of the script running.
ALERT_TIMEOUT = 10000


@pytest.fixture(autouse=True)
def restore_stub_mw() -> Iterator[Any]:
    with running_anki.stub_mw_restored(["anki_shared"], []) as stub:
        yield stub


@pytest.fixture
def slow_media_server(monkeypatch) -> Iterator[None]:
    """A media server that is slow to start, in a process where one has started before.

    Both halves are what an addon suite produces by the second real-Anki test: every test
    builds a new `AnkiQt` and so a new server, and the first one to come up has set the
    readiness `Event` that aqt keeps on the class. The delay stands in for the scheduler
    handing the new server's thread its first time slice late.
    """
    server_class = aqt.mediasrv.MediaServer
    original_run = server_class.run

    def run(self) -> None:
        time.sleep(MEDIA_SERVER_START_DELAY)
        original_run(self)

    monkeypatch.setattr(server_class, "run", run)
    was_set = server_class._ready.is_set()
    server_class._ready.set()
    yield
    if not was_set:
        server_class._ready.clear()


@pytest.fixture
def real_mw(slow_media_server, anki_session, restore_stub_mw) -> Iterator[Any]:
    with running_anki.main_window(anki_session, ["anki_shared"]) as mw:
        yield mw


def test_a_new_anki_waits_for_its_own_media_server_rather_than_an_earlier_ones(real_mw):
    # Without `media_servers_waited_for()` this returns at once, on the strength of an
    # earlier server's readiness, and fails with "'MediaServer' object has no attribute
    # 'server'" -- what opening the Add-cards editor used to hit now and then. Profile load
    # does not ask for the port with the deck browser silenced, so nothing before this does.
    port = real_mw.mediaServer.getPort()

    assert port == int(real_mw.mediaServer.server.effective_port)


def test_a_request_under_way_after_its_anki_is_gone_finds_a_stub_that_answers(restore_stub_mw):
    # The question aqt's interceptor puts to `aqt.mw` about every request of a page that may
    # call Anki's API. An editor's page still has requests under way when its test has ended
    # and the stub is back, and the stub had no `serverURL`: the AttributeError came out of
    # the Qt event loop, as an error in that test's teardown or in the next test to run.
    late = QUrl(f"http://127.0.0.1:{A_PORT}/_anki/i18nResources")

    assert aqt.webview._is_internal_url(late) is False


def test_a_pages_alert_raises_its_text_instead_of_opening_a_box(anki_session, monkeypatch):
    # Anki's own handler ends in a modal box. Should the guard be lost, this test must not be
    # the one that waits in it, so the box is taken away first.
    boxes: list[str] = []
    monkeypatch.setattr(aqt.webview, "showInfo", boxes.append)
    with running_anki.main_window(anki_session, ["anki_shared"]) as mw:
        with anki_session.qtbot.capture_exceptions() as raised:
            mw.web.eval("alert('TypeError: Failed to fetch')")
            anki_session.qtbot.waitUntil(lambda: bool(raised or boxes), timeout=ALERT_TIMEOUT)

    assert boxes == []
    assert "alert('TypeError: Failed to fetch')" in str(raised[0][1])
