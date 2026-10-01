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


def post(base, name):
    req = urllib.request.Request(f"{base}/api/button/{name}", method="POST")
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
    for name, code in [("blackout", 0x57), ("solo", 0x56), ("ind1", 0x5F), ("ind2", 0x60)]:
        assert post(base, name) == 204
        assert mod.button_queue.get_nowait() == code


def test_unknown_button_and_disconnected(server):
    mod, base = server
    mod.write_enabled = True
    assert post(base, "nope") == 404
    mod.state["connected"] = False
    assert post(base, "blackout") == 503
    assert mod.button_queue.empty()
