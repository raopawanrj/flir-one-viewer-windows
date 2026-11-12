"""Cooling analysis of a recording (pure numpy, no camera or window needed).

Experiment this is meant for: warm the part, remove the heat source, record
while it cools. Every pixel then follows roughly

    Temperature(t) = End temperature + Temperature drop * exp(-(t - t0) / Cooling time constant)

Quantities (one name each, used in the app, the files and the report):

  Cooling start (s)            t0, the moment cooling begins. Auto value: the last
                               time the frame-mean Temperature is still within 3 %
                               of its peak. Can be typed in by hand.
  Start temperature (degC)     mean of the first 2 frames at/after Cooling start.
  End temperature (degC)       mean of the frames in the last 2 s of the analysis window.
  Temperature drop (degC)      Start temperature - End temperature.
  Cooling time constant (s)    time for a pixel to lose 63 % of its Temperature drop.
                               Computed per pixel as -1/slope of a straight-line fit of
                               ln((Temperature - End temperature) / Temperature drop)
                               against time, using the samples from Cooling start
                               until the pixel has lost 90 % of its drop.
                               LARGE value = cools slowly, SMALL value = cools fast.
  Pixels not analysed          Temperature drop below the minimum drop, or fewer than
                               4 usable samples. Shown grey, stored as NaN.
"""
from __future__ import annotations

import numpy as np

from .frame import Radiometry


def load_recording(path):
    """Returns (temp[N,H,W] degC, t[N] s, info dict). Shutter-calibration frames are dropped."""
    d = np.load(path)
    raw, t = d["raw"], np.asarray(d["t_s"], float)
    if raw.ndim != 3:
        raise ValueError("Not a recording (this looks like a single snapshot)")
    keep = ~np.asarray(d["shutter_calibration"], bool) if "shutter_calibration" in d.files else np.ones(len(t), bool)
    rad = Radiometry(emissivity=float(d["emissivity"]), reflected_c=float(d["reflected_c"]))
    temp = rad.to_celsius(raw[keep]).astype(np.float32)
    info = dict(emissivity=rad.emissivity, reflected_c=rad.reflected_c, frames=int(keep.sum()),
                dropped_shutter_frames=int((~keep).sum()), duration_s=float(t[-1] - t[0]) if len(t) else 0.0,
                shape=raw.shape[1:])
    return temp, t[keep], info


def auto_cooling_start(t, mean_curve) -> float:
    lo, hi = float(np.nanmin(mean_curve)), float(np.nanmax(mean_curve))
    near_peak = np.nonzero(mean_curve >= hi - 0.03 * (hi - lo))[0]
    return float(t[near_peak[-1]]) if len(near_peak) else float(t[0])


def cooling_analysis(temp, t, cooling_start=None, window_s=None, min_drop=1.0, stop_fraction=0.10):
    temp = np.asarray(temp, np.float32)
    t = np.asarray(t, float)
    mean_curve = np.nanmean(temp.reshape(len(t), -1), axis=1)
    if cooling_start is None:
        cooling_start = auto_cooling_start(t, mean_curve)
    t_stop = t[-1] if window_s is None else min(t[-1], cooling_start + window_s)
    sel = (t >= cooling_start) & (t <= t_stop)
    if sel.sum() < 8:
        raise ValueError("Fewer than 8 frames between Cooling start and the end of the window - "
                         "record longer or move Cooling start earlier.")
    tt, T = t[sel] - cooling_start, temp[sel]
    n_end = max(3, int(np.sum(tt >= tt[-1] - 2.0)))
    t_end = np.nanmean(T[-n_end:], axis=0)
    t_start = np.nanmean(T[:2], axis=0)
    drop = t_start - t_end
    ok = drop >= min_drop
    with np.errstate(divide="ignore", invalid="ignore"):
        frac = (T - t_end) / np.where(ok, drop, np.nan)
        usable = np.logical_and.accumulate(frac > stop_fraction, axis=0) & (frac < 1.3)
        usable[-n_end:] = False                      # those frames define End temperature
        y = np.where(usable, np.log(np.where(usable, frac, 1.0)), 0.0)
        w = usable.astype(np.float64)
        x = tt[:, None, None]
        sw, sx, sy = w.sum(0), (w * x).sum(0), y.sum(0)
        sxx, sxy = (w * x * x).sum(0), (y * x).sum(0)
        slope = (sw * sxy - sx * sy) / (sw * sxx - sx * sx)
        tau = np.where(ok & (sw >= 4) & (slope < 0), -1.0 / slope, np.nan)
    return dict(tau=tau.astype(np.float32), drop=np.where(ok, drop, np.nan).astype(np.float32),
                start_temp=t_start, end_temp=t_end, cooling_start=float(cooling_start),
                window_s=float(tt[-1]), frames_used=int(sel.sum()), mean_curve=mean_curve, t=t,
                analysed_fraction=float(np.isfinite(tau).mean()), min_drop=float(min_drop))
