#!/usr/bin/env python3
"""
Minimal local web app showing live console state: the 24 physical faders'
current output, plus the console's per-mode intensity memory.

Run:
    python3 consolelink/server.py [--debug] [--capture PATH]
Then open http://localhost:8765 in a browser, or http://<this Mac's LAN IP>:8765 from
another device on the same network (e.g. `ipconfig getifaddr en0` for the IP; macOS will
prompt to allow incoming connections for python3 the first time a LAN client connects).

Stdlib only (plus pyusb, already required by protocol.py). No build step, no
npm, no framework -- a background thread polls the console over USB and a
plain http.server serves a static page over Server-Sent Events (GET
/api/events, text/event-stream) so every update is pushed the moment it's
decoded, rather than the page polling and silently skipping whatever changed
between polls.

Reuses the USB protocol implementation from protocol.py rather than
re-deriving it -- see that module's docstring for the
wire-protocol details (idle poll, announce/ack handshake, request_type(),
type=0x0e/0x17 decoding).

Shows: the per-slot Intensity values (INT A, INT B, INT DEV) for all 24
fader slots -- these are the console's stored intensity levels, not raw
physical fader positions. Only whichever mode is currently active on the
console's fader-mode selector reflects live physical fader movement
(type=0x0e); the other two rows come from the console's separately
maintained intensity memory (type=0x0f) and only change when a fader is
moved while that mode is active. Plus Master (a real physical fader) and
Bumps. Independent 1/2, per the manual, are toggle/bump buttons (not
faders) -- shown as lights, decoded from type=0x0c. Also a dedicated
Physical Faders row: 24 bar+light indicators, one per physical fader,
mode-agnostic (type=0x0e for the live value, type=0x16 for the Bump LED --
solid/color-proportional once caught, blinking while the physical fader
hasn't yet caught its stored logical value after a mode switch).
"""
import argparse
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import protocol as sfl
import usb.core
import usb.util

HOST = "0.0.0.0"  # listen on all interfaces, not just loopback, so LAN devices can connect
PORT = 8765
STATIC_DIR = os.path.dirname(os.path.abspath(__file__))

# Same raw-capture mechanism as listen.py (see its module docstring) -- set via --capture PATH
# to log every message (known or not) with full hex + a timestamp, for comparing byte-for-byte
# against a CLI capture when something behaves differently here than in the terminal tool
# despite sharing the same protocol code. Set from args in main().
CAPTURE_PATH = ""

INTENSITY_MODES = ("INT A", "INT B", "INT DEV")  # sub-modes with a decoded intensity bank (type=0x0e/0x0f)
ALL_FADER_MODES = INTENSITY_MODES + ("PARAM 1", "PARAM 2", "MEMS")  # every mode the physical fader-mode selector (type=0x17) can report

state = {
    "connected": False,
    "fader_mode": None,  # None = unknown -- never actually seen a type=0x17; not a guess like "INT A"
    "fader_mode_confirmed": False,  # False = fader_mode is still unknown/unconfirmed
    "mems_page": None,  # 1-indexed MEMS memory page (1-4 seen so far), from type=0x17's
    # data[2] -- see decode_0x17_mems_page in protocol.py. Only meaningful while
    # fader_mode == "MEMS"; None until a MEMS type=0x17 has actually been seen.
    "intensities": {mode: [0] * 24 for mode in INTENSITY_MODES},
    "labels": dict({mode: {} for mode in INTENSITY_MODES}, MEMS={}),  # MEMS is nested one
    # level deeper than the other families: {page (1-indexed): {slot (1-indexed): [lines]}},
    # from type=0x00 (decode_0x00_memory_name). Confirmed only PARTIAL: the connect-time
    # catalog dump sends at most one representative slot per visited page, not every named
    # memory on it (see decode_0x00_memory_name's docstring) -- a missing slot here does not
    # mean nothing is recorded there, only that this dump didn't happen to include it.
    "independent_labels": {},
    "physical_faders": [0] * 24,  # live combined value per fader, mode-agnostic (type=0x0e)
    "physical_fader_lights": [None] * 24,  # None until the first type=0x16 seen for that
    # fader; then {"blinking": True, "value_a": 0-255, "value_b": 0-255} or
    # {"blinking": False, "value": 0-255} -- brightness only, not a color; see
    # decode_0x16_bump_catch in protocol.py for why, and fader_mode below for the hue
    # (console LED is green in every mode except MEMS, which is red)
    "bumps": 0,
    "master": 0,
    "independent1": None,  # None until the first type=0x0c message; then a raw 0-255 value
    "independent2": None,
    "independent1_clicked": None,  # None until the first type=0x0c message; then True/False -- a
    "independent2_clicked": None,  # separate bit from the value above
    "solo": None,  # None until the first type=0x16 message; then "on"/"off"/"blinking"
    "blackout": None,
    "last_update": 0.0,
}
state_lock = threading.Lock()
state_condition = threading.Condition(state_lock)  # notified on every state change, for SSE

def update_state(**kwargs):
    with state_condition:
        state.update(kwargs)
        state["last_update"] = time.time()
        state_condition.notify_all()

# Off by default -- run with --debug to get per-control change logging (which control
# changed, when, to what) plus a liveness heartbeat and visibility into any announce/ack
# handshake failures. This is what found the decode_announce stride bug, and is worth keeping
# around for the next time something looks wrong: it distinguishes "nothing arrived on the
# wire" from "arrived but decoded/rendered wrong" far faster than guessing. Set from args in
# main().
DEBUG = False
_last_logged_master = None
_last_logged_intensities = {mode: [0] * 24 for mode in INTENSITY_MODES}
_t_start = time.time()

def handle_payload(obj_type, data):
    global _last_logged_master
    # Faders (only for active fader mode), bumps and master snapshot
    if obj_type == 0x0e:
        full = sfl.decode_0x0e_full(data)
        if full is not None:
            faders, bumps, master = full
            if DEBUG and master != _last_logged_master:
                print(f"[master] t={time.time() - _t_start:7.3f}  {_last_logged_master} -> {master}",
                      file=sys.stderr)
                _last_logged_master = master
            with state_condition:
                mode = state["fader_mode"]
                # Gated on mode being one of the 3 known INTENSITY_MODES, not just "not None":
                # mode can also be confirmed as PARAM 1/PARAM 2/MEMS (valid per ALL_FADER_MODES,
                # via a real type=0x17), but state["intensities"]/_last_logged_intensities only
                # have entries for INT A/INT B/INT DEV -- whether type=0x0e's raw fader value
                # even means "intensity" in those other modes is still an open RE question (see
                # the protocol.py module docstring/RE notes Part 42), so there's no bank to
                # attribute it to yet, and indexing either dict with "PARAM 1" is a KeyError, not
                # a graceful skip.
                if mode in INTENSITY_MODES:
                    if DEBUG and faders != _last_logged_intensities[mode]:
                        changed = {i + 1: (_last_logged_intensities[mode][i], v)
                                   for i, v in enumerate(faders) if v != _last_logged_intensities[mode][i]}
                        print(f"[intensity:{mode}] t={time.time() - _t_start:7.3f}  "
                              f"confirmed={state['fader_mode_confirmed']}  changed={changed}",
                              file=sys.stderr)
                        _last_logged_intensities[mode] = faders
                    state["intensities"][mode] = faders
                # Unlike intensities[mode] above, this isn't gated on a known mode -- it's the
                # raw live value straight off the wire, not attributed to any particular
                # intensity bank, so there's no "assumed mode" risk in setting it unconditionally.
                state["physical_faders"] = faders
                state["bumps"] = bumps
                state["master"] = master
                state["last_update"] = time.time()
                state_condition.notify_all()
    # Fader mode
    elif obj_type == 0x17:
        mode = sfl.decode_0x17(data)
        if mode in ALL_FADER_MODES:
            if DEBUG:
                print(f"[mode] t={time.time() - _t_start:7.3f}  "
                      f"{state['fader_mode']} -> {mode} (now confirmed)", file=sys.stderr)
            update = {"fader_mode": mode, "fader_mode_confirmed": True}
            if mode == "MEMS":
                page = sfl.decode_0x17_mems_page(data)
                if page is not None:
                    update["mems_page"] = page
                    if DEBUG:
                        print(f"[mems page] t={time.time() - _t_start:7.3f}  page={page}",
                              file=sys.stderr)
            update_state(**update)
    # Labels pages
    elif obj_type == 0x09:
        labels = sfl.decode_0x09_labels(data)
        if labels:
            if DEBUG:
                received = [(family, index, lines)
                            for family, entries in labels.items()
                            for index, lines in entries.items()]
                for family, index, lines in received:
                    print(f"[label] {family} {index}: {lines!r}", file=sys.stderr)
            with state_condition:
                for mode in INTENSITY_MODES:
                    state["labels"][mode].update(labels.get(mode, {}))
                # independent_labels feeds the IND 1/2 button's 3-line name block, same as the
                # intensity rows -- kept as [line1, line2, line3], not joined into one string.
                state["independent_labels"].update(labels.get("Independent", {}))
                state["last_update"] = time.time()
                state_condition.notify_all()
    # Independents (toggle/bump buttons, not faders)
    elif obj_type == 0x0c:
        entries = sfl.decode_0x0c(data)
        update = {}
        if 1 in entries:
            update["independent1_clicked"] = entries[1][0]
            update["independent1"] = entries[1][1]
        if 2 in entries:
            update["independent2_clicked"] = entries[2][0]
            update["independent2"] = entries[2][1]
        # entries[n][2] is [line1, line2, line3] (same 3-line shape decode_0x09_labels uses)
        # -- kept as-is, not joined, so the UI can show 3 lines like the intensity rows do.
        if 1 in entries and any(entries[1][2]):
            update.setdefault("independent_labels", {})
            update["independent_labels"][1] = entries[1][2]
        if 2 in entries and any(entries[2][2]):
            update.setdefault("independent_labels", {})
            update["independent_labels"][2] = entries[2][2]
        if DEBUG and update:
            print(f"[independents] t={time.time() - _t_start:7.3f}  {update}", file=sys.stderr)
        if update:
            update_state(**{k: v for k, v in update.items() if k not in {"independent_labels"}})
            if "independent_labels" in update:
                with state_condition:
                    state["independent_labels"].update(update["independent_labels"])
                    state["last_update"] = time.time()
                    state_condition.notify_all()
    # Solo/Blackout indicators, plus the 24 per-fader Bump-LED blocks in the same payload
    elif obj_type == 0x16:
        flags = sfl.decode_0x16_indicators(data)
        update = {k: v for k, v in (("solo", flags["solo"]), ("blackout", flags["blackout"]))
                  if v is not None}
        if DEBUG and update:
            print(f"[solo/blackout] t={time.time() - _t_start:7.3f}  {update}", file=sys.stderr)
        if update:
            update_state(**update)
        # Own state_condition block, not folded into `update` above -- that dict is only
        # applied when solo/blackout actually changed, and coupling the fader-lights list to
        # that truthiness would be incidental, not a designed guarantee.
        lights = [sfl.decode_0x16_bump_catch(data, n) for n in range(1, 25)]
        with state_condition:
            state["physical_fader_lights"] = lights
            state["last_update"] = time.time()
            state_condition.notify_all()

    # Full intensity table snapshot (3x24) for all three intensity sub-modes at once, not just
    # the currently active one.
    elif obj_type == 0x0f:
        # Unlike 0x0e (mode-agnostic -- only ever reports whichever fader mode is currently
        # active), this holds all three intensity banks at once, so the
        # OTHER two modes' rows (the dimmed ones in the UI) get real data too, not just
        # whatever they were last set to while they happened to be active.
        all_modes = sfl.decode_0x0f_all_modes(data)
        if all_modes is not None:
            with state_condition:
                for mode_name, intensities in all_modes.items():
                    state["intensities"][mode_name] = intensities
                state["last_update"] = time.time()
                state_condition.notify_all()
    # MEMS memory ("Look") names, connect-time only -- see decode_0x00_memory_name in
    # protocol.py for the confirmed record shape (page, slot, both 0-indexed).
    elif obj_type == 0x00:
        result = sfl.decode_0x00_memory_name(data)
        if result is not None:
            page, slot, lines = result
            if DEBUG and any(lines):
                print(f"[mems label] page={page + 1} slot={slot + 1}: {lines!r}", file=sys.stderr)
            with state_condition:
                state["labels"]["MEMS"].setdefault(page + 1, {})[slot + 1] = lines
                state["last_update"] = time.time()
                state_condition.notify_all()


def poll_forever(stop_event):
    """Connect, poll until something goes wrong or the device disappears, then retry."""
    while not stop_event.is_set():
        dev = usb.core.find(idVendor=sfl.VENDOR_ID, idProduct=sfl.PRODUCT_ID)
        found = sfl.find_bulk_interface(dev) if dev is not None else None
        if found is None:
            update_state(connected=False)
            time.sleep(2.0)
            continue

        intf_num, alt, ep_in, ep_out = found

        try:
            if dev.is_kernel_driver_active(intf_num):
                dev.detach_kernel_driver(intf_num)
        except (usb.core.USBError, NotImplementedError):
            pass
        try:
            dev.set_configuration()
        except usb.core.USBError:
            pass
        usb.util.claim_interface(dev, intf_num)

        link = sfl.ConsoleLink(dev, ep_in, ep_out)
        update_state(connected=True)
        print(f"[webapp] connected: {dev.manufacturer!r} {dev.product!r}")

        capture_f = open(CAPTURE_PATH, "a") if CAPTURE_PATH else None
        capture_t0 = time.time()
        if capture_f:
            print(f"[webapp] raw capture logging to {CAPTURE_PATH}", file=sys.stderr)

        def log_capture(kind, obj_type, data):
            if capture_f:
                capture_f.write(f"t={time.time() - capture_t0:8.3f} {kind:8s} "
                                 f"type=0x{obj_type:02x} len={len(data)} raw={data.hex()}\n")
                capture_f.flush()

        # Proactively ask for the current fader MODE before asking for fader/master/bumps
        # state -- type=0x0e is mode-agnostic on the wire (it just reports whatever's on the
        # physical faders right now, for whichever mode happens to be active), and until a
        # real type=0x17 is seen, state["fader_mode"] stays None (unknown) rather than
        # guessing -- handle_payload() drops 0x0e data entirely while it's None.
        # Asking for 0x0e first, before knowing the true mode, used to silently mislabel its
        # data as INT A whenever the console was actually in INT B or INT DEV at connect
        # (found live: "only intensity A are read at startup"). request_type() works for
        # 0x17 exactly like it does for 0x0e, returning the true current mode on demand (not
        # just on a change, unlike the passive announce) --
        # confirmed live with debug_ask_0x17.py against a console sitting in INT DEV mode.
        mode_type, mode_data = link.request_type(0x17)
        if mode_type is not None:
            log_capture("requested", mode_type, mode_data)
            handle_payload(mode_type, mode_data)
        elif DEBUG:
            print("[webapp] proactive 0x17 request got no reply", file=sys.stderr)

        # Proactively ask for current fader/master/bumps state instead of relying on the
        # console's own unprompted announce, which on macOS loses a race against the OS's own
        # automatic USB HID driver probing almost every time -- confirmed live that the
        # console replies with current data to this ack sequence even without ever having
        # announced it first. This is what actually fixes "faders don't show up without
        # touching a control," reliably, independent of any timing race. Sent after the mode
        # request above, so it gets bucketed correctly.
        obj_type, data = link.request_type(0x0e)
        if obj_type is not None:
            log_capture("requested", obj_type, data)
            handle_payload(obj_type, data)
        elif DEBUG:
            print("[webapp] proactive 0x0e request got no reply", file=sys.stderr)

        # type=0x0f holds all three intensity banks at once -- this is what actually gets
        # INT B/INT DEV populated on connect too, not just whichever mode happens to be
        # active (found live: "only intensity A are read at startup" persisted even after the
        # mode-ordering fix above, because that fix only affects 0x0e, which is mode-agnostic
        # by nature and structurally can't report a mode that isn't active).
        all_type, all_data = link.request_type(0x0f)
        if all_type is not None:
            log_capture("requested", all_type, all_data)
            handle_payload(all_type, all_data)
        elif DEBUG:
            print("[webapp] proactive 0x0f request got no reply", file=sys.stderr)

        # Independents and Solo/BlackOut are fetched directly.
        for type_byte, label in ((0x0c, "0x0c"), (0x16, "0x16")):
            obj_type, data = link.request_type(type_byte)
            if obj_type is not None:
                log_capture("requested", obj_type, data)
                handle_payload(obj_type, data)
            elif DEBUG:
                print(f"[webapp] proactive {label} request got no reply", file=sys.stderr)

        consecutive_write_fails = 0
        needs_reset = False
        good_headers = 0
        last_heartbeat = time.time()
        plain_poll_started = time.time()
        gui_requests_sent = False
        while not stop_event.is_set():
            if DEBUG and time.time() - last_heartbeat >= 10.0:
                # Distinguishes "still polling fine, just nothing new to report" from a
                # genuine stall -- if this line stops appearing, the loop itself is stuck
                # somewhere above (not in the announce/ack path, which has its own DROPPED
                # logging), most likely blocked in a read/write call that isn't timing out.
                print(f"[poll] t={time.time() - _t_start:7.3f}  alive, "
                      f"good_headers={good_headers} write_fails={link.write_errors} "
                      f"read_timeouts={link.read_timeouts}", file=sys.stderr)
                last_heartbeat = time.time()

            if not link.write_header(0, 0, (0, 0, 0, 0)):
                consecutive_write_fails += 1
                if consecutive_write_fails > 20:
                    # Repeated write failures right on the OUT pipe (as opposed to a device
                    # that's simply gone) usually means a previous session left the bulk pipe
                    # stalled -- e.g. a process that held the interface got killed without
                    # running its cleanup (SIGTERM skips `finally` blocks; a background thread
                    # being torn down at interpreter exit skips them too). A plain re-claim
                    # doesn't clear that; a USB port reset does, without needing a physical
                    # unplug/replug.
                    print("[webapp] too many write failures, resetting device", file=sys.stderr)
                    needs_reset = True
                    break
                time.sleep(0.05)
                continue
            consecutive_write_fails = 0

            reply = link.read_header()
            if reply is None:
                continue
            good_headers += 1
            if not gui_requests_sent and time.time() - plain_poll_started >= 2.0:
                # Request the full catalog after initial idle polling so the console announces
                # all 74 type=0x09 name pages without suppressing the startup catch-up response.
                link.send_gui_request(1, 0x07)
                gui_requests_sent = True
            _, payload_len, _ = reply
            if payload_len == 0:
                continue
            raw = link.read_payload(payload_len)
            if raw is None:
                continue
            _, obj_type, data = link.decode_in_payload(raw)
            if obj_type is None:
                continue

            if obj_type == 0x28:
                log_capture("announce", obj_type, data)
                announced_entries = sfl.decode_announce(data)
                for announced_type, selector in announced_entries:
                    # NOTE: every `continue` below silently drops this announced update with
                    # NO retry. Never observed firing in practice, but if a future "misses
                    # the last state" report comes back, enable DEBUG and one of these lines
                    # should show up right when it happens.
                    ack_state = (announced_type, selector[0], selector[1], 0)
                    if not link.write_header(2, 0, ack_state):
                        if DEBUG:
                            print(f"[announce 0x{announced_type:02x} t={time.time() - _t_start:7.3f}] "
                                  f"DROPPED: ack write failed", file=sys.stderr)
                        continue
                    if not link.write_header(0, 0, ack_state):
                        if DEBUG:
                            print(f"[announce 0x{announced_type:02x} t={time.time() - _t_start:7.3f}] "
                                  f"DROPPED: follow-up poll write failed", file=sys.stderr)
                        continue
                    ack_reply = link.read_header()
                    if ack_reply is None:
                        if DEBUG:
                            print(f"[announce 0x{announced_type:02x} t={time.time() - _t_start:7.3f}] "
                                  f"DROPPED: ack header read timed out", file=sys.stderr)
                        continue
                    _, ack_payload_len, _ = ack_reply
                    if ack_payload_len == 0:
                        if DEBUG:
                            print(f"[announce 0x{announced_type:02x} t={time.time() - _t_start:7.3f}] "
                                  f"DROPPED: ack reply had payloadLen=0", file=sys.stderr)
                        continue
                    ack_raw = link.read_payload(ack_payload_len)
                    if ack_raw is None:
                        if DEBUG:
                            print(f"[announce 0x{announced_type:02x} t={time.time() - _t_start:7.3f}] "
                                  f"DROPPED: payload read timed out", file=sys.stderr)
                        continue
                    _, real_type, real_data = link.decode_in_payload(ack_raw)
                    if real_type is not None:
                        log_capture("acked", real_type, real_data)
                        handle_payload(real_type, real_data)
            else:
                log_capture("payload", obj_type, data)
                handle_payload(obj_type, data)

        update_state(connected=False)
        if capture_f:
            capture_f.close()
        try:
            usb.util.release_interface(dev, intf_num)
        except usb.core.USBError:
            pass
        if needs_reset:
            try:
                dev.reset()
                print("[webapp] device reset; waiting for re-enumeration", file=sys.stderr)
                time.sleep(2.0)  # give the device time to fully come back before retrying
            except usb.core.USBError as e:
                print(f"[webapp] reset failed: {e} -- may need a physical unplug/replug",
                      file=sys.stderr)
        usb.util.dispose_resources(dev)
        time.sleep(1.0)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # keep stdout to the USB connect/disconnect prints above

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._serve_file("index.html", "text/html")
        elif self.path == "/style.css":
            self._serve_file("style.css", "text/css")
        elif self.path == "/app.js":
            self._serve_file("app.js", "text/javascript")
        elif self.path == "/api/state":
            # One-shot snapshot, used only for the initial page load before the
            # SSE stream below takes over. Not used for the live updates.
            with state_lock:
                body = json.dumps(state).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/events":
            self._serve_events()
        else:
            self.send_response(404)
            self.end_headers()

    def _serve_events(self):
        """Server-Sent Events: push the current state every time it changes (or at
        least once a second as a heartbeat), so the client never has to poll and
        never silently skips an update that arrived between polls."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        try:
            while True:
                with state_condition:
                    state_condition.wait(timeout=1.0)
                    payload = json.dumps(state)
                self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass  # client navigated away/closed the tab

    def _serve_file(self, name, content_type):
        try:
            with open(os.path.join(STATIC_DIR, name), "rb") as f:
                body = f.read()
        except FileNotFoundError:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class QuietThreadingHTTPServer(ThreadingHTTPServer):
    """`handle_error` is a SERVER hook (called from ThreadingMixIn.process_request_thread),
    not a request-handler one -- it does NOT belong on Handler above; overriding it there
    silently never fires. socketserver's default here prints a full traceback for ANY
    exception while reading/writing a connection, including a browser tab closing or
    reloading mid-request, which resets the TCP connection and is completely routine,
    especially for a long-lived SSE stream. Not a crash (only that one connection's
    thread ends; the server and every other connection keep running) -- just noisy."""

    def handle_error(self, request, client_address):
        exc_type = sys.exc_info()[0]
        if exc_type in (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
            return
        super().handle_error(request, client_address)


def main():
    global DEBUG, CAPTURE_PATH
    parser = argparse.ArgumentParser(
        description="Local web app showing live console state (physical faders + per-mode intensities).")
    parser.add_argument("--debug", action="store_true",
                         help="Per-control change logging to stderr, plus a liveness "
                              "heartbeat and handshake diagnostics.")
    parser.add_argument("--capture", metavar="PATH", default="",
                         help="Append every message (decoded or not) as a timestamped hex "
                              "line to PATH, for comparing against a packet capture when "
                              "something behaves unexpectedly.")
    args = parser.parse_args()
    DEBUG = args.debug
    CAPTURE_PATH = args.capture

    stop_event = threading.Event()
    # Not a daemon thread: daemon threads are hard-killed at interpreter exit with no
    # chance to run their `finally` cleanup, which is exactly what would leave the USB
    # interface claimed/pipes stalled for the next run (see the `needs_reset` comment
    # above). Joining it below ensures usb.util.release_interface() actually runs on a
    # normal Ctrl+C shutdown.
    poll_thread = threading.Thread(target=poll_forever, args=(stop_event,))
    poll_thread.start()

    server = QuietThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Serving on http://{HOST}:{PORT} -- Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping.")
    finally:
        stop_event.set()
        server.shutdown()
        poll_thread.join(timeout=5.0)


if __name__ == "__main__":
    main()
