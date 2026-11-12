"""
NanoVNA-F V3 Front Panel - bench-VNA style remote control.

Run:  python vna_panel.py            (auto-detects the NanoVNA COM port)
      python vna_panel.py --sim      (built-in simulator with a 1 GHz bandpass DUT)
      python vna_panel.py --port COM9
"""
import argparse
import datetime
import math
import os
import sys

import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QFont, QImage, QPixmap
from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QFrame, QLabel, QHBoxLayout,
                             QVBoxLayout, QGridLayout, QComboBox, QFileDialog, QMessageBox,
                             QInputDialog, QDialog, QPlainTextEdit, QPushButton, QSizePolicy)

from vna_device import VNAWorker, list_ports, SIM_PORT, FMIN, FMAX
from vna_cal import Calibration, interp_c
from vna_widgets import PanelButton, Knob, PowerBar, PortConnector, ClickFrame, DPadCenter

APP_NAME = "NanoVNA-F V3 Front Panel"
SLOTS = 8
C0 = 299792458.0
TRACE_COLORS = ["#f2e53d", "#3fd0e0", "#e040e0", "#3f8fff"]

FORMATS = ["LogMag", "Phase", "GDelay", "Smith", "Polar", "SWR", "LinMag",
           "Real", "Imag", "UPhase", "|Z|", "R", "X", "TDR"]
# scale/div, ref value, ref position (div from bottom), unit
FMT_DEF = {
    "LogMag": (10.0, 0.0, 8, "dB"), "Phase": (45.0, 0.0, 5, "°"), "UPhase": (500.0, 0.0, 5, "°"),
    "GDelay": (1.0, 0.0, 5, "ns"), "SWR": (1.0, 1.0, 0, ""), "LinMag": (0.1, 0.0, 0, "U"),
    "Real": (0.2, 0.0, 5, "U"), "Imag": (0.2, 0.0, 5, "U"), "|Z|": (20.0, 0.0, 0, "Ω"),
    "R": (20.0, 0.0, 0, "Ω"), "X": (20.0, 0.0, 5, "Ω"), "TDR": (0.1, 0.0, 0, "U"),
    "Smith": (1.0, 0.0, 0, "U"), "Polar": (1.0, 0.0, 0, "U"),
}
CIRCULAR = ("Smith", "Polar")


# ---------------------------------------------------------------- helpers
def fmt_freq(f, digits=6):
    a = abs(f)
    if a >= 1e9:
        return "%.*f GHz" % (digits, f / 1e9)
    if a >= 1e6:
        return "%.*f MHz" % (digits, f / 1e6)
    if a >= 1e3:
        return "%.*f kHz" % (max(digits - 3, 0), f / 1e3)
    return "%.0f Hz" % f


def eng(v, unit, digits=3):
    if v == 0 or not np.isfinite(v):
        return "%s %s" % ("0" if v == 0 else "--", unit)
    for s, m in (("G", 1e9), ("M", 1e6), ("k", 1e3), ("", 1), ("m", 1e-3), ("µ", 1e-6), ("n", 1e-9), ("p", 1e-12), ("f", 1e-15)):
        if abs(v) >= m * 0.9995:
            return "%.*g %s%s" % (digits + 1, v / m, s, unit)
    return "%.3g %s" % (v, unit)


def step125(v, up):
    v = max(abs(v), 1e-15)
    e = 10 ** math.floor(math.log10(v) + 1e-9)
    m = v / e
    if up:
        return next(s for s in (1, 2, 5, 10) if s > m * (1 + 1e-9)) * e
    return [s for s in (0.5, 1, 2, 5) if s < m * (1 - 1e-9)][-1] * e


def nice125(v):
    v = max(abs(v), 1e-12)
    e = 10 ** math.floor(math.log10(v))
    for s in (1, 2, 5, 10):
        if s * e >= v * 0.999:
            return s * e
    return 10 * e


class Param:
    FREQ_UNITS = [("GHz", 1e9), ("MHz", 1e6), ("kHz", 1e3), ("Hz", 1)]

    def __init__(self, name, get, set_, fmt, fine, coarse=None, units=None, default_unit=None):
        self.name, self.get, self.set, self.fmt, self.fine = name, get, set_, fmt, fine
        self.coarse = coarse or (lambda up: fine(10 if up else -10))
        self.units = units or [("Enter", 1)]
        self.default_unit = default_unit if default_unit is not None else self.units[0][1]


class MI:
    def __init__(self, label, kind="action", press=None, value=None, param=None):
        self.label, self.kind, self.press, self.value, self.param = label, kind, press, value, param


class Trace:
    def __init__(self, n, sparam, fmt):
        self.n = n
        self.color = TRACE_COLORS[n]
        self.sparam = sparam
        self.delay = 0.0
        self.phase_offset = 0.0
        self.smooth = False
        self.aperture = 2.0
        self.mem = None              # (freqs, complex)
        self.display = "Data"        # Data | Mem | Data&Mem
        self.math = False            # Data/Mem
        self.set_format(fmt)

    def set_format(self, fmt):
        self.fmt = fmt
        self.scale, self.ref, self.refpos, self.unit = FMT_DEF[fmt]


# ---------------------------------------------------------------- data formatting
def tdr(freqs, s, vf):
    n = len(s)
    nfft = 1 << int(math.ceil(math.log2(n * 4)))
    w = np.kaiser(n, 6.0)
    h = np.fft.ifft(s * w, nfft) * nfft / np.sum(w)
    df = freqs[1] - freqs[0] if n > 1 else 1
    t = np.arange(nfft) / (nfft * df)
    half = nfft // 2
    return t[:half] * C0 * vf / 2, np.abs(h[:half])


def format_data(fmt, freqs, s, vf=0.66, phase_offset=0.0):
    """Returns (x, y) real arrays; Smith/Polar return (re, im)."""
    if fmt in CIRCULAR:
        return s.real, s.imag
    if fmt == "TDR":
        return tdr(freqs, s, vf)
    if fmt == "LogMag":
        y = 20 * np.log10(np.abs(s) + 1e-15)
    elif fmt == "Phase":
        y = (np.degrees(np.angle(s)) + phase_offset + 180) % 360 - 180
    elif fmt == "UPhase":
        y = np.degrees(np.unwrap(np.angle(s))) + phase_offset
    elif fmt == "GDelay":
        ph = np.unwrap(np.angle(s))
        y = -np.gradient(ph, 2 * np.pi * freqs) * 1e9 if len(s) > 2 else np.zeros(len(s))
    elif fmt == "SWR":
        m = np.clip(np.abs(s), 0, 0.9999)
        y = (1 + m) / (1 - m)
    elif fmt == "LinMag":
        y = np.abs(s)
    elif fmt == "Real":
        y = s.real
    elif fmt == "Imag":
        y = s.imag
    else:
        z = 50 * (1 + s) / (1 - s + 1e-15)
        y = {"|Z|": np.abs(z), "R": z.real, "X": z.imag}[fmt]
    return freqs, y


def smooth(y, aperture_pct):
    n = len(y)
    k = max(1, int(round(n * aperture_pct / 100)))
    if k < 2:
        return y
    kern = np.ones(k) / k
    pad = np.pad(y, (k // 2, k - 1 - k // 2), mode="edge")
    return np.convolve(pad, kern, mode="valid")


def smith_grid():
    paths = []
    t = np.tan(np.linspace(-np.pi / 2 + 1e-3, np.pi / 2 - 1e-3, 400)) * 3
    for r in (0, 0.2, 0.5, 1, 2, 5):
        z = r + 1j * np.sinh(np.linspace(-6, 6, 500))
        g = (z - 1) / (z + 1)
        paths.append(g)
    for x in (0.2, 0.5, 1, 2, 5):
        for sx in (1, -1):
            z = np.concatenate([np.linspace(0, 1, 100), np.exp(np.linspace(0, 6, 300))]) + 1j * sx * x
            paths.append((z - 1) / (z + 1))
    paths.append(np.array([-1 + 0j, 1 + 0j]))
    return paths


def polar_grid():
    paths = []
    th = np.linspace(0, 2 * np.pi, 361)
    for r in (0.2, 0.4, 0.6, 0.8, 1.0):
        paths.append(r * np.exp(1j * th))
    for a in range(0, 180, 30):
        v = np.exp(1j * np.radians(a))
        paths.append(np.array([-v, v]))
    return paths


# ---------------------------------------------------------------- stylesheet
QSS = """
QMainWindow, #bezel { background: qlineargradient(x1:0,y1:0,x2:0,y2:1, stop:0 #eef0f2, stop:1 #dcdfe3); }
#brand { color:#1d4f9c; font: bold 17pt "Segoe UI"; }
#model { color:#2b3340; font: 16pt "Segoe UI"; }
#tag { color:#20262f; font: bold 11pt "Segoe UI"; }
#range { background:#111; color:white; font: bold 10pt "Segoe UI"; padding:2px 14px; border-radius:2px; }
#screenBezel { background:#1d1f23; border-radius:10px; }
#screen { background:#000; border:2px solid #4b5563; }
#screen QLabel { color:#dcdcdc; font: 8pt "Segoe UI"; }
#toolbar { background:#2a2d33; border-bottom:1px solid #4b5563; }
#logo { color:#f5c400; font: bold italic 13pt "Segoe UI"; padding: 0 8px; }
QPushButton#tb { background:#3a3f47; color:#e8e8e8; border:1px solid #5a616b; border-radius:3px;
                 font: 8pt "Segoe UI"; min-width:34px; min-height:22px; padding:0 4px; }
QPushButton#tb:hover { border-color:#3b8ef0; }
QPushButton#tb:pressed { background:#1f6fd1; }
#panel { background:#000; border:1px solid #2b2b2b; }
#panel[active="true"] { border:2px solid #3ecf8e; }
#statusBar { background:#16181b; border-top:1px solid #4b5563; }
#statusBar QLabel { color:#d8d8d8; font: 8pt "Segoe UI"; }
#menuCol { background:#14161a; border-left:1px solid #3d434c; }
#menuTitle { background:#1f4f8f; color:white; font: bold 10pt "Segoe UI"; }
#slot { background:#22262c; border:1px solid #353b44; border-radius:3px; margin:2px 4px; }
#slot:hover { border-color:#3b8ef0; }
#slot[active="true"] { background:#16457e; border-color:#5aa0ff; }
#slotLabel { color:white; font: bold 9pt "Segoe UI"; }
#slotValue { color:#cfcfcf; font: 8.5pt "Segoe UI"; }
QPushButton#pg { background:#2a2f36; color:#ccc; border:1px solid #444b55; border-radius:3px; font: 9pt "Segoe UI"; min-height:22px; }
#entryBox { color:white; background: rgba(20,60,120,225); border:1px solid #4a8be0; font: 11pt "Segoe UI"; padding:4px 10px; }
#msgBox { color:#ffd166; background: rgba(0,0,0,200); border:1px solid #555; font: 9pt "Segoe UI"; padding:3px 8px; }
#readout { color:#e8e8e8; font: 8pt "Consolas"; background: rgba(0,0,0,150); }

#ctrlPanel { background: qlineargradient(x1:0,y1:0,x2:0,y2:1, stop:0 #e7e9ec, stop:1 #d9dce0);
             border:1px solid #b7bcc3; border-radius:8px; }
#grp { background:#cfd3d9; border-radius:4px; }
#grpTitle { color:#4a5260; font: bold 7pt "Segoe UI"; letter-spacing:1px; }
QPushButton#key { background: qlineargradient(x1:0,y1:0,x2:0,y2:1, stop:0 #fbfbfb, stop:1 #d5d9de);
    border:1px solid #6f7782; border-bottom:2px solid #59616c; border-radius:5px; color:#1e2530;
    font: 8.5pt "Segoe UI"; min-height:30px; min-width:62px; }
QPushButton#key:hover, QPushButton#num:hover, QPushButton#dpad:hover { border-color:#3b7ddd; }
QPushButton#key:pressed, QPushButton#num:pressed { background:#c0c6ce; }
QPushButton#key:checked { border:2px solid #2f7de1; background: qlineargradient(x1:0,y1:0,x2:0,y2:1, stop:0 #ffffff, stop:1 #c7dbf5); }
QPushButton#num { background: qlineargradient(x1:0,y1:0,x2:0,y2:1, stop:0 #fbfbfb, stop:1 #d5d9de);
    border:1px solid #6f7782; border-bottom:2px solid #59616c; border-radius:5px; color:#111;
    font: bold 13pt "Segoe UI"; min-height:32px; min-width:48px; }
QPushButton#auto { background: qlineargradient(x1:0,y1:0,x2:0,y2:1, stop:0 #7fe08a, stop:1 #2fa548);
    border:1px solid #1f7a33; border-radius:5px; color:#0d2a14; font: bold 9pt "Segoe UI"; min-height:24px; min-width:62px; }
QPushButton#preset { background: qlineargradient(x1:0,y1:0,x2:0,y2:1, stop:0 #ffe873, stop:1 #e8c21c);
    border:1px solid #a88a0c; border-radius:5px; color:#7a1010; font: bold 9pt "Segoe UI"; min-height:24px; min-width:62px; }
QPushButton#dpad { background: qlineargradient(x1:0,y1:0,x2:0,y2:1, stop:0 #ffffff, stop:1 #d0d5db);
    border:1px solid #6f7782; border-radius:6px; color:#222; font: 12pt "Segoe UI"; min-width:36px; min-height:30px; }
QComboBox { background:white; border:1px solid #8995a6; border-radius:3px; padding:1px 6px; font: 9pt "Segoe UI"; min-width:120px; }
#lowerPanel { background: qlineargradient(x1:0,y1:0,x2:0,y2:1, stop:0 #d9dce0, stop:1 #c8ccd2);
              border:1px solid #b2b8c0; border-radius:6px; }
#small { color:#3c4a5e; font: 8pt "Segoe UI"; }
"""


# ======================================================================
class TracePanel(ClickFrame):
    def __init__(self, app, tr):
        super().__init__()
        self.app = app
        self.tr = tr
        self.setObjectName("panel")
        v = QVBoxLayout(self)
        v.setContentsMargins(3, 2, 3, 2)
        v.setSpacing(0)
        self.header = QLabel()
        self.header.setTextFormat(Qt.TextFormat.RichText)
        v.addWidget(self.header)
        self.plot = pg.PlotWidget(background="#000000")
        self.plot.setMenuEnabled(False)
        self.plot.setMouseEnabled(False, False)
        self.plot.hideButtons()
        pi = self.plot.getPlotItem()
        pi.hideAxis("bottom")
        ax = pi.getAxis("left")
        ax.setWidth(42)
        ax.setStyle(tickLength=0)
        ax.setTickFont(QFont("Segoe UI", 7))
        ax.setTextPen("#bfbfbf")
        v.addWidget(self.plot, 1)
        self.footer = QLabel()
        self.footer.setTextFormat(Qt.TextFormat.RichText)
        v.addWidget(self.footer)
        self.readout = QLabel(self.plot)
        self.readout.setObjectName("readout")
        self.readout.setTextFormat(Qt.TextFormat.RichText)
        self.readout.hide()

        gp = pg.mkPen((85, 85, 85), width=1, style=Qt.PenStyle.DashLine)
        self.vgrid = [pg.InfiniteLine(angle=90, pen=gp) for _ in range(11)]
        self.hgrid = [pg.InfiniteLine(angle=0, pen=gp) for _ in range(11)]
        self.cgrid = [pg.PlotCurveItem(pen=pg.mkPen((85, 85, 85), width=1)) for _ in range(30)]
        for it in self.vgrid + self.hgrid + self.cgrid:
            self.plot.addItem(it)
        self.mem_curve = pg.PlotCurveItem(pen=pg.mkPen("#9a9a9a", width=1))
        self.curve = pg.PlotCurveItem(pen=pg.mkPen(tr.color, width=1.4))
        self.plot.addItem(self.mem_curve)
        self.plot.addItem(self.curve)
        self.ref_arrow = pg.TextItem("▶", color=tr.color, anchor=(0, 0.5))
        self.plot.addItem(self.ref_arrow)
        self.mk_scatter = pg.ScatterPlotItem(size=9, pen=pg.mkPen("w"), brush=pg.mkBrush(0, 0, 0, 0))
        self.plot.addItem(self.mk_scatter)
        self.mk_text = [pg.TextItem("", color="w", anchor=(0.5, 1.5)) for _ in range(4)]
        for t in self.mk_text:
            self.plot.addItem(t)
        self.bw_lines = [pg.InfiniteLine(angle=90, pen=pg.mkPen("#ff8a3d", style=Qt.PenStyle.DotLine)) for _ in range(2)]
        for ln in self.bw_lines:
            self.plot.addItem(ln)
            ln.hide()
        self.plot.scene().sigMouseClicked.connect(self.scene_clicked)
        self.clicked.connect(lambda: self.app.select_trace(self.tr.n))
        self.geom = None
        self.x = self.y = None

    def scene_clicked(self, ev):
        vb = self.plot.getPlotItem().vb
        self.app.panel_clicked(self.tr.n, vb.mapSceneToView(ev.scenePos()))

    def set_active(self, on):
        self.setProperty("active", "true" if on else "false")
        self.style().unpolish(self)
        self.style().polish(self)

    def setup_geometry(self, start, stop, xs=None):
        tr = self.tr
        circ = tr.fmt in CIRCULAR
        key = (tr.fmt, tr.scale, tr.ref, tr.refpos, start, stop, None if xs is None else (xs[0], xs[-1]))
        if key == self.geom:
            return
        self.geom = key
        pi = self.plot.getPlotItem()
        vb = pi.vb
        if circ:
            pi.hideAxis("left")
            vb.setAspectLocked(True)
            for ln in self.vgrid + self.hgrid:
                ln.hide()
            paths = smith_grid() if tr.fmt == "Smith" else polar_grid()
            for i, c in enumerate(self.cgrid):
                if i < len(paths):
                    p = paths[i] * tr.scale
                    c.setData(p.real, p.imag)
                    c.show()
                else:
                    c.hide()
            r = tr.scale * 1.05
            vb.setRange(xRange=(-r, r), yRange=(-r, r), padding=0)
            self.ref_arrow.hide()
        else:
            pi.showAxis("left")
            vb.setAspectLocked(False)
            for c in self.cgrid:
                c.hide()
            x0, x1 = (start, stop) if xs is None else (xs[0], xs[-1])
            bot = tr.ref - tr.refpos * tr.scale
            top = bot + 10 * tr.scale
            vb.setRange(xRange=(x0, x1), yRange=(bot, top), padding=0)
            for i in range(11):
                self.vgrid[i].setValue(x0 + i * (x1 - x0) / 10)
                self.hgrid[i].setValue(bot + i * tr.scale)
                self.vgrid[i].show()
                self.hgrid[i].show()
            ticks = [(bot + i * tr.scale, "%g" % round(bot + i * tr.scale, 9)) for i in range(11)]
            pi.getAxis("left").setTicks([ticks, []])
            self.ref_arrow.setPos(x0, tr.ref)
            self.ref_arrow.show()


# ======================================================================
class VNAPanel(QMainWindow):
    def __init__(self, args):
        super().__init__()
        self.args = args
        self.setWindowTitle(APP_NAME)
        self.worker = None
        self.is_sim = False

        # channel state
        self.start, self.stop, self.points = 1e6, 3e9, 201
        self.chunk = 201
        self.running = True
        self.avg_on, self.avg_n, self.avg_count = False, 16, 0
        self.avg_s11 = self.avg_s21 = None
        self.vf = 0.66
        self.correction = "Device"        # Device | App | Off
        self.cal = Calibration()
        self.cal_avg = 3
        self.acq = None
        self.sim_dut = "filter"
        self.freqs = None
        self.s = None
        self.swt = 0.0
        self.sweeps = 0

        self.traces = [Trace(0, "S11", "Smith"), Trace(1, "S21", "LogMag"),
                       Trace(2, "S21", "Phase"), Trace(3, "S11", "SWR")]
        self.ntraces = 4
        self.maximize = False
        self.at = 0                       # active trace
        self.markers = [dict(on=False, idx=0) for _ in range(4)]
        self.mk = 0
        self.search_track = None          # "max" | "min" | None
        self.target = -3.0
        self.bw_on = False
        self.bw_level = 3.0
        self.bw_result = None

        self.menu_stack = ["FREQ"]
        self.menu_page = 0
        self.active = None
        self.entry = None
        self.tokens = {}
        self.next_token = 1

        self.build_params()
        self.menus = self.build_menus()
        self.build_ui()
        self.relayout()
        self.render_menu()
        self.update_all()

        self.clock = QTimer(self)
        self.clock.timeout.connect(self.update_status)
        self.clock.start(1000)

        self.refresh_ports()
        if args.sim:
            self.port_combo.setCurrentIndex(self.port_combo.findData(SIM_PORT))
            QTimer.singleShot(100, self.connect_device)
        elif args.port:
            i = self.port_combo.findData(args.port)
            if i < 0:
                self.port_combo.insertItem(0, args.port, args.port)
                i = 0
            self.port_combo.setCurrentIndex(i)
            QTimer.singleShot(100, self.connect_device)
        elif any(t for _, _, t in list_ports()):
            QTimer.singleShot(300, self.connect_device)
        else:
            QTimer.singleShot(200, lambda: self.flash("No NanoVNA found - pick a port (or Simulator) and press the power key", 8000))

    # ================================================================ UI build
    def build_ui(self):
        root = QWidget()
        root.setObjectName("bezel")
        self.setCentralWidget(root)
        outer = QHBoxLayout(root)
        outer.setContentsMargins(14, 10, 14, 10)
        outer.setSpacing(12)

        # ---- left: brand + screen + lower panel
        left = QVBoxLayout()
        left.setSpacing(6)
        top = QHBoxLayout()
        b = QLabel("NanoVNA")
        b.setObjectName("brand")
        m = QLabel("-F V3")
        m.setObjectName("model")
        t = QLabel("      Vector Network Analyzer      ")
        t.setObjectName("tag")
        r = QLabel("1MHz-6GHz")
        r.setObjectName("range")
        for w in (b, m, t, r):
            top.addWidget(w)
        top.addStretch(1)
        left.addLayout(top)

        sb = QFrame()
        sb.setObjectName("screenBezel")
        sbl = QVBoxLayout(sb)
        sbl.setContentsMargins(16, 14, 16, 14)
        sbl.addWidget(self.build_screen())
        left.addWidget(sb, 1)

        low = QFrame()
        low.setObjectName("lowerPanel")
        ll = QHBoxLayout(low)
        ll.setContentsMargins(14, 6, 14, 4)
        self.power = PowerBar()
        self.power.clicked.connect(self.toggle_connect)
        ll.addWidget(self.power)
        ll.addSpacing(14)
        col = QVBoxLayout()
        l = QLabel("USB port")
        l.setObjectName("small")
        col.addWidget(l)
        row = QHBoxLayout()
        self.port_combo = QComboBox()
        self.port_combo.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        row.addWidget(self.port_combo)
        rb = PanelButton("⟳", "key", w=36)
        rb.setStyleSheet("min-width:30px;")
        rb.clicked.connect(self.refresh_ports)
        row.addWidget(rb)
        col.addLayout(row)
        col.addStretch(1)
        ll.addLayout(col)
        ll.addStretch(1)
        ll.addWidget(PortConnector(1, "#e8cf2a", "S11 / TX\nPORT 1"))
        ll.addSpacing(40)
        ll.addWidget(PortConnector(2, "#2bb5c9", "S21 / RX\nPORT 2"))
        left.addWidget(low)
        outer.addLayout(left, 1)

        outer.addWidget(self.build_controls())

    def build_screen(self):
        scr = QFrame()
        scr.setObjectName("screen")
        self.screen = scr
        h = QHBoxLayout(scr)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(0)
        v = QVBoxLayout()
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        tb = QFrame()
        tb.setObjectName("toolbar")
        tl = QHBoxLayout(tb)
        tl.setContentsMargins(4, 3, 4, 3)
        tl.setSpacing(4)
        lg = QLabel("NanoVNA")
        lg.setObjectName("logo")
        tl.addWidget(lg)
        for text, tip, fn in (("Tr+", "Next trace", self.next_trace),
                              ("▦", "Layout 1/2/4 traces", self.cycle_layout),
                              ("⤢", "Maximize active trace", lambda: self.set_maximize(not self.maximize)),
                              ("Auto", "Auto scale all traces", self.auto_scale_all),
                              ("→M", "Data → Memory (active trace)", self.data_to_mem),
                              ("Cal", "Calibration menu", lambda: self.open_menu("CAL")),
                              ("Mkr", "Marker menu", lambda: self.open_menu("MARKER")),
                              ("S2P", "Save Touchstone", self.save_s2p),
                              ("📷", "Save screenshot", self.save_png),
                              ("❚❚", "Run / Hold", self.toggle_hold)):
            b = PanelButton(text, "tb")
            b.setToolTip(tip)
            b.clicked.connect(fn)
            tl.addWidget(b)
        tl.addStretch(1)
        b = PanelButton("?", "tb")
        b.clicked.connect(self.show_help)
        tl.addWidget(b)
        v.addWidget(tb)

        self.grid_host = QWidget()
        self.grid = QGridLayout(self.grid_host)
        self.grid.setContentsMargins(3, 3, 3, 3)
        self.grid.setSpacing(3)
        self.panels = [TracePanel(self, tr) for tr in self.traces]
        v.addWidget(self.grid_host, 1)

        st = QFrame()
        st.setObjectName("statusBar")
        sl = QHBoxLayout(st)
        sl.setContentsMargins(6, 2, 6, 2)
        self.st_left = QLabel()
        self.st_right = QLabel()
        self.st_left.setTextFormat(Qt.TextFormat.RichText)
        self.st_right.setTextFormat(Qt.TextFormat.RichText)
        sl.addWidget(self.st_left)
        sl.addStretch(1)
        sl.addWidget(self.st_right)
        v.addWidget(st)
        h.addLayout(v, 1)

        mc = QFrame()
        mc.setObjectName("menuCol")
        mc.setFixedWidth(138)
        ml = QVBoxLayout(mc)
        ml.setContentsMargins(0, 0, 0, 4)
        ml.setSpacing(0)
        self.menu_title = QLabel()
        self.menu_title.setObjectName("menuTitle")
        self.menu_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.menu_title.setFixedHeight(30)
        ml.addWidget(self.menu_title)
        self.slots = []
        for i in range(SLOTS):
            f = ClickFrame()
            f.setObjectName("slot")
            f.setCursor(Qt.CursorShape.PointingHandCursor)
            sv = QVBoxLayout(f)
            sv.setContentsMargins(2, 1, 2, 1)
            sv.setSpacing(0)
            a = QLabel()
            a.setObjectName("slotLabel")
            a.setAlignment(Qt.AlignmentFlag.AlignCenter)
            bb = QLabel()
            bb.setObjectName("slotValue")
            bb.setAlignment(Qt.AlignmentFlag.AlignCenter)
            bb.setTextFormat(Qt.TextFormat.RichText)
            sv.addStretch(1)
            sv.addWidget(a)
            sv.addWidget(bb)
            sv.addStretch(1)
            f.clicked.connect(lambda i=i: self.softkey(i))
            ml.addWidget(f, 1)
            self.slots.append((f, a, bb))
        pr = QHBoxLayout()
        pr.setContentsMargins(4, 2, 4, 0)
        b = PanelButton("◀ Back", "pg")
        b.clicked.connect(self.menu_return)
        pr.addWidget(b)
        self.page_btn = PanelButton("1/1 ▼", "pg")
        self.page_btn.clicked.connect(self.menu_next_page)
        pr.addWidget(self.page_btn)
        ml.addLayout(pr)
        h.addWidget(mc)

        self.entry_box = QLabel(scr)
        self.entry_box.setObjectName("entryBox")
        self.entry_box.hide()
        self.msg = QLabel(scr)
        self.msg.setObjectName("msgBox")
        self.msg.hide()
        self.msg_timer = QTimer(self)
        self.msg_timer.setSingleShot(True)
        self.msg_timer.timeout.connect(self.msg.hide)
        return scr

    def build_controls(self):
        cp = QFrame()
        cp.setObjectName("ctrlPanel")
        cp.setFixedWidth(400)
        v = QVBoxLayout(cp)
        v.setContentsMargins(10, 10, 10, 10)
        v.setSpacing(8)
        self.hard = {}

        def hk(text, menu):
            b = PanelButton(text, "key")
            b.setCheckable(True)
            b.clicked.connect(lambda: self.open_menu(menu))
            self.hard[menu] = b
            return b

        r0 = QHBoxLayout()
        r0.addStretch(1)
        r0.addWidget(hk("System", "SYSTEM"))
        b = PanelButton("Auto", "auto")
        b.setToolTip("Auto scale all traces")
        b.clicked.connect(self.auto_scale_all)
        r0.addWidget(b)
        b = PanelButton("Preset", "preset")
        b.clicked.connect(self.preset)
        r0.addWidget(b)
        v.addLayout(r0)

        def group(title, rows, cols):
            f = QFrame()
            f.setObjectName("grp")
            gl = QGridLayout(f)
            gl.setContentsMargins(6, 2, 6, 6)
            gl.setHorizontalSpacing(8)
            gl.setVerticalSpacing(6)
            t = QLabel(title)
            t.setObjectName("grpTitle")
            gl.addWidget(t, 0, 0, 1, cols)
            for r, row in enumerate(rows):
                for c, w in enumerate(row):
                    gl.addWidget(w, r + 1, c)
            return f

        v.addWidget(group("CONTROL", [[hk("Freq", "FREQ"), hk("Avg BW", "AVG"), hk("Trace", "TRACE"), hk("Display", "DISPLAY")],
                                      [hk("Port Ext", "PORTEXT"), hk("Sweep", "SWEEP"), hk("Trigger", "TRIGGER"), hk("Scale", "SCALE")]], 4))
        mid = QHBoxLayout()
        lv = QVBoxLayout()
        lv.setSpacing(8)
        lv.addWidget(group("MEASUREMENT", [[hk("Meas", "MEAS"), hk("Marker", "MARKER")],
                                           [hk("Format", "FORMAT"), hk("Search", "SEARCH")]], 2))
        lv.addWidget(group("CAL & MATH", [[hk("Cal", "CAL"), hk("Math", "MATH")]], 2))
        mid.addLayout(lv)
        mid.addStretch(1)
        self.knob = Knob(size=170)
        self.knob.turned.connect(self.knob_turned)
        mid.addWidget(self.knob, 0, Qt.AlignmentFlag.AlignVCenter)
        v.addLayout(mid)

        kp = QGridLayout()
        kp.setHorizontalSpacing(10)
        kp.setVerticalSpacing(8)
        keys = ["1", "2", "3", "4", "5", "6", "7", "8", "9", ".", "0", "+/-"]
        for i, k in enumerate(keys):
            b = PanelButton(k, "num")
            b.clicked.connect(lambda _, k=k: self.key_digit(k))
            kp.addWidget(b, i // 3, i % 3)
        dp = QGridLayout()
        dp.setSpacing(2)
        for (r, c, txt, fn) in ((0, 1, "▲", lambda: self.arrow(True)), (2, 1, "▼", lambda: self.arrow(False)),
                                (1, 0, "◀", lambda: self.knob_turned(-1)), (1, 2, "▶", lambda: self.knob_turned(1))):
            b = PanelButton(txt, "dpad")
            b.clicked.connect(fn)
            dp.addWidget(b, r, c)
        dp.addWidget(DPadCenter(), 1, 1, Qt.AlignmentFlag.AlignCenter)
        kp.addLayout(dp, 0, 3, 2, 2)
        for (r, c, txt, fn) in ((2, 3, "Back", self.key_back), (2, 4, "Hold", self.toggle_hold),
                                (3, 3, "Esc", self.key_esc), (3, 4, "Enter", self.key_enter)):
            b = PanelButton(txt, "key")
            b.setStyleSheet("min-width:52px;")
            b.clicked.connect(fn)
            kp.addWidget(b, r, c)
        v.addLayout(kp)
        v.addStretch(1)
        hint = QLabel("Knob: hover + mouse wheel  ·  type numbers on the keyboard\nF1-F8 = softkeys  ·  click a trace to select / drop a marker")
        hint.setObjectName("small")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.addWidget(hint)
        return cp

    def relayout(self):
        for p in self.panels:
            self.grid.removeWidget(p)
            p.hide()
        if self.maximize:
            show = [self.at]
        else:
            show = list(range(self.ntraces))
            if self.at >= self.ntraces:
                self.at = 0
        pos = {1: [(0, 0, 1, 2)], 2: [(0, 0, 1, 2), (1, 0, 1, 2)],
               3: [(0, 0, 1, 1), (0, 1, 1, 1), (1, 0, 1, 2)], 4: [(0, 0, 1, 1), (0, 1, 1, 1), (1, 0, 1, 1), (1, 1, 1, 1)]}[len(show)]
        for n, (r, c, rs, cs) in zip(show, pos):
            self.grid.addWidget(self.panels[n], r, c, rs, cs)
            self.panels[n].show()
        for i in range(2):
            self.grid.setRowStretch(i, 1 if (len(show) > 1 or i == 0) else 0)
            self.grid.setColumnStretch(i, 1)
        for p in self.panels:
            p.set_active(p.tr.n == self.at)

    # ================================================================ params / menus
    def build_params(self):
        fu = Param.FREQ_UNITS
        T = lambda: self.traces[self.at]

        def fstep(get, set_):
            return lambda n: set_(get() + n * max(self.span() / 100, 1e3))

        def tset(attr, conv=float):
            def f(v):
                setattr(T(), attr, conv(v))
                self.panels[self.at].geom = None
                self.redraw()
            return f

        self.params = {
            "start": Param("Start", lambda: self.start, self.set_start, fmt_freq, fstep(lambda: self.start, self.set_start),
                           lambda up: self.set_start(self.start + (1 if up else -1) * self.span() / 10), fu, 1e6),
            "stop": Param("Stop", lambda: self.stop, self.set_stop, fmt_freq, fstep(lambda: self.stop, self.set_stop),
                          lambda up: self.set_stop(self.stop + (1 if up else -1) * self.span() / 10), fu, 1e6),
            "center": Param("Center", self.center, self.set_center, fmt_freq, fstep(self.center, self.set_center),
                            lambda up: self.set_center(self.center() + (1 if up else -1) * self.span() / 10), fu, 1e6),
            "span": Param("Span", self.span, self.set_span, fmt_freq,
                          lambda n: self.set_span(self._step_n(self.span(), n)),
                          lambda up: self.set_span(self._step_n(self.span(), 1 if up else -1)), fu, 1e6),
            "points": Param("Points", lambda: self.points, self.set_points, lambda v: "%d" % v,
                            lambda n: self.set_points(self.points + 10 * n),
                            lambda up: self.set_points(self.points * 2 if up else self.points // 2)),
            "chunk": Param("Segment", lambda: self.chunk, self.set_chunk, lambda v: "%d pts" % v,
                           lambda n: self.set_chunk(self.chunk + 10 * n), lambda up: self.set_chunk(801 if up else 101)),
            "avg": Param("Avg Factor", lambda: self.avg_n, self.set_avg_n, lambda v: "%d" % v,
                         lambda n: self.set_avg_n(self.avg_n + n),
                         lambda up: self.set_avg_n(self.avg_n * 2 if up else self.avg_n // 2)),
            "aperture": Param("Smo Aperture", lambda: T().aperture, tset("aperture"), lambda v: "%.1f %%" % v,
                              lambda n: tset("aperture")(max(0.1, min(25, T().aperture + 0.5 * n))),
                              lambda up: tset("aperture")(max(0.1, min(25, T().aperture * (2 if up else 0.5))))),
            "scale": Param("Scale/Div", lambda: T().scale, lambda v: tset("scale")(max(1e-6, abs(v))),
                           lambda v: "%g %s" % (v, T().unit), lambda n: tset("scale")(self._step_n(T().scale, n)),
                           lambda up: tset("scale")(self._step_n(T().scale, 1 if up else -1))),
            "ref": Param("Ref Value", lambda: T().ref, tset("ref"), lambda v: "%g %s" % (v, T().unit),
                         lambda n: tset("ref")(T().ref + n * T().scale / 10),
                         lambda up: tset("ref")(T().ref + (1 if up else -1) * T().scale)),
            "refpos": Param("Ref Pos", lambda: T().refpos, lambda v: tset("refpos", int)(max(0, min(10, round(v)))),
                            lambda v: "%d div" % v, lambda n: tset("refpos", int)(max(0, min(10, T().refpos + n))),
                            lambda up: tset("refpos", int)(10 if up else 0)),
            "delay": Param("Elec Delay", lambda: T().delay, tset("delay"), lambda v: eng(v, "s"),
                           lambda n: tset("delay")(T().delay + n * 1e-12),
                           lambda up: tset("delay")(T().delay + (1 if up else -1) * 100e-12),
                           [("ns", 1e-9), ("ps", 1e-12)], 1e-9),
            "phoff": Param("Phase Offset", lambda: T().phase_offset, tset("phase_offset"), lambda v: "%.1f°" % v,
                           lambda n: tset("phase_offset")(T().phase_offset + n),
                           lambda up: tset("phase_offset")(T().phase_offset + (10 if up else -10)), [("deg", 1)]),
            "vf": Param("Velocity Fac", lambda: self.vf, self.set_vf, lambda v: "%.3f" % v,
                        lambda n: self.set_vf(self.vf + 0.01 * n), lambda up: self.set_vf(self.vf + (0.1 if up else -0.1))),
            "mkfreq": Param("Marker Freq", self.mk_freq, self.set_mk_freq, fmt_freq, lambda n: self.move_marker(n),
                            lambda up: self.move_marker(10 if up else -10), fu, 1e6),
            "target": Param("Target", lambda: self.target, self.set_target, lambda v: "%g" % v,
                            lambda n: self.set_target(self.target + 0.1 * n),
                            lambda up: self.set_target(self.target + (1 if up else -1))),
            "bwlevel": Param("BW Level", lambda: self.bw_level, self.set_bw_level, lambda v: "-%g dB" % v,
                             lambda n: self.set_bw_level(self.bw_level + 0.5 * n),
                             lambda up: self.set_bw_level(self.bw_level + (3 if up else -3)), [("dB", 1)]),
            "calavg": Param("Cal Averages", lambda: self.cal_avg, lambda v: setattr(self, "cal_avg", int(max(1, min(64, v)))),
                            lambda v: "%d sweeps" % v, lambda n: setattr(self, "cal_avg", int(max(1, min(64, self.cal_avg + n)))),
                            lambda up: setattr(self, "cal_avg", 16 if up else 1)),
        }

    def build_menus(self):
        T = lambda: self.traces[self.at]

        def prm(label, key):
            return MI(label, "param", param=key)

        def tog(label, get, set_, a="On", b="Off"):
            return MI(label, "toggle", press=lambda: set_(not get()), value=lambda: ([a, b], 0 if get() else 1))

        def choice(label, opts, get, set_):
            def press():
                i = opts.index(get()) if get() in opts else -1
                set_(opts[(i + 1) % len(opts)])
            return MI(label, "toggle", press=press, value=lambda: (opts, opts.index(get()) if get() in opts else -1))

        def act(label, fn, value=None):
            return MI(label, "action", press=fn, value=value)

        def radio(label, get, set_, val):
            return MI(label, "radio", press=lambda: set_(val), value=lambda: get() == val)

        M = {}
        M["FREQ"] = ("Frequency", [prm("Start", "start"), prm("Stop", "stop"), prm("Center", "center"),
                                   prm("Span", "span"), act("Full Span", lambda: self.set_range(FMIN, FMAX))])
        M["AVG"] = ("Average / BW", [tog("Averaging", lambda: self.avg_on, self.set_avg_on), prm("Avg Factor", "avg"),
                                     act("Avg Restart", self.restart_avg, value=lambda: "%d/%d" % (self.avg_count, self.avg_n)),
                                     tog("Smoothing", lambda: T().smooth, self.set_smooth), prm("Smo Aperture", "aperture"),
                                     act("IF BW", lambda: self.flash("IF bandwidth is fixed by the NanoVNA-F firmware"), value=lambda: "device")])
        M["TRACE"] = ("Trace", [choice("Active Trace", ["1", "2", "3", "4"], lambda: str(self.at + 1),
                                       lambda v: self.select_trace(int(v) - 1)),
                                choice("Meas", ["S11", "S21"], lambda: T().sparam, self.set_sparam),
                                act("Format…", lambda: self.open_menu("FORMAT"), value=lambda: T().fmt),
                                tog("Maximize", lambda: self.maximize, self.set_maximize),
                                act("Data→Mem", self.data_to_mem),
                                choice("Display", ["Data", "Mem", "Data&Mem"], lambda: T().display, self.set_display)])
        M["DISPLAY"] = ("Display", [choice("Number of Displays", ["1", "2", "3", "4"], lambda: str(self.ntraces),
                                           lambda v: self.set_ntraces(int(v))),
                                    tog("Maximize", lambda: self.maximize, self.set_maximize),
                                    choice("Active Trace", ["1", "2", "3", "4"], lambda: str(self.at + 1),
                                           lambda v: self.select_trace(int(v) - 1))])
        M["PORTEXT"] = ("Port Extension", [prm("Elec Delay", "delay"), act("Auto Delay", self.auto_delay),
                                           act("Delay 0", lambda: self.params["delay"].set(0.0)),
                                           prm("Phase Offset", "phoff"), prm("Velocity Fac", "vf"),
                                           act("Power", lambda: self.flash("Source power is fixed on the NanoVNA-F"), value=lambda: "fixed")])
        M["SWEEP"] = ("Sweep", [prm("Points", "points"), prm("Segment", "chunk"),
                                act("Sweep Time", lambda: None, value=lambda: "%.2f s" % self.swt),
                                prm("Start", "start"), prm("Stop", "stop")])
        M["TRIGGER"] = ("Trigger", [radio("Continuous", lambda: self.running, self.set_running, True),
                                    radio("Hold", lambda: self.running, self.set_running, False),
                                    act("Single", self.single_sweep), act("Restart", self.restart_avg)])
        M["SCALE"] = ("Scale", [act("Auto Scale", lambda: self.auto_scale(self.at)), act("Auto All", self.auto_scale_all),
                                prm("Scale/Div", "scale"), prm("Ref Value", "ref"), prm("Ref Pos", "refpos"),
                                prm("Elec Delay", "delay"), prm("Phase Offset", "phoff")])
        M["MEAS"] = ("Measure", [radio("S11", lambda: T().sparam, self.set_sparam, "S11"),
                                 radio("S21", lambda: T().sparam, self.set_sparam, "S21"),
                                 act("S12", lambda: self.flash("NanoVNA measures S11/S21 only - reverse the DUT for S12/S22"), value=lambda: "n/a"),
                                 act("S22", lambda: self.flash("NanoVNA measures S11/S21 only - reverse the DUT for S12/S22"), value=lambda: "n/a")])
        M["FORMAT"] = ("Format", [radio(f, lambda: T().fmt, self.set_format, f) for f in FORMATS])
        M["MARKER"] = ("Marker", [choice("Marker", ["1", "2", "3", "4"], lambda: str(self.mk + 1),
                                         lambda v: self.select_marker(int(v) - 1)),
                                  tog("State", lambda: self.markers[self.mk]["on"], self.set_marker_on),
                                  prm("Marker Freq", "mkfreq"),
                                  act("Mkr→Center", lambda: self.mk_to("center")),
                                  act("Mkr→Start", lambda: self.mk_to("start")),
                                  act("Mkr→Stop", lambda: self.mk_to("stop")),
                                  act("Mkr→Delay", self.auto_delay),
                                  act("All Off", self.markers_off)])
        M["SEARCH"] = ("Search", [act("Max", lambda: self.search("max")), act("Min", lambda: self.search("min")),
                                  act("Peak Next", self.peak_next),
                                  prm("Target", "target"), act("Tgt Left", lambda: self.search_target(-1)),
                                  act("Tgt Right", lambda: self.search_target(1)),
                                  choice("Tracking", ["Off", "Max", "Min"],
                                         lambda: (self.search_track or "off").title(),
                                         lambda v: setattr(self, "search_track", None if v == "Off" else v.lower())),
                                  tog("BW Search", lambda: self.bw_on, self.set_bw_on), prm("BW Level", "bwlevel")])
        M["CAL"] = ("Calibration", [
            choice("Correction", ["Device", "App", "Off"], lambda: self.correction, self.set_correction),
            act("Open", lambda: self.measure_std("open"), value=lambda: self.std_state("open")),
            act("Short", lambda: self.measure_std("short"), value=lambda: self.std_state("short")),
            act("Load", lambda: self.measure_std("load"), value=lambda: self.std_state("load")),
            act("Thru", lambda: self.measure_std("thru"), value=lambda: self.std_state("thru")),
            act("Isolation", lambda: self.measure_std("isolation"), value=lambda: self.std_state("isolation") + " (opt)"),
            act("Done / Apply", self.apply_cal, value=lambda: self.cal.kind()),
            act("Clear Cal", self.clear_cal),
            prm("Cal Averages", "calavg"),
            act("Save Cal…", self.save_cal), act("Load Cal…", self.load_cal),
            act("Dev Recall…", self.device_recall), act("Dev Save…", self.device_save),
        ])
        M["MATH"] = ("Math", [act("Data→Mem", self.data_to_mem),
                              choice("Display", ["Data", "Mem", "Data&Mem"], lambda: T().display, self.set_display),
                              tog("Data/Mem", lambda: T().math, self.set_math),
                              act("Load S1P/S2P→Mem", self.load_touchstone_mem),
                              tog("Smoothing", lambda: T().smooth, self.set_smooth), prm("Smo Aperture", "aperture"),
                              prm("Velocity Fac", "vf")])
        M["SYSTEM"] = ("System", [
            act("Connect", self.toggle_connect, value=lambda: "Online" if self.worker else "Offline"),
            act("Device Info", self.device_info), act("Console…", self.console),
            act("Dev Screen", self.device_capture),
            act("Save S1P…", lambda: self.save_touchstone(1)), act("Save S2P…", lambda: self.save_touchstone(2)),
            act("Save CSV…", self.save_csv), act("Save PNG…", self.save_png),
            choice("Sim DUT", ["filter", "open", "short", "load", "thru"], lambda: self.sim_dut, self.set_sim_dut),
            act("Help", self.show_help),
        ])
        return M

    # ---------------------------------------------------------------- menu engine
    def open_menu(self, name):
        self.cancel_entry()
        self.menu_stack = [name]
        self.menu_page = 0
        self.render_menu()

    def menu_return(self):
        if self.entry is not None:
            self.cancel_entry()
            return
        if len(self.menu_stack) > 1:
            self.menu_stack.pop()
        self.menu_page = 0
        self.render_menu()

    def menu_next_page(self):
        pages = max(1, math.ceil(len(self.current_items()) / SLOTS))
        self.menu_page = (self.menu_page + 1) % pages
        self.render_menu()

    def current_items(self):
        if self.entry is not None and self.active is not None:
            return [MI(u, "action", press=lambda m=m: self.apply_entry(m)) for u, m in self.active.units]
        return self.menus[self.menu_stack[-1]][1]

    def render_menu(self):
        name = self.menu_stack[-1]
        for k, b in self.hard.items():
            b.setChecked(k == name)
        items = self.current_items()
        pages = max(1, math.ceil(len(items) / SLOTS))
        self.menu_page = min(self.menu_page, pages - 1)
        self.menu_title.setText("Units" if self.entry is not None else self.menus[name][0])
        page = items[self.menu_page * SLOTS:(self.menu_page + 1) * SLOTS]
        for i, (f, a, b) in enumerate(self.slots):
            if i < len(page):
                it = page[i]
                f.show()
                a.setText(it.label)
                b.setText(self.item_value(it))
                act = (it.kind == "param" and self.active is self.params.get(it.param)) or \
                      (it.kind == "radio" and it.value())
                f.setProperty("active", "true" if act else "false")
            else:
                a.setText("")
                b.setText("")
                f.setProperty("active", "false")
            f.style().unpolish(f)
            f.style().polish(f)
        self.page_btn.setText("%d/%d ▼" % (self.menu_page + 1, pages))

    def item_value(self, it):
        if it.kind == "param":
            p = self.params[it.param]
            return p.fmt(p.get())
        if it.kind == "toggle" and it.value:
            opts, sel = it.value()
            if len(opts) > 2:
                return '<span style="color:#7fd0ff">%s</span>' % (opts[sel] if sel >= 0 else "-")
            return "".join('<span style="background-color:#1f6fd1;color:white">&nbsp;%s&nbsp;</span>' % o if i == sel
                           else '<span style="color:#8a8a8a">&nbsp;%s&nbsp;</span>' % o for i, o in enumerate(opts))
        if it.kind == "radio":
            return "●" if it.value() else "○"
        if it.kind == "action" and it.value:
            return str(it.value())
        return ""

    def softkey(self, i):
        items = self.current_items()
        idx = self.menu_page * SLOTS + i
        if idx >= len(items):
            return
        it = items[idx]
        if it.kind == "param":
            self.cancel_entry()
            self.active = self.params[it.param]
            self.show_active()
        elif it.press:
            it.press()
        self.render_menu()
        self.update_status()

    # ---------------------------------------------------------------- entry / knob
    def show_active(self):
        if self.active is None:
            self.entry_box.hide()
            return
        if self.entry is not None:
            txt = "%s:  <b>%s_</b>" % (self.active.name, self.entry)
        else:
            txt = "%s:  <b>%s</b>" % (self.active.name, self.active.fmt(self.active.get()))
        self.entry_box.setText(txt)
        self.entry_box.adjustSize()
        x = (self.screen.width() - 138 - self.entry_box.width()) // 2
        self.entry_box.move(max(4, x), 34)
        self.entry_box.show()
        self.entry_box.raise_()

    def flash(self, text, ms=4000):
        self.msg.setText(text)
        self.msg.adjustSize()
        self.msg.move(8, self.screen.height() - self.msg.height() - 26)
        self.msg.show()
        self.msg.raise_()
        self.msg_timer.start(ms)

    def key_digit(self, k):
        if self.active is None:
            self.flash("Select a parameter first (e.g. Freq → Start)")
            return
        if self.entry is None:
            self.entry = ""
            self.menu_page = 0
        if k == "+/-":
            self.entry = self.entry[1:] if self.entry.startswith("-") else "-" + self.entry
        elif k == ".":
            if "." not in self.entry:
                self.entry += "."
        else:
            self.entry += k
        self.show_active()
        self.render_menu()

    def key_back(self):
        if self.entry is not None:
            self.entry = self.entry[:-1]
            self.show_active()
        else:
            self.menu_return()

    def key_enter(self):
        if self.entry is not None and self.active is not None:
            self.apply_entry(self.active.default_unit)

    def key_esc(self):
        if self.entry is not None:
            self.cancel_entry()
        else:
            self.active = None
            self.show_active()
            self.render_menu()

    def cancel_entry(self):
        if self.entry is not None:
            self.entry = None
            self.show_active()
            self.render_menu()

    def apply_entry(self, mult):
        try:
            v = float(self.entry) * mult
        except (TypeError, ValueError):
            self.flash("Invalid number")
            self.entry = None
        else:
            self.entry = None
            self.active.set(v)
        self.after_change()

    def knob_turned(self, n):
        if self.entry is not None:
            return
        if self.active is not None:
            self.active.fine(n)
        elif self.markers[self.mk]["on"]:
            self.move_marker(n)
        else:
            self.flash("Knob: select a parameter, or turn on a marker")
            return
        self.after_change()

    def arrow(self, up):
        if self.active is not None:
            self.active.coarse(up)
        elif self.markers[self.mk]["on"]:
            self.move_marker(10 if up else -10)
        self.after_change()

    def after_change(self):
        self.show_active()
        self.render_menu()
        self.redraw()
        self.update_status()

    def keyPressEvent(self, e):
        k, t = e.key(), e.text()
        if t and t in "0123456789.":
            self.key_digit(t)
        elif t == "-":
            self.key_digit("+/-")
        elif k in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.key_enter()
        elif k == Qt.Key.Key_Escape:
            self.key_esc()
        elif k == Qt.Key.Key_Backspace:
            self.key_back()
        elif k in (Qt.Key.Key_Up, Qt.Key.Key_Right):
            self.knob_turned(1)
        elif k in (Qt.Key.Key_Down, Qt.Key.Key_Left):
            self.knob_turned(-1)
        elif k == Qt.Key.Key_PageUp:
            self.arrow(True)
        elif k == Qt.Key.Key_PageDown:
            self.arrow(False)
        elif Qt.Key.Key_F1 <= k <= Qt.Key.Key_F8:
            self.softkey(k - Qt.Key.Key_F1)
        else:
            super().keyPressEvent(e)

    # ================================================================ setters
    def center(self):
        return (self.start + self.stop) / 2

    def span(self):
        return self.stop - self.start

    def _step_n(self, v, n):
        for _ in range(abs(n)):
            v = step125(v, n > 0)
        return v

    def set_range(self, a, b):
        a = max(FMIN, min(a, FMAX))
        b = max(FMIN, min(b, FMAX))
        if b - a < 10e3:
            c = (a + b) / 2
            a, b = max(FMIN, c - 5e3), max(FMIN, c - 5e3) + 10e3
        self.start, self.stop = a, b
        self.push_sweep()

    def set_start(self, v):
        self.set_range(v, max(self.stop, v + 10e3))

    def set_stop(self, v):
        self.set_range(min(self.start, v - 10e3), v)

    def set_center(self, v):
        s = self.span()
        self.set_range(v - s / 2, v + s / 2)

    def set_span(self, v):
        c = self.center()
        self.set_range(c - v / 2, c + v / 2)

    def set_points(self, v):
        self.points = int(max(11, min(v, 1601)))
        self.push_sweep()

    def set_chunk(self, v):
        self.chunk = int(max(11, min(v, 801)))
        if self.worker:
            self.worker.set_chunk(self.chunk)

    def push_sweep(self):
        self.restart_avg()
        self.freqs = None
        self.s = None
        if self.worker:
            self.worker.set_sweep(self.start, self.stop, self.points)
        for p in self.panels:
            p.geom = None
        self.update_all()

    def set_avg_on(self, on):
        self.avg_on = on
        self.restart_avg()

    def set_avg_n(self, v):
        self.avg_n = int(max(1, min(v, 999)))

    def restart_avg(self):
        self.avg_count = 0
        self.avg_s11 = self.avg_s21 = None

    def set_smooth(self, on):
        self.traces[self.at].smooth = on
        self.redraw()

    def set_vf(self, v):
        self.vf = max(0.1, min(v, 1.0))
        self.redraw()

    def set_running(self, run):
        self.running = run
        if self.worker:
            self.worker.set_running(run)
        self.update_status()

    def toggle_hold(self):
        self.set_running(not self.running)
        self.flash("Continuous" if self.running else "Hold")
        self.render_menu()

    def single_sweep(self):
        self.set_running(False)
        if self.worker:
            self.worker.single_sweep()

    # traces ------------------------------------------------------------
    def select_trace(self, n):
        if n >= self.ntraces and not self.maximize:
            self.set_ntraces(n + 1)
        self.at = n
        if self.maximize:
            self.relayout()
        for p in self.panels:
            p.set_active(p.tr.n == n)
        self.show_active()
        self.render_menu()
        self.update_status()

    def next_trace(self):
        self.select_trace((self.at + 1) % self.ntraces)

    def set_ntraces(self, n):
        self.ntraces = n
        self.maximize = False
        if self.at >= n:
            self.at = 0
        self.relayout()
        self.redraw()

    def cycle_layout(self):
        self.set_ntraces({1: 2, 2: 4, 3: 4, 4: 1}[self.ntraces])

    def set_maximize(self, on):
        self.maximize = on
        self.relayout()
        self.redraw()
        self.render_menu()

    def set_sparam(self, sp):
        self.traces[self.at].sparam = sp
        self.redraw()

    def set_format(self, f):
        tr = self.traces[self.at]
        tr.set_format(f)
        self.panels[self.at].geom = None
        self.redraw()
        if f not in CIRCULAR and self.s is not None:
            self.auto_scale(self.at)

    def set_display(self, d):
        if d != "Data" and self.traces[self.at].mem is None:
            self.flash("No memory trace (Data→Mem first)")
            return
        self.traces[self.at].display = d
        self.redraw()

    def set_math(self, on):
        if on and self.traces[self.at].mem is None:
            self.flash("No memory trace (Data→Mem first)")
            return
        self.traces[self.at].math = on
        self.redraw()

    def data_to_mem(self):
        tr = self.traces[self.at]
        if self.s is None:
            self.flash("No data yet")
            return
        tr.mem = (self.freqs.copy(), self.trace_complex(tr, use_math=False).copy())
        self.flash("Tr%d data stored to memory" % (tr.n + 1))
        self.redraw()

    # markers -----------------------------------------------------------
    def npts(self):
        return len(self.freqs) if self.freqs is not None else self.points

    def idx_of(self, f):
        return int(max(0, min(self.npts() - 1, round((f - self.start) / max(self.span(), 1) * (self.npts() - 1)))))

    def mk_freq(self):
        m = self.markers[self.mk]
        if self.freqs is None:
            return self.center()
        return float(self.freqs[min(m["idx"], len(self.freqs) - 1)])

    def set_mk_freq(self, f):
        m = self.markers[self.mk]
        m["on"] = True
        m["idx"] = self.idx_of(f)

    def move_marker(self, n):
        m = self.markers[self.mk]
        m["on"] = True
        m["idx"] = max(0, min(self.npts() - 1, m["idx"] + n))
        self.search_track = None

    def select_marker(self, i):
        self.mk = i
        self.active = None
        self.show_active()

    def set_marker_on(self, on):
        m = self.markers[self.mk]
        m["on"] = on
        if on and self.freqs is not None and m["idx"] == 0:
            m["idx"] = len(self.freqs) // 2
        self.redraw()

    def markers_off(self):
        for m in self.markers:
            m["on"] = False
        self.search_track = None
        self.bw_on = False
        self.redraw()

    def mk_to(self, what):
        if not self.markers[self.mk]["on"]:
            self.flash("Marker is off")
            return
        f = self.mk_freq()
        {"center": self.set_center, "start": self.set_start, "stop": self.set_stop}[what](f)

    def panel_clicked(self, n, pt):
        if n != self.at:
            self.select_trace(n)
            return
        if self.freqs is None:
            return
        tr = self.traces[n]
        m = self.markers[self.mk]
        if tr.fmt in CIRCULAR:
            d = self.trace_complex(tr) / tr.scale
            m["idx"] = int(np.argmin(np.abs(d - complex(pt.x(), pt.y()) / tr.scale)))
        elif tr.fmt == "TDR":
            return
        else:
            m["idx"] = self.idx_of(pt.x())
        m["on"] = True
        self.search_track = None
        self.redraw()
        self.render_menu()

    # search ------------------------------------------------------------
    def active_y(self):
        tr = self.traces[self.at]
        d = self.trace_complex(tr)
        if tr.fmt in CIRCULAR or tr.fmt == "TDR":
            return 20 * np.log10(np.abs(d) + 1e-15)
        _, y = format_data(tr.fmt, self.freqs, d, self.vf, tr.phase_offset)
        return smooth(y, tr.aperture) if tr.smooth else y

    def search(self, kind, quiet=False):
        if self.s is None:
            return
        y = self.active_y()
        m = self.markers[self.mk]
        m["on"] = True
        m["idx"] = int(np.argmax(y) if kind == "max" else np.argmin(y))
        if not quiet:
            self.redraw()

    def peak_next(self):
        if self.s is None:
            return
        y = self.active_y()
        m = self.markers[self.mk]
        cur = y[m["idx"]] if m["on"] else np.inf
        pk = np.where((y[1:-1] > y[:-2]) & (y[1:-1] >= y[2:]))[0] + 1
        lower = [i for i in pk if y[i] < cur - 1e-9]
        if lower:
            m["on"] = True
            m["idx"] = int(max(lower, key=lambda i: y[i]))
            self.redraw()
        else:
            self.flash("No more peaks")

    def set_target(self, v):
        self.target = v

    def search_target(self, direction):
        if self.s is None:
            return
        y = self.active_y() - self.target
        m = self.markers[self.mk]
        i = m["idx"] if m["on"] else (0 if direction > 0 else len(y) - 1)
        rng = range(i + 1, len(y)) if direction > 0 else range(i - 1, 0, -1)
        for j in rng:
            k = j - direction
            if 0 <= k < len(y) and np.sign(y[j]) != np.sign(y[k]):
                m["on"] = True
                m["idx"] = j if abs(y[j]) < abs(y[k]) else k
                self.redraw()
                return
        self.flash("Target %g not found to the %s" % (self.target, "right" if direction > 0 else "left"))

    def set_bw_on(self, on):
        self.bw_on = on
        if on:
            self.search("max", quiet=True)
        self.redraw()

    def set_bw_level(self, v):
        self.bw_level = max(0.1, min(v, 60))

    def compute_bw(self):
        """-N dB bandwidth around the max of the active trace (LogMag of its S-parameter)."""
        tr = self.traces[self.at]
        y = 20 * np.log10(np.abs(self.trace_complex(tr)) + 1e-15)
        f = self.freqs
        i = int(np.argmax(y))
        lvl = y[i] - self.bw_level
        lo = i
        while lo > 0 and y[lo] > lvl:
            lo -= 1
        hi = i
        while hi < len(y) - 1 and y[hi] > lvl:
            hi += 1
        if y[lo] > lvl or y[hi] > lvl:
            return None

        def cross(a, b):
            t = (lvl - y[a]) / (y[b] - y[a]) if y[b] != y[a] else 0
            return f[a] + t * (f[b] - f[a])
        f1, f2 = cross(lo, lo + 1), cross(hi - 1, hi)
        bw = f2 - f1
        cf = (f1 + f2) / 2
        return dict(f1=f1, f2=f2, bw=bw, cf=cf, q=cf / bw if bw > 0 else 0, loss=y[i], fmax=f[i])

    def auto_delay(self):
        """Electrical delay from the phase slope (at the marker if on, else over the sweep)."""
        if self.s is None:
            return
        tr = self.traces[self.at]
        s = self.s[tr.sparam]
        ph = np.unwrap(np.angle(s))
        w = 2 * np.pi * self.freqs
        m = self.markers[self.mk]
        if m["on"]:
            i = m["idx"]
            a, b = max(0, i - 5), min(len(s) - 1, i + 5)
            tau = -(ph[b] - ph[a]) / (w[b] - w[a])
        else:
            tau = -np.polyfit(w, ph, 1)[0]
        tr.delay = float(tau)
        self.flash("Tr%d electrical delay = %s" % (tr.n + 1, eng(tau, "s")))
        self.redraw()

    # scaling -----------------------------------------------------------
    def auto_scale(self, n):
        if self.s is None:
            return
        tr = self.traces[n]
        d = self.trace_complex(tr)
        if tr.fmt in CIRCULAR:
            tr.scale = 1.0 if np.max(np.abs(d)) <= 1.0 else nice125(np.max(np.abs(d)))
        else:
            _, y = format_data(tr.fmt, self.freqs, d, self.vf, tr.phase_offset)
            y = y[np.isfinite(y)]
            if len(y) == 0:
                return
            lo, hi = float(np.min(y)), float(np.max(y))
            if tr.fmt == "Phase":
                tr.scale, tr.ref, tr.refpos = 45.0, 0.0, 5
            elif tr.fmt == "SWR":
                hi = min(hi, 10.0)
                tr.scale = nice125(max((hi - 1) / 9, 0.01))
                tr.ref, tr.refpos = 1.0, 0
            else:
                sc = nice125(max((hi - lo) / 8, 1e-6))
                tr.scale = sc
                tr.refpos = 5
                tr.ref = round(((hi + lo) / 2) / sc) * sc
        self.panels[n].geom = None
        self.redraw()

    def auto_scale_all(self):
        for n in range(4):
            self.auto_scale(n)
        self.render_menu()

    # calibration -------------------------------------------------------
    def set_correction(self, c):
        if c == "App" and self.cal.terms is None:
            self.flash("No app calibration yet - measure standards, then Done/Apply")
            return
        self.correction = c
        self.send("cal on" if c == "Device" else "cal off")
        self.restart_avg()
        self.update_status()

    def std_state(self, name):
        if self.acq and self.acq["name"] == name:
            return "measuring %d/%d" % (len(self.acq["s11"]), self.cal_avg)
        return "✓" if self.cal.has(name) else "—"

    def measure_std(self, name):
        if not self.worker:
            self.flash("Not connected")
            return
        if self.acq:
            self.flash("Already measuring %s" % self.acq["name"])
            return
        if self.correction != "App":
            self.send("cal off")       # raw data needed
        if self.is_sim:
            self.send("simdut %s" % ("load" if name == "isolation" else name))
        self.acq = dict(name=name, skip=1, s11=[], s21=[])
        if not self.running:
            self.set_running(True)
        hint = {"open": "Connect OPEN to port 1", "short": "Connect SHORT to port 1", "load": "Connect LOAD to port 1",
                "thru": "Connect THRU port 1 → port 2", "isolation": "Load both ports"}[name]
        self.flash("Measuring %s… (%s)" % (name.upper(), hint))
        self.render_menu()

    def finish_std(self):
        a = self.acq
        self.cal.add(a["name"], self.freqs, np.mean(a["s11"], axis=0), np.mean(a["s21"], axis=0))
        self.flash("%s measured ✓" % a["name"].upper())
        self.acq = None
        if self.is_sim:
            self.send("simdut %s" % self.sim_dut)
        if self.correction == "Device":
            self.send("cal on")
        self.render_menu()

    def apply_cal(self):
        try:
            k = self.cal.solve()
        except ValueError as e:
            self.flash(str(e), 6000)
            return
        self.correction = "App"
        self.send("cal off")
        self.restart_avg()
        self.flash("Calibration applied: %s" % k)
        self.render_menu()
        self.update_status()

    def clear_cal(self):
        self.cal.clear()
        if self.correction == "App":
            self.set_correction("Off")
        self.render_menu()

    def save_cal(self):
        if self.cal.terms is None:
            self.flash("Nothing to save")
            return
        fn, _ = QFileDialog.getSaveFileName(self, "Save calibration", "nanovna_cal.npz", "Calibration (*.npz)")
        if fn:
            self.cal.save(fn)
            self.flash("Saved " + os.path.basename(fn))

    def load_cal(self):
        fn, _ = QFileDialog.getOpenFileName(self, "Load calibration", "", "Calibration (*.npz)")
        if fn:
            try:
                k = self.cal.load(fn)
            except Exception as e:
                self.flash("Load failed: %s" % e)
                return
            self.set_range(self.cal.freqs[0], self.cal.freqs[-1])
            self.set_points(len(self.cal.freqs))
            self.correction = "App"
            self.send("cal off")
            self.flash("Loaded %s cal, sweep set to match" % k)
            self.render_menu()

    def device_recall(self):
        n, ok = QInputDialog.getInt(self, "Recall", "Recall device cal/state slot (0-6):", 0, 0, 6)
        if ok:
            self.send("recall %d" % n)
            self.correction = "Device"
            self.flash("Recalled slot %d (device sweep settings changed; app sweep unchanged)" % n)

    def device_save(self):
        n, ok = QInputDialog.getInt(self, "Save", "Save device cal/state to slot (0-6):", 0, 0, 6)
        if ok:
            self.send("save %d" % n)

    def set_sim_dut(self, d):
        self.sim_dut = d
        if self.is_sim:
            self.send("simdut %s" % d)

    # ================================================================ device
    def refresh_ports(self):
        cur = self.port_combo.currentData()
        self.port_combo.clear()
        best = -1
        for dev, desc, is_v in list_ports():
            self.port_combo.addItem(dev + ("  (NanoVNA)" if is_v else ""), dev)
            if is_v and best < 0:
                best = self.port_combo.count() - 1
        self.port_combo.addItem(SIM_PORT, SIM_PORT)
        i = self.port_combo.findData(cur) if cur else -1
        self.port_combo.setCurrentIndex(i if i >= 0 else max(best, 0))

    def toggle_connect(self):
        if self.worker:
            self.disconnect_device()
        else:
            self.connect_device()

    def connect_device(self):
        if self.worker:
            return
        port = self.port_combo.currentData()
        self.is_sim = port == SIM_PORT
        w = VNAWorker(port)
        w.sweep_ready.connect(self.on_sweep)
        w.connected.connect(self.on_connected)
        w.disconnected.connect(self.on_disconnected)
        w.reply.connect(self.on_reply)
        w.status.connect(self.flash)
        w.set_sweep(self.start, self.stop, self.points)
        w.set_chunk(self.chunk)
        w.set_running(self.running)
        self.worker = w
        w.start()
        self.power.setChecked(True)
        self.update_status()

    def disconnect_device(self):
        w = self.worker
        if w:
            self.worker = None
            w.stop()
            w.wait(5000)
            self.power.setChecked(False)
            self.acq = None
            self.render_menu()
            self.update_status()

    def on_connected(self, ver):
        if self.sender() is not self.worker:
            return
        self.version = ver.splitlines()[0] if ver else "NanoVNA"
        self.send("cal on" if self.correction == "Device" else "cal off")
        if self.is_sim:
            self.send("simdut %s" % self.sim_dut)
        self.flash("Connected: %s" % self.version)
        self.render_menu()
        self.update_status()

    def on_disconnected(self, reason):
        w = self.sender()
        if w is not None:
            w.deleteLater()
        if w is not self.worker and self.worker is not None:
            return
        self.worker = None
        self.acq = None
        self.power.setChecked(False)
        self.flash(reason, 8000)
        self.render_menu()
        self.update_status()

    def send(self, cmd, cb=None):
        if not self.worker:
            return
        token = None
        if cb:
            token = self.next_token
            self.next_token += 1
            self.tokens[token] = cb
        self.worker.send(cmd, token)

    def on_reply(self, token, result):
        cb = self.tokens.pop(token, None)
        if cb:
            cb(result)

    def device_info(self):
        if not self.worker:
            self.flash("Not connected")
            return
        self.send("version", lambda a: self.send("info", lambda b: self.send(
            "SN", lambda c: self.text_dialog("Device info", "%s\n\n%s\n\nSN: %s" % (a, b, c)))))

    def console(self):
        if not self.worker:
            self.flash("Not connected")
            return
        cmd, ok = QInputDialog.getText(self, "NanoVNA console", "Shell command (help, info, cal, version…):")
        if ok and cmd.strip():
            self.send(cmd.strip(), lambda r: self.text_dialog("> " + cmd.strip(), r or "(no output)"))

    def text_dialog(self, title, text):
        d = QDialog(self)
        d.setWindowTitle(title)
        v = QVBoxLayout(d)
        t = QPlainTextEdit(text)
        t.setReadOnly(True)
        t.setFont(QFont("Consolas", 9))
        v.addWidget(t)
        d.resize(560, 380)
        d.exec()

    def device_capture(self):
        if not self.worker:
            self.flash("Not connected")
            return
        self.flash("Capturing device screen…")
        token = self.next_token
        self.next_token += 1
        self.tokens[token] = self.show_capture
        self.worker.capture(token)

    def show_capture(self, res):
        w, h, data = res
        px = np.frombuffer(data, dtype="<u2").reshape(h, w).astype(np.uint32)
        r = ((px >> 11) & 0x1F) * 255 // 31
        g = ((px >> 5) & 0x3F) * 255 // 63
        b = (px & 0x1F) * 255 // 31
        rgb = np.ascontiguousarray(np.dstack([r, g, b]).astype(np.uint8))
        img = QImage(rgb.data, w, h, 3 * w, QImage.Format.Format_RGB888).copy()
        d = QDialog(self)
        d.setWindowTitle("NanoVNA screen")
        v = QVBoxLayout(d)
        lbl = QLabel()
        lbl.setPixmap(QPixmap.fromImage(img))
        v.addWidget(lbl)
        sb = QPushButton("Save PNG…")
        sb.clicked.connect(lambda: self._save_img(img))
        v.addWidget(sb)
        d.exec()

    def _save_img(self, img):
        fn, _ = QFileDialog.getSaveFileName(self, "Save image", self.default_name("nanovna_screen", "png"), "PNG (*.png)")
        if fn:
            img.save(fn)

    # ================================================================ files
    def default_name(self, stem, ext):
        return "%s_%s.%s" % (stem, datetime.datetime.now().strftime("%Y%m%d_%H%M%S"), ext)

    def save_png(self):
        fn, _ = QFileDialog.getSaveFileName(self, "Save screenshot", self.default_name("nanovna_panel", "png"), "PNG (*.png)")
        if fn:
            self.grab().save(fn)
            self.flash("Saved " + os.path.basename(fn))

    def save_s2p(self):
        self.save_touchstone(2)

    def save_touchstone(self, ports):
        if self.s is None:
            self.flash("No data yet")
            return
        ext = "s%dp" % ports
        fn, _ = QFileDialog.getSaveFileName(self, "Save Touchstone", self.default_name("nanovna", ext),
                                            "Touchstone (*.%s)" % ext)
        if not fn:
            return
        with open(fn, "w") as fh:
            fh.write("! NanoVNA-F V3 Front Panel  %s  correction=%s %s\n" %
                     (datetime.datetime.now().isoformat(timespec="seconds"), self.correction,
                      self.cal.kind() if self.correction == "App" else ""))
            if ports == 2:
                fh.write("! 1.5-port VNA: S12 and S22 not measured (written as 0)\n")
            fh.write("# HZ S RI R 50\n")
            for i, f in enumerate(self.freqs):
                a, b = self.s["S11"][i], self.s["S21"][i]
                if ports == 1:
                    fh.write("%d %.9e %.9e\n" % (f, a.real, a.imag))
                else:
                    fh.write("%d %.9e %.9e %.9e %.9e 0 0 0 0\n" % (f, a.real, a.imag, b.real, b.imag))
        self.flash("Saved " + os.path.basename(fn))

    def save_csv(self):
        if self.s is None:
            self.flash("No data yet")
            return
        fn, _ = QFileDialog.getSaveFileName(self, "Save CSV", self.default_name("nanovna", "csv"), "CSV (*.csv)")
        if not fn:
            return
        cols = ["freq_hz", "s11_re", "s11_im", "s21_re", "s21_im"]
        data = [self.freqs, self.s["S11"].real, self.s["S11"].imag, self.s["S21"].real, self.s["S21"].imag]
        for tr in self.traces[:self.ntraces]:
            if tr.fmt in CIRCULAR or tr.fmt == "TDR":
                continue
            _, y = format_data(tr.fmt, self.freqs, self.trace_complex(tr), self.vf, tr.phase_offset)
            cols.append("tr%d_%s_%s" % (tr.n + 1, tr.sparam, tr.fmt))
            data.append(y)
        np.savetxt(fn, np.column_stack(data), delimiter=",", header=",".join(cols), comments="", fmt="%.9g")
        self.flash("Saved " + os.path.basename(fn))

    def load_touchstone_mem(self):
        fn, _ = QFileDialog.getOpenFileName(self, "Load Touchstone into memory", "", "Touchstone (*.s1p *.s2p)")
        if not fn:
            return
        try:
            self._load_touchstone(fn)
        except Exception as e:
            self.flash("Could not read Touchstone file: %s" % e, 6000)

    def _load_touchstone(self, fn):
        mult, fmt = 1e9, "MA"
        rows = []
        for line in open(fn, encoding="utf-8", errors="strict"):
            line = line.split("!")[0].strip()
            if not line:
                continue
            if line.startswith("#"):
                tok = line[1:].upper().split()
                mult = {"HZ": 1, "KHZ": 1e3, "MHZ": 1e6, "GHZ": 1e9}.get(tok[0] if tok else "GHZ", 1e9)
                for t in tok:
                    if t in ("MA", "DB", "RI"):
                        fmt = t
                continue
            rows.append([float(x) for x in line.split()])
        a = np.array(rows)
        f = a[:, 0] * mult
        col = 1 if self.traces[self.at].sparam == "S11" or a.shape[1] < 5 else 3

        def cplx(x, y):
            if fmt == "RI":
                return x + 1j * y
            mag = 10 ** (x / 20) if fmt == "DB" else x
            return mag * np.exp(1j * np.radians(y))
        self.traces[self.at].mem = (f, cplx(a[:, col], a[:, col + 1]))
        self.traces[self.at].display = "Data&Mem"
        self.flash("Loaded %s into Tr%d memory" % (os.path.basename(fn), self.at + 1))
        self.redraw()

    # ================================================================ data path
    def on_sweep(self, freqs, s11, s21, dt):
        if self.sender() is not self.worker:
            return
        if len(freqs) != self.points or abs(freqs[0] - round(self.start)) > 1 or abs(freqs[-1] - round(self.stop)) > 1:
            return
        self.swt = dt
        self.sweeps += 1
        if self.acq:
            a = self.acq
            if a["skip"] > 0:
                a["skip"] -= 1
            else:
                a["s11"].append(s11)
                a["s21"].append(s21)
                if len(a["s11"]) >= self.cal_avg:
                    self.freqs = freqs
                    self.finish_std()
            self.render_menu()
        if self.correction == "App" and self.cal.terms is not None and self.cal.covers(freqs):
            s11, s21 = self.cal.apply(freqs, s11, s21)
        if self.avg_on and self.avg_n > 1:
            if self.avg_s11 is None or len(self.avg_s11) != len(s11):
                self.avg_s11, self.avg_s21, self.avg_count = s11.copy(), s21.copy(), 1
            else:
                self.avg_count = min(self.avg_count + 1, self.avg_n)
                self.avg_s11 += (s11 - self.avg_s11) / self.avg_count
                self.avg_s21 += (s21 - self.avg_s21) / self.avg_count
            s11, s21 = self.avg_s11.copy(), self.avg_s21.copy()
        self.freqs = freqs
        self.s = {"S11": s11, "S21": s21}
        if self.search_track:
            self.search(self.search_track, quiet=True)
        if self.bw_on:
            self.bw_result = self.compute_bw()
        self.redraw()
        self.update_status()

    def trace_complex(self, tr, use_math=True):
        d = self.s[tr.sparam]
        if tr.delay:
            d = d * np.exp(1j * 2 * np.pi * self.freqs * tr.delay)
        if use_math and tr.math and tr.mem is not None:
            m = tr.mem[1] if len(tr.mem[0]) == len(self.freqs) and np.allclose(tr.mem[0], self.freqs) \
                else interp_c(self.freqs, tr.mem[0], tr.mem[1])
            d = d / np.where(np.abs(m) < 1e-12, 1e-12, m)
        return d

    def mem_complex(self, tr):
        if tr.mem is None:
            return None
        if len(tr.mem[0]) == len(self.freqs) and np.allclose(tr.mem[0], self.freqs):
            return tr.mem[1]
        return interp_c(self.freqs, tr.mem[0], tr.mem[1])

    def update_all(self):
        self.redraw()
        self.update_status()

    def redraw(self):
        for p in self.panels:
            if p.isVisible() or self.maximize is False:
                self.draw_panel(p)

    def draw_panel(self, p):
        tr = p.tr
        circ = tr.fmt in CIRCULAR
        xs = None
        if self.s is not None and tr.fmt == "TDR":
            xs, _ = tdr(self.freqs, self.s[tr.sparam], self.vf)
        p.setup_geometry(self.start, self.stop, xs)
        unit = tr.unit
        if circ:
            sc = "%.3fU/ %.2fU" % (tr.scale / 5 if tr.fmt == "Polar" else tr.scale, tr.scale)
        else:
            sc = "%g%s/ %g%s" % (tr.scale, unit, tr.ref, unit)
        mathtag = "  [D/M]" if tr.math else ""
        p.header.setText("<span style='color:%s'><b>Tr%d</b>&nbsp;&nbsp;%s %s %s%s%s</span>" %
                         (tr.color, tr.n + 1, tr.sparam, tr.fmt, sc,
                          ("  Delay %s" % eng(tr.delay, "s")) if tr.delay else "", mathtag))
        if tr.fmt == "TDR":
            foot = "Ch1:  TDR  0 m  …  %.2f m   (VF %.2f)" % (xs[-1] if xs is not None else 0, self.vf)
        else:
            foot = "Ch1:  Start %s%sStop %s" % (fmt_freq(self.start, 4), "&nbsp;" * 30, fmt_freq(self.stop, 4))
        p.footer.setText("<span style='color:#bdbdbd'>%s</span>" % foot)
        if self.s is None:
            p.curve.setData([], [])
            p.mem_curve.setData([], [])
            p.mk_scatter.setData([], [])
            for t in p.mk_text:
                t.hide()
            p.readout.hide()
            return

        d = self.trace_complex(tr)
        x, y = format_data(tr.fmt, self.freqs, d, self.vf, tr.phase_offset)
        if tr.smooth and not circ:
            y = smooth(y, tr.aperture)
        if not circ:
            bot = tr.ref - tr.refpos * tr.scale
            y = np.clip(y, bot - tr.scale, bot + 11 * tr.scale)
        show_data = tr.display in ("Data", "Data&Mem")
        p.curve.setData(x, y) if show_data else p.curve.setData([], [])
        mc = self.mem_complex(tr)
        if mc is not None and tr.display in ("Mem", "Data&Mem"):
            if tr.delay:
                pass  # memory already stored with delay applied
            mx, my = format_data(tr.fmt, self.freqs, mc, self.vf, tr.phase_offset)
            p.mem_curve.setData(mx, my)
        else:
            p.mem_curve.setData([], [])

        # markers
        pts = []
        lines = []
        for i, m in enumerate(self.markers):
            t = p.mk_text[i]
            if not m["on"] or tr.fmt == "TDR":
                t.hide()
                continue
            k = min(m["idx"], len(self.freqs) - 1)
            mx, my = float(x[k]), float(y[k])
            pts.append(dict(pos=(mx, my), symbol="o" if circ else "t", brush=(0, 0, 0, 0) if circ else "w",
                            pen=pg.mkPen("#7fd0ff" if i == self.mk else "w", width=1.5)))
            t.setText(str(i + 1))
            t.setColor("#7fd0ff" if i == self.mk else "w")
            t.setPos(mx, my)
            t.show()
            f = self.freqs[k]
            lines.append("%s%d&nbsp;%s&nbsp;&nbsp;%s" % ("▶" if i == self.mk else "&nbsp;", i + 1,
                                                         fmt_freq(f, 6), self.marker_value(tr, d[k], y[k], f)))
        p.mk_scatter.setData(pts)
        if self.bw_on and self.bw_result and tr.n == self.at and not circ:
            r = self.bw_result
            p.bw_lines[0].setValue(r["f1"])
            p.bw_lines[1].setValue(r["f2"])
            for ln in p.bw_lines:
                ln.show()
            lines.append("<span style='color:#ff8a3d'>BW(-%gdB) %s&nbsp; CF %s<br>Q %.1f&nbsp; Loss %.2f dB</span>" %
                         (self.bw_level, eng(r["bw"], "Hz", 5), fmt_freq(r["cf"], 4), r["q"], r["loss"]))
        else:
            for ln in p.bw_lines:
                ln.hide()
            if self.bw_on and tr.n == self.at and not circ:
                lines.append("<span style='color:#ff8a3d'>BW: not found</span>")
        if lines:
            p.readout.setText("<br>".join(lines))
            p.readout.adjustSize()
            p.readout.move(max(46, p.plot.width() - p.readout.width() - 6), 4)
            p.readout.show()
            p.readout.raise_()
        else:
            p.readout.hide()

    def marker_value(self, tr, s, y, f):
        if tr.fmt == "Smith":
            z = 50 * (1 + s) / (1 - s + 1e-15)
            w = 2 * math.pi * f
            if z.imag >= 0:
                lc = eng(z.imag / w, "H")
            else:
                lc = eng(-1 / (w * z.imag), "F")
            return "%.2f %sj%.2f Ω  %s" % (z.real, "+" if z.imag >= 0 else "-", abs(z.imag), lc)
        if tr.fmt == "Polar":
            return "%.4f ∠%.2f°" % (abs(s), math.degrees(np.angle(s)))
        if tr.fmt == "GDelay":
            return "%.3f ns" % y
        return "%.4g %s" % (y, tr.unit)

    def update_status(self):
        tr = self.traces[self.at]
        if self.correction == "App":
            if self.freqs is not None and not self.cal.covers(self.freqs):
                cal = "<span style='color:#ff5050'>Cal: OUT OF RANGE</span>"
            elif self.freqs is not None and self.cal.interpolated(self.freqs):
                cal = "<span style='color:#ffd166'>Cal: %s (interp)</span>" % self.cal.kind()
            else:
                cal = "<span style='color:#3ecf8e'>Cal: %s</span>" % self.cal.kind()
        else:
            cal = {"Device": "Cal: Device", "Off": "<span style='color:#ff8a3d'>Cal: OFF</span>"}[self.correction]
        avg = ("Avg %d/%d" % (self.avg_count, self.avg_n)) if self.avg_on else "Avg Off"
        trig = "Continuous" if self.running else "Hold"
        parts = ["Tr %d" % (self.at + 1), "Ch 1", "Internal Trig", trig, avg, cal,
                 "%d pts" % self.points, "SWT %.2f s" % self.swt]
        if self.acq:
            parts.append("<span style='color:#ffd166'>Measuring %s</span>" % self.acq["name"].upper())
        self.st_left.setText("&nbsp;|&nbsp;".join(parts))
        if self.worker:
            dev = "<span style='color:#3ecf8e'>● %s</span>" % ("SIM" if self.is_sim else getattr(self, "version", "Online"))
        else:
            dev = "<span style='color:#888'>● Offline</span>"
        self.st_right.setText("%s&nbsp;&nbsp;&nbsp;%s" % (dev, datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")))

    # ================================================================ preset / help
    def preset(self):
        self.cancel_entry()
        self.active = None
        self.show_active()
        self.traces = [Trace(0, "S11", "Smith"), Trace(1, "S21", "LogMag"), Trace(2, "S21", "Phase"), Trace(3, "S11", "SWR")]
        for p, tr in zip(self.panels, self.traces):
            p.tr = tr
            p.geom = None
        self.ntraces, self.maximize, self.at = 4, False, 0
        self.markers_off()
        self.avg_on = False
        self.set_running(True)
        self.relayout()
        self.set_range(1e6, 3e9)
        self.set_points(201)
        self.open_menu("FREQ")
        self.flash("Preset (app calibration kept)")

    def show_help(self):
        QMessageBox.information(self, "Help", (
            "<b>NanoVNA-F V3 Front Panel</b><br><br>"
            "<b>Hard keys</b> open menus on the right side of the screen (or press F1-F8).<br>"
            "A parameter softkey makes it the <i>active function</i>: turn the <b>knob</b> (hover + mouse wheel, "
            "or drag), use ◀ ▶ for fine and ▲ ▼ for coarse steps, or type a value and pick a unit.<br><br>"
            "<b>Traces</b>: click a trace panel to make it active; click again on its plot to drop the marker.<br>"
            "<b>Cal</b>: Correction = Device uses the NanoVNA's own cal. For app SOLT: measure Open, Short, Load "
            "(port 1), Thru (port 1→2), optional Isolation, then Done/Apply. The cal interpolates if you narrow "
            "the sweep inside the cal range.<br><br>"
            "<b>Search → BW Search</b> gives -3 dB bandwidth, center and Q of the active trace.<br>"
            "<b>Port Ext → Auto Delay</b> removes linear phase (cable / fixture length).<br>"
            "<b>Format → TDR</b>: bandpass impulse response vs distance (set Velocity Factor).<br>"
            "<b>System</b>: console, device screen capture, Touchstone/CSV export, simulator DUT."))

    def closeEvent(self, e):
        self.disconnect_device()
        super().closeEvent(e)


def main():
    ap = argparse.ArgumentParser(description=APP_NAME)
    ap.add_argument("--sim", action="store_true", help="use the built-in simulator")
    ap.add_argument("--port", help="serial port, e.g. COM9")
    ap.add_argument("--screenshot", help=argparse.SUPPRESS)
    args = ap.parse_args()
    pg.setConfigOptions(antialias=True, foreground="#d0d0d0")
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setStyleSheet(QSS)
    w = VNAPanel(args)
    w.resize(1560, 820)
    w.show()
    if os.environ.get("DEMO"):
        def demo():
            w.set_range(500e6, 1.5e9)
            w.markers[0]["on"] = True
            w.select_trace(1)
            w.search_track = "max"
            w.set_bw_on(True)
            w.open_menu("SEARCH")
        QTimer.singleShot(600, demo)
        QTimer.singleShot(2500, w.auto_scale_all)
    if args.screenshot:
        def snap():
            w.grab().save(args.screenshot)
            app.quit()
        QTimer.singleShot(int(os.environ.get("SNAP_MS", "4000")), snap)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
