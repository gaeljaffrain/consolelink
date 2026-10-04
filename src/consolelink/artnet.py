"""
Optional Art-Net output of the console's two DMX universes (type=0x0d, see protocol.py).

Stdlib only. The console sends a full DMX snapshot only when a patched output changes, but
Art-Net receivers and nodes expect a periodic refresh (the spec asks for a keep-alive at least
every ~4 s), so ArtNetSender re-sends the last snapshot itself: immediately on change (capped at
--artnet-rate), otherwise every --artnet-keepalive seconds.

Console universe 1 and 2 go out as Art-Net port-addresses N and N+1, N being --artnet-universe
(default 0: Art-Net numbers universes from 0, so some visualisers label these "1" and "2").

Not meant for critical use: the frames travel USB poll -> UDP with no timing guarantee.
"""
import socket
import struct
import sys
import threading
import time

ARTNET_PORT = 6454
DEFAULT_DEST = "127.0.0.1"
DEFAULT_RATE_HZ = 40.0
DEFAULT_KEEPALIVE_S = 1.0
UNIVERSE_SIZE = 512
_ERROR_LOG_INTERVAL_S = 5.0


def build_artdmx(universe, levels, sequence=0):
    """One ArtDmx packet: `levels` (up to 512 values, 0-255) on 15-bit port-address `universe`.

    `sequence` 1-255 lets receivers reorder packets; 0 disables that. Data length is padded to an
    even number as the spec requires (512 already is).
    """
    if not 0 <= universe <= 0x7FFF:
        raise ValueError(f"Art-Net universe out of range: {universe}")
    data = bytes(levels)
    if len(data) > UNIVERSE_SIZE:
        raise ValueError(f"too many DMX levels: {len(data)}")
    if len(data) % 2:
        data += b"\x00"
    return (b"Art-Net\x00"
            + struct.pack("<H", 0x5000)             # OpCode ArtDmx, little-endian
            + struct.pack(">H", 14)                 # protocol version, hi byte first
            + bytes([sequence & 0xFF, 0,            # Sequence, Physical
                     universe & 0xFF, universe >> 8])  # SubUni (low byte), Net (bits 8-14)
            + struct.pack(">H", len(data))          # Length, big-endian
            + data)


def resolve_dest(dest):
    """'broadcast' -> the limited broadcast address; anything else is used as given."""
    return "255.255.255.255" if dest.lower() == "broadcast" else dest


class ArtNetSender:
    def __init__(self, dest=DEFAULT_DEST, start_universe=0, rate_hz=DEFAULT_RATE_HZ,
                 keepalive_s=DEFAULT_KEEPALIVE_S, port=ARTNET_PORT):
        if rate_hz <= 0 or keepalive_s <= 0:
            raise ValueError("Art-Net rate and keepalive must be positive")
        self.addr = (resolve_dest(dest), port)
        self.start_universe = start_universe
        self.min_interval = 1.0 / rate_hz
        self.keepalive = keepalive_s
        self.packets_sent = 0
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        self._cond = threading.Condition()
        self._frame = None  # latest [universe 1, universe 2], or None before the first update
        self._dirty = False
        self._stopped = False
        self._sequence = [0, 0]
        self._last_error_log = 0.0
        self._thread = threading.Thread(target=self._run, name="artnet", daemon=True)

    def start(self):
        self._thread.start()
        return self

    def _log_error(self, addr, error):
        now = time.monotonic()
        if now - self._last_error_log >= _ERROR_LOG_INTERVAL_S:
            self._last_error_log = now
            print(f"[artnet] send to {addr[0]}:{addr[1]} failed: {error}", file=sys.stderr)

    def update(self, universe1, universe2):
        """Called from the USB poll thread with each decoded snapshot; never blocks on the network."""
        with self._cond:
            self._frame = [bytes(universe1), bytes(universe2)]
            self._dirty = True
            self._cond.notify()

    def stop(self):
        with self._cond:
            self._stopped = True
            self._cond.notify()
        if self._thread.is_alive():
            self._thread.join(timeout=2.0)
        self._sock.close()

    def _run(self):
        last_sent = float("-inf")
        while True:
            with self._cond:
                while True:
                    if self._stopped:
                        return
                    now = time.monotonic()
                    if self._dirty:
                        wait = last_sent + self.min_interval - now
                    elif self._frame is not None:
                        wait = last_sent + self.keepalive - now
                    else:
                        wait = None
                    if wait is not None and wait <= 0:
                        break
                    self._cond.wait(wait)
                frame = self._frame
                self._dirty = False
            last_sent = time.monotonic()
            self._send(frame)

    def _send(self, frame):
        for i, levels in enumerate(frame):
            self._sequence[i] = self._sequence[i] % 255 + 1  # 1..255, never 0
            packet = build_artdmx(self.start_universe + i, levels, self._sequence[i])
            try:
                self._sock.sendto(packet, self.addr)
                self.packets_sent += 1
            except OSError as e:
                self._log_error(self.addr, e)


def add_arguments(parser):
    group = parser.add_argument_group("Art-Net output")
    group.add_argument("--artnet", nargs="?", const=DEFAULT_DEST, default=None, metavar="DEST",
                       help="Send both DMX universes as Art-Net to DEST: an IP address, or "
                            f"'broadcast'. Bare --artnet sends to {DEFAULT_DEST} (a visualiser on "
                            "this machine). Off by default.")
    group.add_argument("--artnet-universe", type=int, default=0, metavar="N",
                       help="Art-Net universe for console universe 1 (universe 2 is N+1). "
                            "Default 0.")
    group.add_argument("--artnet-rate", type=float, default=DEFAULT_RATE_HZ, metavar="HZ",
                       help=f"Maximum send rate on changes (default {DEFAULT_RATE_HZ:g}).")
    group.add_argument("--artnet-keepalive", type=float, default=DEFAULT_KEEPALIVE_S, metavar="SEC",
                       help="Re-send the last frame this often when nothing changes "
                            f"(default {DEFAULT_KEEPALIVE_S:g}).")


def start_sender(dest, universe=0, rate_hz=DEFAULT_RATE_HZ, keepalive_s=DEFAULT_KEEPALIVE_S):
    """A started ArtNetSender to `dest`. Bad values or an unresolvable/unusable destination
    raise ValueError (the message is ready to show to the user)."""
    if not 0 <= universe <= 0x7FFE:
        raise ValueError("the Art-Net universe must be between 0 and 32766")
    if rate_hz <= 0 or keepalive_s <= 0:
        raise ValueError("the Art-Net rate and keepalive must be positive")
    try:
        sender = ArtNetSender(dest, universe, rate_hz, keepalive_s)
    except OSError as e:
        raise ValueError(f"Art-Net destination {dest!r}: {e}") from e
    print(f"Art-Net output to {sender.addr[0]}:{sender.addr[1]}, universes "
          f"{sender.start_universe} and {sender.start_universe + 1}")
    return sender.start()


def sender_from_args(args, parser):
    """A started ArtNetSender if --artnet was given, else None. Bad values exit via parser.error."""
    if args.artnet is None:
        return None
    try:
        return start_sender(args.artnet, args.artnet_universe, args.artnet_rate,
                            args.artnet_keepalive)
    except ValueError as e:
        parser.error(f"--artnet: {e}")
