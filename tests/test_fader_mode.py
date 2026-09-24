"""Fader-mode selector (type=0x17): family in data[0], intensity sub-mode in data[4], PARAM
sub-mode in data[3], MEMS page in data[2]."""
import pytest

import protocol as sfl


@pytest.mark.parametrize("tag, mode", [
    ("int_a", "INT A"),
    ("int_b", "INT B"),
    ("int_dev", "INT DEV"),
    ("param1", "PARAM 1"),
    ("param2", "PARAM 2"),
    ("mems", "MEMS"),
])
def test_each_mode_button(capture, tag, mode):
    assert sfl.decode_0x17(capture("fader_modes.log").tagged(tag).data) == mode


@pytest.mark.parametrize("page", [1, 2, 3, 4])
def test_mems_page(capture, page):
    data = capture("fader_modes.log").tagged(f"mems_page{page}").data
    assert sfl.decode_0x17(data) == "MEMS"
    assert sfl.decode_0x17_mems_page(data) == page


def test_short_data_gives_none():
    assert sfl.decode_0x17(bytes(3)) is None
    assert sfl.decode_0x17_mems_page(bytes(2)) is None
