"""Live fader table (type=0x0e) and stored intensity banks (type=0x0f)."""
import protocol as sfl


def test_live_faders_bumps_and_master(capture):
    data = capture("faders.log").tagged("int_a_faders").data
    faders, bumps, master = sfl.decode_0x0e_full(data)
    assert faders == [187, 94] + [0] * 22
    assert (bumps, master) == (255, 255)
    assert sfl.decode_0x0e(data) == {"Fader1": 187, "Fader2": 94, "Bumps": 255, "Master": 255}


def test_stored_banks_match_the_live_int_a_faders(capture):
    """The INT A bank holds the same levels the live table shows once INT A is selected."""
    banks = sfl.decode_0x0f_all_modes(capture("faders.log").tagged("banks_at_connect").data)
    assert banks == {"INT A": [187, 94] + [0] * 22, "INT B": [0] * 24, "INT DEV": [0] * 24}
