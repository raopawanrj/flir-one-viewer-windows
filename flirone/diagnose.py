"""Step 1 on a new PC: check that Windows + the driver can talk to the FLIR One.

    python diagnose.py

Prints what the PC sees on USB, tries to start the stream for a few seconds,
and writes the first raw frame to first_frame.bin. If anything fails, send the
printed text (and first_frame.bin if it exists) back for debugging.
"""
from __future__ import annotations

import sys
import time

import usb.core
import usb.util

from flirone import usb_device as U
from flirone.frame import HEADER_LEN, FrameAssembler, FrameError, Radiometry, parse_frame


def describe(dev):
    print(f"Device {dev.idVendor:04X}:{dev.idProduct:04X}  bcdDevice={dev.bcdDevice:04X} "
          f"configurations={dev.bNumConfigurations}")
    for cfg in dev:
        print(f"  Configuration {cfg.bConfigurationValue}: {cfg.bNumInterfaces} interface(s)")
        for intf in cfg:
            eps = ", ".join(f"0x{ep.bEndpointAddress:02X}"
                            f"({['ctrl', 'iso', 'bulk', 'int'][ep.bmAttributes & 3]},{ep.wMaxPacketSize})"
                            for ep in intf)
            print(f"    Interface {intf.bInterfaceNumber} alt {intf.bAlternateSetting} "
                  f"class 0x{intf.bInterfaceClass:02X}: {eps or 'no endpoints'}")


class _Tee:
    """Everything printed also goes to diagnose_log.txt, so the log can be read back later."""

    def __init__(self, path):
        self.f = open(path, "a", encoding="utf-8")
        self.out = sys.stdout

    def write(self, s):
        self.out.write(s)
        self.f.write(s)
        self.f.flush()

    def flush(self):
        self.out.flush()


def windows_driver():
    """Which Windows driver is bound to the camera right now (set by Zadig)."""
    if sys.platform != "win32":
        return
    import subprocess
    cmd = ("Get-CimInstance Win32_PnPSignedDriver | Where-Object { $_.DeviceID -like '*VID_09CB&PID_1996*' } "
           "| ForEach-Object { $_.DeviceName + ' | provider=' + $_.DriverProviderName + ' | inf=' + $_.InfName "
           "+ ' | version=' + $_.DriverVersion + ' | id=' + $_.DeviceID }")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", cmd], capture_output=True, text=True,
                             timeout=40).stdout.strip()
        print("== Windows driver bound to the camera ==")
        print(out or "  (no entry found)")
    except Exception as e:  # noqa: BLE001
        print("Could not query the Windows driver:", e)


def main():
    sys.stdout = _Tee("diagnose_log.txt")
    print(f"\n######## diagnose run {time.strftime('%Y-%m-%d %H:%M:%S')} ########")
    windows_driver()
    print("== libusb backends ==")
    bes = U.backends()
    for name, _ in bes:
        print("  ", name)
    if not bes:
        print("  none found -> pip install libusb-package")
        return 1

    print("\n== Looking for FLIR One (09CB:1996) ==")
    try:
        dev, name = U.find_camera()
    except U.CameraError as e:
        print(e)
        print("\nAll USB devices visible to libusb:")
        for d in usb.core.find(find_all=True, backend=bes[0][1]) or []:
            print(f"   {d.idVendor:04X}:{d.idProduct:04X}")
        return 1
    print("Found via", name)
    try:
        describe(dev)
    except Exception as e:  # noqa: BLE001
        print("Could not read descriptors:", e)
    print("Frame endpoint 0x85 lives in configuration", U.frame_config_value(dev))
    usb.util.dispose_resources(dev)

    print("\n== Starting stream (trying each method until one delivers data) ==")
    try:
        st = U.open_stream(print, use_cache=False)
    except U.CameraError as e:
        print("FAILED:\n" + str(e))
        return 1
    print("Using:", st.label)

    asm, frames, nbytes, t0 = FrameAssembler(), 0, 0, time.time()
    rad = Radiometry()
    data = st.pending
    while time.time() - t0 < 6:
        if data is None:
            try:
                data = st.read(U.EP_FRAME, 1 << 20, 200)
            except Exception as e:  # noqa: BLE001
                if U._is_timeout(e):
                    data = b""
                else:
                    print("Read error:", e)
                    break
        nbytes += len(data)
        if nbytes and nbytes == len(data):
            print("First bytes on the frame endpoint:", data[:32].hex(" "))
        for wire in asm.feed(data):
            frames += 1
            if frames == 1:
                with open("first_frame.bin", "wb") as f:
                    f.write(wire)
                print("Header bytes:", wire[:HEADER_LEN].hex(" "))
            try:
                fr = parse_frame(wire)
                if frames == 1:
                    t = rad.to_celsius(fr.raw)
                    print(f"Layout {fr.layout}; raw counts {fr.raw.min()}..{fr.raw.max()}; "
                          f"temperature {t.min():.1f}..{t.max():.1f} degC; JPEG {len(fr.jpeg)} bytes")
                    print("Status:", fr.status)
            except FrameError as e:
                if frames == 1:
                    print("Frame received but could not be decoded:", e)
        data = None
        for ep in (U.EP_IAP, U.EP_FILEIO):
            try:
                st.read(ep, 1 << 16, 5)
            except Exception:  # noqa: BLE001
                pass
    st.close()
    dt = time.time() - t0
    print(f"\n{frames} frame(s), {nbytes} bytes in {dt:.1f} s ({frames / dt:.1f} frames/s)")
    print(f"Recovered read errors: {st.error_count}")
    if frames:
        print("SUCCESS - run: python app.py")
        return 0
    print("Data path open, but no complete frames were decoded.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
