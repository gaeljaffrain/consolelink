"""Channel names (type=0x09, one 39-byte record per item) and Independents (type=0x0c)."""
import protocol as sfl


def name_table(capture):
    table = {}
    for message in capture("labels.log").of_type(0x09):
        for family, entries in sfl.decode_0x09_labels(message.data).items():
            table.setdefault(family, {}).update(entries)
    return table


def test_name_table_families_and_positions(capture):
    table = name_table(capture)
    assert table["INT A"][1] == ["Front", "light", ""]
    assert table["INT A"][15] == ["CYC", "", "Blue"]
    assert table["INT DEV"][1] == ["Revo", "Left", ""]
    assert table["INT DEV"][24] == ["LED", "Par", ""]


def test_third_line_is_one_character_short(capture):
    """The record is a byte short of a full third line, so the console itself truncates a
    6-character third word ("Orange" shows as "Orang" on its own display too)."""
    assert name_table(capture)["INT A"][5] == ["Side", "", "Orang"]


def test_independents_at_rest(capture):
    entries = sfl.decode_0x0c(capture("independents.log").tagged("both_off").data)
    assert entries == {1: (False, 73, ["Work", "light", ""]), 2: (False, 196, ["", "", ""])}


def test_independent_click_is_separate_from_its_level(capture):
    ind1 = sfl.decode_0x0c(capture("independents.log").tagged("ind1_on").data)
    ind2 = sfl.decode_0x0c(capture("independents.log").tagged("ind2_on").data)
    assert (ind1[1][:2], ind1[2][:2]) == ((True, 73), (False, 196))
    assert (ind2[1][:2], ind2[2][:2]) == ((False, 73), (True, 196))
