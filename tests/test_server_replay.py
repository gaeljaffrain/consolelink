"""Replays recorded traffic through server.handle_payload -- the same entry point the USB loop
uses -- and checks the state the web page is sent. No USB device needed."""
import importlib

import pytest

import server as server_module


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
