"""USB side of the FLIR One: find it, start the stream, deliver frames.

The start-up sequence follows the open-source Linux driver flirone-v4l2:
  1. select the USB configuration that carries the frame endpoint (normally 3)
  2. claim interfaces 0 (iAP), 1 (file I/O), 2 (frames)
  3. alternate setting 0 on interfaces 2 and 1 (stop), then 1 on interface 1
     and 1 on interface 2 (start)
  4. keep reading bulk endpoint 0x85 (frames); drain 0x81 and 0x83
"""
from __future__ import annotations

import os
import re
import sys
import threading
import time
from typing import Callable, Optional

import usb.core
import usb.util

from .frame import Frame, FrameAssembler, FrameError, parse_frame

VID, PID = 0x09CB, 0x1996
EP_FRAME, EP_IAP, EP_FILEIO = 0x85, 0x81, 0x83
DEFAULT_CONFIG = 3

DRIVER_HELP = (
    "Windows has no libusb-compatible driver bound to the camera.\n"
    "Run Zadig (https://zadig.akeo.ie): Options > List All Devices, untick\n"
    "'Ignore Hubs or Composite Parents', pick the FLIR ONE entry with USB ID\n"
    "09CB 1996 (the composite parent, not 'Interface 0/1/2'), choose libusbK\n"
    "and click Replace/Install Driver. Then unplug and re-plug the camera."
)


LOG_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "flir_log.txt")


def log_line(text: str):
    """Append a time-stamped line to flir_log.txt next to app.py (never raises)."""
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {text}\n")
    except OSError:
        pass


class CameraGone(RuntimeError):
    """The camera dropped off USB (switched off, flat battery, firmware reset, unplugged)."""


class CameraError(RuntimeError):
    pass


def backends():
    """All usable libusb backends as (name, backend), best first."""
    found = []
    try:
        import libusb_package
        be = libusb_package.get_libusb1_backend()
        if be is not None:
            found.append(("libusb-1.0 (bundled)", be))
    except Exception:
        pass
    try:
        import usb.backend.libusb1 as b1
        be = b1.get_backend()
        if be is not None:
            found.append(("libusb-1.0 (system)", be))
    except Exception:
        pass
    try:
        import usb.backend.libusb0 as b0
        be = b0.get_backend()
        if be is not None:
            found.append(("libusb-win32 / libusb0", be))
    except Exception:
        pass
    return found


def find_camera():
    """Return (device, backend_name) or raise CameraError."""
    bes = backends()
    if not bes:
        raise CameraError("No libusb library found. Run: pip install libusb-package")
    for name, be in bes:
        try:
            dev = usb.core.find(idVendor=VID, idProduct=PID, backend=be)
        except Exception:
            dev = None
        if dev is not None:
            return dev, name
    raise CameraError(
        "FLIR One not found on USB (looking for ID 09CB:1996).\n"
        "Check: camera switched ON (press its power button, light blinking/green),\n"
        "battery charged, and a DATA-capable USB-C adapter or cable is used."
    )


def frame_config_value(dev) -> int:
    """Configuration that contains the frame endpoint (0x85); falls back to 3."""
    try:
        for cfg in dev:
            for intf in cfg:
                for ep in intf:
                    if ep.bEndpointAddress == EP_FRAME:
                        return cfg.bConfigurationValue
    except Exception:
        pass
    return DEFAULT_CONFIG


def _is_timeout(err: Exception) -> bool:
    if type(err).__name__ == "KTimeout":
        return True
    if isinstance(err, getattr(usb.core, "USBTimeoutError", ())):
        return True
    s = str(err).lower()
    return "timeout" in s or "timed out" in s or getattr(err, "errno", None) in (110, 60, 10060)


EP_INTF = {EP_IAP: 0, EP_FILEIO: 1, EP_FRAME: 2}
CACHE = os.path.join(os.path.expanduser("~"), ".flirone_strategy.txt")

# How the "start" commands are sent and how data is read back.
#   raw : commands as raw control requests, reads through pyusb (what the Linux driver does)
#   api : commands through the library's set-alternate-setting call, reads through the
#         low-level backend. Needed with libusb-1.0 on Windows: the camera lists the
#         endpoint-less descriptor first, and that library only learns where endpoint
#         0x85 lives when the alternate setting is switched through its own call.
STRATEGIES = ("raw", "api")


class Stream:
    def __init__(self, dev, label, lowlevel):
        self.dev, self.label, self.lowlevel = dev, label, lowlevel
        self.pending = b""
        self.side_bytes = b""
        self.error_count = 0

    def restart(self):
        self.dev.ctrl_transfer(0x01, 0x0B, 1, 2, b"\x00\x00", 500)

    def read(self, ep, size, timeout) -> bytes:
        if self.lowlevel:
            buf = usb.util.create_buffer(size)
            n = self.dev._ctx.backend.bulk_read(self.dev._ctx.handle, ep, EP_INTF[ep], buf, timeout)
            return bytes(buf[:n])
        return bytes(self.dev.read(ep, size, timeout=timeout))

    def close(self):
        try:
            self.dev.ctrl_transfer(0x01, 0x0B, 0, 2, None, 200)   # stop frames
        except Exception:  # noqa: BLE001
            pass
        try:
            usb.util.dispose_resources(self.dev)
        except Exception:  # noqa: BLE001
            pass


def _start(dev, strategy):
    def raw(interface, setting, data=None):
        dev.ctrl_transfer(0x01, 0x0B, setting, interface, data, 500)

    def api(interface, setting):
        try:
            dev.set_interface_altsetting(interface=interface, alternate_setting=setting)
        except Exception:  # noqa: BLE001
            raw(interface, setting)

    send = raw if strategy == "raw" else api
    send(2, 0)      # stop frames
    send(1, 0)      # stop file I/O
    send(1, 1)      # start file I/O
    if strategy == "raw":
        try:
            raw(2, 1, b"\x00\x00")   # start frames (reference driver sends 2 data bytes)
        except usb.core.USBError:
            raw(2, 1)
    else:
        api(2, 1)


def _attempt(be, strategy, probe_s=3.0):
    """Try one backend + strategy. Returns (Stream or None, text describing the outcome)."""
    dev = usb.core.find(idVendor=VID, idProduct=PID, backend=be)
    if dev is None:
        return None, "camera not visible through this library"
    st = Stream(dev, "", lowlevel=(strategy == "api"))
    try:
        want = frame_config_value(dev)
        try:
            active = dev.get_active_configuration().bConfigurationValue
        except Exception:  # noqa: BLE001
            active = None
        if active != want:
            dev.set_configuration(want)
        for i in (0, 1, 2):
            try:
                if dev.is_kernel_driver_active(i):
                    dev.detach_kernel_driver(i)
            except Exception:  # noqa: BLE001
                pass
            usb.util.claim_interface(dev, i)
        _start(dev, strategy)
        t_end = time.time() + probe_s
        while time.time() < t_end:
            try:
                data = st.read(EP_FRAME, 1 << 20, 200)
            except Exception as e:  # noqa: BLE001
                if _is_timeout(e):
                    continue
                st.close()
                return None, f"started, but reading failed: {e}"
            if data:
                st.pending = data
                return st, f"OK - received {len(data)} bytes"
        st.close()
        return None, f"started, but no data in {probe_s:.0f} s"
    except NotImplementedError as e:
        st.close()
        return None, f"no usable driver ({e})"
    except Exception as e:  # noqa: BLE001
        st.close()
        return None, f"{type(e).__name__}: {e}"


class KStream:
    """Same interface as Stream, but talking to libusbK.dll directly (see klib.py)."""

    MAX_CONSECUTIVE_ERRORS = 40

    def __init__(self, kdev, label):
        self.k, self.label, self.pending = kdev, label, b""
        self.side_bytes = b""      # messages the camera sent on 0x81 while connecting
        self._timeouts = {}
        self.error_count = 0          # recovered read errors since the stream opened
        self._consecutive = 0

    # Windows errors that mean "the device is no longer there"
    GONE = {433: "no such device", 1167: "device not connected", 2: "not found", 6: "invalid handle",
            22: "device does not recognise the command", 21: "device not ready"}

    def read(self, ep, size, timeout) -> bytes:
        from .klib import KError, KTimeout
        if self._timeouts.get(ep) != timeout:
            self.k.set_timeout(ep, timeout)
            self._timeouts[ep] = timeout
        try:
            data = self.k.read(ep, size)
        except KTimeout:
            raise
        except KError as e:
            if ep != EP_FRAME:
                # The two message endpoints are optional. Never let a problem there
                # disturb the video: no retries, no resets, just "nothing read".
                raise KTimeout() from None
            self.error_count += 1
            self._consecutive += 1
            if self._consecutive <= 3 or self._consecutive % 10 == 0:
                log_line(f"read error on 0x{ep:02X}: Windows error {e.winerr} (consecutive {self._consecutive})")
            if e.winerr in self.GONE:
                raise CameraGone(f"Windows error {e.winerr}: {self.GONE[e.winerr]}") from None
            if self._consecutive > self.MAX_CONSECUTIVE_ERRORS:
                raise CameraGone(f"endpoint 0x{ep:02X} kept failing (Windows error {e.winerr})") from None
            if e.winerr == 170:
                # "busy": an earlier request on this endpoint is still being cancelled. Just wait.
                time.sleep(0.1)
            else:
                # Transfer error (typically Windows error 31): short pause, and an
                # endpoint reset only if the error repeats.
                time.sleep(0.03)
                if self._consecutive >= 2:
                    self.k.reset_pipe(ep)
            raise KTimeout() from None
        self._consecutive = 0
        return data

    def restart(self):
        """Send the start-frames command again (used when the stream goes quiet)."""
        from .klib import KError
        try:
            self.k.control(0x01, 0x0B, 1, 2, b"\x00\x00")
        except KError:
            self.k.control(0x01, 0x0B, 1, 2)

    def close(self):
        try:
            self.k.control(0x01, 0x0B, 0, 2)   # stop frames
        except Exception:  # noqa: BLE001
            pass
        self.k.close()


K_VARIANTS = ("full", "start-only", "frame-only")


def _show(b: bytes, n=160) -> str:
    return "".join(chr(c) if 32 <= c < 127 else "." for c in b[:n])


def _attempt_k(variant, probe_s=6.0):
    """libusbK.dll direct.
    full        stop frames, stop file I/O, start file I/O, start frames (reference sequence)
    start-only  start file I/O, start frames
    frame-only  start frames
    While waiting for video, the two other endpoints (0x81, 0x83) are read too:
    the camera may hold back video until its messages there are collected."""
    try:
        from .klib import KDevice, KError, KTimeout
        kd = KDevice()
    except Exception as e:  # noqa: BLE001
        return None, f"{e}"
    st = KStream(kd, "")
    notes = list(kd.notes)
    side = {EP_IAP: b"", EP_FILEIO: b""}
    errors = {}
    try:
        if variant == "full":
            kd.control(0x01, 0x0B, 0, 2)
            kd.control(0x01, 0x0B, 0, 1)
        if variant != "frame-only":
            kd.control(0x01, 0x0B, 1, 1)
        try:
            kd.control(0x01, 0x0B, 1, 2, b"\x00\x00")
        except KError:
            kd.control(0x01, 0x0B, 1, 2)
        t_end = time.time() + probe_s
        got = None
        while time.time() < t_end and got is None:
            for ep, tmo in ((EP_FRAME, 150), (EP_IAP, 30), (EP_FILEIO, 30)):
                try:
                    data = st.read(ep, 1 << 20, tmo)
                except KTimeout:
                    continue
                except KError as e:
                    errors[ep] = errors.get(ep, 0) + 1
                    if errors[ep] == 1:
                        notes.append(f"0x{ep:02X}: {e}")
                    kd.reset_pipe(ep)
                    if errors[ep] > 20 and ep == EP_FRAME:
                        raise
                    continue
                if not data:
                    continue
                if ep == EP_FRAME:
                    got = data
                    break
                side[ep] += data
        for ep, b in side.items():
            if b:
                notes.append(f'0x{ep:02X} sent {len(b)} bytes: "{_show(b)}"')
        if errors:
            notes.append("read errors " + ", ".join(f"0x{ep:02X} x{n}" for ep, n in errors.items()))
        note = (" | ".join(notes) + " | ") if notes else ""
        if got:
            st.pending = got
            st.side_bytes = side[EP_IAP]
            return st, f"{note}OK - received {len(got)} bytes"
        st.close()
        return None, f"{note}started, but no video data in {probe_s:.0f} s"
    except Exception as e:  # noqa: BLE001
        st.close()
        return None, " | ".join(notes + [str(e)])


def open_stream(log: Callable[[str], None] = lambda s: None, use_cache=True, only=None) -> Stream:
    """Find a method that actually delivers data, and return the running stream."""
    find_camera()   # raises a helpful error if the camera is not on USB at all
    combos = []
    if sys.platform == "win32":
        combos += [("libusbK direct", None, v) for v in K_VARIANTS]
    combos += [(n, be, s) for n, be in backends() for s in STRATEGIES]
    if use_cache and os.path.exists(CACHE):
        try:
            want = open(CACHE).read().strip()
            combos.sort(key=lambda c: f"{c[0]}|{c[2]}" != want)
        except OSError:
            pass
    if only:   # reconnect: use just the method that worked before
        combos = [c for c in combos if f"{c[0]} / {c[2]}" == only] or combos
    results = []
    for name, be, strat in combos:
        label = f"{name} / {strat}"
        log(f"  trying [{label}] ...")
        st, msg = _attempt_k(strat) if be is None else _attempt(be, strat)
        log(f"  [{label}] {msg}")
        results.append(f"[{label}] {msg}")
        if st is not None:
            st.label = label
            try:
                with open(CACHE, "w") as f:
                    f.write(f"{name}|{strat}")
            except OSError:
                pass
            return st
        # A rejected command can crash the camera's firmware; it then takes 5-10 s to
        # come back on USB. Wait for that so the next method gets a fair try.
        if only:
            break
        log("    (waiting 8 s for the camera to settle)")
        time.sleep(8)
    raise CameraError("The camera is visible and accepts commands, but no method could read "
                      "its video data:\n  " + "\n  ".join(results))


_BATTERY = re.compile(rb'"batteryVoltageUpdate","data":\{"voltage":([0-9.]+),"percentage":(\d+)\}')
_MSG_TYPE = re.compile(rb'\{"type":"(\w+)"')
BATTERY_POLL_S = 60.0


class CameraThread(threading.Thread):
    """Reads the camera in the background and calls on_frame(Frame) for each image.

    If the camera drops off USB after it has been streaming, the thread waits for it
    to come back and reconnects by itself. Everything notable goes to flir_log.txt."""

    def __init__(self, on_frame: Callable[[Frame], None],
                 on_status: Callable[[str], None] = lambda s: None,
                 layout: Optional[str] = None):
        super().__init__(daemon=True)
        self.on_frame, self.on_status, self.layout = on_frame, on_status, layout
        self._halt = threading.Event()
        self.error: Optional[str] = None
        self.reconnects = 0
        self.battery: Optional[dict] = None     # {"percentage": int, "voltage": float, "age": perf_counter}
        self._seen_types = set()

    def _handle_messages(self, b: bytes):
        """Pick the battery level out of the camera's JSON messages (endpoint 0x81)."""
        if not b:
            return
        m = _BATTERY.findall(b)
        if m:
            volts, pct = float(m[-1][0]), int(m[-1][1])
            if self.battery is None or self.battery["percentage"] != pct:
                log_line(f"battery {pct} % ({volts:.2f} V)")
            self.battery = {"percentage": pct, "voltage": volts, "age": time.perf_counter()}
        for typ in set(_MSG_TYPE.findall(b)) - self._seen_types:
            self._seen_types.add(typ)
            i = b.find(b'{"type":"' + typ)
            log_line(f'camera message type "{typ.decode()}": {_show(b[i:i + 220], 220)}')

    def stop(self):
        self._halt.set()

    def _stream_once(self, st, label_suffix=""):
        """Runs until stop is requested (returns frame count) or the stream fails (raises)."""
        asm = FrameAssembler()
        t_open = last_frame = last_side = time.perf_counter()
        frames, warned, shown = 0, False, False
        last_batt = t_open
        self._handle_messages(st.side_bytes)
        data = st.pending
        while not self._halt.is_set():
            if data is None:
                try:
                    data = st.read(EP_FRAME, 1 << 20, 500)
                except CameraGone:
                    log_line(f"stream ended after {frames} frames, {time.perf_counter() - t_open:.1f} s, "
                             f"{st.error_count} read errors")
                    raise
                except Exception as e:  # noqa: BLE001
                    if not _is_timeout(e):
                        raise
            now = time.perf_counter()
            if data:
                for wire in asm.feed(data):
                    try:
                        fr = parse_frame(wire, self.layout, timestamp=now)
                    except FrameError as e:
                        log_line(f"bad frame skipped: {e}")
                        continue
                    frames += 1
                    if frames == 1:
                        log_line(f"first frame: {fr.layout}, status {fr.status}")
                    self.on_frame(fr)
                    if warned or not shown:
                        self.on_status(f"Camera streaming ({st.label}){label_suffix}")
                        shown = True
                    last_frame, warned = now, False
            had_data, data = bool(data), None
            if not had_data and now - last_side > 1.0:
                # Video is quiet (shutter calibration, start-up): empty the two message
                # endpoints once, as the working Linux code does. Not done while frames flow.
                last_side = now
                for ep in (EP_FILEIO, EP_IAP):
                    try:
                        b = st.read(ep, 1 << 16, 20)
                    except CameraGone:
                        raise
                    except Exception:  # noqa: BLE001
                        continue
                    if ep == EP_IAP:
                        self._handle_messages(b)
            if now - last_batt > BATTERY_POLL_S:
                # one short look at the message endpoint per minute, for the battery level
                last_batt = now
                try:
                    self._handle_messages(st.read(EP_IAP, 1 << 16, 30))
                except CameraGone:
                    raise
                except Exception:  # noqa: BLE001
                    pass
            if now - last_frame > 4:
                if not warned:
                    warned = True
                    log_line("no frames for 4 s - re-sending the start command")
                    self.on_status("No frames for 4 s - asking the camera to restart the stream")
                try:
                    st.restart()
                except Exception:  # noqa: BLE001
                    pass
                last_frame = now
        return frames

    def run(self):
        streamed_before = False
        label = None
        log_line("=== Connect pressed ===")
        while not self._halt.is_set():
            st, why = None, ""
            try:
                self.on_status("Reconnecting to camera..." if streamed_before else "Connecting to camera...")
                st = open_stream(log_line, only=label)
                label = st.label
                log_line(f"stream open via {label}")
                streamed_before = True
                suffix = f" - reconnected {self.reconnects}x" if self.reconnects else ""
                self._stream_once(st, suffix)
            except CameraGone as e:
                why = str(e)
            except CameraError as e:
                why = str(e).splitlines()[0]
                if not streamed_before:
                    self.error = str(e)
            except Exception as e:  # noqa: BLE001 - report anything to the UI
                why = f"{type(e).__name__}: {e}"
                if not streamed_before:
                    self.error = why
            finally:
                if st is not None:
                    st.close()
            if self._halt.is_set():
                break
            if self.error:
                log_line("connect failed: " + self.error.replace("\n", " / "))
                self.on_status("ERROR: " + self.error)
                return
            self.reconnects += 1
            log_line(f"camera lost ({why}); retry in 3 s")
            self.on_status(f"Camera dropped off USB ({why}). Is it still switched on and charged? "
                           f"Retrying every 3 s...")
            self._halt.wait(3)
        log_line("=== stopped ===")
