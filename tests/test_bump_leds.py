"""Per-fader Bump LEDs (type=0x16): 6 bytes per fader at 1+6*(N-1), two RGB triples (the two
blink phases). The console sends the color itself -- green in INT/PARAM, red in MEMS."""
import protocol as sfl

DIM_RED = [0x46, 0, 0]
IDLE_RED = [0x0a, 0, 0]
# MEMS page 1 of the test console has memories on these faders (see test_mems_names.py).
MEMS_PAGE1_RECORDED = {1, 2, 3, 4, 9, 10, 11, 12, 13, 14, 15, 16, 22, 23, 24}


def leds(message):
    return {n: sfl.decode_0x16_bump_catch(message.data, n) for n in range(1, 25)}


def test_mems_at_rest_lights_recorded_memories_dim_red(capture):
    lights = leds(capture("bump_leds.log").tagged("mems_rest"))
    for n, light in lights.items():
        expected = DIM_RED if n in MEMS_PAGE1_RECORDED else IDLE_RED
        assert light == {"blinking": False, "color": expected}, f"fader {n}"


def test_mems_fader_up_lights_its_bump_full_red(capture):
    lights = leds(capture("bump_leds.log").tagged("mems_fader1_up"))
    assert lights[1] == {"blinking": False, "color": [255, 0, 0]}
    assert lights[2] == {"blinking": False, "color": DIM_RED}


def test_int_catch_blink_is_green_pair(capture):
    lights = leds(capture("bump_leds.log").tagged("int_a_catch_blink"))
    assert lights[1] == {"blinking": True, "color_a": [0, 0x46, 0], "color_b": [0, 0x0a, 0]}
    assert lights[2] == {"blinking": False, "color": [0, 94, 0]}


def test_int_caught_fader_is_green_at_its_level(capture):
    lights = leds(capture("bump_leds.log").tagged("int_a_tracking"))
    assert lights[1] == {"blinking": False, "color": [0, 0xcd, 0]}
    assert lights[2] == {"blinking": False, "color": [0, 94, 0]}
    assert all(lights[n] == {"blinking": False, "color": [0, 0, 0]} for n in range(3, 25))


def test_fader_24_does_not_read_into_the_next_led_region(capture):
    """The byte right after fader 24's block (offset 145) belongs to another LED region
    (0x63 here). Reading it as part of fader 24 made that fader blink permanently."""
    message = capture("bump_leds.log").tagged("startup")
    assert message.data[145] == 0x63
    lights = leds(message)
    assert lights[1] == {"blinking": False, "color": [0, 0x29, 0]}
    assert lights[24] == {"blinking": False, "color": [0, 0, 0]}


def test_short_data_gives_none():
    assert sfl.decode_0x16_bump_catch(bytes(10), 24) is None
