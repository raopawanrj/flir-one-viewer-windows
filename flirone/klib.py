"""Direct access to the camera through libusbK.dll (Windows only).

Why this exists: the camera lists an endpoint-less descriptor ("alt 1") before
the one that carries the endpoints ("alt 0"), and uses "SET_INTERFACE alt 1"
only as a start command. The generic libraries (libusb-1.0, libusb-win32) react
to that command by switching their own view to alt 1 and then refuse to read
endpoint 0x85. Here the command is sent as an opaque control request and the
endpoint is read by its address, with no library bookkeeping in between.

libusbK.dll is installed into the Windows System32 folder by Zadig together with the
libusbK driver, and can also talk to devices bound to WinUSB or libusb-win32.
"""
from __future__ import annotations

import ctypes
import sys

VID, PID = 0x09CB, 0x1996
PIPE_AUTO_CLEAR_STALL = 0x02
PIPE_TRANSFER_TIMEOUT = 0x03
ERROR_SEM_TIMEOUT = 121


class KError(OSError):
    """winerr holds the Windows error code when there is one."""

    def __init__(self, msg, winerr=0):
        super().__init__(msg)
        self.winerr = winerr

    def __str__(self):
        return self.args[0]


class KTimeout(KError):
    def __init__(self):
        super().__init__("read timed out", ERROR_SEM_TIMEOUT)

    def __str__(self):
        return "read timed out"


class _Setup(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("RequestType", ctypes.c_ubyte), ("Request", ctypes.c_ubyte),
                ("Value", ctypes.c_ushort), ("Index", ctypes.c_ushort), ("Length", ctypes.c_ushort)]


def _load():
    if sys.platform != "win32":
        raise KError("libusbK is Windows-only")
    try:
        k = ctypes.WinDLL("libusbK.dll", use_last_error=True)
    except OSError as e:
        raise KError(f"libusbK.dll not found ({e}) - install the libusbK driver once with Zadig") from e
    vp, u8, u32, B = ctypes.c_void_p, ctypes.c_ubyte, ctypes.c_uint, ctypes.c_int
    P = ctypes.POINTER
    sig = {
        "LstK_Init": [P(vp), u32],
        "LstK_Free": [vp],
        "LstK_FindByVidPid": [vp, ctypes.c_int, ctypes.c_int, P(vp)],
        "UsbK_Init": [P(vp), vp],
        "UsbK_Free": [vp],
        "UsbK_ClaimInterface": [vp, u8, B],
        "UsbK_SelectInterface": [vp, u8, B],
        "UsbK_ControlTransfer": [vp, _Setup, ctypes.c_char_p, u32, P(u32), vp],
        "UsbK_ReadPipe": [vp, u8, ctypes.c_void_p, u32, P(u32), vp],
        "UsbK_SetPipePolicy": [vp, u8, u32, u32, ctypes.c_void_p],
        "UsbK_ResetPipe": [vp, u8],
    }
    for name, args in sig.items():
        fn = getattr(k, name)
        fn.argtypes, fn.restype = args, B
    return k


class KDevice:
    def __init__(self):
        self.k = _load()
        self.lst = ctypes.c_void_p()
        self.h = ctypes.c_void_p()
        if not self.k.LstK_Init(ctypes.byref(self.lst), 0):
            raise KError(f"LstK_Init failed (Windows error {ctypes.get_last_error()})")
        info = ctypes.c_void_p()
        if not self.k.LstK_FindByVidPid(self.lst, VID, PID, ctypes.byref(info)):
            self.close()
            raise KError("camera not listed by libusbK (driver is not libusbK / WinUSB / libusb-win32)")
        if not self.k.UsbK_Init(ctypes.byref(self.h), info):
            err = ctypes.get_last_error()
            self.close()
            raise KError(f"UsbK_Init failed (Windows error {err})")
        self._buf = None
        self.notes = []
        # give control requests a 1 s limit, so a busy camera cannot hang the program
        v = ctypes.c_uint(1000)
        self.k.UsbK_SetPipePolicy(self.h, 0x00, PIPE_TRANSFER_TIMEOUT, 4, ctypes.byref(v))
        for i in (0, 1, 2):
            if not self.k.UsbK_ClaimInterface(self.h, i, 0):
                self.notes.append(f"claim interface {i}: Windows error {ctypes.get_last_error()}")

    def control(self, request_type, request, value, index, data=b""):
        n = ctypes.c_uint(0)
        pkt = _Setup(request_type, request, value, index, len(data))
        if not self.k.UsbK_ControlTransfer(self.h, pkt, data if data else None, len(data),
                                           ctypes.byref(n), None):
            raise KError(f"control request failed (Windows error {ctypes.get_last_error()})")

    def set_timeout(self, ep, ms):
        v = ctypes.c_uint(ms)
        self.k.UsbK_SetPipePolicy(self.h, ep, PIPE_TRANSFER_TIMEOUT, 4, ctypes.byref(v))
        b = ctypes.c_ubyte(1)
        self.k.UsbK_SetPipePolicy(self.h, ep, PIPE_AUTO_CLEAR_STALL, 1, ctypes.byref(b))

    def read(self, ep, size) -> bytes:
        # one buffer per endpoint, so a late-completing read on one endpoint can never
        # scribble over data that belongs to another
        if self._buf is None:
            self._buf = {}
        buf = self._buf.get(ep)
        if buf is None or len(buf) < size:
            buf = self._buf[ep] = ctypes.create_string_buffer(size)
        n = ctypes.c_uint(0)
        if not self.k.UsbK_ReadPipe(self.h, ep, buf, size, ctypes.byref(n), None):
            err = ctypes.get_last_error()
            if err == ERROR_SEM_TIMEOUT:
                if 0 < n.value <= size:
                    return buf.raw[:n.value]
                raise KTimeout()
            raise KError(f"read of endpoint 0x{ep:02X} failed (Windows error {err})", err)
        return buf.raw[:n.value]

    def reset_pipe(self, ep):
        self.k.UsbK_ResetPipe(self.h, ep)

    def close(self):
        try:
            if self.h:
                self.k.UsbK_Free(self.h)
                self.h = ctypes.c_void_p()
            if self.lst:
                self.k.LstK_Free(self.lst)
                self.lst = ctypes.c_void_p()
        except Exception:  # noqa: BLE001
            pass
