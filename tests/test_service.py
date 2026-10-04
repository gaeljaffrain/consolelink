"""Service: start/stop the poll thread, web server and Art-Net sender without the command line."""
import http.client
import socket

import pytest

from consolelink import app


def new_service(**kwargs):
    kwargs.setdefault("port", 0)
    return app.Service(app.Settings(**kwargs))


def get_status(port, path="/api/events"):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request("GET", path)
        resp = conn.getresponse()
        return resp.status, resp.getheader("Content-Type")
    finally:
        conn.close()


def test_serves_then_stops_and_frees_the_port():
    service = new_service().start()
    try:
        port = service.port
        assert get_status(port) == (200, "text/event-stream")
        assert get_status(port, "/") [0] == 200
    finally:
        service.stop()
    with socket.socket() as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # as the server itself does
        sock.bind(("127.0.0.1", port))  # nothing listens any more


def test_can_start_again_after_stop():
    for _ in range(2):
        service = new_service().start()
        try:
            assert get_status(service.port, "/")[0] == 200
        finally:
            service.stop()


def test_settings_reach_the_server_state():
    service = new_service(allow_write=True, password="pw").start()
    try:
        assert app.state["write_enabled"] is True
        assert app.state["write_password_required"] is True
        assert app.write_password == "pw"
    finally:
        service.stop()
    service = new_service().start()
    try:
        assert app.state["write_enabled"] is False
        assert app.write_password == ""
    finally:
        service.stop()


def test_port_in_use_raises_and_leaves_nothing_running():
    first = new_service().start()
    try:
        with pytest.raises(OSError):
            new_service(port=first.port).start()
    finally:
        first.stop()


def test_bad_settings_are_refused():
    with pytest.raises(ValueError):
        new_service(listen="network", allow_write=True).start()  # needs a password
    with pytest.raises(ValueError):
        new_service(listen="everywhere").start()
    with pytest.raises(ValueError):
        new_service(artnet_dest="127.0.0.1", artnet_universe=99999).start()


def test_no_web_has_no_server():
    service = new_service(web=False, debug=True).start()
    try:
        assert service.port is None
    finally:
        service.stop()
