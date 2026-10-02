"""Replays recorded traffic through server.handle_payload -- the same entry point the USB loop
uses -- and checks the state the web page is sent. No USB device needed."""
import importlib

import pytest

from consolelink import app as server_module


@pytest.fixture
def server():
    # handle_payload writes into module-level state; reload for a clean copy per test.
    return importlib.reload(server_module)


def replay(server, *captures):
    for cap in captures:
        for message in cap.messages:
            if message.type != 0x28:  # announces are handled by the USB loop, not here
                server.handle_payload(message.type, message.data)


def test_mems_names_land_in_their_own_page_and_slot(server, capture):
    replay(server, capture("mems_names.log"))
    mems = server.state["labels"]["MEMS"]
    assert sorted(mems) == [1, 2, 3]
    assert mems[1][1] == ["Look 1", "", ""]
    assert mems[1][16] == ["R-G-B", "effect", "LEDs"]
    assert mems[2] == {1: ["page2", "mem1", ""]}
    assert mems[3] == {5: ["page3", "mem5", ""]}


def test_led_colors_reach_state(server, capture):
    replay(server, capture("indicators.log"))
    assert server.state["blackout"] == "blinking"
    assert server.state["indicator_lights"]["blackout"]["color_a"] == [0x37, 0x37, 0xff]

    server.handle_payload(0x16, capture("bump_leds.log").tagged("mems_fader1_up").data)
    assert server.state["physical_fader_lights"][0] == {"blinking": False, "color": [255, 0, 0]}


def test_fader_mode_and_mems_page(server, capture):
    server.handle_payload(0x17, capture("fader_modes.log").tagged("mems_page3").data)
    assert (server.state["fader_mode"], server.state["mems_page"]) == ("MEMS", 3)


def test_lcd_text_reaches_state(server, capture):
    assert server.state["lcd"] is None
    server.handle_payload(0x15, capture("lcd.log").tagged("mems_held").data)
    assert server.state["lcd"][2:] == ["Memory page:1       ", "Bump 1-12 to change "]


def test_dmx_is_none_until_the_first_message(server):
    assert server.state["dmx"] is None


def test_dmx_reaches_state_as_two_universes(server, capture):
    cap = capture("dmx.log")
    server.handle_payload(0x0d, cap.tagged("ind1_on").data)
    u1, u2 = server.state["dmx"]
    assert len(u1) == len(u2) == 512
    assert u1[510] == 255 and u1[511] == 0  # IND 1 on DMX 511, IND 2 (DMX 512) still off

    server.handle_payload(0x0d, cap.tagged("ind1_off").data)
    assert server.state["dmx"][0][510] == 0


def test_dmx_is_replaced_by_each_snapshot_not_merged(server, capture):
    cap = capture("dmx.log")
    server.handle_payload(0x0d, cap.tagged("f1_0").data)
    assert server.state["dmx"][0][0] == 100
    server.handle_payload(0x0d, cap.tagged("f1_5").data)
    assert server.state["dmx"][0][0] == 0


def test_malformed_dmx_leaves_state_untouched(server, capture):
    server.handle_payload(0x0d, capture("dmx.log").tagged("connect").data)
    before = server.state["dmx"]
    server.handle_payload(0x0d, b"\x04\x00" + bytes(10))
    assert server.state["dmx"] is before


def test_undecoded_types_are_reported_under_debug(server, capsys):
    server.debug = True
    server.handle_payload(0x10, bytes(range(20)))
    server.handle_payload(0x7e, b"\x01\x02")
    err = capsys.readouterr().err
    assert "type=0x10" in err and "known, undecoded" in err
    assert "type=0x7e" in err and "UNEXPECTED" in err


def test_undecoded_types_are_silent_without_debug(server, capsys):
    before = dict(server.state)
    server.handle_payload(0x10, bytes(20))
    server.handle_payload(0x7e, b"\x01")
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""
    assert server.state == before
