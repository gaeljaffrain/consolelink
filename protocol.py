#!/usr/bin/env python3
"""
Shared low-level protocol library for talking to the console over USB, reverse-engineered in
../SmartFade_SmartSoft_reverse_engineering_notes.md. Used by both listen.py (a terminal live
printer) and server.py (the web app) -- neither re-derives any of this, both just import it.

Opens the console over USB, claims the bulk vendor interface (interface 1), and speaks the
real wire protocol confirmed against captured SmartSoft <-> console traffic:

  - Idle poll: a 12-byte header, 6x uint16 LE (msgType, payloadLen, state[4]),
    sent OUT with msgType=0, payloadLen=0, state=[0,0,0,0]. (NOT msgType=2 --
    that was an earlier, incorrect guess; a client that only ever sends
    msgType=2 gets zero replies, matching the original failed experiment in
    Part 4 of the RE notes.)
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
    real capture traces (RE notes, the tracec.pcapng excerpt used to work out
    this handshake); skipping it and only ever polling with msgType=0 means
    the announce repeats forever and the real value is never delivered.
  - `ConsoleLink.request_type()` gets the same data on demand instead,
    without waiting for the console to announce it first (RE notes Part 31)
    -- confirmed to work for every type tried (0x0e/0x17/0x0f/0x0c/0x16),
    and is what both apps actually use at connect time now (Part 34).

Decodes:
  - type=0x0e (99 bytes): Fader 1-24, Bumps, Master (Part 8-9, confirmed
    against real hardware across three separate captures). Mode-agnostic --
    reports whatever's on a fader regardless of which fader mode is active
    (Part 15): a fader's raw value means different things (an INT A level,
    an INT B level, a device intensity, ...) depending on type=0x17 below.
  - type=0x0f (75 bytes): unlike 0x0e, holds all three fader-mode banks at
    once (Part 33) -- `[header:1][INT A: 24][INT B: 24][DEVICE INT: 24]`.
  - type=0x11 (209 bytes): Crossfader Live, Crossfader Next (Part 9).
  - type=0x15 (81 bytes): the console's two physical LCDs, verbatim ASCII --
    1 marker byte + 80 characters (2 displays x 2 lines x 20 chars). This is
    also currently the only way to see the 3 wheels' live values (Part 11) --
    they don't touch type=0x0e/0x11 at all, only this LCD text and a
    correlated-but-undecoded type=0x16.
  - type=0x16 (637 bytes, mostly undecoded): bytes 517-528 are two adjacent
    6-byte Solo/BlackOut indicator blocks (Part 29). Per-fader 6-byte blocks
    starting at byte 2 (`2+6*(N-1)` / `5+6*(N-1)` for fader N, confirmed for
    N=1/2/24, Part 37-38) are each fader's Bump-LED "catch" indicator --
    fixed at (0x46, 0x0a) while that fader's Bump LED is blinking (unlatched
    after a fader-mode switch), else tracks its live output 1:1.
  - type=0x17 (7 bytes): the fader-mode selector (INT A/INT B/DEVICE INT,
    Part 15) -- only fires unprompted on a mode CHANGE, but request_type()
    gets the true current mode on demand regardless (Part 32).
  - type=0x18 (variable): the Device/Palette-Select row's resulting
    selection state as `[count][ids...]`, not a raw button code (Part 13).
  - type=0x0c (85 bytes): Independent 1/2 name + on/off state (Part 28).
  - Anything else: not decoded here (most buttons, wheels' raw deltas,
    curves, names/groups/cues -- see the RE notes for what's been tried).
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
# not yet decoded -- not fader/crossfader data, safe to ignore for fader testing. See "Known
# vs unknown" sections of the RE notes (Parts 6-9) for what's been tried.
# 0x00: constant ~195-byte payload, unrelated to any specific control (seen live, not yet in
#       any pcap capture) -- possibly a periodic status/heartbeat block.
# 0x0d: correlated sibling of 0x0e (Part 7 Sec.53), ~1026 bytes -- still not decoded (0x0f,
#       its other sibling, IS decoded: it's the same per-fader data as 0x0d but for all three
#       fader modes at once rather than just the active one, see decode_0x0f_all_modes / Part 33
#       -- 0x0d is presumably a similarly richer/differently-scaled version, not yet worked out).
# 0x10: ~1026 bytes, same data[0]=0x04 header convention as 0x0d -- likely a sibling of 0x11
#       (crossfaders) the same way 0x0d is a sibling of 0x0e. Seen live, not yet in a pcap capture.
# 0x16: ~637-642 byte full-table dump, correlated with wheel moves and occasional full
#       refreshes; likely a live RGB-ish color-preview value (Part 11 Sec.71), mostly not
#       decoded -- type=0x15 already gives an exact, plain-text readout of whatever a wheel is
#       adjusting. EXCEPTIONS: bytes 517-528 are decoded (decode_0x16_indicators, Part 29) --
#       two adjacent 6-byte Solo/BlackOut indicator blocks (Solo also has a confirmed distinct
#       "blinking" pattern, Part 38, not yet wired into decode_0x16_indicators). Each fader's
#       2-byte Bump-LED blink/catch indicator (decode_0x16_bump_catch, Part 37-38) lives at
#       `2+6*(N-1)` / `5+6*(N-1)`.
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

    sfl.decode_0x0e (in ../smartfade_listen.py) deliberately omits zero entries -- that's the
    right behaviour for its terminal printer (only print what changed). But type=0x0e is a full
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


FADER_MODE_NAMES = {0: "INT A", 1: "INT B", 2: "DEVICE INT"}


def decode_0x17(data):
    """type=0x17 (7 bytes): data[4] = fader mode being switched to (Part 15). Only fires on change."""
    if len(data) < 5:
        return None
    mode = data[4]
    return FADER_MODE_NAMES.get(mode, f"unknown({mode})")


def decode_0x18(data):
    """type=0x18 (variable): [count][id, id, ...] = resulting Device/Palette-Select selection (Part 13)."""
    if len(data) < 1:
        return None
    count = data[0]
    return list(data[1:1 + count])


def decode_0x09_labels(data):
    """Decode the per-item name table for INT A / INT B / DEVICE INT / Independents.

    The console sends a series of 39-byte records, each structured as:
      page: index 0, then tag 0x03 0x06, then a UTF-16LE 2-line label.
    The page number maps to the 74 positional entries of the console's item model:
      0-23   IntA1-24
      24-47  IntB1-24
      48-71  Device1-24
      72-73  Independent1-2
    Returns a dict keyed by item family, with values of {index: label}.
    """
    if len(data) < 39:
        return {"INT A": {}, "INT B": {}, "DEVICE INT": {}, "Independent": {}}

    result = {"INT A": {}, "INT B": {}, "DEVICE INT": {}, "Independent": {}}
    if len(data) % 39:
        data = data[:len(data) - (len(data) % 39)]

    for offset in range(0, len(data), 39):
        record = data[offset:offset + 39]
        if len(record) < 28:
            continue
        page = record[0]
        if page < 0 or page > 73:
            continue

        line1 = record[4:16].decode("utf-16-le", errors="ignore").replace("\uffff", "").rstrip("\x00\ufffd").strip()
        line2 = record[16:28].decode("utf-16-le", errors="ignore").replace("\uffff", "").rstrip("\x00\ufffd").strip()
        label = " ".join(part for part in (line1, line2) if part).strip()
        if not label:
            continue

        if 0 <= page <= 23:
            family = "INT A"
            index = page + 1
        elif 24 <= page <= 47:
            family = "INT B"
            index = page - 24 + 1
        elif 48 <= page <= 71:
            family = "DEVICE INT"
            index = page - 48 + 1
        else:
            family = "Independent"
            index = page - 72 + 1

        result[family][index] = label
    return result


def decode_0x0f_all_modes(data):
    """type=0x0f (75 bytes): unlike type=0x0e (mode-agnostic -- only ever reports whichever
    fader mode is CURRENTLY active, Part 15), this holds all three fader-mode banks
    simultaneously. The console has to keep this internally regardless -- switching modes
    instantly redisplays a completely different set of 24 values, which couldn't happen if
    the non-active modes' values weren't stored somewhere. Layout: `[header:1][INT A: 24]
    [INT B: 24][DEVICE INT: 24][+2 more bytes, likely Bumps/Master -- not decoded here since
    type=0x0e already covers those]`. Confirmed live: set fader 1 to three distinct, known
    values across all three modes (39%/66%/21% -> raw 100/168/53) and found all three at
    offsets 1, 25, 49 -- exactly 24 apart (RE notes Part 33).
    Returns {"INT A": [24 raw values], "INT B": [...], "DEVICE INT": [...]}, or None if data
    is too short.
    """
    if len(data) < 73:
        return None
    order = ("INT A", "INT B", "DEVICE INT")
    return {order[m]: [data[1 + m * 24 + n] for n in range(24)] for m in range(3)}


_0X0C_MARKER = bytes.fromhex("ff0001030600")


def decode_0x0c(data):
    """type=0x0c: Independent 1/2 name + on/off state table (RE notes Part 28).

    Confirmed live against an isolated single-button test (traces/capture_ind1.log): a solo
    type=0x28 announce of 0x0c fires only on an Independent button press, nothing else. Each
    entry is `[state: 0x00/0x01][marker: ff 00 01 03 06 00][name, UTF-16LE]`; the byte right
    before the marker flips 0x00<->0x01 exactly tracking that Independent's on/off state.
    Entries appear in physical order (1st = IND 1, 2nd = IND 2). The name field's exact
    fixed-width layout isn't pinned down -- decoding here just strips embedded NULs, which
    may collapse an intentional space (the test console's IND 1 decoded to "Worklight", which
    could be "Work light" with the gap lost). Marker-scanning rather than fixed offsets, so
    it degrades gracefully if there are ever more than 2 entries.

    Returns e.g. {1: (True, 'Worklight'), 2: (False, '')}.
    """
    starts = [i for i in range(len(data) - len(_0X0C_MARKER) + 1)
              if data[i:i + len(_0X0C_MARKER)] == _0X0C_MARKER]
    result = {}
    for n, off in enumerate(starts):
        if off < 1:
            continue
        state = bool(data[off - 1])
        name_start = off + len(_0X0C_MARKER)
        name_end = (starts[n + 1] - 1) if n + 1 < len(starts) else len(data)
        name_raw = data[name_start:name_end if name_end > name_start else name_start]
        if len(name_raw) % 2:
            name_raw = name_raw[:-1]
        name = "".join(ch for ch in name_raw.decode("utf-16-le", errors="ignore") if ch.isprintable())
        result[n + 1] = (state, name)
    return result


def decode_0x16_indicators(data):
    """type=0x16 (637 bytes, still mostly undecoded -- Part 11 Sec.71): two adjacent 6-byte
    indicator blocks, confirmed live against two independent isolated single-button captures
    (traces/capture_solo_blackout.log, cross-checked against traces/capture_ind_blackout_solo.log;
    RE notes Part 29 Sec.122-124):
      - offset 517-522: Solo.     `ff`*6 = on, anything else = off.
      - offset 523-528: BlackOut. `00 00 ff 00 00 ff` = on, anything else = off.
    "Anything else" (not just the specific `0a`*6 resting/filler value seen in every live
    toggle test so far) counts as off: a console that's never had this button touched since
    power-on could plausibly rest at some other byte pattern never captured live, and there's
    no evidence either indicator has more than two real states (RE notes Part 32). Everything
    else in this 637-byte payload remains undecoded. Returns
    {"solo": bool|None, "blackout": bool|None} -- None only if data is too short to contain
    these offsets at all.
    """
    def read(lo, hi, on_pattern):
        chunk = data[lo:hi]
        if len(chunk) < hi - lo:
            return None
        return chunk == on_pattern

    return {
        "solo": read(517, 523, b"\xff" * 6),
        "blackout": read(523, 529, bytes.fromhex("0000ff0000ff")),
    }


_0X16_BLINK_PAIR = (0x46, 0x0A)


def decode_0x16_bump_catch(data, fader):
    """type=0x16, per-fader 6-byte block (RE notes Part 37-38): the Bump LED for a fader
    that's "unlatched" after a fader-mode switch -- i.e. its saved logical value doesn't match
    the physical fader position, so moving it has no effect on output until the physical
    position catches up. `fader` is 1-indexed (1-24, same convention as decode_0x0e's
    `Fader{N}`); its block is `data[2+6*(fader-1)]` / `data[5+6*(fader-1)]`. Formula confirmed
    live across three separate faders at three different offsets (fader 1 -> 2/5, fader 2 ->
    8/11, fader 24 -> 140/143 -- traces/trace_fader0_wiggle_then_catch.pcapng and
    trace_blink_bump2_24_solo.pcapng):

      - While unlatched (console shows the Bump LED blinking, visibly dim rather than a hard
        on/off flash -- confirmed by eye against real hardware): both bytes sit at a fixed
        `(0x46, 0x0a)` pair, regardless of how long the wiggling goes on, how far the physical
        fader actually moves, or *which* fader it is (identical constant for faders 1, 2 and
        24 alike). Read as the blink's two displayed brightness levels (matches the observed
        dim pulsing far better than a literal on/off pair would), sent once as a pair when
        blinking starts -- the alternation itself is handled locally, not re-sent per flash.
      - The instant the fader is physically caught, both bytes start tracking the fader's own
        live combined output 1:1 (matches the concurrent type=0x0e reading step for step
        across a full 246->0 ramp down to 0, each type=0x16 arriving a few ms after the
        type=0x0e it mirrors).
      - At rest (fader untouched, value unchanged), both bytes equal the fader's steady value
        (e.g. `(0xff, 0xff)` while resting at 255/full) -- indistinguishable on the wire from
        "just caught," since both just mean "LED solid, showing this value."

    Returns None if data is too short, else {"blinking": True} while unlatched, or
    {"blinking": False, "value": <0-255>} once latched/at rest.
    """
    off = 2 + 6 * (fader - 1)
    if len(data) < off + 4:
        return None
    a, b = data[off], data[off + 3]
    if (a, b) == _0X16_BLINK_PAIR:
        return {"blinking": True}
    return {"blinking": False, "value": a if a == b else None}


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
        SmartSoft traffic, RE notes Part 6 Sec.51 / Part 27 Sec.117)."""
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
        (version query) then subtype=0x07 (full show-catalog request) right after connecting
        (RE notes Part 6 Sec.51). subtype=0x07's reply is a single huge type=0x28 announce
        (191 entries in the traces this was found from) covering the static show catalog --
        names, groups, cues, curves, current show/firmware info, and a few pieces of current
        control state (Independents/Solo/BlackOut/fader mode/selection) that used to be sent
        this way before Part 31's request_type() turned out to reach those directly and much
        faster (Part 34). Kept here as a working, tested way to reach the *rest* of the
        catalog (names/groups/cues/curves) for whenever that's actually needed -- not part of
        either app's default startup flow anymore. The reply(ies) aren't read here -- the
        caller's normal poll loop picks them up exactly like any other announce, since the
        announce/ack handling is generic."""
        data = bytes([subtype, 0, 0])
        if not self.write_header(1, 4 + len(data)):
            return False
        return self.write_payload(seq, 0x27, data)

    def request_type(self, type_byte):
        """Proactively ask the console for the current value of a given object type, without
        waiting for it to announce that type first (RE notes Part 31). Same ack sequence
        normally sent only in response to a real type=0x28 announce (msgType=2 with
        state[0]=type, then a plain poll) -- but the console replies with current data even
        when it never announced this type first. Confirmed live for type=0x0e specifically:
        this is what actually solves "fader/master/bumps don't show up without touching a
        control" -- reliably, without depending on winning the OS-level race that makes the
        console's own unprompted announce unreliable on macOS (Part 30 Sec.129).
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
