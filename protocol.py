#!/usr/bin/env python3
"""
Shared low-level protocol library for talking to the console over USB, reverse-engineered from
captured SmartSoft <-> console traffic. Used by both listen.py (a terminal live printer) and
server.py (the web app) -- neither re-derives any of this, both just import it.

Opens the console over USB, claims the bulk vendor interface (interface 1), and speaks the
wire protocol reverse-engineered from captured SmartSoft <-> console traffic:

  - Idle poll: a 12-byte header, 6x uint16 LE (msgType, payloadLen, state[4]),
    sent OUT with msgType=0, payloadLen=0, state=[0,0,0,0]. NOT msgType=2 --
    a client that only sends msgType=2 gets zero replies.
  - The console replies with a 12-byte header too. If payloadLen>0, a second
    read on the same endpoint fetches exactly that many payload bytes.
  - When a control's value changes, the console first sends an "announce"
    (payload type=0x28) listing which object type(s) are ready (e.g.
    type=0x0e for faders/Bumps/Master, type=0x11 for the crossfaders). The
    host must ACK by sending one OUT header with msgType=2 and
    state[0]=<the announced type value>, followed by a plain msgType=0 poll
    -- only then does the console reply with the real data (payload type
    matching what was announced, wrapped in an IN header with msgType=3).
    This ack step is NOT optional -- skipping it means the announce repeats
    forever and the real value is never delivered.
  - `ConsoleLink.request_type()` gets the same data on demand instead of
    waiting for an announce -- works for every type both apps request at
    connect (0x0e/0x17/0x0f/0x0c/0x16).

Decodes:
  - type=0x0e (99 bytes): Fader 1-24, Bumps, Master (confirmed against real
    hardware across three separate captures). Mode-agnostic -- reports
    whatever's on a fader regardless of which fader mode is active: a
    fader's raw value means different things (an INT A level, an INT B
    level, a device intensity, ...) depending on type=0x17 below.
  - type=0x0f (75 bytes): the stored intensity table -- unlike 0x0e (which
    only ever reports whichever fader mode is physically active), this holds
    all three intensity sub-mode banks (INT A/INT B/INT DEV) at once --
    `[header:1][INT A: 24][INT B: 24][INT DEV: 24]`.
  - type=0x11 (209 bytes): Crossfader Live, Crossfader Next.
  - type=0x15 (81 bytes): the console's two physical LCDs, verbatim ASCII --
    1 marker byte + 80 characters (2 displays x 2 lines x 20 chars). This is
    also currently the only way to see the 3 wheels' live values -- they
    don't touch type=0x0e/0x11 at all, only this LCD text and a
    correlated-but-undecoded type=0x16.
  - type=0x16 (637 bytes, mostly undecoded): bytes 517-528 are two adjacent
    6-byte Solo/BlackOut indicator blocks. Bytes `1+6*(N-1)` onward hold
    fader N's Bump LED as two RGB triples (the two blink phases) -- green in
    INT/PARAM modes, red in MEMS; see decode_0x16_bump_catch.
  - type=0x17 (7 bytes): the fader-mode selector, all six modes (INT A/INT
    B/INT DEV/PARAM 1/PARAM 2/MEMS) -- only fires unprompted on a mode
    CHANGE, but request_type() gets the true current mode on demand
    regardless.
  - type=0x18 (variable): the Device/Palette-Select row's resulting
    selection state as `[count][ids...]`, not a raw button code.
  - type=0x0c (85 bytes): Independent 1/2 name + on/off state.
  - Anything else: not decoded here (most buttons, wheels' raw deltas,
    curves, names/groups/cues).
"""

import struct
import sys

import usb.core
import usb.util

VENDOR_ID = 0x14D5
PRODUCT_ID = 0x0201

HEADER_LEN = 12
IO_TIMEOUT_MS = 200

# Message types confirmed to exist on the wire (from pcap captures and/or live sessions) but
# not yet decoded -- not fader/crossfader data, safe to ignore for fader testing.
# 0x00: constant ~195-byte payload, unrelated to any specific control (seen live, not yet in
#       any pcap capture) -- possibly a periodic status/heartbeat block.
# 0x0d: correlated sibling of 0x0e, ~1026 bytes -- still not decoded (0x0f, its other sibling,
#       IS decoded: it's the same per-slot intensity data as 0x0d but for all three intensity
#       sub-modes at once, see decode_0x0f_all_modes -- 0x0d is presumably a
#       similarly richer/differently-scaled version, not yet worked out).
# 0x10: ~1026 bytes, same data[0]=0x04 header convention as 0x0d -- likely a sibling of 0x11
#       (crossfaders) the same way 0x0d is a sibling of 0x0e. Seen live, not yet in a pcap capture.
# 0x16: ~637-642 byte full-table dump, correlated with wheel moves and occasional full
#       refreshes; likely a live RGB-ish color-preview value, mostly not decoded -- type=0x15
#       already gives an exact, plain-text readout of whatever a wheel is adjusting.
#       EXCEPTIONS: bytes 517-528 are decoded (decode_0x16_indicators) -- two adjacent 6-byte
#       Solo/BlackOut indicator blocks (Solo also has a confirmed distinct "blinking" pattern,
#       not yet wired into decode_0x16_indicators). Each fader's 6-byte Bump-LED block (two RGB
#       triples, decode_0x16_bump_catch) lives at `1+6*(N-1)`.
KNOWN_UNDECODED_TYPES = {0x00, 0x0d, 0x10}


def find_bulk_interface(dev):
    """Find the (interface_number, alt_setting, ep_in, ep_out) with two bulk endpoints."""
    for cfg in dev:
        for intf in cfg:
            ep_in = ep_out = None
            for ep in intf:
                xfer_type = usb.util.endpoint_type(ep.bmAttributes)
                if xfer_type != usb.util.ENDPOINT_TYPE_BULK:
                    continue
                if usb.util.endpoint_direction(ep.bEndpointAddress) == usb.util.ENDPOINT_IN:
                    ep_in = ep
                else:
                    ep_out = ep
            if ep_in is not None and ep_out is not None:
                return intf.bInterfaceNumber, intf.bAlternateSetting, ep_in, ep_out
    return None


def pack_header(msg_type, payload_len, state=(0, 0, 0, 0)):
    return struct.pack("<HHHHHH", msg_type, payload_len, *state)


def decode_announce(data):
    """type=0x28 payload: [0x00][count][type,0,0,0,0]*count -- returns list of announced types and selectors.

    Catalog announcements use the four bytes after the type as an item selector, echoed back
    as state[1]/state[2] of the ack header. For type 0x09 the first field is the name-table
    page number; for type 0x00 (MEMS memory names) it's (page, slot), both 0-indexed.

    The two fields have DIFFERENT byte orders on the wire: the first is big-endian, the
    second little-endian. Confirmed for the second field by 0x00's page-3/slot-5 memory,
    announced as `02 04 00` after the page's high byte, and acked by real SmartSoft as
    state[2]=4 (trace_fresh_start.pcapng acks slots 1-23 the same way) -- reading it
    big-endian gave 1024, so the console answered every slot with slot 0's record.
    """
    if len(data) < 2:
        return []
    count = data[1]
    entries = []
    for i in range(count):
        off = 2 + 5 * i
        if off + 5 <= len(data):
            selector = (struct.unpack_from(">H", data, off + 1)[0],
                        struct.unpack_from("<H", data, off + 3)[0])
            entries.append((data[off], selector))
    return entries

def decode_0x0e(data):
    """type=0x0e (99 bytes): Fader 1-24 (duplicated pair), Bumps=data[97], Master=data[98]."""
    if len(data) < 99:
        return {}
    values = {}
    for n in range(1, 25):
        off = 1 + 4 * (n - 1)
        if data[off] or data[off + 1]:
            values[f"Fader{n}"] = data[off]
    if data[97]:
        values["Bumps"] = data[97]
    if data[98]:
        values["Master"] = data[98]
    return values

def decode_0x0e_full(data):
    """Full snapshot decode of type=0x0e: ALL 24 fader values (zero included) + bumps + master.

    decode_0x0e() above omits zero entries, which suits a terminal printer that only shows
    what changed. But the message is a full snapshot every time, not a delta -- a stateful
    client needs all 26 values back so it can replace its whole state each message, rather than
    merging present keys and leaving a fader that moved to 0 stuck at its last nonzero value.
    """
    if len(data) < 99:
        return None
    faders = [data[1 + 4 * n] for n in range(24)]
    return faders, data[97], data[98]

def decode_0x11(data):
    """type=0x11 (209 bytes): CrossfaderLive=data[195], CrossfaderNext=data[196]."""
    if len(data) < 197:
        return {}
    values = {}
    if data[195]:
        values["CrossfaderLive"] = data[195]
    if data[196]:
        values["CrossfaderNext"] = data[196]
    return values


def decode_0x15(data):
    """type=0x15 (81 bytes): [marker byte] + 80 ASCII chars = both LCDs, 2 lines x 20 chars each."""
    if len(data) < 81:
        return None
    text = data[1:81].decode("ascii", errors="replace")
    lines = [text[i:i + 20] for i in range(0, 80, 20)]
    return lines


INTENSITY_SUBMODE_NAMES = {0: "INT A", 1: "INT B", 2: "INT DEV"}


def decode_0x17(data):
    """type=0x17 (7 bytes): the fader-mode selector. Only fires unprompted on a mode change;
    use request_type() for the current mode on demand.

    Two-field encoding:
      data[0]: family -- 0 = intensity (INT A/B/INT DEV), 1 = MEMS,
               2 = PARAM (device parameters)
      data[4]: sub-mode within the intensity family (0/1/2 = INT A/INT B/
               INT DEV) -- stale/meaningless once data[0] != 0
      data[3]: sub-mode within the PARAM family (0 = PARAM 1, 1 = PARAM 2)
               -- stale/meaningless once data[0] != 2
      data[2]: the currently selected MEMS page (see decode_0x17_mems_page below) --
               stale/meaningless once data[0] != 1
    """
    if len(data) < 5:
        return None
    family = data[0]
    if family == 0:
        return INTENSITY_SUBMODE_NAMES.get(data[4], f"unknown intensity submode({data[4]})")
    if family == 1:
        return "MEMS"
    if family == 2:
        return "PARAM 1" if data[3] == 0 else "PARAM 2"
    return f"unknown family({family})"


def decode_0x17_mems_page(data):
    """type=0x17, MEMS family only (data[0] == 1): the currently selected memory page,
    1-indexed to match the console's own LCD text ("Memory page:1" / "Bump 1-12 to change",
    type=0x15, shown only while the MEMS button is physically held down). `data[2]` is the
    0-indexed page number, and reflects the console's persisted current page, not just an
    ephemeral "while held" value.
    Meaningless/stale outside the MEMS family -- callers should only read this when
    decode_0x17(data) == "MEMS".

    Returns the 1-indexed page number, or None if data is too short.
    """
    if len(data) < 3:
        return None
    return data[2] + 1


def decode_0x18(data):
    """type=0x18 (variable): [count][id, id, ...] = resulting Device/Palette-Select selection."""
    if len(data) < 1:
        return None
    count = data[0]
    return list(data[1:1 + count])


def decode_0x09_labels(data):
    """Decode the per-item name table for INT A / INT B / INT DEV / Independents.

    The console sends a series of 39-byte records, each structured as:
      page: index 0, then tag 0x03 0x06 + 1 pad byte, then a UTF-16LE 3-line label:
      line1[12] (6 chars), line2[12] (6 chars), line3[11] (5 chars + 1 dangling byte --
      the record is a byte short of a full 6-char third line, so the console's own firmware
      silently truncates a 6-character third word to 5, e.g. "Orange" -> "Orang". This is a
      genuine one-byte-short field in the wire format, not a decode bug on this end -- matches
      what the real console's own display shows.
    The page number maps to the 74 positional entries of the console's item model:
      0-23   IntA1-24
      24-47  IntB1-24
      48-71  Device1-24
      72-73  Independent1-2
    Returns a dict keyed by item family, with values of {index: [line1, line2, line3]} --
    the label's 3 lines exactly as laid out on the wire (and on the console's own display),
    not collapsed into one string, so a caller can render them on their own 3 rows the way
    the console does rather than word-wrapping a joined string to a different width.
    """
    if len(data) < 39:
        return {"INT A": {}, "INT B": {}, "INT DEV": {}, "Independent": {}}

    result = {"INT A": {}, "INT B": {}, "INT DEV": {}, "Independent": {}}
    if len(data) % 39:
        data = data[:len(data) - (len(data) % 39)]

    for offset in range(0, len(data), 39):
        record = data[offset:offset + 39]
        if len(record) < 39:
            continue
        page = record[0]
        if page < 0 or page > 73:
            continue

        line1 = record[4:16].decode("utf-16-le", errors="ignore").replace("\uffff", "").rstrip("\x00\ufffd").strip()
        line2 = record[16:28].decode("utf-16-le", errors="ignore").replace("\uffff", "").rstrip("\x00\ufffd").strip()
        line3 = record[28:39].decode("utf-16-le", errors="ignore").replace("\uffff", "").rstrip("\x00\ufffd").strip()
        if not (line1 or line2 or line3):
            continue

        if 0 <= page <= 23:
            family = "INT A"
            index = page + 1
        elif 24 <= page <= 47:
            family = "INT B"
            index = page - 24 + 1
        elif 48 <= page <= 71:
            family = "INT DEV"
            index = page - 48 + 1
        else:
            family = "Independent"
            index = page - 72 + 1

        result[family][index] = [line1, line2, line3]
    return result


def decode_0x0f_all_modes(data):
    """type=0x0f (75 bytes): the stored intensity table. Unlike type=0x0e (mode-agnostic --
    only ever reports whichever fader mode is CURRENTLY active), this holds all three
    intensity sub-mode banks simultaneously. Layout: `[header:1][INT A: 24][INT B: 24]
    [INT DEV: 24][+2 more bytes, likely Bumps/Master -- not decoded here since type=0x0e
    already covers those]`.
    Returns {"INT A": [24 raw intensity values], "INT B": [...], "INT DEV": [...]}, or
    None if data is too short.
    """
    if len(data) < 73:
        return None
    order = ("INT A", "INT B", "INT DEV")
    return {order[m]: [data[1 + m * 24 + n] for n in range(24)] for m in range(3)}


def decode_0x00_memory_name(data):
    """type=0x00, connect-time only: one MEMS memory ("Look")'s name, plus (not decoded here)
    a variable-length body carrying that memory's own recorded content.

    `data[0]` is the 0-indexed MEMS page and `data[1]` the 0-indexed slot within it (see
    decode_0x17_mems_page) -- confirmed as a genuine two-field split, not a single combined
    index.

    The connect-time catalog announces one type=0x00 entry per recorded memory, with (page,
    slot) as the announce selector (see decode_announce), and the reply carries that same
    page/slot here. A slot never announced has no memory recorded.

    The name sits in the LAST 38 bytes of the message, tail-anchored regardless of overall
    message length (not a fixed absolute offset -- the body before it is presumably that
    memory's own scene data, scaling with complexity): `[tag: 0x03 0x06][pad: 0x00]
    [line1: 12-byte UTF-16LE][line2: 12-byte UTF-16LE][line3: 11-byte UTF-16LE]` -- the same
    tag and 3-line (6/6/5-char) shape as decode_0x09_labels, including that decoder's same
    one-byte-short third line.

    Returns (page, slot, [line1, line2, line3]) -- both 0-indexed, lines exactly as laid out
    on the wire, same shape as decode_0x09_labels/decode_0x0c so callers can render/join them
    the same way -- or None if data is too short or the expected tag isn't where it should be
    (rather than silently decoding garbage as a name).
    """
    if len(data) < 40:
        return None
    page = data[0]
    slot = data[1]
    tag_off = len(data) - 38
    if data[tag_off:tag_off + 2] != b"\x03\x06":
        return None
    name = data[tag_off + 3:tag_off + 38]
    line1 = name[0:12].decode("utf-16-le", errors="ignore").replace("￿", "").rstrip("\x00�").strip()
    line2 = name[12:24].decode("utf-16-le", errors="ignore").replace("￿", "").rstrip("\x00�").strip()
    line3 = name[24:35].decode("utf-16-le", errors="ignore").replace("￿", "").rstrip("\x00�").strip()
    return page, slot, [line1, line2, line3]


_0X0C_MARKER = bytes.fromhex("0001030600")


def decode_0x0c(data):
    """type=0x0c: Independent 1/2 name + click-state + live value table.

    Each entry is `[clicked: 0x00/0x01][value: 0-255][marker: 00 01 03 06 00][name: 3 lines,
    same 12+12+11-byte layout as type=0x09's NamedItem]` -- 42 bytes per entry, found by
    marker-scanning (degrades gracefully if there are ever more than 2 entries). `clicked` is
    a genuinely separate bit from `value` -- "is this Independent's button currently
    held/latched on" is independent from "what level is it set to". Entries appear in physical
    order (1st = IND 1, 2nd = IND 2). The name is 3 fixed-width lines, not a variable-length
    run bounded by the next marker.

    Returns e.g. {1: (False, 73, ['Work', 'light', '']), 2: (False, 196, ['', '', ''])} --
    (clicked, raw 0-255 value, [line1, line2, line3] -- same shape as decode_0x09_labels).
    """
    starts = [i for i in range(len(data) - len(_0X0C_MARKER) + 1)
              if data[i:i + len(_0X0C_MARKER)] == _0X0C_MARKER]
    result = {}
    for n, off in enumerate(starts):
        if off < 2:
            continue
        clicked = bool(data[off - 2])
        value = data[off - 1]
        name_start = off + len(_0X0C_MARKER)
        line1 = data[name_start:name_start + 12].decode("utf-16-le", errors="ignore").rstrip("\x00").strip()
        line2 = data[name_start + 12:name_start + 24].decode("utf-16-le", errors="ignore").rstrip("\x00").strip()
        line3 = data[name_start + 24:name_start + 35].decode("utf-16-le", errors="ignore").rstrip("\x00").strip()
        result[n + 1] = (clicked, value, [line1, line2, line3])
    return result


def decode_0x16_indicators(data):
    """type=0x16 (637 bytes, still mostly undecoded): two adjacent 6-byte indicator blocks:
      - offset 517-522: Solo.     `ff ff ff` / `ff ff ff` (RGB white, both halves equal) = on.
      - offset 523-528: BlackOut. `00 00 ff` / `00 00 ff` (RGB blue,  both halves equal) = on.
    Each block is two consecutive 3-byte RGB colors -- the button's own LED is driven by this
    pair, alternating between them at a fixed local rate. A steady (non-blinking) LED is the
    degenerate case where both halves are the same color (`0a 0a 0a` / `0a 0a 0a`, a dim gray,
    is the observed resting/idle color for both indicators).

    "Blinking" is decoded generically as "the two halves of this button's block don't match",
    not from a hardcoded blink-specific byte pattern -- this correctly flags BlackOut blinking
    too (e.g. the console's own "Master pulled down while BlackOut is off" warning) even though
    BlackOut's own blink colors have never been directly observed on the wire; only Solo's has.
    Returns {"solo": "on"|"off"|"blinking"|None, "blackout": ...} -- None only if data is too
    short to contain these offsets at all.
    """
    def read(lo, hi, on_color):
        chunk = data[lo:hi]
        if len(chunk) < hi - lo:
            return None
        mid = lo + (hi - lo) // 2
        half1, half2 = data[lo:mid], data[mid:hi]
        if half1 != half2:
            return "blinking"
        return "on" if half1 == on_color else "off"

    return {
        "solo": read(517, 523, b"\xff\xff\xff"),
        "blackout": read(523, 529, bytes.fromhex("0000ff")),
    }


def decode_0x16_bump_catch(data, fader):
    """type=0x16, per-fader Bump LED. `fader` is 1-indexed (1-24, same convention as
    decode_0x0e's `Fader{N}`).

    Each fader has a 6-byte block at `1+6*(N-1)`: two RGB triples, the LED's two blink
    phases -- the same shape as Solo/BlackOut's blocks (decode_0x16_indicators). The color
    comes from the console itself: green `(0, v, 0)` in INT A/B/DEV and PARAM 1/2, red
    `(v, 0, 0)` in MEMS.

      - At rest / caught: both triples equal, level tracking the fader's live combined
        output 1:1 (INT/PARAM). In MEMS: 0xff while the memory is up, 0x46 for a recorded
        memory at rest, 0x0a for an empty slot.
      - Unlatched after a fader-mode switch (physical position hasn't caught its stored
        value): the triples differ, e.g. green 0x46 / 0x0a -- sent once when blinking
        starts; the alternation is local to the console, not re-sent per flash.

    "Blinking" is decoded generically as "the two triples differ".

    Returns None if data is too short, else {"blinking": True, "color_a": [r, g, b],
    "color_b": [r, g, b]} or {"blinking": False, "color": [r, g, b]}.
    """
    return _rgb_pair(data, 1 + 6 * (fader - 1))


def decode_0x16_indicator_lights(data):
    """type=0x16: Solo/BlackOut's LED colors, the same 6-byte two-RGB-triple blocks that
    decode_0x16_indicators reads as on/off/blinking (offsets 517 / 523). Solo on is white
    `ff ff ff`, BlackOut on is blue `00 00 ff`, BlackOut's blink is `37 37 ff` / `0a 0a 0a`,
    idle is `0a 0a 0a` for both.

    Returns {"solo": ..., "blackout": ...}, each in decode_0x16_bump_catch's shape (None if
    data is too short).
    """
    return {"solo": _rgb_pair(data, 517), "blackout": _rgb_pair(data, 523)}


def _rgb_pair(data, off):
    """A type=0x16 6-byte LED block at `off`: two RGB triples, the LED's two blink phases.
    Returns None if data is too short, else {"blinking": True, "color_a": [r, g, b],
    "color_b": [r, g, b]} if the halves differ, or {"blinking": False, "color": [r, g, b]}.
    """
    if len(data) < off + 6:
        return None
    a, b = list(data[off:off + 3]), list(data[off + 3:off + 6])
    if a != b:
        return {"blinking": True, "color_a": a, "color_b": b}
    return {"blinking": False, "color": a}


class ConsoleLink:
    def __init__(self, dev, ep_in, ep_out):
        self.dev = dev
        self.ep_in = ep_in
        self.ep_out = ep_out
        self.write_errors = 0
        self.read_timeouts = 0

    def write_header(self, msg_type, payload_len=0, state=(0, 0, 0, 0)):
        try:
            self.dev.write(self.ep_out.bEndpointAddress, pack_header(msg_type, payload_len, state),
                            timeout=IO_TIMEOUT_MS)
            return True
        except usb.core.USBError as e:
            self.write_errors += 1
            if self.write_errors <= 5:
                print(f"[write error: {e}]", file=sys.stderr)
            return False

    def read_header(self):
        try:
            header = bytes(self.dev.read(self.ep_in.bEndpointAddress, HEADER_LEN, timeout=IO_TIMEOUT_MS))
        except usb.core.USBError:
            self.read_timeouts += 1
            return None
        if len(header) < HEADER_LEN:
            return None
        msg_type, payload_len = struct.unpack_from("<HH", header, 0)
        state = struct.unpack_from("<HHHH", header, 4)
        return msg_type, payload_len, state

    def read_payload(self, payload_len):
        try:
            return bytes(self.dev.read(self.ep_in.bEndpointAddress, payload_len, timeout=1000))
        except usb.core.USBError as e:
            print(f"[payload read error: {e}]", file=sys.stderr)
            return None

    def decode_in_payload(self, raw):
        """IN payload framing: [seq:2 LE][objLen:2 LE][type:1][objLen bytes of data]."""
        if len(raw) < 5:
            return None, None, b""
        seq = struct.unpack_from("<H", raw, 0)[0]
        obj_len = struct.unpack_from("<H", raw, 2)[0]
        obj_type = raw[4]
        data = raw[5:5 + obj_len]
        return seq, obj_type, data

    def write_payload(self, seq, obj_type, data=b""):
        """OUT payload framing: [seq:1][objLen:2 LE][type:1][data] -- companion to a
        msgType=1 write_header(1, 4 + len(data)) sent just before it (confirmed from real
        SmartSoft traffic)."""
        payload = bytes([seq & 0xFF]) + struct.pack("<H", len(data)) + bytes([obj_type]) + data
        try:
            self.dev.write(self.ep_out.bEndpointAddress, payload, timeout=IO_TIMEOUT_MS)
            return True
        except usb.core.USBError as e:
            self.write_errors += 1
            if self.write_errors <= 5:
                print(f"[write error: {e}]", file=sys.stderr)
            return False

    def send_gui_request(self, seq, subtype):
        """Send a type=0x27 GUI request OUT (msgType=1) -- real SmartSoft sends subtype=0x09
        (version query) then subtype=0x07 (full show-catalog request) right after connecting.
        subtype=0x07's reply is a single huge type=0x28 announce (191 entries) covering the
        static show catalog -- names, groups, cues, curves, current show/firmware info.
        `server.py` still uses subtype=0x07 to fetch the type=0x09 name-table pages -- the rest
        of the fast connect-time state (fader/master/bumps/mode/independents/solo/blackout)
        uses request_type() instead. The reply(ies) aren't read here -- the caller's normal
        poll loop picks them up like any other announce, since the announce/ack handling is
        generic."""
        data = bytes([subtype, 0, 0])
        if not self.write_header(1, 4 + len(data)):
            return False
        return self.write_payload(seq, 0x27, data)

    def request_type(self, type_byte):
        """Proactively ask the console for the current value of a given object type, without
        waiting for it to announce that type first. Same ack sequence normally sent only in
        response to a real type=0x28 announce (msgType=2 with state[0]=type, then a plain
        poll) -- but the console replies with current data even when it never announced this
        type first. This is what both apps use at connect instead of
        relying on the console's own unprompted announce, which loses a race against macOS's
        automatic USB HID driver probing almost every time.
        Returns (obj_type, data), or (None, None) on any failure/timeout.
        """
        if not self.write_header(2, 0, (type_byte, 0, 0, 0)):
            return None, None
        if not self.write_header(0, 0, (type_byte, 0, 0, 0)):
            return None, None
        reply = self.read_header()
        if reply is None:
            return None, None
        _, payload_len, _ = reply
        if payload_len == 0:
            return None, None
        raw = self.read_payload(payload_len)
        if raw is None:
            return None, None
        return self.decode_in_payload(raw)[1:]
