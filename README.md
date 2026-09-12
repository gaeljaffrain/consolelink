# consolelink

Talk to an ETC SmartFade ML lighting console over USB and see its live state
(faders, master, bumps, independents, solo/blackout) without SmartSoft.

Built from the reverse-engineered wire protocol documented in
[`../SmartFade_SmartSoft_reverse_engineering_notes.md`](../SmartFade_SmartSoft_reverse_engineering_notes.md).

## Contents

| File | What it is |
|---|---|
| [`protocol.py`](protocol.py) | Shared library: USB framing, the idle-poll/announce/ack handshake, and decoders for every known message type. Everything else imports this rather than re-deriving the protocol. |
| [`listen.py`](listen.py) | Terminal tool — connects and prints every control change live until Ctrl+C. |
| [`server.py`](server.py) | Local web app — polls the console in a background thread and serves [`index.html`](index.html) over Server-Sent Events, so a browser tab shows live values. |
| [`index.html`](index.html) | Static single-page UI for `server.py`: fader bars (INT A / INT B / DEVICE INT), Master, Bumps, Independents, Solo/BlackOut. |

## Requirements

- Python 3
- [`pyusb`](https://pypi.org/project/pyusb/) (`pip3 install pyusb`)
- A libusb backend (e.g. `brew install libusb` on macOS)
- The console connected over USB and powered on

Both tools look for the console at `idVendor=0x14D5, idProduct=0x0201` and
claim the vendor bulk interface directly — no vendor driver required, but you
may need permissions to access the raw USB device (on macOS this generally
works out of the box for a user-space process).

## Usage

**Terminal live printer:**

```
python3 listen.py
```

Prints each control's name and value the moment it changes. Ctrl+C to stop.

**Web app:**

```
python3 server.py
```

Then open http://localhost:8765. The page updates live as controls move; it
also auto-reconnects if the console is unplugged and replugged.

**Environment variables** (both tools):

| Variable | Effect |
|---|---|
| `SMARTFADE_DEBUG=1` | Per-control change logging to stderr, plus a liveness heartbeat and handshake diagnostics. |
| `SMARTFADE_CAPTURE=<path>` | Append every message (decoded or not) as a timestamped hex line to `<path>`, for comparing against a packet capture when something behaves unexpectedly. |

## How it works

The console speaks a request/reply protocol over two USB bulk endpoints: a
12-byte header (msgType, payloadLen, 4×uint16 state) is polled OUT
repeatedly, and the console replies with its own header, followed by a
payload when it has one. When a control changes, the console first sends an
"announce" (payload type `0x28`) naming which object type(s) are ready; the
host acks by naming that type back, then the real payload follows. Both
tools also proactively request the types they need at connect time
(`ConsoleLink.request_type()`) rather than waiting for an announce, since
the console's own unprompted announce loses a race against the OS's USB
probing on connect.

See `protocol.py`'s module docstring and the RE notes for the full
message-type breakdown (which types are decoded, which are known-but-not-yet
decoded, and why).
