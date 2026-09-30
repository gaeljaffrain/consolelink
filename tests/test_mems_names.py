"""MEMS memory names: the connect-time type=0x00 catalog, and the type=0x28 announce selector
that addresses each memory by (page, slot)."""
from consolelink import protocol as sfl

# Every recorded memory on the test console, as shown on its own display: (page, slot), both
# 0-indexed as on the wire -> the name's 3 lines.
EXPECTED_MEMORIES = {
    (0, 0): ["Look 1", "", ""],
    (0, 1): ["Green", "Violet", ""],
    (0, 2): ["Fanned", "on", "Wall"],
    (0, 3): ["Circle", "in", "front"],
    (0, 8): ["Fire", "", ""],
    (0, 9): ["Green", "ground", ""],
    (0, 10): ["CMY", "Effect", ""],
    (0, 11): ["Blue", "wave", ""],
    (0, 12): ["Rainbo", "w", "LEDs"],
    (0, 13): ["All in", "Red", "LEDs"],
    (0, 14): ["Rainbo", "w FXs", "LEDs"],
    (0, 15): ["R-G-B", "effect", "LEDs"],
    (0, 21): ["", "", ""],  # recorded, never named
    (0, 22): ["", "", ""],
    (0, 23): ["", "", ""],
    (1, 0): ["page2", "mem1", ""],
    (2, 4): ["page3", "mem5", ""],
}


def test_announce_selector_addresses_each_memory_by_page_and_slot(capture):
    """The selector's second half is little-endian: page 3 / slot 5 arrives as `02 04 00` and
    must decode to (2, 4). Read big-endian it became 1024, and the console then answered every
    ack with slot 0's record."""
    announce = capture("mems_names.log").tagged("catalog")
    entries = sfl.decode_announce(announce.data)
    memories = [selector for obj_type, selector in entries if obj_type == 0x00]
    assert memories == list(EXPECTED_MEMORIES)


def test_announce_selector_keeps_name_table_pages(capture):
    """type=0x09's selector is the name-table page in the first half, second half 0."""
    announce = capture("mems_names.log").tagged("catalog")
    entries = sfl.decode_announce(announce.data)
    pages = [selector for obj_type, selector in entries if obj_type == 0x09]
    assert pages == [(page, 0) for page in range(74)]


def test_each_reply_carries_its_own_page_slot_and_name(capture):
    replies = capture("mems_names.log").of_type(0x00)
    decoded = {(page, slot): lines
               for page, slot, lines in map(sfl.decode_0x00_memory_name, (m.data for m in replies))}
    assert decoded == EXPECTED_MEMORIES


def test_memory_name_rejects_data_without_the_name_tag():
    assert sfl.decode_0x00_memory_name(bytes(40)) is None
    assert sfl.decode_0x00_memory_name(bytes(10)) is None
