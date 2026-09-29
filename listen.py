#!/usr/bin/env python3
"""
Terminal live printer for the console, built on top of protocol.py -- see that module's
docstring for the wire-protocol details (idle poll, announce/ack handshake, request_type(),
type=0x0e/0x17/... decoding). This file is just the terminal UI: connect, print whatever
changes, live, until Ctrl+C.
"""

import argparse
import sys
import time

import usb.core
import usb.util

import protocol as sfl

# Pass --capture PATH to also log every message (known or not) with FULL raw hex and a
# timestamp to a plain-text file, in addition to the normal decoded live-print below. This
# is for finding not-yet-decoded controls (buttons, etc) without a Wireshark/USBPcap
# capture: run `python3 consolelink/listen.py --capture traces/capture_x.log`, press
# whatever's being investigated, Ctrl+C, then read the log back like a pcap decode. The normal
# live-print path already shows undecoded types but truncates to 16 bytes and skips known
# types (0x0e, 0x17, ...) entirely -- this captures everything, untruncated.


def main():
    parser = argparse.ArgumentParser(description="Terminal live printer for the console.")
    parser.add_argument("--capture", metavar="PATH", default="",
                         help="Append every message (decoded or not) as a timestamped hex "
                              "line to PATH, for finding not-yet-decoded controls.")
    parser.add_argument("--dmx", action="store_true",
                         help="Also print DMX output changes (type=0x0d, one line per address "
                              "that changes). Off by default -- it is very chatty. --capture "
                              "logs the messages either way.")
    args = parser.parse_args()
    capture_path = args.capture
    print_dmx = args.dmx

    dev = usb.core.find(idVendor=sfl.VENDOR_ID, idProduct=sfl.PRODUCT_ID)
    if dev is None:
        print(f"No device found with VID={sfl.VENDOR_ID:04x} PID={sfl.PRODUCT_ID:04x}."
              " Is the SmartFade ML plugged in?", file=sys.stderr)
        sys.exit(1)

    print(f"Found device: {dev.manufacturer!r} {dev.product!r} (bus {dev.bus}, addr {dev.address})")

    found = sfl.find_bulk_interface(dev)
    if found is None:
        print("Could not find an interface with both a bulk IN and bulk OUT endpoint.", file=sys.stderr)
        sys.exit(1)
    intf_num, alt, ep_in, ep_out = found
    print(f"Using interface {intf_num} (alt {alt}): "
          f"IN=0x{ep_in.bEndpointAddress:02x} (max {ep_in.wMaxPacketSize}), "
          f"OUT=0x{ep_out.bEndpointAddress:02x} (max {ep_out.wMaxPacketSize})")

    try:
        if dev.is_kernel_driver_active(intf_num):
            dev.detach_kernel_driver(intf_num)
    except (usb.core.USBError, NotImplementedError):
        pass  # macOS backend often doesn't support/need this for vendor interfaces

    try:
        dev.set_configuration()
        print("SET_CONFIGURATION sent.")
    except usb.core.USBError as e:
        print(f"[set_configuration: {e} -- continuing, may already be configured]")

    usb.util.claim_interface(dev, intf_num)
    print("Interface claimed. Polling... (Ctrl+C to stop)\n")

    link = sfl.ConsoleLink(dev, ep_in, ep_out)

    known = {}  # control name -> last printed value
    last_lcd = None  # last-printed LCD text (list of 4 lines), to only print on change
    good_headers = 0
    last_report = time.time()
    t_start = time.time()

    capture_f = open(capture_path, "a") if capture_path else None
    if capture_f:
        print(f"Raw capture logging to {capture_path} (--capture)")

    def log_capture(kind, obj_type, data):
        if capture_f:
            capture_f.write(f"t={time.time() - t_start:8.3f} {kind:8s} "
                             f"type=0x{obj_type:02x} len={len(data)} raw={data.hex()}\n")
            capture_f.flush()

    def show(name, value):
        if known.get(name) != value:
            known[name] = value
            formatted = f"{value:3d}" if isinstance(value, int) else str(value)
            print(f"  {name:16s} = {formatted}")

    def handle_payload(obj_type, data):
        nonlocal last_lcd
        if obj_type == 0x0e:
            for name, value in sfl.decode_0x0e(data).items():
                show(name, value)
        elif obj_type == 0x11:
            for name, value in sfl.decode_0x11(data).items():
                show(name, value)
        elif obj_type == 0x15:
            lines = sfl.decode_0x15(data)
            if lines and lines != last_lcd:
                last_lcd = lines
                print(f"  LCD 1: [{lines[0]}] / [{lines[1]}]")
                print(f"  LCD 2: [{lines[2]}] / [{lines[3]}]")
        elif obj_type == 0x17:
            mode = sfl.decode_0x17(data)
            if mode:
                show("FaderMode", mode)
        elif obj_type == 0x18:
            ids = sfl.decode_0x18(data)
            if ids is not None:
                show("Selection", ids)
        elif obj_type == 0x0c:
            for idx, (clicked, value, lines) in sfl.decode_0x0c(data).items():
                name = " ".join(part for part in lines if part)
                label = f"Independent{idx}" + (f" ({name})" if name else "")
                show(label, f"{'ON' if clicked else 'off'} value={value}")
        elif obj_type == 0x16:
            flags = sfl.decode_0x16_indicators(data)
            if flags["solo"] is not None:
                show("Solo", flags["solo"].upper())
            if flags["blackout"] is not None:
                show("BlackOut", flags["blackout"].upper())
        elif obj_type == 0x0f:
            all_modes = sfl.decode_0x0f_all_modes(data)
            if all_modes is not None:
                for mode_name, intensities in all_modes.items():
                    for i, v in enumerate(intensities):
                        if v:
                            show(f"{mode_name}.Intensity{i + 1}", v)
        elif obj_type == 0x0d:
            universes = sfl.decode_0x0d_dmx(data) if print_dmx else None
            if universes is not None:
                for u, levels in enumerate(universes, 1):
                    for i, v in enumerate(levels):
                        if v:
                            show(f"DMX U{u}.{i + 1}", v)
                        elif known.get(f"DMX U{u}.{i + 1}"):
                            show(f"DMX U{u}.{i + 1}", 0)
        elif obj_type == 0x28:
            pass  # announces are handled inline in the main loop, nothing to show here
        elif obj_type in sfl.KNOWN_UNDECODED_TYPES:
            # Seen in the pcap captures (or this session) but not decoded -- not fader data,
            # not an error. Printed for visibility only; safe to ignore for fader testing.
            print(f"  [known, undecoded: type=0x{obj_type:02x} len={len(data)} raw={data[:16].hex()}...]")
        else:
            print(f"  [UNEXPECTED type=0x{obj_type:02x} len={len(data)} raw={data[:16].hex()}...] "
                  f"-- new, not seen before; worth reporting back")

    # Ask for the true current fader mode before asking for fader/master/bumps state --
    # type=0x0e is mode-agnostic on the wire, and until a real type=0x17 is seen the terminal
    # printer has no confirmed mode context either. request_type() works for 0x17 the same way
    # it does for 0x0e, unlike the passive announce, which only ever fires on a change.
    mode_type, mode_data = link.request_type(0x17)
    if mode_type is not None:
        log_capture("requested", mode_type, mode_data)
        handle_payload(mode_type, mode_data)

    # Ask for current fader/master/bumps state directly rather than relying on the console's
    # own unprompted announce, which loses a race against macOS's automatic USB HID driver
    # probing almost every time. Sent after the mode request above, so it gets bucketed
    # correctly instead of being mislabeled under the assumed default.
    req_type, req_data = link.request_type(0x0e)
    if req_type is not None:
        log_capture("requested", req_type, req_data)
        handle_payload(req_type, req_data)

    # type=0x0f holds all three intensity banks at once -- unlike 0x0e, this doesn't need the
    # mode known ahead of time, and gets every mode's live values right away instead of just
    # whichever mode happens to be active.
    all_type, all_data = link.request_type(0x0f)
    if all_type is not None:
        log_capture("requested", all_type, all_data)
        handle_payload(all_type, all_data)

    # Independents and Solo/BlackOut, on demand -- same trick as above. This tool doesn't need
    # anything else out of the full show catalog (names/groups/cues/curves -- all real, just
    # not printed here).
    for type_byte in (0x0c, 0x16):
        req_type, req_data = link.request_type(type_byte)
        if req_type is not None:
            log_capture("requested", req_type, req_data)
            handle_payload(req_type, req_data)

    try:
        while True:
            now = time.time()
            if now - last_report >= 5.0:
                print(f"[heartbeat] good_headers={good_headers} read_timeouts={link.read_timeouts} "
                      f"write_errors={link.write_errors}")
                last_report = now

            if not link.write_header(0, 0, (0, 0, 0, 0)):
                time.sleep(0.05)
                continue

            reply = link.read_header()
            if reply is None:
                continue
            msg_type, payload_len, state = reply
            good_headers += 1

            if payload_len == 0:
                continue

            raw = link.read_payload(payload_len)
            if raw is None:
                continue
            seq, obj_type, data = link.decode_in_payload(raw)
            if obj_type is None:
                continue

            if obj_type == 0x28:
                log_capture("announce", obj_type, data)
                # Announce: ack each announced type in turn, then read its real data.
                for (announced_type, selector) in sfl.decode_announce(data):
                    ack_state = (announced_type, selector[0], selector[1], 0)
                    if not link.write_header(2, 0, ack_state):
                        continue
                    if not link.write_header(0, 0, ack_state):
                        continue
                    ack_reply = link.read_header()
                    if ack_reply is None:
                        continue
                    ack_type, ack_payload_len, _ = ack_reply
                    if ack_payload_len == 0:
                        continue
                    ack_raw = link.read_payload(ack_payload_len)
                    if ack_raw is None:
                        continue
                    _, real_type, real_data = link.decode_in_payload(ack_raw)
                    if real_type is not None:
                        log_capture("acked", real_type, real_data)
                        handle_payload(real_type, real_data)
            else:
                log_capture("payload", obj_type, data)
                handle_payload(obj_type, data)

    except KeyboardInterrupt:
        print("\nStopping.")
    finally:
        usb.util.release_interface(dev, intf_num)
        usb.util.dispose_resources(dev)
        if capture_f:
            capture_f.close()


if __name__ == "__main__":
    main()
