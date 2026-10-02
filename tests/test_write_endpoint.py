"""POST /api/button/<name>: refused unless started with --allow-write, never reaches the USB queue."""
import importlib
import threading
import urllib.error
import urllib.request
from http.server import HTTPServer

import pytest

from consolelink import app as server_module


@pytest.fixture
def server():
    mod = importlib.reload(server_module)
    httpd = HTTPServer(("127.0.0.1", 0), mod.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield mod, f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()


def post(base, name, path=None):
    req = urllib.request.Request(f"{base}{path or '/api/button/' + name}", method="POST")
    try:
        return urllib.request.urlopen(req).status
    except urllib.error.HTTPError as e:
        return e.code


def test_refused_by_default(server):
    mod, base = server
    mod.state["connected"] = True
    assert post(base, "blackout") == 403
    assert mod.button_queue.empty()


def test_allowed_queues_the_button_code(server):
    mod, base = server
    mod.write_enabled = True
    mod.state["connected"] = True
    for name, code in [("blackout", 0x57), ("solo", 0x56), ("ind1", 0x5F), ("ind2", 0x60),
                       ("mode-int-a", 0x41), ("mode-int-b", 0x42), ("mode-int-dev", 0x40),
                       ("mode-param-1", 0x43), ("mode-param-2", 0x44), ("mode-mems", 0x3F)]:
        assert post(base, name) == 204
        assert mod.button_queue.get_nowait() == ("tap", code)


def test_unknown_button_and_disconnected(server):
    mod, base = server
    mod.write_enabled = True
    assert post(base, "nope") == 404
    mod.state["connected"] = False
    assert post(base, "blackout") == 503
    assert mod.button_queue.empty()


def test_bump_and_page_requests_queue_events(server):
    mod, base = server
    mod.write_enabled = True
    mod.state["connected"] = True
    for path, event in [("/api/bump/1/press", ("press", 0)),
                        ("/api/bump/24/keepalive", ("keepalive", 23)),
                        ("/api/bump/12/release", ("release", 11)),
                        ("/api/mems_page/1", ("page", 1)),
                        ("/api/mems_page/12", ("page", 12))]:
        assert post(base, None, path) == 204
        assert mod.button_queue.get_nowait() == event


def test_bump_and_page_ranges_and_gates(server):
    mod, base = server
    for path in ["/api/bump/0/press", "/api/bump/25/press", "/api/bump/x/press",
                 "/api/bump/1/toggle", "/api/mems_page/0", "/api/mems_page/13",
                 "/api/mems_page/-1"]:
        mod.write_enabled = True
        mod.state["connected"] = True
        assert post(base, None, path) == 404, path
    assert mod.button_queue.empty()
    mod.write_enabled = False
    assert post(base, None, "/api/bump/1/press") == 403
    assert post(base, None, "/api/mems_page/2") == 403
    mod.write_enabled = True
    mod.state["connected"] = False
    assert post(base, None, "/api/bump/1/press") == 503
    assert post(base, None, "/api/mems_page/2") == 503
    assert mod.button_queue.empty()


def test_fader_moves_coalesce_to_the_latest_value(server):
    mod, base = server
    mod.write_enabled = True
    mod.state["connected"] = True
    for path in ["/api/fader/1/10", "/api/fader/1/200", "/api/fader/24/0"]:
        assert post(base, None, path) == 204
    assert post(base, None, "/api/fader/master/255") == 204
    assert post(base, None, "/api/fader/bumps/7") == 204
    assert mod.fader_writes == {1: 200, 24: 0, 25: 255, 26: 7}
    assert mod.button_queue.empty()


def test_fader_ranges_and_gates(server):
    mod, base = server
    mod.write_enabled = True
    mod.state["connected"] = True
    for path in ["/api/fader/0/5", "/api/fader/29/5", "/api/fader/1/256", "/api/fader/1/-1",
                 "/api/fader/x/5", "/api/fader/1/x", "/api/fader/live/5", "/api/fader/25/5"]:
        assert post(base, None, path) == 404, path
    mod.write_enabled = False
    assert post(base, None, "/api/fader/1/5") == 403
    mod.write_enabled = True
    mod.state["connected"] = False
    assert post(base, None, "/api/fader/1/5") == 503
    assert mod.fader_writes == {}
