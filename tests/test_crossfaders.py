"""Crossfader Live/Next (type=0x11). Checked against which crossfader was being moved: Live alone,
then Next alone, then both together."""
import importlib

from consolelink import protocol as sfl
from consolelink import app as server_module


def levels(cap, tag):
    return sfl.decode_0x11_full(cap.tagged(tag).data)


def test_live_alone_changes_only_the_live_byte(capture):
    cap = capture("crossfaders.log")
    (live_a, next_a), (live_b, next_b) = levels(cap, "live_a"), levels(cap, "live_b")
    assert live_a != live_b
    assert next_a == next_b


def test_next_alone_changes_only_the_next_byte(capture):
    cap = capture("crossfaders.log")
    (live_a, next_a), (live_b, next_b) = levels(cap, "next_a"), levels(cap, "next_b")
    assert next_a != next_b
    assert live_a == live_b


def test_moving_both_changes_both_bytes(capture):
    cap = capture("crossfaders.log")
    (live_a, next_a), (live_b, next_b) = levels(cap, "both_a"), levels(cap, "both_b")
    assert live_a != live_b
    assert next_a != next_b


def test_connect_time_request_reaches_state(capture):
    """Both crossfaders were left partway up before connecting, so the connect-time answer must
    carry nonzero levels -- the page's bars aren't stuck at 0 until the first move."""
    server = importlib.reload(server_module)
    assert (server.state["crossfader_live"], server.state["crossfader_next"]) == (0, 0)
    data = capture("crossfaders.log").tagged("connect").data
    server.handle_payload(0x11, data)
    live, next_ = server.state["crossfader_live"], server.state["crossfader_next"]
    assert 0 < live < 255 and 0 < next_ < 255
    assert (live, next_) == sfl.decode_0x11_full(data)


def pct(raw):
    """The console's own raw-byte -> displayed-percent rounding (same as the page's pct())."""
    return round(raw / 255 * 100)


def test_fader_output_is_scaled_by_live_and_master(capture):
    """Faders 1-3 at full in INT DEV. Live at 60% brought the devices' output to 60%; Master at
    60% on top brought it to 36% -- both fader tables report that output, not the lever."""
    cap = capture("output_scaling.log")

    faders, _, master = sfl.decode_0x0e_full(cap.tagged("start_faders").data)
    assert faders[:3] == [255] * 3 and master == 255
    assert sfl.decode_0x11_full(cap.tagged("start_crossfader").data)[0] == 255

    live, _ = sfl.decode_0x11_full(cap.tagged("live_60").data)
    assert pct(live) == 60
    faders, _, master = sfl.decode_0x0e_full(cap.tagged("faders_live_60").data)
    assert [pct(v) for v in faders[:3]] == [60] * 3 and master == 255

    faders, _, master = sfl.decode_0x0e_full(cap.tagged("faders_live_60_master_60").data)
    assert pct(master) == 60
    assert [pct(v) for v in faders[:3]] == [36] * 3
    bank = sfl.decode_0x0f_all_modes(cap.tagged("bank_live_60_master_60").data)["INT DEV"]
    assert [pct(v) for v in bank[:3]] == [36] * 3
