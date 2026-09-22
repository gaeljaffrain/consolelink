#!/usr/bin/env python3
"""
Shared low-level protocol library for talking to the console over USB, reverse-engineered from
captured SmartSoft <-> console traffic. Used by both listen.py (a terminal live printer) and
server.py (the web app) -- neither re-derives any of this, both just import it.

Opens the console over USB, claims the bulk vendor interface (interface 1), and speaks the
real wire protocol confirmed against captured SmartSoft <-> console traffic:

  - Idle poll: a 12-byte header, 6x uint16 LE (msgType, payloadLen, state[4]),
    sent OUT with msgType=0, payloadLen=0, state=[0,0,0,0]. (NOT msgType=2 --
    that was an earlier, incorrect guess; a client that only ever sends
    msgType=2 gets zero replies.)
  - The console replies with a 12-byte header too. If payloadLen>0, a second
    read on the same endpoint fetches exactly that many payload bytes.
  - When a control's value changes, the console doesn't just push the new
    value: it first sends an "announce" (payload type=0x28) listing which
    object type(s) are ready (e.g. type=0x0e for faders/Bumps/Master,
    type=0x11 for the crossfaders). The host must ACK by sending one OUT
    header with msgType=2 and state[0]=<the announced type value>, followed
    by a plain msgType=0 poll -- only then does the console reply with the
    real data (payload type matching what was announced, wrapped in an IN
    header with msgType=3). This ack step is NOT optional -- confirmed from
    real capture traces (the tracec.pcapng excerpt used to work out this
    handshake); skipping it and only ever polling with msgType=0 means
    the announce repeats forever and the real value is never delivered.
  - `ConsoleLink.request_type()` gets the same data on demand instead,
    without waiting for the console to announce it first -- confirmed to
    work for every type tried (0x0e/0x17/0x0f/0x0c/0x16), and is what both
    apps actually use at connect time now.

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
    6-byte Solo/BlackOut indicator blocks. Per-fader 6-byte blocks starting
    at byte 2 (`2+6*(N-1)` / `5+6*(N-1)` for fader N, confirmed for
    N=1/2/24) are each fader's Bump-LED "catch" indicator -- fixed at
    (0x46, 0x0a) while that fader's Bump LED is blinking (unlatched after a
    fader-mode switch), else tracks its live output 1:1.
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
#       not yet wired into decode_0x16_indicators). Each fader's 2-byte Bump-LED blink/catch
#       indicator (decode_0x16_bump_catch) lives at `2+6*(N-1)` / `5+6*(N-1)`.
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

    Catalog announcements use the four bytes after the type as an item selector; for type
    0x09 this is the name-table page number.
    """
    if len(data) < 2:
        return []
    count = data[1]
    entries = []
    for i in range(count):
        off = 2 + 5 * i
        if off + 5 <= len(data):
            # The catalog entry selector is encoded big-endian inside the announce payload,
            # even though the surrounding USB protocol headers use little-endian fields.
            selector = struct.unpack_from(">HH", data, off + 1)
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

    decode_0x0e() above deliberately omits zero entries -- that's the right behaviour for a
    terminal printer that only shows what changed. But type=0x0e is a full
    snapshot every time, not a delta: a fader that's been moved all the way down to 0 legitimately
    has nothing to report for that slot. A stateful client that only applies present keys (as an
    earlier version of this file did) never resets that slot back to 0 -- the bar gets stuck at
    its last nonzero value. This full decode always returns all 26 values so the app can just
    replace the whole snapshot on every message, matching what the message actually represents.
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
    """type=0x17 (7 bytes): the fader-mode selector. Only fires on change.

    Two-field encoding (data[4] was confirmed for the intensity family
    first; a later capture cycling INT A -> INT B -> INT DEV -> PARAM 1 ->
    PARAM 2 -> MEMS -> INT A with nothing else touched, one button per ~2s,
    gave a clean single-bit-diff transition at every step and confirmed the
    rest):
      data[0]: family -- 0 = intensity (INT A/B/INT DEV), 1 = MEMS,
               2 = PARAM (device parameters)
      data[4]: sub-mode within the intensity family (0/1/2 = INT A/INT B/
               INT DEV) -- stale/meaningless once data[0] != 0
      data[3]: sub-mode within the PARAM family (0 = PARAM 1, 1 = PARAM 2)
               -- stale/meaningless once data[0] != 2
      data[2]: the currently selected MEMS page (see decode_0x17_mems_page below) --
               stale/meaningless once data[0] != 1
    MEMS corroborated independently: the console's LCD (type=0x15) shows
    "Memory page:1" the instant data[0] becomes 1.
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
    0-indexed page number -- confirmed live cycling page 1 -> 2 -> 3 -> 4 -> 1
    (traces/mems_switch_pages.log): every LCD "Memory page:N" transition lines up exactly
    with data[2] becoming N-1, across all 4 pages, both while pressing a Bump button to
    change page and on the very next MEMS-held press (i.e. it's the console's persisted
    current page, not just an ephemeral "while held" value).

    Not yet confirmed: whether the per-fader memory NAMES (decode_0x00_memory_name) are
    windowed by this page at all -- no capture has shown fresh name traffic when switching
    pages, and the one show captured so far has nothing named beyond page 1, so there's
    nothing to verify names against on pages 2-4 yet. Meaningless/stale outside the MEMS
    family -- callers should only read this when decode_0x17(data) == "MEMS".

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
      silently truncates a 6-character third word to 5, e.g. "Orange" -> "Orang". Confirmed
      against trace_fresh_start.pcapng's full 74-record table: 10 of 74 records use this third
      line for a color name ("Side"/""/"Orang", "Window"/"Left"/"Cyan", "CYC"/""/"Red", ...),
      and "Orang" for "Orange" is exactly what the real console's own display shows -- not a
      decode bug on this end, a genuine one-byte-short field in the wire format.
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
    intensity sub-mode banks simultaneously. The console has to keep this internally
    regardless -- switching modes instantly redisplays a completely different set of 24
    values, which couldn't happen if the non-active sub-modes' values weren't stored
    somewhere. Layout: `[header:1][INT A: 24][INT B: 24][INT DEV: 24][+2 more bytes,
    likely Bumps/Master -- not decoded here since type=0x0e already covers those]`. Confirmed
    live: set fader 1 to three distinct, known values across all three modes (39%/66%/21% ->
    raw 100/168/53) and found all three at offsets 1, 25, 49 -- exactly 24 apart.
    Returns {"INT A": [24 raw intensity values], "INT B": [...], "INT DEV": [...]}, or
    None if data is too short.
    """
    if len(data) < 73:
        return None
    order = ("INT A", "INT B", "INT DEV")
    return {order[m]: [data[1 + m * 24 + n] for n in range(24)] for m in range(3)}


def decode_0x00_memory_name(data):
    """type=0x00, connect-time only: one MEMS memory ("Look")'s name, plus (not decoded here)
    a variable-length body carrying that memory's own recorded content -- confirmed live
    against real named memories from traces/trace_fresh_start.pcapng: "Look 1", "Green
    Violet", "Fire", "CMY Effect", "Blue wave", "Fanned on Wall", "Circle in front", "Rainbow
    LEDs", "All in Red LEDs", "Rainbow FXs LEDs", "R-G-B effect LEDs" -- verified against the
    console owner's own recollection of these names, not just plausible-looking text.

    `data[0]` is the 0-indexed MEMS page (12 pages exist -- hold the MEMS button, press Bump
    1-12 to pick one, see decode_0x17_mems_page) and `data[1]` is the 0-indexed slot within
    that page. Every record in trace_fresh_start.pcapng/mems_switch_pages.log had data[0]==0
    (all on page 1) with data[1] varying 0-23, which first looked like a single combined
    index -- corrected against traces/mems_names_page1-2-3.log, which named a memory
    "page2 mem1" specifically so its wire position would be self-describing, and it decoded
    at data[0]=1 (0-indexed page 2), data[1]=0 (slot 1, fader 1 -- confirmed against the
    console owner's own account of where they recorded it) -- confirming the two-field split,
    not a single 0-287 index.

    Confirmed limitation (not a decode bug): this connect-time catalog is NOT a full per-slot
    enumeration of every page's 24 memories. The same capture also had a memory recorded on
    page 3 at fader 5 ("page3 mem5", confirmed by the console owner), which never appears
    anywhere in the trace at data[0]=2 -- the only page-3 record present is data[0]=2,
    data[1]=0, and it decodes as genuinely blank/erased (0xff-filled, not 0x00, so unwritten,
    not a parse failure). So the catalog appears to send at most one representative entry per
    visited page (whichever slot happens to be selected/highlighted, defaulting to slot 0
    unless navigated elsewhere), not every named memory on that page. Getting a specific
    slot's real name likely needs that slot actively selected/highlighted on the console
    (e.g. via its own memory-list UI) before/during the capture, the same way the gobo/color
    picker names in an earlier trace only appeared once scrolled into view -- not yet tried.
    Callers should expect this to only ever populate a subset of slots, never assume a miss
    means "no memory recorded there."

    Also not yet resolved: whether the catalog re-sends fresh data when switching pages --
    no capture so far has shown new type=0x00 traffic on a page switch, consistent with the
    (partial) catalog being sent once at connect, not re-fetched per page.

    The name itself sits in the LAST 38 bytes of the message, tail-anchored regardless of
    overall message length (confirmed at message lengths 323/339/355/575/1047/4995, always
    landing the tag at exactly `len(data) - 38`, never a fixed absolute offset -- the body
    before it is presumably that memory's own scene data, scaling with complexity the same way
    a type=0x04 Stack step's body does): `[tag: 0x03 0x06][pad: 0x00][line1: 12-byte UTF-16LE]
    [line2: 12-byte UTF-16LE][line3: 11-byte UTF-16LE]` -- the exact same tag and 3-line
    (6/6/5-char) shape as decode_0x09_labels, including that decoder's same documented
    one-byte-short third line (confirmed here too: "Rainbow LEDs" -> line1 "Rainbo", line2 "w
    FXs"/"w", i.e. the console's own firmware truncates the same way, not a decode bug).

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
    """type=0x0c: Independent 1/2 name + click-state + live value table (corrected against a
    real capture with a non-full value, and again for the name field's real structure).

    Each entry is `[clicked: 0x00/0x01][value: 0-255][marker: 00 01 03 06 00][name: 3 lines,
    same 12+12+11-byte layout as type=0x09's NamedItem]` -- 42 bytes per entry.
    Originally the marker was thought to be 6 bytes (`ff 00 01 03 06 00`) with a single
    boolean byte before it -- that was `capture_ind1.log`'s single on/off press test, which
    coincidentally sat at a "full" value (0xff) the whole time, making the value byte look like
    a constant part of the marker. `traces/capture_ind1_toggle.log` (IND 1 set to 29%, IND 2 to
    77%, both toggled twice) exposed the real, shorter 5-byte marker and confirmed the value
    byte's meaning directly: `73/255 = 29%` and `196/255 = 77%`, exact matches to what was set
    on the console. The `clicked` byte (still 0x00/0x01 in every sample) is a genuinely separate
    bit from the value -- it toggled twice in that capture while the value stayed constant, i.e.
    "is this Independent's button currently held/latched on" is independent from "what level is
    it set to." Entries appear in physical order (1st = IND 1, 2nd = IND 2).

    The name field was originally read as "everything between this marker and the next,
    nulls stripped" -- which happened to produce the right characters but silently dropped the
    space between multi-word names (`capture_ind1_toggle.log`'s IND 1 decoded to "Worklight"
    instead of "Work light", because nothing reinserted a boundary between the fixed-width
    "Work"+pad and "light"+pad sub-fields once the padding nulls were gone). Confirmed directly
    against that capture: IND 2's fields start at exactly byte 43 of the payload -- 1 (clicked)
    + 1 (value) + 5 (marker) + 35 (3-line name) + 1 = 43 into IND 1's own record -- so the name
    really is 3 fixed-width lines like every other named item in this protocol, not a variable-
    length run bounded by the next marker. Marker-scanning still finds each entry's start, so
    this degrades gracefully if there are ever more than 2 entries.

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
    """type=0x16 (637 bytes, still mostly undecoded): two adjacent 6-byte indicator blocks,
    confirmed live against two independent isolated single-button captures
    (traces/capture_solo_blackout.log, cross-checked against traces/capture_ind_blackout_solo.log):
      - offset 517-522: Solo.     `ff ff ff` / `ff ff ff` (RGB white, both halves equal) = on.
      - offset 523-528: BlackOut. `00 00 ff` / `00 00 ff` (RGB blue,  both halves equal) = on.
    Each 6-byte block is two consecutive 3-byte RGB colors -- the button's own LED is
    literally driven by this pair, alternating between them at a fixed local rate. A steady
    (non-blinking) LED is just the degenerate case where both halves happen to be the same
    color (`0a 0a 0a` / `0a 0a 0a`, a dim gray, is the observed resting/idle color for both
    indicators). Confirmed against real bytes (not just the summary above) from
    traces/trace_blink_bump2_24_solo.pcapng: pressing a Bump button while Solo is active blinks
    Solo between its on-color and black, `ff ff ff` / `00 00 00` -- i.e. the two halves
    genuinely differ on the wire exactly while the real console shows that indicator visibly
    flashing, and are equal at every other sample in that capture. The same per-fader Bump-LED
    blink block (`decode_0x16_bump_catch`) fits the identical pattern one level down: its
    confirmed `(0x46, 0x0a)` blink pair is just the R channel of two dim, unequal reds (G/B
    stay 0), vs. equal R when solid/tracking.
    So "blinking" is decoded generically as "the two halves of this button's block don't
    match", not from a hardcoded blink-specific byte pattern -- this correctly flags BlackOut
    blinking too (e.g. the console's own "Master pulled down while BlackOut is off" warning
    blink) even though BlackOut's own blink colors have never been directly observed on the
    wire; only Solo's blink has (see above). Returns {"solo": "on"|"off"|"blinking"|None,
    "blackout": ...} -- None only if data is too short to contain these offsets at all.
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
    """type=0x16, per-fader Bump LED for a fader that's "unlatched" after a fader-mode switch
    -- i.e. its saved logical value doesn't match the physical fader position, so moving it has
    no effect on output until the physical position catches up. `fader` is 1-indexed (1-24,
    same convention as decode_0x0e's `Fader{N}`).

    Reads only the R byte of each of the block's two halves -- `data[2+6*(fader-1)]` and
    `data[5+6*(fader-1)]` -- deliberately, NOT the full 3-byte RGB half each offset starts
    (`decode_0x16_indicators`'s Solo/BlackOut blocks are genuinely 3-byte-RGB pairs, and an
    earlier version of this function assumed the same 6-byte shape here). Live capture found
    that assumption wrong at the boundary: for fader 24 specifically, the trailing byte of its
    would-be 6-byte block (offset 145 in traces/capture_webapp_startup.log, t=12.861) is `0x63`
    -- not fader 24's own idle `0x00`, but the leading byte of a completely different, adjacent
    24-button LED region (matches the `0x63 63 63...` run in
    traces/trace_24buttons_top_device_palette_select.pcapng), which made fader 24 register as
    permanently "blinking" at rest for no on-console reason. Only the R byte at each half has
    ever been directly confirmed to track a fader's own live value, across three separate
    faders at three different offsets (fader 1 -> 2/5, fader 2 -> 8/11, fader 24 -> 140/143 --
    traces/trace_fader0_wiggle_then_catch.pcapng and trace_blink_bump2_24_solo.pcapng):

      - While unlatched (console shows the Bump LED blinking, visibly dim rather than a hard
        on/off flash -- confirmed by eye against real hardware): both R bytes sit at a fixed,
        unequal pair, `(0x46, 0x0a)` -- two dim reds -- regardless of how long the wiggling
        goes on, how far the physical fader actually moves, or *which* fader it is (identical
        constant for faders 1, 2 and 24 alike). Read as the blink's two displayed brightness
        levels (matches the observed dim pulsing far better than a literal on/off pair would),
        sent once as a pair when blinking starts -- the alternation itself is handled locally,
        not re-sent per flash.
      - The instant the fader is physically caught, both R bytes start tracking the fader's own
        live combined output 1:1 (matches the concurrent type=0x0e reading step for step across
        a full 246->0 ramp down to 0, each type=0x16 arriving a few ms after the type=0x0e it
        mirrors).
      - At rest (fader untouched, value unchanged), both R bytes equal the fader's steady value
        (e.g. `(0xff, 0xff)` while resting at 255/full) -- indistinguishable on the wire from
        "just caught," since both just mean "LED solid, showing this value."

    "Blinking" is decoded generically as "the two R bytes don't match" -- the same rule
    `decode_0x16_indicators` uses for its full RGB halves, applied here to just the confirmed
    byte -- rather than comparing against the single confirmed blink-pair constant. Every trace
    sample seen so far is either exactly `(0x46, 0x0a)` or the two bytes being equal; this
    generalization additionally treats any other unequal pair as blinking too, which has never
    actually been observed on the wire.

    This byte is brightness only, not a color -- there is no confirmed evidence the fader Bump
    LED block is genuine RGB the way Solo/BlackOut's blocks are. (An earlier version of this
    function read the full would-be 6-byte RGB half and reported it as red, i.e. assumed
    G=B=0; that was never actually confirmed either -- it just happened to match every solid
    reading seen so far -- and reading the full 6 bytes is what caused the fader-24 boundary
    bug above.) Per the console's owner, checking against real hardware: the Bump LED is green
    in every fader mode except MEMS, where it's red -- i.e. the console picks the LED's hue
    locally from the active fader mode, not from anything sent per-fader on the wire. Callers
    should derive the display color from the currently known `fader_mode` (type=0x17) and use
    this function's value purely as brightness.

    Returns None if data is too short, else {"blinking": True, "value_a": 0-255,
    "value_b": 0-255} while unlatched, or {"blinking": False, "value": 0-255} once
    latched/at rest.
    """
    off = 2 + 6 * (fader - 1)
    if len(data) < off + 4:
        return None
    a, b = data[off], data[off + 3]
    if a != b:
        return {"blinking": True, "value_a": a, "value_b": b}
    return {"blinking": False, "value": a}


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
        subtype=0x07's reply is a single huge type=0x28 announce (191 entries in the traces
        this was found from) covering the static show catalog -- names, groups, cues, curves,
        current show/firmware info, and a few pieces of current control state
        (Independents/Solo/BlackOut/fader mode/selection) that used to be sent this way before
        request_type() turned out to reach those directly and much faster. Kept here as a
        working, tested way to reach the *rest* of the catalog (names/groups/cues/curves) for
        whenever that's actually needed -- not part of either app's default startup flow
        anymore. The reply(ies) aren't read here -- the caller's normal poll loop picks them
        up exactly like any other announce, since the announce/ack handling is generic."""
        data = bytes([subtype, 0, 0])
        if not self.write_header(1, 4 + len(data)):
            return False
        return self.write_payload(seq, 0x27, data)

    def request_type(self, type_byte):
        """Proactively ask the console for the current value of a given object type, without
        waiting for it to announce that type first. Same ack sequence normally sent only in
        response to a real type=0x28 announce (msgType=2 with state[0]=type, then a plain
        poll) -- but the console replies with current data even when it never announced this
        type first. Confirmed live for type=0x0e specifically: this is what actually solves
        "fader/master/bumps don't show up without touching a control" -- reliably, without
        depending on winning the OS-level race that makes the console's own unprompted
        announce unreliable on macOS.
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
