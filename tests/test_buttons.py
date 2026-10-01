"""Virtual button presses (type=0x14 OUT): the bytes real SmartSoft sent when its Live tab
toggled BlackOut, and which the console answered by flipping its BlackOut indicator."""
from consolelink import protocol as sfl


class FakeEndpoint:
    bEndpointAddress = 0x04


class FakeDev:
    def __init__(self):
        self.writes = []

    def write(self, endpoint, data, timeout=None):
        self.writes.append(bytes(data))


def make_link():
    dev = FakeDev()
    return sfl.ConsoleLink(dev, None, FakeEndpoint()), dev


def test_blackout_press_matches_smartsoft(monkeypatch):
    monkeypatch.setattr(sfl, "BUTTON_RELEASE_DELAY", 0)
    link, dev = make_link()
    assert link.press_button(sfl.BUTTON_BLACKOUT)
    assert dev.writes == [
        bytes.fromhex("010007000000000000000000"),  # msgType=1, payloadLen=7
        bytes.fromhex("02" "0300" "14" "015701"),   # seq=2, objLen=3, type=0x14, press
        bytes.fromhex("010007000000000000000000"),
        bytes.fromhex("03" "0300" "14" "015700"),   # seq=3, release
    ]


def test_sequence_continues_after_connect_requests():
    link, dev = make_link()
    link.send_button(sfl.BUTTON_SOLO, True)
    link.send_button(sfl.BUTTON_SOLO, False)
    assert [w[0] for w in dev.writes[1::2]] == [2, 3]


def test_codes_match_smartsoft():
    assert (sfl.BUTTON_SOLO, sfl.BUTTON_BLACKOUT, sfl.BUTTON_IND1, sfl.BUTTON_IND2) == (
        0x56, 0x57, 0x5F, 0x60)
