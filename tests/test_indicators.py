"""Solo/BlackOut/Int Only/Go Mode LEDs (type=0x16): the colors the console sends, and the
on/off/blinking state read from them (light_state)."""
from consolelink import protocol as sfl

WHITE, BLUE, IDLE = [255, 255, 255], [0, 0, 255], [10, 10, 10]


def solid(color):
    return {"blinking": False, "color": color}


def blink(a, b):
    return {"blinking": True, "color_a": a, "color_b": b}


def states(data):
    return {k: sfl.light_state(v) for k, v in sfl.decode_0x16_indicator_lights(data).items()}


def test_both_on(capture):
    data = capture("indicators.log").tagged("both_on").data
    assert states(data) == {"solo": "on", "blackout": "on", "go_mode": "off", "int_only": "off"}
    assert sfl.decode_0x16_indicator_lights(data) == {
        "solo": solid(WHITE), "blackout": solid(BLUE), "go_mode": solid(IDLE), "int_only": solid(IDLE)}


def test_blackout_only(capture):
    data = capture("indicators.log").tagged("blackout_only").data
    assert states(data) == {"solo": "off", "blackout": "on", "go_mode": "off", "int_only": "off"}
    assert sfl.decode_0x16_indicator_lights(data) == {
        "solo": solid(IDLE), "blackout": solid(BLUE), "go_mode": solid(IDLE), "int_only": solid(IDLE)}


def test_both_off(capture):
    data = capture("indicators.log").tagged("both_off").data
    assert states(data) == {"solo": "off", "blackout": "off", "go_mode": "off", "int_only": "off"}
    assert sfl.decode_0x16_indicator_lights(data) == {
        "solo": solid(IDLE), "blackout": solid(IDLE), "go_mode": solid(IDLE), "int_only": solid(IDLE)}


def test_blackout_blinks_light_blue_when_master_is_down(capture):
    """Not BlackOut's solid-on blue: a lighter 37 37 ff, alternating with idle."""
    data = capture("indicators.log").tagged("blackout_blink").data
    assert states(data)["blackout"] == "blinking"
    assert sfl.decode_0x16_indicator_lights(data)["blackout"] == {
        "blinking": True, "color_a": [0x37, 0x37, 0xff], "color_b": IDLE}


def test_short_data_gives_none():
    none = {"solo": None, "blackout": None, "go_mode": None, "int_only": None}
    assert states(bytes(100)) == none
    assert sfl.decode_0x16_indicator_lights(bytes(100)) == none


def test_mems_mode_leds(capture):
    """INT ONLY and GO MODE toggled in MEMS (confirmed on the console's own LEDs), gone in INT A."""
    for tag, go, int_ in [("int_on", "off", "on"), ("both_off", "off", "off"), ("go_on", "on", "off"),
                          ("both_on", "on", "on"), ("int_a_mode", "off", "off"),
                          ("back_in_mems", "on", "on")]:
        flags = states(capture("mems_modes.log").tagged(tag).data)
        assert (flags["go_mode"], flags["int_only"]) == (go, int_), tag
    lights = sfl.decode_0x16_indicator_lights(capture("mems_modes.log").tagged("go_on").data)
    assert lights["go_mode"] == solid(BLUE) and lights["int_only"] == solid(IDLE)


def test_mems_mode_button_codes_match_smartsoft():
    assert (sfl.BUTTON_INT_ONLY, sfl.BUTTON_GO_MODE) == (0x3E, 0x3C)


def test_light_state_reads_the_colors_not_a_known_on_color():
    assert sfl.light_state(None) is None
    for idle in ([0, 0, 0], [10, 10, 10], [0, 10, 0], [10, 0, 0]):
        assert sfl.light_state(solid(idle)) == "off", idle
    # Any steady color above the idle level is lit, including ones never seen on the console.
    for lit in (WHITE, BLUE, [0, 255, 0], [255, 100, 0], [0x46, 0, 0], [0, 0, 11]):
        assert sfl.light_state(solid(lit)) == "on", lit
    assert sfl.light_state(blink([0x37, 0x37, 0xff], IDLE)) == "blinking"
    assert sfl.light_state(blink(IDLE, [0, 0x46, 0])) == "blinking"
    assert sfl.light_state(blink([0, 0, 0], IDLE)) == "off"  # both phases idle: nothing to see
