"""The console's two LCDs (type=0x15): 4 lines of 20 chars, as shown on the console."""
from consolelink import protocol as sfl


def test_device_parameter_screen(capture):
    data = capture("lcd.log").tagged("focus_page").data
    assert sfl.decode_0x15(data) == [
        "Pan    FOCUS  Tilt  ",
        "[   ]  Coarse [   ] ",
        "       Int.      2: ",
        "       [   ]     1/2",
    ]


def test_mems_page_prompt(capture):
    data = capture("lcd.log").tagged("mems_held").data
    assert sfl.decode_0x15(data) == [
        " " * 20,
        " " * 20,
        "Memory page:1       ",
        "Bump 1-12 to change ",
    ]


def test_short_payload_is_rejected():
    assert sfl.decode_0x15(b"P" + b" " * 79) is None
