#!/usr/bin/env python3
"""
Minimal local web app showing live SmartFade ML fader values.

Run:
    python3 webapp/server.py
Then open http://localhost:8765 in a browser.

Stdlib only (plus pyusb, already required by ../smartfade_listen.py). No
build step, no npm, no framework -- a background thread polls the console
over USB and a plain http.server serves a static page over Server-Sent
Events (GET /api/events, text/event-stream) so every update is pushed the
moment it's decoded, rather than the page polling and silently skipping
whatever changed between polls.

Reuses the USB protocol implementation from ../smartfade_listen.py (Parts
6-15 of the RE notes) rather than re-deriving it -- see that file's module
docstring for the wire-protocol details (idle poll, announce/ack handshake,
type=0x0e/0x17 decoding).

Shows: Fader 1-24 under each of the three known fader modes (INT A, INT B,
DEVICE INT -- these are the SAME 24 physical faders, re-labeled depending on
which mode is currently active on the console, per RE notes Part 15), plus
Master and Bumps. Independent 1/2, per the manual, are toggle/bump buttons
(not faders) -- shown as lights, decoded from type=0x0c (RE notes Part 28).
"""
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import smartfade_listen as sfl  # noqa: E402

import usb.core  # noqa: E402
import usb.util  # noqa: E402

HOST = "localhost"
PORT = 8765
STATIC_DIR = os.path.dirname(os.path.abspath(__file__))

# Same raw-capture mechanism as ../smartfade_listen.py (see its module docstring) -- set
# SMARTFADE_CAPTURE=<path> to log every message (known or not) with full hex + a timestamp,
# for comparing byte-for-byte against a CLI capture when something behaves differently here
# than in the terminal tool despite sharing the same protocol code.
CAPTURE_PATH = os.environ.get("SMARTFADE_CAPTURE", "")

FADER_MODES = ("INT A", "INT B", "DEVICE INT")

state = {
    "connected": False,
    "fader_mode": "INT A",
    "fader_mode_confirmed": False,  # False = assumed default, never actually seen a type=0x17
    "faders": {mode: [0] * 24 for mode in FADER_MODES},
    "bumps": 0,
    "master": 0,
    "independent1": None,  # None until the first type=0x0c message; then True/False (RE notes Part 28)
    "independent2": None,
    "solo": None,  # None until the first type=0x16 message; then True/False (RE notes Part 29)
    "blackout": None,
    "catching_up": False,  # True while replaying the Part 30 startup catalog request
    "catchup_total": 0,
    "catchup_done": 0,
    "last_update": 0.0,
}
state_lock = threading.Lock()
state_condition = threading.Condition(state_lock)  # notified on every state change, for SSE


def update_state(**kwargs):
    with state_condition:
        state.update(kwargs)
        state["last_update"] = time.time()
        state_condition.notify_all()


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


# Off by default -- run with SMARTFADE_DEBUG=1 to get per-control change logging (which
# control changed, when, to what) plus a liveness heartbeat and visibility into any
# announce/ack handshake failures. This is what found the decode_announce stride bug
# (Part 26 of the RE notes) and is worth keeping around for the next time something looks
# wrong: it distinguishes "nothing arrived on the wire" from "arrived but decoded/rendered
# wrong" far faster than guessing.
DEBUG = os.environ.get("SMARTFADE_DEBUG", "") not in ("", "0")
_last_logged_master = None
_last_logged_faders = {mode: [0] * 24 for mode in FADER_MODES}
_t_start = time.time()


def handle_payload(obj_type, data):
    global _last_logged_master
    if obj_type == 0x0e:
        full = decode_0x0e_full(data)
        if full is not None:
            faders, bumps, master = full
            if DEBUG and master != _last_logged_master:
                print(f"[master] t={time.time() - _t_start:7.3f}  {_last_logged_master} -> {master}",
                      file=sys.stderr)
                _last_logged_master = master
            with state_condition:
                mode = state["fader_mode"]
                if DEBUG and faders != _last_logged_faders[mode]:
                    changed = {i + 1: (_last_logged_faders[mode][i], v)
                               for i, v in enumerate(faders) if v != _last_logged_faders[mode][i]}
                    print(f"[faders:{mode}] t={time.time() - _t_start:7.3f}  "
                          f"confirmed={state['fader_mode_confirmed']}  changed={changed}",
                          file=sys.stderr)
                    _last_logged_faders[mode] = faders
                state["faders"][mode] = faders
                state["bumps"] = bumps
                state["master"] = master
                state["last_update"] = time.time()
                state_condition.notify_all()
    elif obj_type == 0x17:
        mode = sfl.decode_0x17(data)
        if mode in FADER_MODES:
            if DEBUG:
                print(f"[mode] t={time.time() - _t_start:7.3f}  "
                      f"{state['fader_mode']} -> {mode} (now confirmed)", file=sys.stderr)
            update_state(fader_mode=mode, fader_mode_confirmed=True)
    elif obj_type == 0x0c:
        entries = sfl.decode_0x0c(data)
        update = {}
        if 1 in entries:
            update["independent1"] = entries[1][0]
        if 2 in entries:
            update["independent2"] = entries[2][0]
        if DEBUG and update:
            print(f"[independents] t={time.time() - _t_start:7.3f}  {update}", file=sys.stderr)
        if update:
            update_state(**update)
    elif obj_type == 0x16:
        flags = sfl.decode_0x16_indicators(data)
        update = {k: v for k, v in (("solo", flags["solo"]), ("blackout", flags["blackout"]))
                  if v is not None}
        if DEBUG and update:
            print(f"[solo/blackout] t={time.time() - _t_start:7.3f}  {update}", file=sys.stderr)
        if update:
            update_state(**update)


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

        link = sfl.SmartFadeLink(dev, ep_in, ep_out)
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

        # Proactively ask for current fader/master/bumps state instead of relying on the
        # console's own unprompted announce, which on macOS loses a race against the OS's own
        # automatic USB HID driver probing almost every time (RE notes Part 30 Sec.129) --
        # confirmed live that the console replies with current data to this ack sequence even
        # without ever having announced it first (Part 31). This is what actually fixes
        # "faders don't show up without touching a control," reliably, independent of any
        # timing race. Sent before anything else, since it doesn't depend on being first.
        obj_type, data = link.request_type(0x0e)
        if obj_type is not None:
            log_capture("requested", obj_type, data)
            handle_payload(obj_type, data)
        elif DEBUG:
            print("[webapp] proactive 0x0e request got no reply", file=sys.stderr)

        # Real SmartSoft's connect sequence (RE notes Part 6 Sec.51 / Part 30): the automatic
        # startup catch-up (Part 27) only covers fader/master/bumps state -- it does NOT cover
        # button-toggle state (Independents, Solo, BlackOut -- type=0x0c and type=0x16), which
        # only gets (re-)announced in response to this explicit request. Sent on every fresh
        # connection, not just the first, matching what SmartSoft itself does every time it
        # connects. Replies are picked up by the normal poll loop below like any other announce.
        #
        # NOT sent here, before the loop -- that was tried and broke the Part 27 catch-up:
        # the console's automatic one-shot startup replay fires as the reply to the very FIRST
        # idle poll of a session, and sending this msgType=1 request first pre-empts it (it's
        # no longer the first OUT message). Also NOT sent right after just one successful poll
        # (tried that too, still broke it): real SmartSoft itself waits a good while (1.6-6s
        # across different captures) of plain idle-polling before ever sending its first
        # type=0x27 request -- the automatic catch-up apparently isn't necessarily done after
        # a single round-trip (e.g. if more than one distinct thing needs replaying, it may be
        # drained one idle-poll-response at a time), and firing the catalog request too eagerly
        # can cut that off mid-drain. Sent instead after a flat delay of plain idle polling,
        # comfortably longer than anything seen completing in a trace. RE notes Part 30 Sec.128.
        gui_requests_sent = False
        catchup_deadline = None  # set once the catalog request is actually sent
        connect_time = time.time()
        GUI_REQUEST_DELAY = 2.0

        consecutive_write_fails = 0
        needs_reset = False
        good_headers = 0
        last_heartbeat = time.time()
        while not stop_event.is_set():
            if not gui_requests_sent and time.time() - connect_time >= GUI_REQUEST_DELAY:
                gui_requests_sent = True
                link.send_gui_request(0, 0x09)
                link.send_gui_request(1, 0x07)
                update_state(catching_up=True, catchup_total=0, catchup_done=0)
                catchup_deadline = time.time() + 20.0  # safety net: never leave the UI stuck "loading"

            if catchup_deadline is not None and state["catching_up"] and time.time() > catchup_deadline:
                update_state(catching_up=False)

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
                announced_types = sfl.decode_announce(data)
                # The startup catalog request (Part 30) gets ONE huge announce (~191 entries in
                # the traces this was found from); every ordinary live-update announce is a
                # handful of types at most. That size gap is what tells this apart from a
                # coincidental small announce arriving mid-catchup, so a live button press
                # during those few seconds doesn't get miscounted as catalog progress.
                in_catalog_batch = state["catching_up"] and len(announced_types) > 5
                if in_catalog_batch:
                    update_state(catchup_total=len(announced_types), catchup_done=0)
                for announced_type in announced_types:
                    if in_catalog_batch:
                        with state_condition:
                            state["catchup_done"] += 1
                            if state["catchup_done"] >= state["catchup_total"]:
                                state["catching_up"] = False
                            state["last_update"] = time.time()
                            state_condition.notify_all()
                    # NOTE: every `continue` below silently drops this announced update with
                    # NO retry. Never observed firing in practice, but if a future "misses
                    # the last state" report comes back, enable DEBUG and one of these lines
                    # should show up right when it happens.
                    if not link.write_header(2, 0, (announced_type, 0, 0, 0)):
                        if DEBUG:
                            print(f"[announce 0x{announced_type:02x} t={time.time() - _t_start:7.3f}] "
                                  f"DROPPED: ack write failed", file=sys.stderr)
                        continue
                    if not link.write_header(0, 0, (announced_type, 0, 0, 0)):
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

        update_state(connected=False, catching_up=False)
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
