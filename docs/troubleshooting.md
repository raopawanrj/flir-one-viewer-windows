# Troubleshooting

Start with `diagnose.bat`. It prints one line per connection method and saves
everything to `diagnose_log.txt`. While the app runs, problems are written to
`flir_log.txt` in the same folder.

## Messages from diagnose.bat or run.bat

| Message | Likely cause | What to do |
| --- | --- | --- |
| The window closes immediately | An old copy of the launcher scripts | Use the `.bat` files from this repository; they stay open on errors |
| `Python was not found` | Python missing or not on PATH | Install Python from python.org with **Add python.exe to PATH** ticked |
| `FLIR One not found on USB` | Camera off, flat battery, charge-only cable, or no driver | Switch the camera on, charge it, try another cable or port, then check the driver in Zadig |
| `libusbK.dll not found` | The libusbK driver was never installed | Install it with Zadig ([setup guide](setup.md), step 4) |
| `camera not listed by libusbK` | The camera has a different driver | In Zadig, set the composite parent to libusbK |
| `The camera is visible and accepts commands, but no method could read its video data` | The camera is in an odd state, or the driver is on the wrong entry | Switch the camera off and on, re-plug it, and confirm in Zadig that libusbK is on the **composite parent**. Run `diagnose.bat` again |
| `started, but no video data in 6 s` | The camera did not start streaming | Same as the row above |

## Messages in the app's status line

| Message | Meaning | What to do |
| --- | --- | --- |
| `Camera streaming (...)` | Normal | Nothing |
| `Camera streaming (...) - reconnected Nx` | The stream dropped and came back N times | Occasional reconnects are harmless. If they are frequent, check the battery and open an issue with `flir_log.txt` |
| `Camera dropped off USB (...). Retrying every 3 s...` | The camera left USB | Check it is still on and charged. It reconnects by itself when it returns |
| `No frames for 4 s - asking the camera to restart the stream` | The stream went quiet | Usually recovers. The camera also sleeps after about an hour idle; switch it back on |
| `Camera battery: N % - LOW` | Battery below 20 % | Plug a charger into the camera's own charging port |

## Windows error codes in the logs

| Code | Meaning | Is it a problem? |
| --- | --- | --- |
| 31 | A transfer on the video channel failed | One at every connect is normal and clears itself |
| 170 | The video channel is busy | The app waits and retries. Repeated runs of it are worth reporting |
| 433 | The camera is no longer on USB | The camera switched off, lost power or reset. Check the battery |
| 121 | A read timed out | Normal while the camera is quiet |

## Things that are not bugs

- **Frames marked "SHUTTER CALIBRATION".** The camera closes an internal shutter now and
  then to recalibrate. Those frames show the shutter, not the scene. Recordings flag them
  and the cooling analysis leaves them out.
- **Temperature a few °C off.** The conversion uses generic constants, not your camera's
  factory calibration. See the accuracy note in the README.
- **Only one program at a time.** Close other tools that use the camera before starting
  this one.

## Reporting a problem

Open an issue and attach `diagnose_log.txt` (and `flir_log.txt` if the app was
running). Say which FLIR One model you have and what its light was doing.
