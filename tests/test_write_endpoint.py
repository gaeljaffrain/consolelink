"""Write endpoints: refused unless started with --allow-write, and only with its password."""
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


def post(base, name, path=None, pw="secret"):
    url = f"{base}{path or '/api/button/' + name}" + (f"?pw={pw}" if pw is not None else "")
    req = urllib.request.Request(url, method="POST")
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
    mod.write_password = "secret"
    mod.state["connected"] = True
    for name, code in [("blackout", 0x57), ("solo", 0x56), ("ind1", 0x5F), ("ind2", 0x60),
                       ("mode-int-a", 0x41), ("mode-int-b", 0x42), ("mode-int-dev", 0x40),
                       ("mode-param-1", 0x43), ("mode-param-2", 0x44), ("mode-mems", 0x3F),
                       ("int-only", 0x3E), ("go-mode", 0x3C)]:
        assert post(base, name) == 204
        assert mod.button_queue.get_nowait() == ("tap", code)


def test_unknown_button_and_disconnected(server):
    mod, base = server
    mod.write_enabled = True
    mod.write_password = "secret"
    assert post(base, "nope") == 404
    mod.state["connected"] = False
    assert post(base, "blackout") == 503
    assert mod.button_queue.empty()


def test_bump_and_page_requests_queue_events(server):
    mod, base = server
    mod.write_enabled = True
    mod.write_password = "secret"
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
        mod.write_password = "secret"
        mod.state["connected"] = True
        assert post(base, None, path) == 404, path
    assert mod.button_queue.empty()
    mod.write_enabled = False
    assert post(base, None, "/api/bump/1/press") == 403
    assert post(base, None, "/api/mems_page/2") == 403
    mod.write_enabled = True
    mod.write_password = "secret"
    mod.state["connected"] = False
    assert post(base, None, "/api/bump/1/press") == 503
    assert post(base, None, "/api/mems_page/2") == 503
    assert mod.button_queue.empty()


def test_fader_moves_coalesce_to_the_latest_value(server):
    mod, base = server
    mod.write_enabled = True
    mod.write_password = "secret"
    mod.state["connected"] = True
    for path in ["/api/fader/1/10", "/api/fader/1/200", "/api/fader/24/0"]:
        assert post(base, None, path) == 204
    assert post(base, None, "/api/fader/master/255") == 204
    assert post(base, None, "/api/fader/bumps/7") == 204
    assert post(base, None, "/api/fader/live/9") == 204
    assert post(base, None, "/api/fader/next/8") == 204
    assert mod.fader_writes == {1: 200, 24: 0, 25: 255, 26: 7, 27: 9, 28: 8}
    assert mod.button_queue.empty()


def test_fader_ranges_and_gates(server):
    mod, base = server
    mod.write_enabled = True
    mod.write_password = "secret"
    mod.state["connected"] = True
    for path in ["/api/fader/0/5", "/api/fader/29/5", "/api/fader/1/256", "/api/fader/1/-1",
                 "/api/fader/x/5", "/api/fader/1/x", "/api/fader/xfade/5", "/api/fader/25/5"]:
        assert post(base, None, path) == 404, path
    mod.write_enabled = False
    assert post(base, None, "/api/fader/1/5") == 403
    mod.write_enabled = True
    mod.write_password = "secret"
    mod.state["connected"] = False
    assert post(base, None, "/api/fader/1/5") == 503
    assert mod.fader_writes == {}


def test_static_files_are_revalidated(server):
    mod, base = server
    for path in ["/", "/app.js", "/style.css"]:
        with urllib.request.urlopen(base + path) as r:
            assert r.headers["Cache-Control"] == "no-cache", path


def test_wrong_or_missing_password_is_refused(server, monkeypatch):
    mod, base = server
    monkeypatch.setattr(mod.time, "sleep", lambda s: None)
    mod.write_enabled = True
    mod.write_password = "secret"
    mod.state["connected"] = True
    for path in ["/api/button/blackout", "/api/bump/1/press", "/api/fader/1/5",
                 "/api/mems_page/1", "/api/auth"]:
        assert post(base, None, path, pw="wrong") == 401, path
        assert post(base, None, path, pw=None) == 401, path
        assert post(base, None, path, pw="") == 401, path
    assert mod.button_queue.empty() and mod.fader_writes == {}


def test_auth_check_only_validates_the_password(server):
    mod, base = server
    assert post(base, None, "/api/auth") == 403  # writing not enabled
    mod.write_enabled = True
    mod.write_password = "pa ss&word"
    assert post(base, None, "/api/auth", pw="pa%20ss%26word") == 204
    mod.state["connected"] = False
    assert post(base, None, "/api/auth", pw="pa%20ss%26word") == 204  # no console needed
    assert mod.button_queue.empty()
