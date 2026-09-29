"""DMX output (type=0x0d). Checked against what was patched and moved on the console: INT A
faders 1-3 on DMX 1-3, IND 1 and IND 2 on DMX 511 and 512, universe 1."""
import pytest

import protocol as sfl

FADER_ADDRESS = {1: 1, 2: 2, 3: 3}
STEPS = range(6)


def dmx(cap, tag):
    return sfl.decode_0x0d_dmx(cap.tagged(tag).data)


def changed(before, after):
    """(universe, address) of every level that differs between two decoded snapshots."""
    return [(u + 1, i + 1) for u in range(2) for i in range(512) if before[u][i] != after[u][i]]


def test_both_universes_of_512_levels(capture):
    u1, u2 = dmx(capture("dmx.log"), "connect")
    assert len(u1) == len(u2) == 512


def test_second_universe_is_idle_when_nothing_is_patched_there(capture):
    cap = capture("dmx.log")
    assert all(dmx(cap, msg.tag)[1] == [0] * 512 for msg in cap.messages)


@pytest.mark.parametrize("fader", FADER_ADDRESS)
def test_fader_moves_only_its_own_address(capture, fader):
    cap = capture("dmx.log")
    tags = [f"f{fader}_{i}" for i in STEPS]
    for a, b in zip(tags, tags[1:]):
        assert changed(dmx(cap, a), dmx(cap, b)) == [(1, FADER_ADDRESS[fader])]


@pytest.mark.parametrize("fader", FADER_ADDRESS)
def test_fader_comes_back_to_zero(capture, fader):
    u1, _ = dmx(capture("dmx.log"), f"f{fader}_5")
    assert u1[FADER_ADDRESS[fader] - 1] == 0


def test_ind_buttons_switch_addresses_511_and_512_between_0_and_full(capture):
    cap = capture("dmx.log")
    for on, off, address in (("ind1_on", "ind1_off", 511), ("ind2_on", "ind2_off", 512)):
        assert dmx(cap, on)[0][address - 1] == 255
        assert dmx(cap, off)[0][address - 1] == 0
    assert changed(dmx(cap, "ind1_on"), dmx(cap, "ind1_off")) == [(1, 511)]
    assert changed(dmx(cap, "ind2_on"), dmx(cap, "ind2_off")) == [(1, 512)]


def test_other_addresses_are_untouched_by_the_ind_presses(capture):
    """Same level everywhere but the pressed address, so the decode isn't shifted by one."""
    cap = capture("dmx.log")
    before, after = dmx(cap, "ind1_off"), dmx(cap, "ind2_on")
    assert changed(before, after) == [(1, 512)]


@pytest.mark.parametrize("bad", [
    b"",
    b"\x04",
    bytes([0x04, 0x00]) + bytes(1023),      # one channel short
    bytes([0x04, 0x00]) + bytes(1025),      # one channel too many
    bytes([0x02, 0x00]) + bytes(1024),      # channel count is big-endian: 0x0200 = 512, not 1024
    bytes([0x00, 0x04]) + bytes(1024),      # byte-swapped header
])
def test_malformed_payload_is_rejected(bad):
    assert sfl.decode_0x0d_dmx(bad) is None


def test_decode_splits_at_the_universe_boundary():
    data = bytes([0x04, 0x00]) + bytes([1]) + bytes(510) + bytes([2]) + bytes([3]) + bytes(510) + bytes([4])
    u1, u2 = sfl.decode_0x0d_dmx(data)
    assert (u1[0], u1[511], u2[0], u2[511]) == (1, 2, 3, 4)
