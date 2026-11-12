"""
tinySA Front Panel - a bench-analyzer style remote control for tinySA / tinySA Ultra.

Run:  python tinysa_panel.py            (auto-detects a tinySA COM port)
      python tinysa_panel.py --sim      (built-in simulator, no hardware)
      python tinysa_panel.py --port COM7 --model ultra
"""
import argparse
import csv
import datetime
import math
import os
import sys

import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QFont, QImage, QPixmap, QColor
from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QFrame, QLabel,
                             QHBoxLayout, QVBoxLayout, QGridLayout, QGroupBox,
                             QComboBox, QFileDialog, QMessageBox, QInputDialog,
                             QDialog, QPlainTextEdit, QPushButton, QSizePolicy)

from device import DeviceWorker, list_ports, MODELS, SIM_PORT
from widgets import PanelButton, Knob, PowerButton, Connector, ClickFrame

APP_NAME = "tinySA Front Panel"
SLOTS = 7          # softkeys per menu page
TRACE_COLOR = "#f2e53d"


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


def fmt_rbw_khz(k):
    if k is None:
        return "Auto"
    return "%g kHz" % k if k >= 1 else "%g Hz" % (k * 1e3)


def step125(v, up):
    """Next value in a 1-2-5 sequence."""
    v = max(v, 1e-12)
    e = 10 ** math.floor(math.log10(v) + 1e-9)
    m = v / e
    seq = [1, 2, 5, 10]
    if up:
        nxt = next(s for s in seq if s > m * (1 + 1e-9))
        return nxt * e
    prv = [s for s in [0.5, 1, 2, 5] if s < m * (1 - 1e-9)][-1]
    return prv * e


class Param:
    """An editable instrument parameter (knob, arrows, keypad)."""
    FREQ_UNITS = [("GHz", 1e9), ("MHz", 1e6), ("kHz", 1e3), ("Hz", 1)]

    def __init__(self, name, get, set_, fmt, fine, coarse=None, units=None, default_unit=None):
        self.name = name
        self.get = get
        self.set = set_
        self.fmt = fmt
        self.fine = fine            # fine(steps)
        self.coarse = coarse or (lambda up: fine(10 if up else -10))
        self.units = units or [("Enter", 1)]
        self.default_unit = default_unit if default_unit is not None else self.units[0][1]


class MI:
    """Menu item. kind: action | param | toggle | menu."""

    def __init__(self, label, kind="action", press=None, value=None, param=None, menu=None):
        self.label = label
        self.kind = kind
        self.press = press
        self.value = value
        self.param = param
        self.menu = menu


class Marker:
    def __init__(self):
        self.on = False
        self.idx = 0
        self.delta = False


# ---------------------------------------------------------------- stylesheet
QSS = """
QMainWindow, #bezel { background: qlineargradient(x1:0,y1:0,x2:0,y2:1, stop:0 #f3f4f6, stop:1 #dfe2e6); }
#brand { background:#f5c400; color:#1a1a1a; font: bold 17pt "Segoe UI"; padding:0px 10px; border-radius:3px; }
#model { color:#2b3340; font: 16pt "Segoe UI"; }
#tag   { color:#2b3340; font: 11pt "Segoe UI"; }
#tagi  { color:#2b3340; font: italic bold 13pt "Segoe UI"; }
#screenBezel { background: qlineargradient(x1:0,y1:0,x2:1,y2:1, stop:0 #e8eaee, stop:1 #c9ced5);
               border:1px solid #a7adb6; border-radius:14px; }
#screen { background:#000; border:3px solid #2a2d33; border-radius:4px; }
#screen QLabel { color:#e8e8e8; font: 9pt "Segoe UI"; }
#hdrLogo { color:#f5c400; font: bold italic 12pt "Segoe UI"; }
#pill { color:#4cd964; border:1px solid #4cd964; border-radius:3px; padding:0 4px; font: 8pt "Segoe UI"; }
#statusCol { background:#101010; border-right:1px solid #333; }
#statusItem { color:#7fbf7f; font: 7pt "Segoe UI"; }
#menuCol { background:#151515; border-left:1px solid #333; }
#menuTitle { background:#2c2c2c; color:white; font: bold 10pt "Segoe UI"; border-bottom:1px solid #444; }
#slot { background:#1d1d1d; border-bottom:1px solid #3a3a3a; }
#slot[active="true"] { background:#16457e; }
#slotLabel { color:white; font: bold 9pt "Segoe UI"; }
#slotValue { color:#d8d8d8; font: 9pt "Segoe UI"; }
#pageLbl { color:#bbb; font: 9pt "Segoe UI"; }
#tableHdr { background:#0f3d73; color:white; font: bold 9pt "Segoe UI"; padding-left:60px; }
#bigMarker { color:white; font: 15pt "Segoe UI"; background: transparent; }
#entryBox { color:white; background: rgba(20,60,120,210); border:1px solid #4a8be0; font: 11pt "Segoe UI"; padding:4px 8px; }
#msgBox { color:#ffd166; background: rgba(0,0,0,170); font: 9pt "Segoe UI"; padding:2px 6px; }

#ctrlPanel { background: qlineargradient(x1:0,y1:0,x2:0,y2:1, stop:0 #e3e9f1, stop:1 #d3dce8);
             border:1px solid #a9b9d0; border-radius:16px; }
QGroupBox { border:2px solid #a9bad2; border-radius:10px; margin-top:13px; padding:12px 6px 6px 6px;
            background:#e4eaf2; }
QGroupBox::title { subcontrol-origin: margin; subcontrol-position: top center; padding:0 12px;
                   color:#7189ad; font: 10pt "Segoe UI"; background:#e4eaf2;
                   border:2px solid #a9bad2; border-bottom:none; border-top-left-radius:8px; border-top-right-radius:8px; }

QPushButton#key { background: qlineargradient(x1:0,y1:0,x2:0,y2:1, stop:0 #ffffff, stop:1 #d6dbe2);
    border:1px solid #8995a6; border-radius:13px; color:#1e2530; font: 8.5pt "Segoe UI";
    min-height:36px; min-width:58px; padding:0 3px; }
QPushButton#key:hover, QPushButton#soft:hover, QPushButton#keyDark:hover, QPushButton#arrow:hover { border:2px solid #3b7ddd; }
QPushButton#key:pressed, QPushButton#soft:pressed { background:#bfc7d2; }
QPushButton#key:checked { border:2px solid #2f7de1; background: qlineargradient(x1:0,y1:0,x2:0,y2:1, stop:0 #ffffff, stop:1 #c9dcf5); }
QPushButton#keyDark { background: qlineargradient(x1:0,y1:0,x2:0,y2:1, stop:0 #78879b, stop:1 #4b586b);
    color:white; font: bold 11pt "Segoe UI"; border:1px solid #3d4757; border-radius:9px; min-height:44px; min-width:78px; }
QPushButton#keyDark:checked { background: qlineargradient(x1:0,y1:0,x2:0,y2:1, stop:0 #5c8fd6, stop:1 #2f5fa3); }
QPushButton#keyDark:pressed { background:#3d4757; }
QPushButton#keyGreen { background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 #b8f0b8, stop:1 #5cbf5c);
    color:white; font: bold 10pt "Segoe UI"; border:1px solid #4a9a4a; border-radius:10px; min-height:84px; min-width:44px; }
QPushButton#keyGreen:pressed { background:#4a9a4a; }
QPushButton#side { background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 #ffffff, stop:1 #d6dbe2);
    border:1px solid #8995a6; border-radius:10px; color:#1e2530; font: 9pt "Segoe UI"; min-height:60px; min-width:44px; }
QPushButton#soft { background: qlineargradient(x1:0,y1:0,x2:0,y2:1, stop:0 #f5f7f9, stop:1 #c9d0d9);
    border:1px solid #8a94a3; border-radius:9px; color:#333; font: 13pt "Segoe UI"; min-width:64px; }
QPushButton#arrow { background: qlineargradient(x1:0,y1:0,x2:0,y2:1, stop:0 #7d95b7, stop:1 #4e6a92);
    border:1px solid #3e5577; border-radius:6px; color:white; font: bold 14pt "Segoe UI"; min-height:34px; min-width:62px; }
QPushButton#arrow:pressed { background:#3e5577; }
QComboBox { background:white; border:1px solid #8995a6; border-radius:4px; padding:2px 6px; font: 9pt "Segoe UI"; min-width:120px; }
#portLbl { color:#3c4a5e; font: 8pt "Segoe UI"; }
#hint { color:#5e6f88; font: 8pt "Segoe UI"; }
"""


# ======================================================================
class FrontPanel(QMainWindow):
    def __init__(self, args):
        super().__init__()
        self.args = args
        self.setWindowTitle(APP_NAME)
        self.worker = None
        self.model = args.model if args.model in MODELS else "ultra"

        # instrument state ---------------------------------------------
        self.start, self.stop, self.points = 0.0, 800e6, 450
        self.last_span = None
        self.ref, self.div, self.ref_offset = 0.0, 10.0, 0.0
        self.rbw = None
        self.att = None
        self.input_mode = "low"
        self.lna = False
        self.spur = True
        self.trace_mode = "Clear Write"
        self.avg_n = 8
        self.avg_count = 0
        self.running = True
        self.markers = [Marker() for _ in range(4)]
        self.mk = 0
        self.cont_peak = False
        self.peak_table = False
        self.peak_sort = "Freq"
        self.peak_thresh = -90.0
        self.peak_excursion = 6.0
        self.waterfall_on = False
        self.wf_rows = 150
        self.wf = None
        self.ref_trace = None
        self.ref_trace_on = False
        self.gen_on = False
        self.gen_freq = 100e6
        self.gen_level = -30.0
        self.freqs = None
        self.trace = None
        self.live = None
        self.swt = 0.0
        self.sweep_count = 0

        # UI state -------------------------------------------------------
        self.menu_stack = ["FREQ"]
        self.menu_page = 0
        self.active = None
        self.entry = None
        self.tokens = {}
        self.next_token = 1

        self.menus = self.build_menus()
        self.build_ui()
        self.apply_model()
        self.update_axes()
        self.render_menu()
        self.update_info()

        self.clock = QTimer(self)
        self.clock.timeout.connect(self.tick)
        self.clock.start(1000)
        self.tick()

        self.refresh_ports()
        if args.sim:
            self.port_combo.setCurrentText(SIM_PORT)
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
            self.flash("No tinySA found - choose a port (or Simulator) and press the power key")

    # ================================================================ UI
    def build_ui(self):
        root = QWidget()
        root.setObjectName("bezel")
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(18, 10, 18, 10)
        outer.setSpacing(6)

        # --- brand strip
        top = QHBoxLayout()
        brand = QLabel("tinySA")
        brand.setObjectName("brand")
        self.model_lbl = QLabel("ULTRA")
        self.model_lbl.setObjectName("model")
        tag = QLabel("   Spectrum Analyzer   ")
        tag.setObjectName("tag")
        self.range_lbl = QLabel("100 kHz - 6 GHz")
        self.range_lbl.setObjectName("tagi")
        for w in (brand, self.model_lbl, tag, self.range_lbl):
            top.addWidget(w)
        top.addStretch(1)
        hint = QLabel("Hover the knob and use the mouse wheel  |  type numbers on the keyboard  |  click the trace to place a marker")
        hint.setObjectName("hint")
        top.addWidget(hint)
        outer.addLayout(top)

        main = QHBoxLayout()
        main.setSpacing(10)
        outer.addLayout(main, 1)

        # --- left side keys
        left = QVBoxLayout()
        left.addSpacing(70)
        b = PanelButton("P\nr\ne\ns\ne\nt", "keyGreen")
        b.clicked.connect(self.preset)
        left.addWidget(b)
        left.addSpacing(14)
        b = PanelButton("S\nn\na\np", "side")
        b.setToolTip("Save screenshot")
        b.clicked.connect(self.save_window_png)
        left.addWidget(b)
        left.addSpacing(8)
        b = PanelButton("H\ne\nl\np", "side")
        b.clicked.connect(self.show_help)
        left.addWidget(b)
        left.addStretch(1)
        main.addLayout(left)

        # --- screen + softkeys + bottom (power, port)
        mid = QVBoxLayout()
        sb = QFrame()
        sb.setObjectName("screenBezel")
        sbl = QHBoxLayout(sb)
        sbl.setContentsMargins(14, 14, 10, 14)
        sbl.setSpacing(12)
        sbl.addWidget(self.build_screen(), 1)
        sbl.addLayout(self.build_softkeys())
        mid.addWidget(sb, 1)

        bot = QHBoxLayout()
        bot.setContentsMargins(20, 6, 0, 0)
        self.power = PowerButton()
        self.power.clicked.connect(self.toggle_connect)
        bot.addWidget(self.power)
        bot.addSpacing(16)
        col = QVBoxLayout()
        l = QLabel("USB port")
        l.setObjectName("portLbl")
        col.addWidget(l)
        self.port_combo = QComboBox()
        self.port_combo.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        col.addWidget(self.port_combo)
        bot.addLayout(col)
        rb = PanelButton("⟳", "key", w=40)
        rb.setToolTip("Rescan ports")
        rb.clicked.connect(self.refresh_ports)
        bot.addWidget(rb)
        bot.addSpacing(10)
        col = QVBoxLayout()
        l = QLabel("Model")
        l.setObjectName("portLbl")
        col.addWidget(l)
        self.model_combo = QComboBox()
        self.model_combo.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.model_combo.addItems(["Auto detect", "tinySA Ultra", "tinySA (Basic)"])
        col.addWidget(self.model_combo)
        bot.addLayout(col)
        bot.addStretch(1)
        mid.addLayout(bot)
        main.addLayout(mid, 1)

        # --- control panel
        right = QVBoxLayout()
        right.addWidget(self.build_control_panel())
        conns = QHBoxLayout()
        conns.addStretch(1)
        conns.addWidget(Connector("CAL OUTPUT 50Ω", "Calibration / generator"))
        conns.addSpacing(30)
        conns.addWidget(Connector("RF 50Ω", "Input / output"))
        right.addLayout(conns)
        main.addLayout(right)

    # ---------------------------------------------------------------- screen
    def build_screen(self):
        scr = QFrame()
        scr.setObjectName("screen")
        h = QHBoxLayout(scr)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(0)

        # status column
        sc = QFrame()
        sc.setObjectName("statusCol")
        sc.setFixedWidth(52)
        scl = QVBoxLayout(sc)
        scl.setContentsMargins(2, 26, 2, 4)
        scl.setSpacing(10)
        self.status_items = {}
        for key in ("TRC", "SWP", "ATT", "RBW", "PTS", "MKR", "WF"):
            l = QLabel()
            l.setObjectName("statusItem")
            l.setAlignment(Qt.AlignmentFlag.AlignCenter)
            scl.addWidget(l)
            self.status_items[key] = l
        scl.addStretch(1)
        h.addWidget(sc)

        # centre
        cv = QVBoxLayout()
        cv.setContentsMargins(4, 2, 4, 2)
        cv.setSpacing(1)
        hdr = QHBoxLayout()
        lg = QLabel("tinySA")
        lg.setObjectName("hdrLogo")
        hdr.addWidget(lg)
        hdr.addStretch(1)
        self.clock_lbl = QLabel()
        hdr.addWidget(self.clock_lbl)
        hdr.addStretch(1)
        self.pill = QLabel("Offline")
        self.pill.setObjectName("pill")
        hdr.addWidget(self.pill)
        cv.addLayout(hdr)

        ph = QHBoxLayout()
        self.ref_lbl = QLabel()
        self.mkr_hdr = QLabel()
        ph.addWidget(self.ref_lbl)
        ph.addStretch(1)
        ph.addWidget(self.mkr_hdr)
        cv.addLayout(ph)

        pg.setConfigOptions(antialias=False, foreground="#d0d0d0")
        self.plot = pg.PlotWidget(background="#000000")
        self.plot.setMenuEnabled(False)
        self.plot.setMouseEnabled(False, False)
        self.plot.hideButtons()
        pi = self.plot.getPlotItem()
        pi.hideAxis("bottom")
        ax = pi.getAxis("left")
        ax.setWidth(40)
        ax.setStyle(tickLength=0)
        ax.setTickFont(QFont("Segoe UI", 8))
        gpen = pg.mkPen((95, 95, 95), width=1, style=Qt.PenStyle.DashLine)
        self.vgrid = [pg.InfiniteLine(angle=90, pen=gpen, movable=False) for _ in range(11)]
        self.hgrid = [pg.InfiniteLine(angle=0, pen=gpen, movable=False) for _ in range(11)]
        for ln in self.vgrid + self.hgrid:
            self.plot.addItem(ln)
        self.ref_curve = pg.PlotDataItem(pen=pg.mkPen("#3fc8ff", width=1))
        self.plot.addItem(self.ref_curve)
        self.curve = pg.PlotDataItem(pen=pg.mkPen(TRACE_COLOR, width=1))
        self.plot.addItem(self.curve)
        self.mk_scatter = pg.ScatterPlotItem(symbol="t", size=9, pen=pg.mkPen("w"), brush=pg.mkBrush("w"))
        self.plot.addItem(self.mk_scatter)
        self.mk_text = [pg.TextItem("", color="w", anchor=(0.5, 1.6)) for _ in range(4)]
        for t in self.mk_text:
            self.plot.addItem(t)
        self.pk_text = []
        self.plot.scene().sigMouseClicked.connect(self.plot_clicked)

        self.big_mk = QLabel(self.plot)
        self.big_mk.setObjectName("bigMarker")
        self.big_mk.move(70, 18)
        self.entry_box = QLabel(self.plot)
        self.entry_box.setObjectName("entryBox")
        self.entry_box.move(70, 105)
        self.entry_box.hide()
        self.msg = QLabel(self.plot)
        self.msg.setObjectName("msgBox")
        self.msg.move(46, 2)
        self.msg.hide()
        self.msg_timer = QTimer(self)
        self.msg_timer.setSingleShot(True)
        self.msg_timer.timeout.connect(self.msg.hide)
        cv.addWidget(self.plot, 1)

        # waterfall
        self.wf_plot = pg.PlotWidget(background="#000000")
        self.wf_plot.setMenuEnabled(False)
        self.wf_plot.setMouseEnabled(False, False)
        self.wf_plot.hideButtons()
        self.wf_plot.hideAxis("bottom")
        self.wf_plot.getAxis("left").setWidth(40)
        self.wf_plot.getAxis("left").setStyle(showValues=False, tickLength=0)
        self.wf_img = pg.ImageItem()
        cmap = pg.ColorMap([0, .25, .5, .75, 1],
                           [(0, 0, 0), (0, 0, 140), (0, 180, 220), (250, 230, 40), (255, 40, 20)])
        self.wf_img.setLookupTable(cmap.getLookupTable(0, 1, 256))
        self.wf_plot.addItem(self.wf_img)
        self.wf_plot.setFixedHeight(130)
        self.wf_plot.hide()
        cv.addWidget(self.wf_plot)

        # info lines
        info = QGridLayout()
        info.setContentsMargins(40, 0, 6, 0)
        info.setHorizontalSpacing(10)
        info.setVerticalSpacing(0)
        self.i_center = QLabel()
        self.i_span = QLabel()
        self.i_rbw = QLabel()
        self.i_pts = QLabel()
        self.i_swt = QLabel()
        info.addWidget(self.i_center, 0, 0)
        info.addWidget(self.i_span, 0, 2, Qt.AlignmentFlag.AlignRight)
        info.addWidget(self.i_rbw, 1, 0)
        info.addWidget(self.i_pts, 1, 1)
        info.addWidget(self.i_swt, 1, 2, Qt.AlignmentFlag.AlignRight)
        cv.addLayout(info)

        # peak table
        self.tbl_hdr = QLabel("Peak Table")
        self.tbl_hdr.setObjectName("tableHdr")
        self.tbl = QLabel()
        self.tbl.setTextFormat(Qt.TextFormat.RichText)
        self.tbl.setFixedHeight(118)
        self.tbl.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.tbl_hdr.hide()
        self.tbl.hide()
        cv.addWidget(self.tbl_hdr)
        cv.addWidget(self.tbl)
        h.addLayout(cv, 1)

        # on-screen menu column
        mc = QFrame()
        mc.setObjectName("menuCol")
        mc.setFixedWidth(118)
        ml = QVBoxLayout(mc)
        ml.setContentsMargins(0, 0, 0, 0)
        ml.setSpacing(0)
        self.menu_title = QLabel()
        self.menu_title.setObjectName("menuTitle")
        self.menu_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.menu_title.setWordWrap(True)
        ml.addWidget(self.menu_title, 1)
        self.slots = []
        for i in range(SLOTS):
            f = ClickFrame()
            f.setObjectName("slot")
            f.setCursor(Qt.CursorShape.PointingHandCursor)
            v = QVBoxLayout(f)
            v.setContentsMargins(3, 2, 3, 2)
            v.setSpacing(1)
            a = QLabel()
            a.setObjectName("slotLabel")
            a.setAlignment(Qt.AlignmentFlag.AlignCenter)
            a.setWordWrap(True)
            b = QLabel()
            b.setObjectName("slotValue")
            b.setAlignment(Qt.AlignmentFlag.AlignCenter)
            b.setTextFormat(Qt.TextFormat.RichText)
            v.addStretch(1)
            v.addWidget(a)
            v.addWidget(b)
            v.addStretch(1)
            f.clicked.connect(lambda i=i: self.softkey(i))
            ml.addWidget(f, 1)
            self.slots.append((f, a, b))
        self.page_lbl = QLabel()
        self.page_lbl.setObjectName("pageLbl")
        self.page_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        ml.addWidget(self.page_lbl, 1)
        h.addWidget(mc)
        return scr

    def build_softkeys(self):
        v = QVBoxLayout()
        v.setSpacing(8)
        b = PanelButton("↩", "soft", h=40)
        b.setToolTip("Return to previous menu")
        b.clicked.connect(self.menu_return)
        v.addWidget(b)
        for i in range(SLOTS):
            b = PanelButton("", "soft")
            b.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
            b.clicked.connect(lambda _, i=i: self.softkey(i))
            v.addWidget(b, 1)
        b = PanelButton("▽", "soft", h=40)
        b.setToolTip("Next menu page")
        b.clicked.connect(self.menu_next_page)
        v.addWidget(b)
        return v

    def build_control_panel(self):
        cp = QFrame()
        cp.setObjectName("ctrlPanel")
        cp.setFixedWidth(600)
        g = QGridLayout(cp)
        g.setContentsMargins(12, 8, 12, 12)
        g.setHorizontalSpacing(10)
        g.setVerticalSpacing(6)
        self.hard_keys = {}

        def hk(text, menu, style="key"):
            b = PanelButton(text, style)
            b.setCheckable(True)
            b.clicked.connect(lambda: self.open_menu(menu))
            self.hard_keys[menu] = b
            return b

        # FREQ/SPAN/AMPT
        fsa = QVBoxLayout()
        fsa.addSpacing(18)
        for t in ("FREQ", "SPAN", "AMPT"):
            fsa.addWidget(hk(t, t, "keyDark"))
        fsa.addStretch(1)
        g.addLayout(fsa, 0, 0, 3, 1)

        def group(title, rows):
            gb = QGroupBox(title)
            gl = QGridLayout(gb)
            gl.setSpacing(6)
            gl.setContentsMargins(6, 6, 6, 4)
            for r, row in enumerate(rows):
                for c, w in enumerate(row):
                    if w is not None:
                        gl.addWidget(w, r, c)
            return gb

        g.addWidget(group("Control", [[hk("BW/Det", "BW"), hk("Sweep/\nTrig", "SWEEP")],
                                      [hk("Trace/\nRef", "TRACE"), hk("Gen", "GEN")]]), 0, 1)
        g.addWidget(group("Marker", [[hk("Marker", "MARKER"), hk("Marker\n→", "MKR_TO")],
                                     [hk("Marker\nFctn", "MKR_FN"), hk("Peak", "PEAK")]]), 0, 2)
        g.addWidget(group("Measure", [[hk("Meas", "MEAS"), hk("Meas\nSetup", "MEAS_SETUP"), hk("Display", "DISPLAY")]]), 1, 1, 1, 2)
        g.addWidget(group("Utility", [[hk("System", "SYSTEM"), hk("Capture", "CAPTURE"), hk("Storage", "STORAGE")]]), 2, 1, 1, 2)

        rk = QVBoxLayout()
        rk.addSpacing(16)
        b = PanelButton("Auto", "key")
        b.clicked.connect(self.auto_tune)
        rk.addWidget(b)
        rk.addSpacing(6)
        b = PanelButton("Run/\nHold", "key")
        b.setToolTip("User key: freeze / resume sweeping")
        b.clicked.connect(self.toggle_run)
        rk.addWidget(b)
        rk.addStretch(1)
        g.addLayout(rk, 0, 3)

        # keypad
        kp = QGroupBox("Edit")
        kl = QGridLayout(kp)
        kl.setSpacing(7)
        keys = ["1", "2", "3", "4", "5", "6", "7", "8", "9", ".", "0", "+/-"]
        for i, k in enumerate(keys):
            b = PanelButton(k, "key")
            b.setStyleSheet("font: 11pt 'Segoe UI'; min-width:54px;")
            b.clicked.connect(lambda _, k=k: self.key_digit(k))
            kl.addWidget(b, i // 3, i % 3)
        for r, (t, fn) in enumerate((("Esc", self.key_esc), ("Back", self.key_back), ("Enter", self.key_enter))):
            b = PanelButton(t, "key")
            b.clicked.connect(fn)
            kl.addWidget(b, r, 3)
        g.addWidget(kp, 3, 0, 1, 3)

        # knob + arrows
        kv = QVBoxLayout()
        kv.addStretch(1)
        self.knob = Knob(size=146)
        self.knob.turned.connect(self.knob_turned)
        kv.addWidget(self.knob, 0, Qt.AlignmentFlag.AlignHCenter)
        kv.addSpacing(18)
        ar = QHBoxLayout()
        b = PanelButton("▼", "arrow")
        b.clicked.connect(lambda: self.arrow(False))
        ar.addWidget(b)
        b = PanelButton("▲", "arrow")
        b.clicked.connect(lambda: self.arrow(True))
        ar.addWidget(b)
        kv.addLayout(ar)
        kv.addStretch(1)
        g.addLayout(kv, 1, 3, 3, 1)
        return cp

    # ============================================================ menus
    def P(self, key):
        return self.params[key]

    def build_menus(self):
        fu = Param.FREQ_UNITS
        W = self

        def fstep(getter, setter, frac=100):
            return lambda n: setter(getter() + n * max(W.span() / frac, 1))

        self.params = {
            "center": Param("Center Freq", self.center, self.set_center, fmt_freq,
                            fstep(self.center, self.set_center),
                            lambda up: self.set_center(self.center() + (1 if up else -1) * self.span() / 10),
                            fu, 1e6),
            "start": Param("Start Freq", lambda: self.start, self.set_start, fmt_freq,
                           fstep(lambda: self.start, self.set_start),
                           lambda up: self.set_start(self.start + (1 if up else -1) * self.span() / 10), fu, 1e6),
            "stop": Param("Stop Freq", lambda: self.stop, self.set_stop, fmt_freq,
                          fstep(lambda: self.stop, self.set_stop),
                          lambda up: self.set_stop(self.stop + (1 if up else -1) * self.span() / 10), fu, 1e6),
            "span": Param("Span", self.span, self.set_span, fmt_freq,
                          lambda n: self.set_span(self._span_steps(n)),
                          lambda up: self.set_span(self._span_steps(1 if up else -1)), fu, 1e6),
            "ref": Param("Ref Level", lambda: self.ref, self.set_ref, lambda v: "%.2f dBm" % v,
                         lambda n: self.set_ref(self.ref + n), lambda up: self.set_ref(self.ref + (self.div if up else -self.div)),
                         [("dBm", 1)]),
            "div": Param("Scale/Div", lambda: self.div, self.set_div, lambda v: "%g dB" % v,
                         lambda n: self.set_div(self._div_step(n)), lambda up: self.set_div(self._div_step(1 if up else -1)),
                         [("dB", 1)]),
            "offset": Param("Ref Offset", lambda: self.ref_offset, self.set_offset, lambda v: "%.2f dB" % v,
                            lambda n: self.set_offset(self.ref_offset + 0.1 * n),
                            lambda up: self.set_offset(self.ref_offset + (1 if up else -1)), [("dB", 1)]),
            "att": Param("Attenuation", lambda: self.att if self.att is not None else 0, self.set_att,
                         lambda v: "%d dB" % v, lambda n: self.set_att((self.att or 0) + n),
                         lambda up: self.set_att((self.att or 0) + (10 if up else -10)), [("dB", 1)]),
            "rbw": Param("RBW", lambda: self.rbw, self.set_rbw, fmt_rbw_khz,
                         lambda n: self._rbw_step(n), lambda up: self._rbw_step(1 if up else -1),
                         [("MHz", 1e3), ("kHz", 1), ("Hz", 1e-3)], 1),
            "points": Param("Points", lambda: self.points, self.set_points, lambda v: "%d" % v,
                            lambda n: self.set_points(self.points + 10 * n),
                            lambda up: self.set_points(self.points * 2 if up else self.points // 2), [("Enter", 1)]),
            "avg": Param("Avg Count", lambda: self.avg_n, self.set_avg, lambda v: "%d" % v,
                         lambda n: self.set_avg(self.avg_n + n), lambda up: self.set_avg(self.avg_n * 2 if up else self.avg_n // 2),
                         [("Enter", 1)]),
            "mkfreq": Param("Marker Freq", self.mk_freq, self.set_mk_freq, fmt_freq,
                            lambda n: self.move_marker(n), lambda up: self.move_marker(10 if up else -10), fu, 1e6),
            "thresh": Param("Pk Threshold", lambda: self.peak_thresh, self.set_thresh, lambda v: "%.1f dBm" % v,
                            lambda n: self.set_thresh(self.peak_thresh + n),
                            lambda up: self.set_thresh(self.peak_thresh + (10 if up else -10)), [("dBm", 1)]),
            "excursion": Param("Pk Excursion", lambda: self.peak_excursion, self.set_excursion, lambda v: "%.1f dB" % v,
                               lambda n: self.set_excursion(self.peak_excursion + 0.5 * n),
                               lambda up: self.set_excursion(self.peak_excursion + (3 if up else -3)), [("dB", 1)]),
            "genf": Param("Gen Freq", lambda: self.gen_freq, self.set_gen_freq, fmt_freq,
                          lambda n: self.set_gen_freq(self.gen_freq + n * 100e3),
                          lambda up: self.set_gen_freq(self.gen_freq + (10e6 if up else -10e6)), fu, 1e6),
            "genl": Param("Gen Level", lambda: self.gen_level, self.set_gen_level, lambda v: "%.1f dBm" % v,
                          lambda n: self.set_gen_level(self.gen_level + n),
                          lambda up: self.set_gen_level(self.gen_level + (10 if up else -10)), [("dBm", 1)]),
            "wfrows": Param("WF Depth", lambda: self.wf_rows, self.set_wf_rows, lambda v: "%d lines" % v,
                            lambda n: self.set_wf_rows(self.wf_rows + 10 * n),
                            lambda up: self.set_wf_rows(self.wf_rows * 2 if up else self.wf_rows // 2), [("Enter", 1)]),
        }

        def prm(label, key):
            return MI(label, "param", param=key)

        def tog(label, getter, setter, a="On", b="Off"):
            return MI(label, "toggle", press=lambda: setter(not getter()),
                      value=lambda: ([a, b], 0 if getter() else 1))

        def choice(label, opts, getter, setter):
            def press():
                i = opts.index(getter()) if getter() in opts else -1
                setter(opts[(i + 1) % len(opts)])
            return MI(label, "toggle", press=press, value=lambda: (opts, opts.index(getter()) if getter() in opts else -1))

        def act(label, fn, value=None):
            return MI(label, "action", press=fn, value=value)

        M = {}
        M["FREQ"] = ("Frequency", [
            prm("Center\nFreq", "center"), prm("Start\nFreq", "start"), prm("Stop\nFreq", "stop"),
            choice("Input", ["Low", "High"], lambda: self.input_mode.title(), lambda v: self.set_input(v.lower())),
            act("Full Band", self.full_span),
        ])
        M["SPAN"] = ("Span", [
            prm("Span", "span"), act("Full Span", self.full_span), act("Last Span", self.last_span_fn),
            act("Span x10", lambda: self.set_span(self.span() * 10)), act("Span /10", lambda: self.set_span(self.span() / 10)),
        ])
        M["AMPT"] = ("Amplitude", [
            prm("Ref Level", "ref"), prm("Scale/Div", "div"),
            prm("Atten", "att"), tog("Atten Auto", lambda: self.att is None, self.set_att_auto, "Auto", "Man"),
            prm("Ref Offset", "offset"),
            tog("LNA", lambda: self.lna, self.set_lna),
            tog("Spur Removal", lambda: self.spur, self.set_spur),
        ])
        M["BW"] = ("BW / Det", [
            prm("RBW", "rbw"), tog("RBW Auto", lambda: self.rbw is None, self.set_rbw_auto, "Auto", "Man"),
            prm("Points", "points"),
        ])
        M["SWEEP"] = ("Sweep / Trig", [
            tog("Sweep", lambda: self.running, self.set_running, "Cont", "Hold"),
            act("Single", self.single_sweep), prm("Points", "points"),
        ])
        M["TRACE"] = ("Trace", [
            choice("Mode", ["Clear Write", "Max Hold", "Min Hold", "Average", "View"],
                   lambda: self.trace_mode, self.set_trace_mode),
            prm("Avg Count", "avg"), act("Clear Trace", self.reset_trace),
            act("Store Ref", self.store_ref), tog("Ref Trace", lambda: self.ref_trace_on, self.set_ref_trace),
            act("Load Ref\nCSV", self.load_ref_csv),
        ])
        M["GEN"] = ("Generator", [
            tog("Output", lambda: self.gen_on, self.set_gen), prm("Gen Freq", "genf"), prm("Gen Level", "genl"),
            act("Gen = Mkr", lambda: self.set_gen_freq(self.mk_freq())),
        ])
        M["MARKER"] = ("Marker", [
            choice("Select", ["1", "2", "3", "4"], lambda: str(self.mk + 1), lambda v: self.select_marker(int(v) - 1)),
            tog("State", lambda: self.markers[self.mk].on, self.set_marker_on),
            prm("Marker\nFreq", "mkfreq"),
            tog("Mode", lambda: self.markers[self.mk].delta, self.set_marker_delta, "Delta", "Norm"),
            act("All Off", self.markers_off),
        ])
        M["MKR_TO"] = ("Marker →", [
            act("Mkr→CF", lambda: self.mk_to("cf")), act("Mkr→Ref", lambda: self.mk_to("ref")),
            act("Mkr→Start", lambda: self.mk_to("start")), act("Mkr→Stop", lambda: self.mk_to("stop")),
            act("Mkr→Gen", lambda: self.set_gen_freq(self.mk_freq())),
        ])
        M["MKR_FN"] = ("Peak Table", [
            tog("State", lambda: self.peak_table, self.set_peak_table),
            choice("Peak Sort", ["Freq", "Ampl"], lambda: self.peak_sort, self.set_peak_sort),
            prm("Pk Threshold", "thresh"), prm("Pk Excursion", "excursion"),
        ])
        M["PEAK"] = ("Peak", [
            act("Peak\nSearch", self.peak_search), act("Next Peak", self.next_peak),
            act("Peak Left", lambda: self.peak_lr(-1)), act("Peak Right", lambda: self.peak_lr(1)),
            tog("Cont Peak", lambda: self.cont_peak, self.set_cont_peak),
            tog("Peak Table", lambda: self.peak_table, self.set_peak_table),
        ])
        M["MEAS"] = ("Measure", [
            tog("Waterfall", lambda: self.waterfall_on, self.set_waterfall),
            tog("Peak Table", lambda: self.peak_table, self.set_peak_table),
        ])
        M["MEAS_SETUP"] = ("Meas Setup", [prm("WF Depth", "wfrows"), act("Clear WF", self.clear_wf)])
        M["DISPLAY"] = ("Display", [
            prm("Ref Level", "ref"), prm("Scale/Div", "div"),
            tog("Waterfall", lambda: self.waterfall_on, self.set_waterfall),
        ])
        M["SYSTEM"] = ("System", [
            act("Connect", self.toggle_connect, value=lambda: "Online" if self.worker else "Offline"),
            act("Device\nInfo", self.device_info), act("Console…", self.console),
            choice("Model", ["Ultra", "Basic"], lambda: self.model.title(), lambda v: self.force_model(v.lower())),
            act("Level\nOffset", self.set_scale_dialog,
                value=lambda: "%g" % MODELS[self.model]["scale"]),
        ])
        M["CAPTURE"] = ("Capture", [
            act("Device\nScreen", self.device_capture), act("Save\nWindow PNG", self.save_window_png),
            act("Save\nPlot PNG", self.save_plot_png),
        ])
        M["STORAGE"] = ("Storage", [
            act("Save\nTrace CSV", self.save_csv), act("Save\nPeaks CSV", self.save_peaks_csv),
            act("Load Ref\nCSV", self.load_ref_csv), act("Save\nWindow PNG", self.save_window_png),
        ])
        return M

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
        items = self.current_items()
        pages = max(1, math.ceil(len(items) / SLOTS))
        self.menu_page = (self.menu_page + 1) % pages
        self.render_menu()

    def current_items(self):
        if self.entry is not None and self.active is not None:
            return [MI(u, "action", press=lambda m=m: self.apply_entry(m)) for u, m in self.active.units]
        return self.menus[self.menu_stack[-1]][1]

    def render_menu(self):
        name = self.menu_stack[-1]
        for k, b in self.hard_keys.items():
            b.setChecked(k == name)
        items = self.current_items()
        pages = max(1, math.ceil(len(items) / SLOTS))
        self.menu_page = min(self.menu_page, pages - 1)
        title = "Units" if self.entry is not None else self.menus[name][0]
        self.menu_title.setText(title)
        page_items = items[self.menu_page * SLOTS:(self.menu_page + 1) * SLOTS]
        for i, (f, a, b) in enumerate(self.slots):
            if i < len(page_items):
                it = page_items[i]
                a.setText(it.label)
                b.setText(self.item_value_html(it))
                act = (it.kind == "param" and self.active is self.params.get(it.param))
                f.setProperty("active", "true" if act else "false")
            else:
                a.setText("")
                b.setText("")
                f.setProperty("active", "false")
            f.style().unpolish(f)
            f.style().polish(f)
        self.page_lbl.setText("%d/%d" % (self.menu_page + 1, pages))

    def item_value_html(self, it):
        if it.kind == "param":
            p = self.params[it.param]
            return p.fmt(p.get())
        if it.kind == "toggle" and it.value:
            opts, sel = it.value()
            if len(opts) > 2:
                return '<span style="color:#7fd0ff">%s</span>' % (opts[sel] if sel >= 0 else "-")
            parts = []
            for i, o in enumerate(opts):
                if i == sel:
                    parts.append('<span style="background-color:#1f6fd1;color:white">&nbsp;%s&nbsp;</span>' % o)
                else:
                    parts.append('<span style="color:#9a9a9a">&nbsp;%s&nbsp;</span>' % o)
            return "".join(parts)
        if it.kind == "action" and it.value:
            return str(it.value())
        if it.kind == "menu":
            return "▸"
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
        elif it.kind == "menu":
            self.menu_stack.append(it.menu)
            self.menu_page = 0
        elif it.press:
            it.press()
        self.render_menu()

    # ============================================================ entry / knob
    def show_active(self):
        if self.active is None:
            self.entry_box.hide()
            return
        if self.entry is not None:
            txt = "%s<br><b>%s_</b>" % (self.active.name, self.entry or "")
        else:
            txt = "%s<br><b>%s</b>" % (self.active.name, self.active.fmt(self.active.get()) if self.active.get() is not None else "Auto")
        self.entry_box.setText(txt)
        self.entry_box.adjustSize()
        self.entry_box.show()
        self.entry_box.raise_()

    def key_digit(self, k):
        if self.active is None:
            self.flash("Select a parameter first (e.g. FREQ → Center Freq)")
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
            self.show_active()
            self.render_menu()
            return
        self.entry = None
        self.active.set(v)
        self.after_change()

    def knob_turned(self, n):
        if self.entry is not None:
            return
        if self.active is not None:
            self.active.fine(n)
        elif self.markers[self.mk].on:
            self.move_marker(n)
        else:
            self.flash("Knob: select a parameter, or turn on a marker")
            return
        self.after_change()

    def arrow(self, up):
        if self.active is not None:
            self.active.coarse(up)
            self.after_change()
        elif self.markers[self.mk].on:
            self.move_marker(10 if up else -10)
            self.after_change()

    def after_change(self):
        self.show_active()
        self.render_menu()
        self.update_info()
        self.redraw()

    def keyPressEvent(self, e):
        k = e.key()
        t = e.text()
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
        elif k == Qt.Key.Key_Up:
            self.knob_turned(1)
        elif k == Qt.Key.Key_Down:
            self.knob_turned(-1)
        elif k == Qt.Key.Key_PageUp:
            self.arrow(True)
        elif k == Qt.Key.Key_PageDown:
            self.arrow(False)
        elif Qt.Key.Key_F1 <= k <= Qt.Key.Key_F7:
            self.softkey(k - Qt.Key.Key_F1)
        else:
            super().keyPressEvent(e)

    def flash(self, text, ms=3500):
        self.msg.setText(text)
        self.msg.adjustSize()
        self.msg.show()
        self.msg.raise_()
        self.msg_timer.start(ms)

    # ============================================================ setters
    def fmax(self):
        return MODELS[self.model]["fmax"]

    def center(self):
        return (self.start + self.stop) / 2

    def span(self):
        return self.stop - self.start

    def set_range(self, start, stop):
        fmax = self.fmax()
        start = max(0.0, min(start, fmax))
        stop = max(0.0, min(stop, fmax))
        if stop - start < 1e3:
            c = (start + stop) / 2
            start, stop = max(0, c - 500), max(0, c - 500) + 1e3
        if (start, stop) != (self.start, self.stop):
            self.last_span = (self.start, self.stop)
        self.start, self.stop = start, stop
        self.push_sweep()

    def set_center(self, v):
        s = self.span()
        self.set_range(v - s / 2, v + s / 2)

    def set_span(self, v):
        c = self.center()
        v = max(1e3, v)
        self.set_range(c - v / 2, c + v / 2)

    def _span_steps(self, n):
        v = self.span()
        for _ in range(abs(n)):
            v = step125(v, n > 0)
        return v

    def set_start(self, v):
        self.set_range(v, max(self.stop, v + 1e3))

    def set_stop(self, v):
        self.set_range(min(self.start, v - 1e3), v)

    def full_span(self):
        hi = MODELS[self.model]["low_max"] if self.input_mode == "low" else 960e6
        lo = 0.0 if self.input_mode == "low" else 240e6
        if self.model == "ultra" and self.input_mode == "low":
            hi = self.fmax()
        self.set_range(lo, hi)

    def last_span_fn(self):
        if self.last_span:
            self.set_range(*self.last_span)

    def set_ref(self, v):
        self.ref = round(max(-150, min(v, 30)), 2)
        self.update_axes()

    def _div_step(self, n):
        seq = [1, 2, 5, 10, 20]
        i = min(range(len(seq)), key=lambda j: abs(seq[j] - self.div))
        return seq[max(0, min(len(seq) - 1, i + n))]

    def set_div(self, v):
        self.div = max(0.5, min(v, 20))
        self.update_axes()

    def set_offset(self, v):
        self.ref_offset = round(v, 2)
        self.reset_trace()

    def set_att(self, v):
        self.att = int(max(0, min(v, MODELS[self.model]["att_max"])))
        self.send("attenuate %d" % self.att)
        self.update_info()

    def set_att_auto(self, auto):
        if auto:
            self.att = None
            self.send("attenuate auto")
            self.update_info()
        else:
            self.set_att(0)

    def rbw_list(self):
        return MODELS[self.model]["rbw"]

    def set_rbw(self, v):
        lst = self.rbw_list()
        v = min(lst, key=lambda x: abs(math.log(x) - math.log(max(v, 1e-3))))
        self.rbw = v
        self.send("rbw %g" % v)
        self.update_info()

    def _rbw_step(self, n):
        lst = self.rbw_list()
        i = lst.index(self.rbw) if self.rbw in lst else len(lst) // 2
        self.set_rbw(lst[max(0, min(len(lst) - 1, i + n))])

    def set_rbw_auto(self, auto):
        if auto:
            self.rbw = None
            self.send("rbw auto")
            self.update_info()
        else:
            self.set_rbw(self.rbw_list()[len(self.rbw_list()) // 2])

    def set_points(self, v):
        self.points = int(max(51, min(v, 3000)))
        self.push_sweep()

    def set_avg(self, v):
        self.avg_n = int(max(2, min(v, 256)))

    def set_input(self, mode):
        self.input_mode = mode
        self.send("mode %s input" % mode)
        self.full_span()

    def set_lna(self, on):
        if self.model != "ultra":
            self.flash("LNA is only on the tinySA Ultra")
            return
        self.lna = on
        self.send("lna %s" % ("on" if on else "off"))

    def set_spur(self, on):
        self.spur = on
        self.send("spur %s" % ("on" if on else "off"))

    def set_running(self, run):
        self.running = run
        if self.worker:
            self.worker.set_running(run and not self.gen_on)
        self.update_info()

    def toggle_run(self):
        self.set_running(not self.running)
        self.flash("Sweep running" if self.running else "Sweep on hold")
        self.render_menu()

    def single_sweep(self):
        self.set_running(False)
        if self.worker:
            self.worker.single_sweep()

    def set_trace_mode(self, m):
        self.trace_mode = m
        if m != "View":
            self.reset_trace()

    def reset_trace(self):
        self.trace = None
        self.avg_count = 0

    def store_ref(self):
        if self.trace is not None:
            self.ref_trace = (self.freqs.copy(), self.trace.copy())
            self.ref_trace_on = True
            self.redraw()

    def set_ref_trace(self, on):
        if on and self.ref_trace is None:
            self.flash("No reference stored (Trace → Store Ref)")
            return
        self.ref_trace_on = on
        self.redraw()

    def set_thresh(self, v):
        self.peak_thresh = max(-150, min(v, 30))

    def set_excursion(self, v):
        self.peak_excursion = max(0.5, min(v, 40))

    def set_wf_rows(self, v):
        self.wf_rows = int(max(20, min(v, 1000)))
        self.wf = None

    def set_gen(self, on):
        self.gen_on = on
        if on:
            for c in ("mode low output", "freq %d" % int(self.gen_freq), "level %.1f" % self.gen_level, "output on"):
                self.send(c)
            self.flash("Generator ON - sweeping paused")
        else:
            self.send("output off")
            self.send("mode %s input" % self.input_mode)
        if self.worker:
            self.worker.set_running(self.running and not on)
        self.update_info()

    def set_gen_freq(self, v):
        self.gen_freq = max(0, min(v, self.fmax()))
        if self.gen_on:
            self.send("freq %d" % int(self.gen_freq))

    def set_gen_level(self, v):
        self.gen_level = max(-120, min(v, 13))
        if self.gen_on:
            self.send("level %.1f" % self.gen_level)

    # markers -----------------------------------------------------------
    def mk_freq(self):
        if self.freqs is None:
            return self.center()
        return float(self.freqs[min(self.markers[self.mk].idx, len(self.freqs) - 1)])

    def set_mk_freq(self, f):
        m = self.markers[self.mk]
        m.on = True
        m.idx = self.idx_of(f)

    def idx_of(self, f):
        n = self.points if self.freqs is None else len(self.freqs)
        return int(round((f - self.start) / max(self.span(), 1) * (n - 1))) if True else 0

    def move_marker(self, n):
        m = self.markers[self.mk]
        m.on = True
        npts = len(self.freqs) if self.freqs is not None else self.points
        m.idx = max(0, min(npts - 1, m.idx + n))

    def select_marker(self, i):
        self.mk = i
        self.active = None
        self.show_active()

    def set_marker_on(self, on):
        m = self.markers[self.mk]
        m.on = on
        if on and self.trace is not None:
            m.idx = int(np.argmax(self.trace))
        self.redraw()

    def set_marker_delta(self, d):
        if self.mk == 0 and d:
            self.flash("Delta is relative to Marker 1 - select marker 2..4")
            return
        self.markers[self.mk].delta = d
        self.markers[0].on = True

    def markers_off(self):
        for m in self.markers:
            m.on = False
            m.delta = False
        self.cont_peak = False
        self.redraw()

    def mk_to(self, what):
        if not self.markers[self.mk].on or self.trace is None:
            self.flash("No active marker")
            return
        f = self.mk_freq()
        if what == "cf":
            self.set_center(f)
        elif what == "start":
            self.set_start(f)
        elif what == "stop":
            self.set_stop(f)
        elif what == "ref":
            self.set_ref(math.ceil(float(self.trace[self.markers[self.mk].idx])))

    def plot_clicked(self, ev):
        if self.freqs is None:
            return
        vb = self.plot.getPlotItem().vb
        pt = vb.mapSceneToView(ev.scenePos())
        m = self.markers[self.mk]
        m.on = True
        m.idx = max(0, min(len(self.freqs) - 1, self.idx_of(pt.x())))
        self.cont_peak = False
        self.redraw()
        self.render_menu()

    # peaks ----------------------------------------------------------------
    def find_peaks(self, d=None):
        d = self.trace if d is None else d
        if d is None or len(d) < 5:
            return []
        n = len(d)
        w = max(3, n // 80)
        cand = np.where((d[1:-1] >= d[:-2]) & (d[1:-1] > d[2:]) & (d[1:-1] > self.peak_thresh))[0] + 1
        peaks = []
        for i in cand:
            lo = d[max(0, i - w):i].min()
            hi = d[i + 1:min(n, i + w + 1)].min()
            if d[i] - min(lo, hi) >= self.peak_excursion or d[i] - max(lo, hi) >= self.peak_excursion:
                peaks.append(int(i))
        # suppress neighbours within w, keeping the strongest
        peaks.sort(key=lambda i: -d[i])
        kept = []
        for i in peaks:
            if all(abs(i - k) > w for k in kept):
                kept.append(i)
        return kept

    def peak_search(self):
        if self.trace is None:
            return
        m = self.markers[self.mk]
        m.on = True
        m.idx = int(np.argmax(self.trace))
        self.redraw()

    def next_peak(self):
        if self.trace is None:
            return
        m = self.markers[self.mk]
        cur = self.trace[m.idx] if m.on else 1e9
        lower = [i for i in self.find_peaks() if self.trace[i] < cur - 1e-9]
        if lower:
            m.on = True
            m.idx = lower[0]
            self.redraw()
        else:
            self.flash("No more peaks")

    def peak_lr(self, direction):
        if self.trace is None:
            return
        m = self.markers[self.mk]
        pk = sorted(self.find_peaks())
        cand = [i for i in pk if (i - m.idx) * direction > 0]
        if cand:
            m.on = True
            m.idx = cand[0] if direction > 0 else cand[-1]
            self.redraw()
        else:
            self.flash("No peak to the %s" % ("right" if direction > 0 else "left"))

    def set_cont_peak(self, on):
        self.cont_peak = on
        if on:
            self.peak_search()

    def set_peak_table(self, on):
        self.peak_table = on
        self.tbl.setVisible(on)
        self.tbl_hdr.setVisible(on)
        self.redraw()

    def set_peak_sort(self, s):
        self.peak_sort = s
        self.redraw()

    def set_waterfall(self, on):
        self.waterfall_on = on
        self.wf_plot.setVisible(on)
        self.wf = None

    def clear_wf(self):
        self.wf = None

    # ============================================================ auto / preset
    def preset(self):
        self.cancel_entry()
        self.active = None
        self.show_active()
        self.input_mode = "low"
        self.send("mode low input")
        self.ref, self.div, self.ref_offset = 0.0, 10.0, 0.0
        self.set_rbw_auto(True)
        self.set_att_auto(True)
        self.points = 450
        self.markers_off()
        self.trace_mode = "Clear Write"
        self.reset_trace()
        if self.gen_on:
            self.set_gen(False)
        self.set_running(True)
        self.full_span()
        self.update_axes()
        self.open_menu("FREQ")
        self.flash("Preset")

    def auto_tune(self):
        """Full span, then zoom on the strongest signal (ignoring the 0 Hz LO feedthrough)."""
        if not self.worker:
            self.flash("Not connected")
            return
        self._auto_state = 1
        self.full_span()
        self.flash("Auto: searching full span…")

    def _auto_continue(self):
        d = self.live
        f = self.freqs
        mask = f > max(self.start, 2e6 + self.span() * 0.01)
        if not mask.any():
            self._auto_state = 0
            return
        i = int(np.argmax(np.where(mask, d, -999)))
        fpk, apk = float(f[i]), float(d[i])
        self._auto_state = 0
        self.set_ref(10 * math.ceil((apk + 10) / 10))
        self.set_span(max(self.span() / 50, 200e3))
        self.set_center(fpk)
        self.markers[0].on = True
        self.mk = 0
        self.cont_peak = True
        self.flash("Auto: %s  %.1f dBm" % (fmt_freq(fpk, 3), apk))

    # ============================================================ device comms
    def refresh_ports(self):
        cur = self.port_combo.currentText()
        self.port_combo.clear()
        best = None
        for dev, desc, is_t in list_ports():
            label = dev + ("  (tinySA)" if is_t else "")
            self.port_combo.addItem(label, dev)
            if is_t and best is None:
                best = label
        self.port_combo.addItem(SIM_PORT, SIM_PORT)
        if cur and self.port_combo.findText(cur) >= 0:
            self.port_combo.setCurrentText(cur)
        elif best:
            self.port_combo.setCurrentText(best)

    def toggle_connect(self):
        if self.worker:
            self.disconnect_device()
        else:
            self.connect_device()

    def connect_device(self):
        if self.worker:
            return
        port = self.port_combo.currentData() or self.port_combo.currentText().split()[0]
        pref = {0: "auto", 1: "ultra", 2: "basic"}[self.model_combo.currentIndex()]
        if self.args.model in MODELS and pref == "auto":
            pref = self.args.model
        self.worker = DeviceWorker(port, pref, sim_model=self.args.model if self.args.model in MODELS else "ultra")
        self.worker.sweep_ready.connect(self.on_sweep)
        self.worker.connected.connect(self.on_connected)
        self.worker.disconnected.connect(self.on_disconnected)
        self.worker.reply.connect(self.on_reply)
        self.worker.status.connect(lambda s: self.flash(s))
        self.worker.set_sweep(self.start, self.stop, self.points)
        self.worker.set_running(self.running and not self.gen_on)
        self.worker.start()
        self.pill.setText("Connecting…")
        self.power.setChecked(True)

    def disconnect_device(self):
        w = self.worker
        if w:
            self.worker = None
            w.stop()
            w.wait(3000)
            self.power.setChecked(False)
            self.pill.setText("Offline")
            self.render_menu()

    def on_connected(self, model, ver):
        if self.sender() is not self.worker:
            return
        self.model = model
        self.apply_model()
        sim = self.worker and self.worker.port == SIM_PORT
        self.pill.setText("SIM" if sim else "Remote")
        first = ver.splitlines()[0] if ver else ""
        self.flash("Connected: %s  (%s)" % (MODELS[model]["name"], first))
        # push current settings to the instrument
        self.send("mode %s input" % self.input_mode)
        self.send("rbw auto" if self.rbw is None else "rbw %g" % self.rbw)
        self.send("attenuate auto" if self.att is None else "attenuate %d" % self.att)
        self.send("spur %s" % ("on" if self.spur else "off"))
        if model == "ultra":
            self.send("lna %s" % ("on" if self.lna else "off"))
        self.render_menu()

    def on_disconnected(self, reason):
        w = self.sender()
        if w is not None:
            w.deleteLater()
        if w is not self.worker and self.worker is not None:
            return  # an old worker finishing after a reconnect
        self.worker = None
        self.power.setChecked(False)
        self.pill.setText("Offline")
        self.flash(reason, 6000)
        self.render_menu()

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

    def push_sweep(self):
        self.reset_trace()
        self.wf = None
        if self.worker:
            self.worker.set_sweep(self.start, self.stop, self.points)
        self.update_axes()
        self.update_info()

    def apply_model(self):
        m = MODELS[self.model]
        self.model_lbl.setText("ULTRA" if self.model == "ultra" else "BASIC")
        self.range_lbl.setText("100 kHz - %s" % ("6 GHz" if self.model == "ultra" else "960 MHz"))
        if self.rbw is not None and self.rbw not in m["rbw"]:
            self.rbw = None
        if self.stop > m["fmax"]:
            self.set_range(self.start, m["fmax"])
        if self.worker:
            self.worker.set_scale(m["scale"])

    def force_model(self, model):
        self.model = model
        self.apply_model()
        self.update_info()

    def set_scale_dialog(self):
        v, ok = QInputDialog.getDouble(self, "Level offset",
                                       "scanraw dBm = raw/32 - offset\n(tinySA 128, Ultra 174)",
                                       MODELS[self.model]["scale"], 0, 400, 1)
        if ok:
            MODELS[self.model]["scale"] = v
            if self.worker:
                self.worker.set_scale(v)

    def device_info(self):
        if not self.worker:
            self.flash("Not connected")
            return
        self.send("version", lambda r: self.send("info", lambda r2: self.text_dialog("Device info", (r or "") + "\n\n" + (r2 or ""))))

    def console(self):
        if not self.worker:
            self.flash("Not connected")
            return
        cmd, ok = QInputDialog.getText(self, "tinySA console", "Shell command (e.g. help, info, config, rbw, temperature):")
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
        px = np.frombuffer(data, dtype=">u2").reshape(h, w).astype(np.uint32)
        r = ((px >> 11) & 0x1F) * 255 // 31
        g = ((px >> 5) & 0x3F) * 255 // 63
        b = (px & 0x1F) * 255 // 31
        rgb = np.ascontiguousarray(np.dstack([r, g, b]).astype(np.uint8))
        img = QImage(rgb.data, w, h, 3 * w, QImage.Format.Format_RGB888).copy()
        d = QDialog(self)
        d.setWindowTitle("tinySA screen")
        v = QVBoxLayout(d)
        lbl = QLabel()
        lbl.setPixmap(QPixmap.fromImage(img).scaled(w * 2, h * 2, Qt.AspectRatioMode.KeepAspectRatio))
        v.addWidget(lbl)
        sb = QPushButton("Save PNG…")
        sb.clicked.connect(lambda: self._save_img(img))
        v.addWidget(sb)
        d.exec()

    def _save_img(self, img):
        fn, _ = QFileDialog.getSaveFileName(self, "Save image", self.default_name("tinysa_screen", "png"), "PNG (*.png)")
        if fn:
            img.save(fn)

    # ============================================================ files
    def default_name(self, stem, ext):
        return "%s_%s.%s" % (stem, datetime.datetime.now().strftime("%Y%m%d_%H%M%S"), ext)

    def save_window_png(self):
        fn, _ = QFileDialog.getSaveFileName(self, "Save screenshot", self.default_name("tinysa_panel", "png"), "PNG (*.png)")
        if fn:
            self.grab().save(fn)
            self.flash("Saved " + os.path.basename(fn))

    def save_plot_png(self):
        fn, _ = QFileDialog.getSaveFileName(self, "Save plot", self.default_name("tinysa_plot", "png"), "PNG (*.png)")
        if fn:
            self.plot.grab().save(fn)
            self.flash("Saved " + os.path.basename(fn))

    def save_csv(self):
        if self.trace is None:
            self.flash("No data yet")
            return
        fn, _ = QFileDialog.getSaveFileName(self, "Save trace", self.default_name("tinysa_trace", "csv"), "CSV (*.csv)")
        if not fn:
            return
        with open(fn, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["# %s  RBW %s  mode %s" % (MODELS[self.model]["name"], fmt_rbw_khz(self.rbw), self.trace_mode)])
            w.writerow(["freq_hz", "trace_dbm", "live_dbm"])
            for f, t, l in zip(self.freqs, self.trace, self.live):
                w.writerow(["%.0f" % f, "%.2f" % t, "%.2f" % l])
        self.flash("Saved " + os.path.basename(fn))

    def save_peaks_csv(self):
        pk = self.find_peaks()
        if not pk:
            self.flash("No peaks")
            return
        fn, _ = QFileDialog.getSaveFileName(self, "Save peaks", self.default_name("tinysa_peaks", "csv"), "CSV (*.csv)")
        if fn:
            with open(fn, "w", newline="") as fh:
                w = csv.writer(fh)
                w.writerow(["freq_hz", "dbm"])
                for i in sorted(pk):
                    w.writerow(["%.0f" % self.freqs[i], "%.2f" % self.trace[i]])

    def load_ref_csv(self):
        fn, _ = QFileDialog.getOpenFileName(self, "Load reference trace", "", "CSV (*.csv)")
        if not fn:
            return
        fs, ds = [], []
        with open(fn) as fh:
            for row in csv.reader(fh):
                try:
                    fs.append(float(row[0]))
                    ds.append(float(row[1]))
                except (ValueError, IndexError):
                    continue
        if fs:
            self.ref_trace = (np.array(fs), np.array(ds))
            self.ref_trace_on = True
            self.redraw()

    # ============================================================ data path
    def on_sweep(self, freqs, vals, dt):
        if self.gen_on or self.sender() is not self.worker:
            return
        if len(freqs) != self.points or abs(freqs[0] - self.start) > 1 or abs(freqs[-1] - self.stop) > 1:
            return  # stale sweep from before a settings change
        d = vals + self.ref_offset
        self.freqs = freqs
        self.live = d
        self.swt = dt
        self.sweep_count += 1
        if self.trace is None or len(self.trace) != len(d):
            self.trace = d.copy()
            self.avg_count = 1
        elif self.trace_mode == "Clear Write":
            self.trace = d
        elif self.trace_mode == "Max Hold":
            self.trace = np.maximum(self.trace, d)
        elif self.trace_mode == "Min Hold":
            self.trace = np.minimum(self.trace, d)
        elif self.trace_mode == "Average":
            # average in power (linear), like a real analyzer's power average
            self.avg_count = min(self.avg_count + 1, self.avg_n)
            lin = 10 ** (self.trace / 10)
            lin += (10 ** (d / 10) - lin) / self.avg_count
            self.trace = 10 * np.log10(lin)
        # View: keep trace

        if getattr(self, "_auto_state", 0) == 1 and self.sweep_count > 0:
            self._auto_continue()
            return
        if self.cont_peak:
            m = self.markers[self.mk]
            m.on = True
            m.idx = int(np.argmax(self.trace))

        if self.waterfall_on:
            if self.wf is None or self.wf.shape[1] != len(d) or self.wf.shape[0] != self.wf_rows:
                self.wf = np.full((self.wf_rows, len(d)), -130.0)
            self.wf = np.roll(self.wf, 1, axis=0)
            self.wf[0] = d
            self.wf_img.setImage(self.wf.T, autoLevels=False,
                                 levels=(self.ref - 10 * self.div, self.ref))
            self.wf_img.setRect(pg.QtCore.QRectF(self.start, 0, self.span(), self.wf_rows))
            self.wf_plot.setXRange(self.start, self.stop, padding=0)
            self.wf_plot.setYRange(0, self.wf_rows, padding=0)
            self.wf_plot.getPlotItem().vb.invertY(True)
        self.redraw()
        self.i_swt.setText("SWT  %.0f ms" % (dt * 1e3))

    def update_axes(self):
        bot = self.ref - 10 * self.div
        pi = self.plot.getPlotItem()
        pi.setXRange(self.start, self.stop, padding=0)
        pi.setYRange(bot, self.ref, padding=0)
        ticks = [(self.ref - i * self.div, "%g" % (self.ref - i * self.div)) for i in range(11)]
        pi.getAxis("left").setTicks([ticks, []])
        for i in range(11):
            self.vgrid[i].setValue(self.start + i * self.span() / 10)
            self.hgrid[i].setValue(bot + i * self.div)
        if self.waterfall_on and self.wf is not None:
            self.wf_img.setLevels((bot, self.ref))
        self.update_info()
        self.redraw()

    def redraw(self):
        if self.trace is not None and self.freqs is not None:
            bot = self.ref - 10 * self.div
            self.curve.setData(self.freqs, np.clip(self.trace, bot - 1, self.ref + 1))
        else:
            self.curve.setData([], [])
        if self.ref_trace_on and self.ref_trace is not None:
            self.ref_curve.setData(*self.ref_trace)
        else:
            self.ref_curve.setData([], [])

        # markers
        xs, ys = [], []
        for i, (m, t) in enumerate(zip(self.markers, self.mk_text)):
            if m.on and self.trace is not None:
                m.idx = min(m.idx, len(self.trace) - 1)
                x, y = self.freqs[m.idx], min(self.trace[m.idx], self.ref)
                xs.append(x)
                ys.append(y)
                t.setText(("Δ" if m.delta else "") + str(i + 1))
                t.setColor("#7fd0ff" if i == self.mk else "w")
                t.setPos(x, y)
                t.show()
            else:
                t.hide()
        self.mk_scatter.setData(xs, ys)

        # peak labels / table
        for t in self.pk_text:
            self.plot.removeItem(t)
        self.pk_text = []
        if self.peak_table and self.trace is not None:
            pk = self.find_peaks()[:10]
            order = sorted(pk) if self.peak_sort == "Freq" else sorted(pk, key=lambda i: -self.trace[i])
            for n, i in enumerate(order):
                t = pg.TextItem(str(n + 1), color="#ffffff", anchor=(0.5, 1.2))
                t.setPos(self.freqs[i], min(self.trace[i], self.ref))
                self.plot.addItem(t)
                self.pk_text.append(t)
            rows = []
            for n, i in enumerate(order):
                rows.append((n + 1, fmt_freq(self.freqs[i]), "%.2f dBm" % self.trace[i]))
            html = ['<table cellspacing="0" cellpadding="1" style="color:#e8e8e8;font-size:8.5pt">',
                    '<tr style="font-weight:bold"><td width=50>&nbsp;&nbsp;Peak</td><td width=150>X Axis</td><td width=100>Amp</td>'
                    '<td width=50>&nbsp;&nbsp;Peak</td><td width=150>X Axis</td><td width=100>Amp</td></tr>']
            for r in range(5):
                cells = ""
                for c in (r, r + 5):
                    if c < len(rows):
                        cells += "<td>&nbsp;&nbsp;%d</td><td>%s</td><td>%s</td>" % rows[c]
                    else:
                        cells += "<td></td><td></td><td></td>"
                html.append("<tr>%s</tr>" % cells)
            html.append("</table>")
            self.tbl.setText("".join(html))

        # big readout
        m = self.markers[self.mk]
        if m.on and self.trace is not None:
            f, a = self.freqs[m.idx], self.trace[m.idx]
            if m.delta and self.markers[0].on:
                m0 = self.markers[0]
                df = f - self.freqs[m0.idx]
                da = a - self.trace[m0.idx]
                txt = "ΔMarker %d<br>%s%s<br>%+.2f dB" % (self.mk + 1, "+" if df >= 0 else "", fmt_freq(df), da)
                self.mkr_hdr.setText("ΔMkr%d  %s  %+.2f dB" % (self.mk + 1, fmt_freq(df, 3), da))
            else:
                txt = "Marker %d<br>%s<br>%.2f dBm" % (self.mk + 1, fmt_freq(f), a)
                self.mkr_hdr.setText("Marker%d  %s  %.2f dBm" % (self.mk + 1, fmt_freq(f, 3), a))
            self.big_mk.setText(txt)
            self.big_mk.adjustSize()
            self.big_mk.show()
        else:
            self.big_mk.hide()
            self.mkr_hdr.setText("")
        self.update_status()

    def update_info(self):
        self.i_center.setText("Center Freq  %s" % fmt_freq(self.center(), 3))
        self.i_span.setText("Span  %s" % fmt_freq(self.span(), 3))
        self.i_rbw.setText("<span style='color:%s'>●</span> RBW  %s" %
                           ("#e04040" if self.rbw is not None else "#777", fmt_rbw_khz(self.rbw)))
        self.i_pts.setText("Start %s   Stop %s   %d pts" % (fmt_freq(self.start, 3), fmt_freq(self.stop, 3), self.points))
        att = "Auto" if self.att is None else "%d dB" % self.att
        self.ref_lbl.setText("Ref %.2f dBm   %g dB/div   Att %s%s" %
                             (self.ref, self.div, att, ("   Offset %+.1f dB" % self.ref_offset) if self.ref_offset else ""))
        self.update_status()

    def update_status(self):
        s = self.status_items
        on = "color:#ffd166"

        def item(k, top, val, hi=False):
            s[k].setText("<div style='%s'>%s<br><b>%s</b></div>" % (on if hi else "", top, val))
        tm = {"Clear Write": "Clr", "Max Hold": "Max", "Min Hold": "Min", "Average": "Avg", "View": "View"}[self.trace_mode]
        item("TRC", "TRACE", tm, self.trace_mode != "Clear Write")
        item("SWP", "SWEEP", "GEN" if self.gen_on else ("Cont" if self.running else "Hold"), not self.running or self.gen_on)
        item("ATT", "ATT", "A" if self.att is None else str(self.att), self.att is not None)
        item("RBW", "RBW", "A" if self.rbw is None else "%g" % self.rbw, self.rbw is not None)
        item("PTS", "PTS", str(self.points))
        item("MKR", "MKR", str(sum(m.on for m in self.markers)), any(m.on for m in self.markers))
        item("WF", "WFALL", "On" if self.waterfall_on else "Off", self.waterfall_on)

    def tick(self):
        self.clock_lbl.setText(datetime.datetime.now().strftime("%H:%M:%S  %Y-%m-%d"))

    def show_help(self):
        QMessageBox.information(self, "Help", (
            "<b>tinySA Front Panel</b><br><br>"
            "<b>Hard keys</b> (FREQ, SPAN, AMPT, …) open a menu on the right of the screen.<br>"
            "<b>Softkeys</b>: click the grey keys or the on-screen labels (F1–F7 also work).<br>"
            "A parameter softkey makes it the <i>active function</i>:<br>"
            "&nbsp;• <b>Knob</b>: hover and use the mouse wheel, or click-drag (Up/Down keys too)<br>"
            "&nbsp;• <b>Arrows</b>: coarse steps (PgUp/PgDn)<br>"
            "&nbsp;• <b>Keypad</b>: type a number (PC keyboard works), then pick a unit softkey or Enter<br>"
            "&nbsp;• <b>Esc</b> cancels; <b>Back</b> deletes a digit<br><br>"
            "With no active function the knob moves the selected marker.<br>"
            "Click on the plot to drop the selected marker there.<br><br>"
            "<b>Auto</b> finds the strongest signal and zooms on it. <b>Run/Hold</b> freezes the trace.<br>"
            "<b>Gen</b> turns the tinySA into a signal generator (sweeping pauses).<br>"
            "<b>System → Console</b> sends raw shell commands."))

    def closeEvent(self, e):
        self.disconnect_device()
        super().closeEvent(e)


def main():
    ap = argparse.ArgumentParser(description=APP_NAME)
    ap.add_argument("--sim", action="store_true", help="use the built-in simulator")
    ap.add_argument("--port", help="serial port, e.g. COM7")
    ap.add_argument("--model", default="auto", choices=["auto", "ultra", "basic"])
    ap.add_argument("--screenshot", help=argparse.SUPPRESS)
    args = ap.parse_args()

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setStyleSheet(QSS)
    w = FrontPanel(args)
    w.resize(1500, 800)
    w.show()
    if os.environ.get("DEMO"):
        def demo():
            w.set_center(700.01e6); w.set_span(500e3); w.set_rbw(3)
            w.set_peak_table(True); w.set_waterfall(os.environ.get("DEMO") == "wf")
            w.markers[0].on = True; w.cont_peak = True
            w.open_menu("MKR_FN"); w.active = w.params["center"]; w.show_active()
        QTimer.singleShot(800, demo)
    if args.screenshot:
        def snap():
            w.grab().save(args.screenshot)
            app.quit()
        QTimer.singleShot(int(os.environ.get("SNAP_MS", "4000")), snap)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
