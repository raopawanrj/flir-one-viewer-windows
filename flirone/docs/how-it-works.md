# How it works

## The problem

A USB device describes itself to the computer as a set of interfaces. Each interface
can have alternate settings, and each setting lists the endpoints (data channels) it
offers. The FLIR One's video interface describes itself like this:

| Interface 2 setting | Endpoints it lists | What the camera really does |
| --- | --- | --- |
| Alt 1 (listed first) | none | Video is running |
| Alt 0 | `0x85` (video in), `0x06` | Video is stopped |

To start video the camera expects the standard USB request "switch interface 2 to
alt 1". It treats that request as a start button and keeps sending video on endpoint
`0x85`. On paper, alt 1 has no endpoints at all.

Linux and Android pass the request to the camera and carry on reading `0x85`. The
general-purpose Windows USB libraries take the description literally:

| What we tried | Result | Why |
| --- | --- | --- |
| libusb-1.0, switch through the library | `Input/Output Error` | The driver moves to alt 1 and removes endpoint `0x85` |
| libusb-1.0, start command as a raw request | `Entity not found` | The library built its endpoint table from the first setting listed, which is the empty one, so it refuses the read |
| libusb-win32, both ways | `Element not found` | Same wall in a third library |

## The workaround

`flirone/klib.py` calls `libusbK.dll` directly with `ctypes`:

1. Open the camera and claim its three interfaces.
2. Send the start sequence as plain control requests. To the driver they are opaque
   messages, so it stays on alt 0 and endpoint `0x85` stays open.
3. Read endpoint `0x85` by its address.
4. If the first read fails with Windows error 31, reset the endpoint once and read again.

The start sequence, in order:

| Request type | Request | Value | Index | Meaning |
| --- | --- | --- | --- | --- |
| `0x01` | `0x0B` | 0 | 2 | Stop video |
| `0x01` | `0x0B` | 0 | 1 | Stop file transfer |
| `0x01` | `0x0B` | 1 | 1 | Start file transfer |
| `0x01` | `0x0B` | 1 | 2 | Start video (sent with 2 zero bytes) |

`flirone/usb_device.py` tries this method first and falls back to the libusb paths,
which are kept for other platforms but have not been tested there. It remembers the
method that worked in `.flirone_strategy.txt` in your home folder.

One limit on what we know: the first working run changed several things at once (the
endpoint reset, reading the camera's message endpoints while waiting, a longer wait).
The combination works. Which single change was decisive was not isolated.

## Frame format

Each frame on endpoint `0x85` is:

| Offset | Size | Content |
| --- | --- | --- |
| 0 | 4 | Magic bytes `EF BE 00 00` |
| 8 | 4 | Frame size (bytes after the 28-byte header) |
| 12 | 4 | Thermal block size |
| 16 | 4 | JPEG size (visible-light camera) |
| 20 | 4 | Status size (a JSON string) |
| 28 | | Thermal block, then JPEG, then status |

All sizes are little-endian 32-bit integers. The thermal block holds 16-bit sensor counts:

| Sensor | Rows | Words per row | Pixels in a row |
| --- | --- | --- | --- |
| 160 x 120 | 120 (plus telemetry rows) | 164 | words 2 to 81 and 84 to 163 |
| 80 x 60 | 60 (plus telemetry rows) | 82 | words 2 to 81 |

The status JSON includes `shutterState`. While it starts with `FFC` the frame shows
the internal shutter, not the scene.

## Temperature conversion

Sensor counts are multiplied by 4 and passed through a Planck-type sensor model with
these generic constants: R1 = 16528.178, R2 = 0.012258549, B = 1427.5, F = 1.0,
O = -1307.0. Emissivity and Reflected temperature correct for the surface. These are
not a camera's factory calibration, so absolute values can be off by a few °C.

## Staying connected

- The app reads only the video endpoint while frames flow. Polling the camera's two
  message endpoints during video caused a stream drop in testing.
- The battery level comes from a JSON message on endpoint `0x81`, read at connect and
  once a minute.
- If the camera leaves USB, the app waits and reconnects with the method that worked.
- The camera's firmware is fragile: unusual or rapid USB traffic can make it leave USB
  for 5 to 10 seconds. The code avoids bursts of resets for that reason.
