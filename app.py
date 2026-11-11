"""FLIR One Viewer for Windows - live thermal view, snapshots and recordings.

    python app.py            normal start (press "Connect camera")
    python app.py --sim      start with the built-in simulated camera

Quantities shown (one name each, used everywhere in the app and the files):
  Temperature (degC)  sensor counts converted with the generic FLIR One model,
                      the Emissivity and the Reflected temperature set on the right.
  ROI mean (degC)     average Temperature inside the rectangle you drag on the image.
  Frame rate (fps)    frames received per second (the camera delivers about 8.7).
"""
from __future__ import annotations

import base64
import csv
import html
import os
import sys
import time
from collections import deque
from datetime import datetime

import numpy as np
from PySide6.QtCore import QBuffer, QObject, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPen
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog,
                               QFormLayout, QGroupBox, QHBoxLayout, QLabel, QMainWindow,
                               QMessageBox, QPushButton, QSpinBox, QVBoxLayout, QWidget)

from flirone.analysis import cooling_analysis, load_recording
from flirone.frame import Frame, Radiometry
from flirone.sim import SimThread

PALETTES = {
    "Iron": [(0, 0, 0), (32, 0, 140), (148, 0, 160), (220, 50, 50), (255, 150, 0), (255, 230, 60), (255, 255, 255)],
    "Inferno": [(0, 0, 4), (40, 11, 84), (101, 21, 110), (159, 42, 99), (212, 72, 66), (245, 125, 21),
                (250, 193, 39), (252, 255, 164)],
    "Gray": [(0, 0, 0), (255, 255, 255)],
}


def make_lut(anchors) -> np.ndarray:
    a = np.array(anchors, dtype=float)
    x = np.linspace(0, 255, len(a))
    xi = np.arange(256)
    return np.stack([np.interp(xi, x, a[:, c]) for c in range(3)], axis=1).astype(np.uint8)


def colorize(temp: np.ndarray, lo: float, hi: float, lut: np.ndarray) -> np.ndarray:
    span = max(hi - lo, 1e-6)
    idx = np.clip((np.nan_to_num(temp, nan=lo) - lo) / span * 255.0, 0, 255).astype(np.uint8)
    return np.ascontiguousarray(lut[idx])


def rgb_qimage(rgb: np.ndarray) -> QImage:
    h, w, _ = rgb.shape
    return QImage(rgb.data, w, h, 3 * w, QImage.Format_RGB888).copy()


class Bridge(QObject):
    frame = Signal(object)
    status = Signal(str)


class ImageView(QWidget):
    """Thermal image with colour bar, cursor read-out and a draggable ROI rectangle."""
    hover = Signal(int, int)        # pixel x, y (-1, -1 when outside)
    roi_changed = Signal()

    BAR_W = 78

    def __init__(self):
        super().__init__()
        self.setMinimumSize(560, 400)
        self.setMouseTracking(True)
        self.img: QImage | None = None
        self.shape = (120, 160)
        self.lo, self.hi = 20.0, 35.0
        self.lut = make_lut(PALETTES["Iron"])
        self.roi: tuple[int, int, int, int] | None = None   # x0, y0, x1, y1 (inclusive-exclusive)
        self._drag = None
        self.unit_label = "Temp (°C)"
        self.empty_text = "No image - press Connect camera (or Simulation)"

    def set_frame(self, rgb, lo, hi):
        self.img = rgb_qimage(rgb)
        self.shape = rgb.shape[:2]
        self.lo, self.hi = lo, hi
        self.update()

    def _target(self) -> QRectF:
        h, w = self.shape
        aw, ah = self.width() - self.BAR_W - 8, self.height() - 8
        s = max(min(aw / w, ah / h), 0.1)
        return QRectF(4 + (aw - w * s) / 2, 4 + (ah - h * s) / 2, w * s, h * s)

    def _pixel(self, pos) -> tuple[int, int]:
        r = self._target()
        h, w = self.shape
        x = int((pos.x() - r.x()) / r.width() * w)
        y = int((pos.y() - r.y()) / r.height() * h)
        return (x, y) if 0 <= x < w and 0 <= y < h else (-1, -1)

    def mouseMoveEvent(self, e):
        x, y = self._pixel(e.position())
        self.hover.emit(x, y)
        if self._drag and x >= 0:
            x0, y0 = self._drag
            self.roi = (min(x0, x), min(y0, y), max(x0, x) + 1, max(y0, y) + 1)
            self.roi_changed.emit()
            self.update()

    def mousePressEvent(self, e):
        x, y = self._pixel(e.position())
        if e.button() == Qt.RightButton:
            self.roi = None
            self.roi_changed.emit()
            self.update()
        elif x >= 0:
            self._drag = (x, y)

    def mouseReleaseEvent(self, e):
        self._drag = None

    def leaveEvent(self, e):
        self.hover.emit(-1, -1)

    def paintEvent(self, e):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(24, 24, 28))
        r = self._target()
        if self.img is not None:
            p.drawImage(r, self.img)        # nearest-neighbour: every sensor pixel stays a square
        else:
            p.setPen(QColor(170, 170, 170))
            p.drawText(r, Qt.AlignCenter, self.empty_text)
        if self.roi:
            h, w = self.shape
            x0, y0, x1, y1 = self.roi
            sx, sy = r.width() / w, r.height() / h
            p.setPen(QPen(QColor(0, 255, 255), 2))
            p.drawRect(QRectF(r.x() + x0 * sx, r.y() + y0 * sy, (x1 - x0) * sx, (y1 - y0) * sy))
        # colour bar
        bx, by, bh = self.width() - self.BAR_W + 6, 24, self.height() - 48
        for i in range(int(bh)):
            c = self.lut[255 - int(i / max(bh - 1, 1) * 255)]
            p.setPen(QColor(int(c[0]), int(c[1]), int(c[2])))
            p.drawLine(bx, by + i, bx + 16, by + i)
        p.setPen(QColor(230, 230, 230))
        for frac in (0, 0.25, 0.5, 0.75, 1.0):
            v = self.hi - frac * (self.hi - self.lo)
            p.drawText(bx + 21, int(by + frac * (bh - 1)) + 5, f"{v:.1f}")
        p.drawText(bx - 2, 14, self.unit_label)
        p.end()


class TracePlot(QWidget):
    """ROI mean (degC) over the last 60 s."""

    def __init__(self):
        super().__init__()
        self.setMinimumHeight(140)
        self.data: deque = deque(maxlen=1200)

    def add(self, t, v):
        self.data.append((t, v))
        self.update()

    def clear(self):
        self.data.clear()
        self.update()

    def paintEvent(self, e):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(250, 250, 250))
        L, T, R, B = 48, 18, 8, 22
        w, h = self.width() - L - R, self.height() - T - B
        p.setPen(QColor(60, 60, 60))
        p.drawText(L, 13, "ROI mean (°C) vs time (s), last 60 s")
        p.drawRect(L, T, w, h)
        pts = [(t, v) for t, v in self.data if np.isfinite(v)]
        if len(pts) >= 2:
            t1 = pts[-1][0]
            pts = [(t, v) for t, v in pts if t >= t1 - 60]
            vs = [v for _, v in pts]
            lo, hi = min(vs), max(vs)
            if hi - lo < 0.5:
                mid = (hi + lo) / 2
                lo, hi = mid - 0.25, mid + 0.25
            p.drawText(2, T + 10, f"{hi:.2f}")
            p.drawText(2, T + h, f"{lo:.2f}")
            p.drawText(L, T + h + 16, "-60")
            p.drawText(L + w - 8, T + h + 16, "0")
            p.setPen(QPen(QColor(200, 60, 30), 1.5))
            poly = [QPointF(L + (t - (t1 - 60)) / 60 * w, T + h - (v - lo) / (hi - lo) * h) for t, v in pts]
            p.setRenderHint(QPainter.Antialiasing)
            p.drawPolyline(poly)
        p.end()


class MainWindow(QMainWindow):
    def __init__(self, start_sim=False):
        super().__init__()
        self.setWindowTitle("FLIR One Viewer")
        self.rad = Radiometry()
        self.thread = None
        self.bridge = Bridge()
        self.bridge.frame.connect(self.on_frame)
        self.bridge.status.connect(self.on_status)
        self.last: Frame | None = None
        self.temp: np.ndarray | None = None
        self.cursor = (-1, -1)
        self.times: deque = deque(maxlen=30)
        self.recording = False
        self.rec_raw, self.rec_t, self.rec_ffc = [], [], []
        self.t0 = None
        self.out_dir = os.path.join(os.path.expanduser("~"), "Documents", "FLIR_One_Captures")

        self.view = ImageView()
        self.view.hover.connect(self.on_hover)
        self.view.roi_changed.connect(self.on_roi_changed)
        self.trace = TracePlot()
        self.visible = QLabel("Visible camera off")
        self.visible.setAlignment(Qt.AlignCenter)
        self.visible.setMinimumHeight(150)
        self.visible.hide()

        # ---- controls
        self.btn_cam = QPushButton("Connect camera")
        self.btn_sim = QPushButton("Simulation")
        self.btn_stop = QPushButton("Disconnect")
        self.btn_cam.clicked.connect(lambda: self.start(False))
        self.btn_sim.clicked.connect(lambda: self.start(True))
        self.btn_stop.clicked.connect(self.stop)
        src = QGroupBox("Source")
        l = QHBoxLayout(src)
        for b in (self.btn_cam, self.btn_sim, self.btn_stop):
            l.addWidget(b)

        self.auto = QCheckBox("Auto scale (1st-99th percentile of each frame)")
        self.auto.setChecked(True)
        self.smin = QDoubleSpinBox(minimum=-40, maximum=400, value=20, decimals=1, suffix=" °C")
        self.smax = QDoubleSpinBox(minimum=-40, maximum=400, value=35, decimals=1, suffix=" °C")
        self.pal = QComboBox()
        self.pal.addItems(PALETTES)
        self.pal.currentTextChanged.connect(self.on_palette)
        self.auto.toggled.connect(lambda on: (self.smin.setEnabled(not on), self.smax.setEnabled(not on)))
        self.smin.setEnabled(False)
        self.smax.setEnabled(False)
        disp = QGroupBox("Display scale - untick Auto to lock it when comparing parts")
        f = QFormLayout(disp)
        f.addRow(self.auto)
        f.addRow("Scale minimum", self.smin)
        f.addRow("Scale maximum", self.smax)
        f.addRow("Palette", self.pal)
        self.chk_vis = QCheckBox("Show visible camera")
        self.chk_vis.toggled.connect(self.visible.setVisible)
        f.addRow(self.chk_vis)

        self.emis = QDoubleSpinBox(minimum=0.05, maximum=1.0, value=0.95, singleStep=0.01, decimals=2)
        self.refl = QDoubleSpinBox(minimum=-40, maximum=200, value=20, decimals=1, suffix=" °C")
        self.emis.valueChanged.connect(self.on_rad)
        self.refl.valueChanged.connect(self.on_rad)
        radb = QGroupBox("Temperature conversion")
        f2 = QFormLayout(radb)
        f2.addRow("Emissivity", self.emis)
        f2.addRow("Reflected temperature", self.refl)

        self.lbl_cursor = QLabel("Cursor: -")
        self.lbl_frame = QLabel("Frame: -")
        self.lbl_roi = QLabel("ROI: drag a rectangle on the image (right-click clears)")
        self.lbl_fps = QLabel("Frame rate: -")
        self.lbl_batt = QLabel("Camera battery: -")
        self.lbl_state = QLabel("Not connected")
        self.lbl_state.setWordWrap(True)
        rd = QGroupBox("Read-outs")
        v = QVBoxLayout(rd)
        for w in (self.lbl_cursor, self.lbl_roi, self.lbl_frame, self.lbl_fps, self.lbl_batt):
            v.addWidget(w)

        self.btn_snap = QPushButton("Snapshot (PNG + CSV)")
        self.btn_rec = QPushButton("Start recording")
        self.btn_dir = QPushButton("Folder...")
        self.btn_raw = QPushButton("Save raw USB frame (debug)")
        self.btn_an = QPushButton("Analyse cooling in a recording...")
        self.btn_an.clicked.connect(self.open_analysis)
        self.last_rec = None
        self.analysis = None
        self.btn_snap.clicked.connect(self.snapshot)
        self.btn_rec.clicked.connect(self.toggle_record)
        self.btn_dir.clicked.connect(self.choose_dir)
        self.btn_raw.clicked.connect(self.save_wire)
        self.lbl_dir = QLabel(self.out_dir)
        self.lbl_dir.setWordWrap(True)
        cap = QGroupBox("Capture")
        v2 = QVBoxLayout(cap)
        h2 = QHBoxLayout()
        h2.addWidget(self.btn_snap)
        h2.addWidget(self.btn_rec)
        v2.addLayout(h2)
        h3 = QHBoxLayout()
        h3.addWidget(self.btn_dir)
        h3.addWidget(self.btn_raw)
        v2.addLayout(h3)
        v2.addWidget(self.btn_an)
        v2.addWidget(self.lbl_dir)

        right = QVBoxLayout()
        for w in (src, disp, radb, rd, cap, self.visible):
            right.addWidget(w)
        right.addStretch(1)
        rw = QWidget()
        rw.setLayout(right)
        rw.setFixedWidth(400)

        left = QVBoxLayout()
        left.addWidget(self.view, 1)
        left.addWidget(self.trace)
        left.addWidget(self.lbl_state)
        root = QHBoxLayout()
        root.addLayout(left, 1)
        root.addWidget(rw)
        c = QWidget()
        c.setLayout(root)
        self.setCentralWidget(c)
        self.resize(1180, 760)
        if start_sim:
            self.start(True)

    # ---- source handling
    def start(self, sim: bool):
        self.stop()
        self.t0 = None
        self.trace.clear()
        if sim:
            self.thread = SimThread(self.bridge.frame.emit, self.bridge.status.emit)
        else:
            try:
                from flirone.usb_device import CameraThread
            except ImportError as e:
                QMessageBox.critical(self, "Missing package", f"{e}\n\nRun: pip install pyusb libusb-package")
                return
            self.thread = CameraThread(self.bridge.frame.emit, self.bridge.status.emit)
        self.thread.start()

    def stop(self):
        if self.recording:
            self.toggle_record()
        if self.thread:
            self.thread.stop()
            self.thread.join(timeout=2)
            self.thread = None
            self.lbl_state.setText("Not connected")

    def closeEvent(self, e):
        self.stop()
        e.accept()

    def on_status(self, s: str):
        self.lbl_state.setText(s)
        if s.startswith("ERROR"):
            QMessageBox.warning(self, "Camera", s)

    # ---- per frame
    def on_rad(self):
        self.rad.emissivity = self.emis.value()
        self.rad.reflected_c = self.refl.value()

    def on_palette(self, name):
        self.view.lut = make_lut(PALETTES[name])

    def roi_stats(self):
        if self.temp is None or not self.view.roi:
            return None
        x0, y0, x1, y1 = self.view.roi
        r = self.temp[y0:y1, x0:x1]
        if r.size == 0 or not np.isfinite(r).any():
            return None
        return float(np.nanmean(r)), float(np.nanmin(r)), float(np.nanmax(r)), float(np.nanstd(r)), r.size

    def on_frame(self, fr: Frame):
        self.last = fr
        if self.t0 is None:
            self.t0 = fr.timestamp
        t = fr.timestamp - self.t0
        self.temp = temp = self.rad.to_celsius(fr.raw)
        if self.auto.isChecked():
            lo, hi = np.nanpercentile(temp, [1, 99])
            if hi - lo < 1.0:
                mid = (hi + lo) / 2
                lo, hi = mid - 0.5, mid + 0.5
        else:
            lo, hi = self.smin.value(), self.smax.value()
            if hi <= lo:
                hi = lo + 0.1
        self.view.set_frame(colorize(temp, lo, hi, self.view.lut), float(lo), float(hi))

        self.times.append(fr.timestamp)
        if len(self.times) > 1:
            self.lbl_fps.setText(f"Frame rate: {(len(self.times) - 1) / (self.times[-1] - self.times[0]):.1f} fps"
                                 f"   Sensor: {fr.layout}"
                                 + ("   SHUTTER CALIBRATION (ignore this frame)" if fr.ffc else ""))
        batt = getattr(self.thread, "battery", None)
        if batt:
            low = batt["percentage"] < 20
            self.lbl_batt.setText(f"Camera battery: {batt['percentage']} %  ({batt['voltage']:.2f} V)"
                                  + ("  - LOW, plug in the camera's charger" if low else ""))
            self.lbl_batt.setStyleSheet("color: #c0392b; font-weight: bold;" if low else "")
        self.lbl_frame.setText(f"Frame: min {np.nanmin(temp):.2f}  mean {np.nanmean(temp):.2f}  "
                               f"max {np.nanmax(temp):.2f} °C")
        self.on_hover(*self.cursor)
        st = self.roi_stats()
        if st:
            self.lbl_roi.setText(f"ROI ({st[4]} px): mean {st[0]:.2f}  min {st[1]:.2f}  max {st[2]:.2f}  "
                                 f"std {st[3]:.2f} °C")
            self.trace.add(t, st[0])
        if self.chk_vis.isChecked() and fr.jpeg:
            img = QImage.fromData(fr.jpeg, "JPG")
            if not img.isNull():
                from PySide6.QtGui import QPixmap
                self.visible.setPixmap(QPixmap.fromImage(img).scaled(380, 285, Qt.KeepAspectRatio))
        if self.recording:
            self.rec_raw.append(fr.raw.copy())
            self.rec_t.append(fr.timestamp)
            self.rec_ffc.append(fr.ffc)
            self.btn_rec.setText(f"Stop recording ({len(self.rec_raw)} frames)")
            if len(self.rec_raw) >= 20000:
                self.toggle_record()

    def on_hover(self, x, y):
        self.cursor = (x, y)
        if x >= 0 and self.temp is not None and y < self.temp.shape[0] and x < self.temp.shape[1]:
            self.lbl_cursor.setText(f"Cursor: pixel ({x}, {y})  {self.temp[y, x]:.2f} °C  "
                                    f"(sensor count {int(self.last.raw[y, x])})")
        else:
            self.lbl_cursor.setText("Cursor: -")

    def on_roi_changed(self):
        self.trace.clear()
        if not self.view.roi:
            self.lbl_roi.setText("ROI: drag a rectangle on the image (right-click clears)")

    # ---- capture
    def choose_dir(self):
        d = QFileDialog.getExistingDirectory(self, "Capture folder", self.out_dir)
        if d:
            self.out_dir = d
            self.lbl_dir.setText(d)

    def _path(self, prefix, ext):
        os.makedirs(self.out_dir, exist_ok=True)
        return os.path.join(self.out_dir, f"{prefix}_{datetime.now():%Y%m%d_%H%M%S}{ext}")

    def _meta(self):
        return dict(emissivity=self.rad.emissivity, reflected_c=self.rad.reflected_c,
                    planck=np.array([self.rad.planck_r1, self.rad.planck_r2, self.rad.planck_b,
                                     self.rad.planck_f, self.rad.planck_o, self.rad.raw_scale]),
                    roi=np.array(self.view.roi if self.view.roi else (-1, -1, -1, -1)))

    def snapshot(self):
        if self.temp is None:
            return
        base = self._path("snap", "")
        rgb = colorize(self.temp, self.view.lo, self.view.hi, self.view.lut)
        rgb_qimage(np.ascontiguousarray(np.kron(rgb, np.ones((4, 4, 1), dtype=np.uint8)))).save(base + ".png")
        with open(base + ".csv", "w", newline="") as f:
            w = csv.writer(f)
            w.writerow([f"Temperature (degC); emissivity {self.rad.emissivity}; reflected "
                        f"{self.rad.reflected_c} degC; display scale {self.view.lo:.2f} to {self.view.hi:.2f} degC; "
                        f"rows = image rows top to bottom"])
            w.writerows([[f"{v:.3f}" for v in row] for row in self.temp])
        np.savez_compressed(base + ".npz", raw=self.last.raw, **self._meta())
        self.lbl_state.setText(f"Saved {base}.png / .csv / .npz")

    def toggle_record(self):
        if not self.recording:
            if self.temp is None:
                return
            self.rec_raw, self.rec_t, self.rec_ffc = [], [], []
            self.recording = True
            self.btn_rec.setText("Stop recording (0 frames)")
            return
        self.recording = False
        self.btn_rec.setText("Start recording")
        if not self.rec_raw:
            return
        path = self._path("rec", ".npz")
        t = np.array(self.rec_t) - self.rec_t[0]
        np.savez_compressed(path, raw=np.stack(self.rec_raw), t_s=t, shutter_calibration=np.array(self.rec_ffc),
                            **self._meta())
        self.lbl_state.setText(f"Saved {len(self.rec_raw)} frames ({t[-1]:.1f} s) to {path}")
        self.last_rec = path
        self.rec_raw = []

    def open_analysis(self):
        self.analysis = AnalysisWindow(self.out_dir, self.last_rec, self.pal.currentText())
        self.analysis.show()

    def save_wire(self):
        if self.last is None or not self.last.wire:
            return
        path = self._path("usbframe", ".bin")
        with open(path, "wb") as f:
            f.write(self.last.wire)
        self.lbl_state.setText(f"Saved {path}")


class CurvePlot(QWidget):
    """Temperature (degC) against time (s) for the frame mean and the pixel under the cursor."""

    def __init__(self):
        super().__init__()
        self.setMinimumHeight(170)
        self.series = []        # (label, t, y, QColor)
        self.marks = []         # (time, label)

    def set(self, series, marks):
        self.series, self.marks = series, marks
        self.update()

    def paintEvent(self, e):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(250, 250, 250))
        L, T, R, B = 52, 20, 10, 24
        w, h = self.width() - L - R, self.height() - T - B
        p.setPen(QColor(60, 60, 60))
        p.drawRect(L, T, w, h)
        if not self.series:
            p.drawText(self.rect(), Qt.AlignCenter, "Open a recording to see Temperature (°C) vs time (s)")
            p.end()
            return
        ts = np.concatenate([s[1] for s in self.series])
        ys = np.concatenate([s[2] for s in self.series])
        ys = ys[np.isfinite(ys)]
        t0, t1 = float(ts.min()), float(ts.max())
        lo, hi = float(ys.min()), float(ys.max())
        if hi - lo < 0.5:
            lo, hi = lo - 0.25, hi + 0.25
        fx = lambda t: L + (t - t0) / max(t1 - t0, 1e-9) * w          # noqa: E731
        fy = lambda v: T + h - (v - lo) / (hi - lo) * h               # noqa: E731
        p.drawText(2, T + 10, f"{hi:.1f}")
        p.drawText(2, T + h, f"{lo:.1f}")
        p.drawText(L, T + h + 17, f"{t0:.0f} s")
        p.drawText(L + w - 34, T + h + 17, f"{t1:.0f} s")
        for tm, label in self.marks:
            if t0 <= tm <= t1:
                p.setPen(QPen(QColor(90, 90, 90), 1, Qt.DashLine))
                p.drawLine(int(fx(tm)), T, int(fx(tm)), T + h)
                p.drawText(int(fx(tm)) + 3, T + 12, label)
        p.setRenderHint(QPainter.Antialiasing)
        x_leg = L + 140
        for label, t, y, col in self.series:
            p.setPen(QPen(col, 1.6))
            p.drawPolyline([QPointF(fx(a), fy(b)) for a, b in zip(t, y) if np.isfinite(b)])
            p.drawText(x_leg, 14, label)
            x_leg += 9 * len(label) + 24
        p.setPen(QColor(60, 60, 60))
        p.drawText(L, 14, "Temperature (°C)")
        p.end()


def png_base64(img: QImage) -> str:
    buf = QBuffer()
    buf.open(QBuffer.WriteOnly)
    img.save(buf, "PNG")
    return base64.b64encode(bytes(buf.data())).decode()


class AnalysisWindow(QWidget):
    """Turns a heat-then-cool recording into two maps. Definitions: flirone/analysis.py."""

    GREY = (95, 95, 95)

    def __init__(self, folder, path=None, palette="Iron"):
        super().__init__()
        self.setWindowTitle("Cooling analysis")
        self.folder, self.path = folder, None
        self.temp = self.t = self.info = self.res = None
        self.lut = make_lut(PALETTES[palette])

        self.btn_open = QPushButton("Open recording...")
        self.btn_open.clicked.connect(self.choose)
        self.lbl_file = QLabel("No recording loaded")
        self.auto_start = QCheckBox("Find Cooling start automatically")
        self.auto_start.setChecked(True)
        self.start = QDoubleSpinBox(minimum=0, maximum=100000, decimals=2, suffix=" s")
        self.start.setEnabled(False)
        self.window = QSpinBox(minimum=3, maximum=3600, value=30, suffix=" s")
        self.min_drop = QDoubleSpinBox(minimum=0.2, maximum=100, value=1.0, decimals=1, suffix=" °C")
        self.auto_scale = QCheckBox("Auto colour scales (2nd-98th percentile)")
        self.auto_scale.setChecked(True)
        self.tau_lo = QDoubleSpinBox(minimum=0, maximum=10000, value=0, decimals=1, suffix=" s")
        self.tau_hi = QDoubleSpinBox(minimum=0, maximum=10000, value=20, decimals=1, suffix=" s")
        self.drop_lo = QDoubleSpinBox(minimum=0, maximum=500, value=0, decimals=1, suffix=" °C")
        self.drop_hi = QDoubleSpinBox(minimum=0, maximum=500, value=30, decimals=1, suffix=" °C")
        self.btn_run = QPushButton("Recalculate")
        self.btn_run.clicked.connect(self.run)
        self.btn_save = QPushButton("Save maps + report")
        self.btn_save.clicked.connect(self.save)
        self.auto_start.toggled.connect(lambda on: self.start.setEnabled(not on))
        scale_boxes = (self.tau_lo, self.tau_hi, self.drop_lo, self.drop_hi)
        self.auto_scale.toggled.connect(lambda on: [b.setEnabled(not on) for b in scale_boxes])
        for b in scale_boxes:
            b.setEnabled(False)

        form = QFormLayout()
        form.addRow(self.auto_start)
        form.addRow("Cooling start", self.start)
        form.addRow("Analysis window after Cooling start", self.window)
        form.addRow("Minimum Temperature drop to analyse a pixel", self.min_drop)
        form.addRow(self.auto_scale)
        form.addRow("Cooling time constant scale: minimum", self.tau_lo)
        form.addRow("Cooling time constant scale: maximum", self.tau_hi)
        form.addRow("Temperature drop scale: minimum", self.drop_lo)
        form.addRow("Temperature drop scale: maximum", self.drop_hi)
        ctl = QVBoxLayout()
        ctl.addWidget(self.btn_open)
        ctl.addWidget(self.lbl_file)
        ctl.addLayout(form)
        ctl.addWidget(self.btn_run)
        ctl.addWidget(self.btn_save)
        self.lbl_info = QLabel("")
        self.lbl_info.setWordWrap(True)
        self.lbl_info.setAlignment(Qt.AlignTop)
        self.lbl_info.setMinimumHeight(230)
        ctl.addWidget(self.lbl_info)
        ctl.addStretch(1)
        cw = QWidget()
        cw.setLayout(ctl)
        cw.setFixedWidth(400)

        self.v_tau, self.v_drop = ImageView(), ImageView()
        for v, unit, title in ((self.v_tau, "Time (s)", "Cooling time constant (s) - large = cools slowly"),
                               (self.v_drop, "Drop (°C)", "Temperature drop (°C)")):
            v.unit_label, v.empty_text = unit, "Open a recording"
            v.setMinimumSize(380, 300)
            v.hover.connect(self.on_hover)
        maps = QHBoxLayout()
        for v, title in ((self.v_tau, "Cooling time constant (s) - large value = cools slowly"),
                         (self.v_drop, "Temperature drop (°C)")):
            col = QVBoxLayout()
            col.addWidget(QLabel(f"<b>{title}</b>"))
            col.addWidget(v, 1)
            maps.addLayout(col)
        self.plot = CurvePlot()
        self.lbl_px = QLabel("Move the cursor over a map to see that pixel's values and its cooling curve. "
                             "Grey pixels were not analysed.")
        self.lbl_px.setWordWrap(True)
        left = QVBoxLayout()
        left.addLayout(maps, 1)
        left.addWidget(self.lbl_px)
        left.addWidget(self.plot)
        root = QHBoxLayout(self)
        root.addLayout(left, 1)
        root.addWidget(cw)
        self.resize(1320, 760)
        if path:
            self.load(path)

    # ---- loading / computing
    def choose(self):
        path, _ = QFileDialog.getOpenFileName(self, "Open recording", self.folder, "Recordings (rec_*.npz *.npz)")
        if path:
            self.load(path)

    def load(self, path):
        try:
            self.temp, self.t, self.info = load_recording(path)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "Cooling analysis", f"Could not read {path}:\n{e}")
            return
        self.path = path
        self.lbl_file.setText(os.path.basename(path))
        self.auto_start.setChecked(True)
        self.run()

    def run(self):
        if self.temp is None:
            return
        try:
            self.res = cooling_analysis(self.temp, self.t,
                                        None if self.auto_start.isChecked() else self.start.value(),
                                        float(self.window.value()), self.min_drop.value())
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "Cooling analysis", str(e))
            return
        r = self.res
        self.start.setValue(r["cooling_start"])
        if self.auto_scale.isChecked():
            for key, lo, hi in (("tau", self.tau_lo, self.tau_hi), ("drop", self.drop_lo, self.drop_hi)):
                vals = r[key][np.isfinite(r[key])]
                if vals.size:
                    a, b = np.percentile(vals, [2, 98])
                    lo.setValue(float(a))
                    hi.setValue(float(max(b, a + 0.1)))
        self.v_tau.lut = self.v_drop.lut = self.lut
        self.v_tau.set_frame(self._rgb("tau"), self.tau_lo.value(), max(self.tau_hi.value(), self.tau_lo.value() + .1))
        self.v_drop.set_frame(self._rgb("drop"), self.drop_lo.value(),
                              max(self.drop_hi.value(), self.drop_lo.value() + .1))
        tau = r["tau"][np.isfinite(r["tau"])]
        stats = (f"median {np.median(tau):.2f} s, 2nd-98th percentile {np.percentile(tau, 2):.2f} to "
                 f"{np.percentile(tau, 98):.2f} s") if tau.size else "no pixel could be analysed"
        self.lbl_info.setText(
            f"Recording: {self.info['frames']} frames over {self.info['duration_s']:.1f} s "
            f"({self.info['dropped_shutter_frames']} shutter-calibration frames left out).\n"
            f"Cooling start: {r['cooling_start']:.2f} s. Frames used: {r['frames_used']} "
            f"over {r['window_s']:.1f} s.\n"
            f"Pixels analysed: {100 * r['analysed_fraction']:.0f} %.\n"
            f"Cooling time constant: {stats}."
            + ("\nWARNING: the analysis window is shorter than 5x the median Cooling time constant, so the "
               "part has not finished cooling inside it and the values are underestimated. Record longer "
               "and lengthen the window." if tau.size and r["window_s"] < 5 * np.median(tau) else ""))
        self.on_hover(-1, -1)

    def _rgb(self, key):
        lo, hi = (self.tau_lo, self.tau_hi) if key == "tau" else (self.drop_lo, self.drop_hi)
        m = self.res[key]
        rgb = colorize(m, lo.value(), max(hi.value(), lo.value() + 0.1), self.lut)
        rgb[~np.isfinite(m)] = self.GREY
        return rgb

    def on_hover(self, x, y):
        if self.res is None:
            return
        r = self.res
        series = [("frame mean", r["t"], r["mean_curve"], QColor(70, 70, 70))]
        marks = [(r["cooling_start"], "Cooling start"), (r["cooling_start"] + r["window_s"], "window end")]
        h, w = r["tau"].shape
        if 0 <= x < w and 0 <= y < h:
            tau, drop = r["tau"][y, x], r["drop"][y, x]
            txt = (f"Pixel ({x}, {y}):  Cooling time constant {tau:.2f} s,  Temperature drop {drop:.2f} °C,  "
                   f"Start temperature {r['start_temp'][y, x]:.2f} °C,  End temperature {r['end_temp'][y, x]:.2f} °C"
                   if np.isfinite(tau) else
                   f"Pixel ({x}, {y}): not analysed (Temperature drop "
                   f"{r['start_temp'][y, x] - r['end_temp'][y, x]:.2f} °C is below the minimum, or too few samples)")
            self.lbl_px.setText(txt)
            series.append((f"pixel ({x}, {y})", r["t"], self.temp[:, y, x], QColor(200, 60, 30)))
        self.plot.set(series, marks)

    # ---- saving
    def save(self):
        if self.res is None:
            return
        r, base = self.res, os.path.splitext(self.path)[0] + "_cooling"
        imgs = {}
        for key, name in (("tau", "cooling_time_constant_s"), ("drop", "temperature_drop_degC")):
            rgb = np.ascontiguousarray(np.kron(self._rgb(key), np.ones((4, 4, 1), dtype=np.uint8)))
            imgs[key] = rgb_qimage(rgb)
            imgs[key].save(f"{base}_{name}.png")
            np.savetxt(f"{base}_{name}.csv", r[key], delimiter=",", fmt="%.3f",
                       header=f"{name}; rows = image rows top to bottom; nan = pixel not analysed", comments="# ")
        tau = r["tau"][np.isfinite(r["tau"])]
        esc = html.escape
        rows = [("Recording file", os.path.basename(self.path)),
                ("Frames in recording", f"{self.info['frames']} over {self.info['duration_s']:.1f} s "
                                        f"({self.info['dropped_shutter_frames']} shutter-calibration frames left out)"),
                ("Emissivity / Reflected temperature", f"{self.info['emissivity']:.2f} / {self.info['reflected_c']:.1f} °C"),
                ("Cooling start", f"{r['cooling_start']:.2f} s"
                                  + (" (found automatically)" if self.auto_start.isChecked() else " (set by hand)")),
                ("Analysis window", f"{r['window_s']:.1f} s, {r['frames_used']} frames"),
                ("Minimum Temperature drop", f"{r['min_drop']:.1f} °C"),
                ("Pixels analysed", f"{100 * r['analysed_fraction']:.0f} %"),
                ("Cooling time constant, median", f"{np.median(tau):.2f} s" if tau.size else "-"),
                ("Cooling time constant, 2nd-98th percentile",
                 f"{np.percentile(tau, 2):.2f} to {np.percentile(tau, 98):.2f} s" if tau.size else "-"),
                ("Colour scale, Cooling time constant", f"{self.tau_lo.value():.1f} to {self.tau_hi.value():.1f} s"),
                ("Colour scale, Temperature drop", f"{self.drop_lo.value():.1f} to {self.drop_hi.value():.1f} °C")]
        defs = [("Temperature (°C)", "Surface temperature estimated from the camera's sensor counts with the generic "
                 "FLIR One model. Absolute values can be off by a few °C; differences and changes over time are reliable."),
                ("Cooling start (s)", "The moment cooling begins: the last time the frame-mean Temperature is still "
                 "within 3 % of its peak, unless set by hand."),
                ("Start temperature (°C)", "Mean of the first 2 frames at or after Cooling start, per pixel."),
                ("End temperature (°C)", "Mean of the frames in the last 2 s of the analysis window, per pixel."),
                ("Temperature drop (°C)", "Start temperature minus End temperature, per pixel."),
                ("Cooling time constant (s)", "Time for a pixel to lose 63 % of its Temperature drop. Computed as "
                 "-1/slope of a straight-line fit of ln((Temperature - End temperature) / Temperature drop) against "
                 "time, from Cooling start until the pixel has lost 90 % of its drop. Large = cools slowly."),
                ("Grey pixels", "Not analysed: Temperature drop below the minimum, or fewer than 4 usable samples.")]
        page = ["<!doctype html><meta charset='utf-8'><title>Cooling analysis</title>",
                "<style>body{font-family:Segoe UI,Arial,sans-serif;max-width:1000px;margin:24px auto;padding:0 16px;"
                "color:#222}td,th{border:1px solid #ccc;padding:5px 9px;text-align:left;vertical-align:top}"
                "table{border-collapse:collapse;margin:10px 0}img{width:100%;image-rendering:pixelated}"
                ".maps{display:flex;gap:16px}.maps div{flex:1}</style>",
                f"<h1>Cooling analysis</h1><p>Generated {datetime.now():%Y-%m-%d %H:%M} by FLIR One Viewer from "
                f"<code>{esc(os.path.basename(self.path))}</code>. Both maps use the colour scales listed below; "
                "keep them fixed (untick Auto colour scales) when comparing parts.</p>",
                "<h2>Maps</h2><div class='maps'>",
                f"<div><b>Cooling time constant (s)</b><img src='data:image/png;base64,{png_base64(imgs['tau'])}'></div>",
                f"<div><b>Temperature drop (°C)</b><img src='data:image/png;base64,{png_base64(imgs['drop'])}'></div>",
                "</div><h2>Settings and summary</h2><table>",
                *[f"<tr><th>{esc(a)}</th><td>{esc(b)}</td></tr>" for a, b in rows],
                "</table><h2>What each quantity means</h2><table>",
                *[f"<tr><th>{esc(a)}</th><td>{esc(b)}</td></tr>" for a, b in defs],
                "</table><p>Per-pixel values are in the CSV files saved next to this report.</p>"]
        with open(base + "_report.html", "w", encoding="utf-8") as f:
            f.write("\n".join(page))
        self.lbl_info.setText(self.lbl_info.text().split("\nSaved")[0] + f"\nSaved {base}_report.html, 2 PNG, 2 CSV")


def main():
    app = QApplication(sys.argv)
    win = MainWindow(start_sim="--sim" in sys.argv)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
