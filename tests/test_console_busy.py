"""A console that another program holds must not end the poll thread: it keeps retrying."""
import importlib
import threading
import time

import usb.core
import usb.util

from consolelink import app as server_module
from consolelink import protocol as sfl


class FakeDevice:
    idProduct = sfl.PRODUCT_ID

    def is_kernel_driver_active(self, intf):
        return False

    def set_configuration(self):
        pass


def wait_for(condition, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if condition():
            return True
        time.sleep(0.02)
    return False


def test_claim_failure_is_retried_not_fatal(monkeypatch):
    server = importlib.reload(server_module)
    claims = []

    def claim(dev, intf):
        claims.append(intf)
        raise usb.core.USBError("Access denied (insufficient permissions)", errno=13)

    monkeypatch.setattr(usb.core, "find", lambda **kw: FakeDevice())
    monkeypatch.setattr(sfl, "find_bulk_interface", lambda dev: (0, 0, 0x81, 0x01))
    monkeypatch.setattr(usb.util, "claim_interface", claim)
    monkeypatch.setattr(usb.util, "dispose_resources", lambda dev: None)
    stop = threading.Event()
    thread = threading.Thread(target=server.poll_forever, args=(stop,))
    thread.start()
    try:
        assert wait_for(lambda: server.state["console_busy"])
        assert server.state["connected"] is False
        assert thread.is_alive()
    finally:
        stop.set()
        thread.join(timeout=5)
    assert not thread.is_alive()
    assert claims
