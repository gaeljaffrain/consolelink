"""Virtual button presses (type=0x14 OUT): the bytes real SmartSoft sent when its Live tab
toggled BlackOut, and which the console answered by flipping its BlackOut indicator."""
import pytest

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


def test_mode_and_bump_codes_match_smartsoft():
    assert (sfl.BUTTON_MODE_INT_A, sfl.BUTTON_MODE_INT_B, sfl.BUTTON_MODE_INT_DEV,
            sfl.BUTTON_MODE_PARAM_1, sfl.BUTTON_MODE_PARAM_2, sfl.BUTTON_MODE_MEMS) == (
        0x41, 0x42, 0x40, 0x43, 0x44, 0x3F)
    # Bump 1, 2, 3, 12 and 24 were confirmed on the console; the code is fader - 1.
    assert [sfl.bump_code(n) for n in (1, 2, 3, 12, 24)] == [0x00, 0x01, 0x02, 0x0B, 0x17]
    for bad in (0, 25):
        with pytest.raises(ValueError):
            sfl.bump_code(bad)


def test_bump_press_and_release_bytes():
    link, dev = make_link()
    assert link.send_button(sfl.bump_code(24), True)
    assert link.send_button(sfl.bump_code(24), False)
    assert dev.writes == [
        bytes.fromhex("010007000000000000000000"),
        bytes.fromhex("02" "0300" "14" "011701"),   # fader 24 down
        bytes.fromhex("010007000000000000000000"),
        bytes.fromhex("03" "0300" "14" "011700"),   # fader 24 up
    ]


def test_mems_page_select_matches_smartsoft():
    """SmartSoft's PAGE dropdown: one type=0x27 request, subtype 0x0a, then the 0-based page."""
    link, dev = make_link()
    assert link.send_mems_page(12)
    assert link.send_mems_page(1)
    assert dev.writes == [
        bytes.fromhex("010007000000000000000000"),
        bytes.fromhex("02" "0300" "27" "0a0b00"),   # page 12
        bytes.fromhex("010007000000000000000000"),
        bytes.fromhex("03" "0300" "27" "0a0000"),   # page 1
    ]
    for bad in (0, 13):
        with pytest.raises(ValueError):
            link.send_mems_page(bad)
