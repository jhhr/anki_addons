"""The shared running-Anki harness itself, against a real `AnkiQt`.

The addon suites exercise `running_anki` constantly, but only by accident of timing: the
failures it exists to prevent are races, and a race that loses once in ten runs is not a
test. What is here forces each race to lose every time, so a harness fix that stops working
fails here rather than as a flake somewhere else.
"""

import time
from typing import Any, Iterator

import aqt.mediasrv
import pytest

from anki_shared.testing import running_anki

# Long enough that Anki asks for the port while the server thread is still asleep; short
# enough to be cheap. Anki asks within milliseconds of starting the thread.
MEDIA_SERVER_START_DELAY = 0.5


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
