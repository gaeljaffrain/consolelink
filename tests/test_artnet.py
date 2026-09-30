"""Art-Net output. Packet layout is checked against the Art-Net specification (ArtDmx), not against
the builder's own output; the DMX levels come from what was patched and moved on the console (see
test_dmx.py). Sockets are local UDP on an ephemeral port -- no network or console needed."""
import argparse
import importlib
import socket
import time

import pytest

import artnet
import consolelink as server_module

HEADER = b"Art-Net\x00" + b"\x00\x50" + b"\x00\x0e"  # ID, OpCode ArtDmx (0x5000 LE), ProtVer 14


def test_artdmx_layout_matches_the_spec():
    levels = list(range(256)) * 2
    packet = artnet.build_artdmx(1, levels, sequence=7)
    assert packet[:12] == HEADER
    assert packet[12] == 7          # Sequence
    assert packet[13] == 0          # Physical
    assert packet[14:16] == b"\x01\x00"  # SubUni, Net
    assert packet[16:18] == b"\x02\x00"  # Length 512, big-endian
    assert packet[18:] == bytes(levels)
    assert len(packet) == 18 + 512


def test_universe_splits_into_subuni_and_net():
    packet = artnet.build_artdmx(0x0123, [0] * 512)
    assert packet[14] == 0x23 and packet[15] == 0x01


def test_short_data_is_padded_to_an_even_length():
    packet = artnet.build_artdmx(0, [1, 2, 3])
    assert packet[16:18] == b"\x00\x04" and packet[18:] == b"\x01\x02\x03\x00"


@pytest.mark.parametrize("universe", [-1, 0x8000])
def test_universe_out_of_range_is_rejected(universe):
    with pytest.raises(ValueError):
        artnet.build_artdmx(universe, [0] * 512)


def test_broadcast_keyword():
    assert artnet.resolve_dest("Broadcast") == "255.255.255.255"
    assert artnet.resolve_dest("192.168.1.20") == "192.168.1.20"


@pytest.fixture
def receiver():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    sock.settimeout(1.0)
    yield sock
    sock.close()


@pytest.fixture
def make_sender(receiver):
    senders = []

    def make(**kw):
        sender = artnet.ArtNetSender("127.0.0.1", port=receiver.getsockname()[1], **kw)
        senders.append(sender)
        return sender.start()

    yield make
    for sender in senders:
        sender.stop()


def recv_frame(sock):
    """The next two packets as {port-address: (sequence, levels)}."""
    frame = {}
    for _ in range(2):
        packet = sock.recv(1024)
        assert packet[:12] == HEADER
        frame[packet[14] | packet[15] << 8] = (packet[12], packet[18:])
    return frame


def test_both_universes_are_sent_on_consecutive_addresses(make_sender, receiver):
    sender = make_sender(start_universe=4)
    sender.update([10] * 512, [20] * 512)
    frame = recv_frame(receiver)
    assert sorted(frame) == [4, 5]
    assert frame[4][1] == bytes([10]) * 512 and frame[5][1] == bytes([20]) * 512


def test_nothing_is_sent_before_the_first_frame(make_sender, receiver):
    make_sender(keepalive_s=0.05)
    receiver.settimeout(0.3)
    with pytest.raises(socket.timeout):
        receiver.recv(1024)


def test_last_frame_is_resent_as_keepalive_with_advancing_sequence(make_sender, receiver):
    sender = make_sender(keepalive_s=0.05)
    sender.update([1] * 512, [2] * 512)
    first, second = recv_frame(receiver), recv_frame(receiver)
    assert first[0][1] == second[0][1] == bytes([1]) * 512
    assert second[0][0] == first[0][0] + 1


def test_sequence_wraps_from_255_to_1_never_0(make_sender, receiver):
    sender = make_sender(keepalive_s=0.001, rate_hz=1000)
    sender.update([0] * 512, [0] * 512)
    seen = [recv_frame(receiver)[0][0] for _ in range(300)]
    assert 0 not in seen
    assert 255 in seen and seen[seen.index(255) + 1] == 1


def test_burst_of_changes_is_coalesced_to_the_rate_limit(make_sender, receiver):
    sender = make_sender(rate_hz=10, keepalive_s=60)
    sender.update([0] * 512, [0] * 512)
    recv_frame(receiver)  # first send is immediate
    for value in range(1, 21):
        sender.update([value] * 512, [0] * 512)
    start = time.monotonic()
    frame = recv_frame(receiver)
    assert time.monotonic() - start >= 0.05      # held back by the 10 Hz cap
    assert frame[0][1] == bytes([20]) * 512      # and it carries the latest value, not a stale one


def test_send_errors_do_not_stop_the_sender(make_sender, receiver):
    sender = make_sender(keepalive_s=60)

    class FlakySocket:  # fails the first two sends, then behaves
        def __init__(self, real):
            self.real, self.calls = real, 0

        def sendto(self, packet, addr):
            self.calls += 1
            if self.calls <= 2:
                raise OSError("network is unreachable")
            return self.real.sendto(packet, addr)

        def close(self):
            self.real.close()

    sender._sock = FlakySocket(sender._sock)
    sender.update([1] * 512, [1] * 512)
    time.sleep(0.1)
    sender.update([2] * 512, [2] * 512)
    assert recv_frame(receiver)[0][1] == bytes([2]) * 512


def test_cli_flags(receiver):
    parser = argparse.ArgumentParser()
    artnet.add_arguments(parser)
    assert artnet.sender_from_args(parser.parse_args([]), parser) is None
    assert parser.parse_args(["--artnet"]).artnet == "127.0.0.1"
    with pytest.raises(SystemExit):
        artnet.sender_from_args(parser.parse_args(["--artnet", "--artnet-rate", "0"]), parser)


def test_server_forwards_dmx_snapshots(make_sender, receiver, capture):
    server = importlib.reload(server_module)
    server.artnet_sender = make_sender()
    cap = capture("dmx.log")
    server.handle_payload(0x0d, cap.tagged("ind1_on").data)
    frame = recv_frame(receiver)
    u1, u2 = frame[0][1], frame[1][1]
    assert u1[510] == 255 and u1[511] == 0  # IND 1 on DMX 511, IND 2 (DMX 512) off
    assert u2 == bytes(512)


def test_server_ignores_malformed_dmx(make_sender, receiver):
    server = importlib.reload(server_module)
    server.artnet_sender = make_sender(keepalive_s=60)
    server.handle_payload(0x0d, b"\x04\x00" + bytes(10))
    receiver.settimeout(0.2)
    with pytest.raises(socket.timeout):
        receiver.recv(1024)
