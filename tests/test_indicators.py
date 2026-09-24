"""Solo/BlackOut (type=0x16, offsets 517 and 523): on/off/blinking state and the LED colors."""
import protocol as sfl

WHITE, BLUE, IDLE = [255, 255, 255], [0, 0, 255], [10, 10, 10]


def solid(color):
    return {"blinking": False, "color": color}


def test_both_on(capture):
    data = capture("indicators.log").tagged("both_on").data
    assert sfl.decode_0x16_indicators(data) == {"solo": "on", "blackout": "on"}
    assert sfl.decode_0x16_indicator_lights(data) == {"solo": solid(WHITE), "blackout": solid(BLUE)}


def test_blackout_only(capture):
    data = capture("indicators.log").tagged("blackout_only").data
    assert sfl.decode_0x16_indicators(data) == {"solo": "off", "blackout": "on"}
    assert sfl.decode_0x16_indicator_lights(data) == {"solo": solid(IDLE), "blackout": solid(BLUE)}


def test_both_off(capture):
    data = capture("indicators.log").tagged("both_off").data
    assert sfl.decode_0x16_indicators(data) == {"solo": "off", "blackout": "off"}
    assert sfl.decode_0x16_indicator_lights(data) == {"solo": solid(IDLE), "blackout": solid(IDLE)}


def test_blackout_blinks_light_blue_when_master_is_down(capture):
    """Not BlackOut's solid-on blue: a lighter 37 37 ff, alternating with idle."""
    data = capture("indicators.log").tagged("blackout_blink").data
    assert sfl.decode_0x16_indicators(data)["blackout"] == "blinking"
    assert sfl.decode_0x16_indicator_lights(data)["blackout"] == {
        "blinking": True, "color_a": [0x37, 0x37, 0xff], "color_b": IDLE}


def test_short_data_gives_none():
    assert sfl.decode_0x16_indicators(bytes(100)) == {"solo": None, "blackout": None}
    assert sfl.decode_0x16_indicator_lights(bytes(100)) == {"solo": None, "blackout": None}
