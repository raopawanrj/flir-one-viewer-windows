import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from flirone.analysis import cooling_analysis  # noqa: E402


def test_recovers_time_constants():
    rng = np.random.default_rng(0)
    h, w = 60, 80
    tau_true = np.full((h, w), 5.0)
    tau_true[20:30, 30:45] = 8.0                       # slow-cooling "defect"
    cold = np.zeros((h, w), bool)
    cold[:5] = True                                    # background that never warmed up
    t = np.arange(0, 45, 0.115)
    hold = 3.0
    temp = np.empty((len(t), h, w), np.float32)
    for i, ti in enumerate(t):
        amp = 20.0 if ti < hold else 20.0 * np.exp(-(ti - hold) / tau_true)
        temp[i] = 22 + np.where(cold, 0.0, amp) + rng.normal(0, 0.08, (h, w))
    r = cooling_analysis(temp, t)
    assert abs(r["cooling_start"] - hold) < 0.6
    assert abs(np.nanmedian(r["tau"][35:, :]) - 5.0) < 0.25
    assert abs(np.nanmedian(r["tau"][20:30, 30:45]) - 8.0) < 0.4
    assert np.isnan(r["tau"][:5]).all()                # background is not analysed
    print("cooling start", r["cooling_start"], "median drop", float(np.nanmedian(r["drop"][35:, :])))
    assert abs(np.nanmedian(r["drop"][35:, :]) - 20.0) < 1.5  # start is detected a frame or two late


if __name__ == "__main__":
    test_recovers_time_constants()
    print("ok test_recovers_time_constants")
