"""Painted front-panel widgets for the VNA panel."""
import math

from PyQt6.QtCore import Qt, QPointF, QRectF, pyqtSignal, QSize
from PyQt6.QtGui import (QPainter, QColor, QPen, QRadialGradient, QLinearGradient,
                         QFont, QPolygonF, QPainterPath)
from PyQt6.QtWidgets import QWidget, QAbstractButton, QPushButton, QFrame


class PanelButton(QPushButton):
    def __init__(self, text, style="key", parent=None, w=None, h=None):
        super().__init__(text, parent)
        self.setObjectName(style)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        if w:
            self.setFixedWidth(w)
        if h:
            self.setFixedHeight(h)


class ClickFrame(QFrame):
    clicked = pyqtSignal()

    def mousePressEvent(self, e):
        self.clicked.emit()


class Knob(QWidget):
    """Rotary encoder with curved direction arrows. Hover + mouse wheel turns it."""
    turned = pyqtSignal(int)
    DETENT = 15.0

    def __init__(self, parent=None, size=160):
        super().__init__(parent)
        self.setFixedSize(size, size)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.setToolTip("Hover and use the mouse wheel (or drag) to turn")
        self.angle = 0.0
        self.hover = False
        self._acc = 0
        self._drag = None
        self._dacc = 0.0

    def enterEvent(self, e):
        self.hover = True
        self.update()

    def leaveEvent(self, e):
        self.hover = False
        self.update()

    def _step(self, n):
        if n:
            self.angle = (self.angle + n * self.DETENT) % 360
            self.update()
            self.turned.emit(n)

    def wheelEvent(self, e):
        self._acc += e.angleDelta().y() or e.angleDelta().x()
        s = int(self._acc / 120)
        if s:
            self._acc -= s * 120
            self._step(s)
        e.accept()

    def _ang(self, p):
        return math.degrees(math.atan2(p.y() - self.height() / 2, p.x() - self.width() / 2))

    def mousePressEvent(self, e):
        self._drag = self._ang(e.position())
        self._dacc = 0.0
        self.setCursor(Qt.CursorShape.ClosedHandCursor)

    def mouseMoveEvent(self, e):
        if self._drag is None:
            return
        a = self._ang(e.position())
        d = (a - self._drag + 180) % 360 - 180
        self._drag = a
        self._dacc += d
        s = int(self._dacc / 20)
        if s:
            self._dacc -= s * 20
            self._step(s)

    def mouseReleaseEvent(self, e):
        self._drag = None
        self.setCursor(Qt.CursorShape.OpenHandCursor)

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        c = QPointF(self.width() / 2, self.height() / 2)
        R = min(self.width(), self.height()) / 2 - 3

        # curved arrows (left = CCW, right = CW)
        p.setPen(QPen(QColor("#9aa3ad"), 7, Qt.PenStyle.SolidLine, Qt.PenCapStyle.FlatCap))
        rr = R - 4
        box = QRectF(c.x() - rr, c.y() - rr, 2 * rr, 2 * rr)
        p.drawArc(box, 150 * 16, 60 * 16)
        p.drawArc(box, -30 * 16, 60 * 16)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#9aa3ad"))
        for ang, sgn in ((210, 1), (-30, -1)):
            a = math.radians(ang)
            tip = c + QPointF(rr * math.cos(a), -rr * math.sin(a))
            t = QPointF(-math.sin(a), -math.cos(a)) * sgn          # tangent direction
            nrm = QPointF(math.cos(a), -math.sin(a))
            p.drawPolygon(QPolygonF([tip + t * 10, tip - nrm * 9, tip + nrm * 9]))

        rk = R * 0.80
        if self.hover:
            p.setPen(QPen(QColor(40, 120, 230, 210), 3))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(c, rk + 4, rk + 4)
        sk = QRadialGradient(c - QPointF(rk * .3, rk * .3), rk * 1.6)
        sk.setColorAt(0, QColor("#ffffff"))
        sk.setColorAt(1, QColor("#b3b8bf"))
        p.setPen(QPen(QColor("#8f969f"), 1))
        p.setBrush(sk)
        p.drawEllipse(c, rk, rk)
        p.save()
        p.translate(c)
        p.rotate(self.angle)
        p.setPen(QPen(QColor(110, 116, 126, 170), 1.3))
        for _ in range(16):
            p.drawLine(QPointF(0, -rk + 2), QPointF(0, -rk * 0.84))
            p.rotate(22.5)
        p.restore()
        rc = rk * 0.80
        cg = QRadialGradient(c - QPointF(rc * .35, rc * .35), rc * 1.5)
        cg.setColorAt(0, QColor("#ffffff"))
        cg.setColorAt(0.6, QColor("#eceef1"))
        cg.setColorAt(1, QColor("#c3c8cf"))
        p.setPen(QPen(QColor("#b0b6be"), 1))
        p.setBrush(cg)
        p.drawEllipse(c, rc, rc)
        a = math.radians(self.angle - 90)
        dp = c + QPointF(math.cos(a) * rc * 0.62, math.sin(a) * rc * 0.62)
        dg = QRadialGradient(dp + QPointF(2, 2), rc * 0.17)
        dg.setColorAt(0, QColor("#f4f5f7"))
        dg.setColorAt(1, QColor("#a7adb6"))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(dg)
        p.drawEllipse(dp, rc * 0.13, rc * 0.13)
        p.end()


class DPadCenter(QWidget):
    """Yellow ring behind the arrow keys."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(34, 34)

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        c = QPointF(17, 17)
        p.setPen(QPen(QColor("#c9a82a"), 4))
        g = QRadialGradient(c, 15)
        g.setColorAt(0, QColor("#6d7684"))
        g.setColorAt(1, QColor("#3f4752"))
        p.setBrush(g)
        p.drawEllipse(c, 13, 13)
        p.end()


class PowerBar(QAbstractButton):
    """Tall power key with a green light bar."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setFixedSize(46, 76)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("Connect / disconnect")

    def sizeHint(self):
        return QSize(46, 76)

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(2, 2, -2, -2)
        g = QLinearGradient(r.topLeft(), r.topRight())
        g.setColorAt(0, QColor("#f7f7f7"))
        g.setColorAt(1, QColor("#c9ced5") if not self.isDown() else QColor("#aeb4bc"))
        p.setPen(QPen(QColor("#7f8894"), 1.5))
        p.setBrush(g)
        p.drawRoundedRect(r, 10, 10)
        bar = QRectF(r.center().x() - 5, r.top() + 14, 10, r.height() - 28)
        if self.isChecked():
            glow = QRadialGradient(bar.center(), 26)
            glow.setColorAt(0, QColor(60, 255, 90, 110))
            glow.setColorAt(1, QColor(60, 255, 90, 0))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(glow)
            p.drawEllipse(bar.center(), 26, 30)
            p.setBrush(QColor("#2ee05a"))
        else:
            p.setBrush(QColor("#8a929c"))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(bar, 5, 5)
        p.end()


class PortConnector(QWidget):
    """N-type style port with coloured number tag."""

    def __init__(self, num, color, sub, parent=None):
        super().__init__(parent)
        self.num = str(num)
        self.color = QColor(color)
        self.sub = sub
        self.setFixedSize(190, 104)

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        c = QPointF(110, 50)
        # hex boss
        poly = QPolygonF([c + QPointF(46 * math.cos(math.radians(60 * i)), 46 * math.sin(math.radians(60 * i)))
                          for i in range(6)])
        g = QLinearGradient(c - QPointF(40, 40), c + QPointF(40, 40))
        g.setColorAt(0, QColor("#f2f3f5"))
        g.setColorAt(1, QColor("#aab0b8"))
        p.setPen(QPen(QColor("#8d949d"), 1))
        p.setBrush(g)
        p.drawPolygon(poly)
        for r, col in ((32, "#5d636b"), (29, None), (21, "#2b2b2b"), (13, "#f2efe6")):
            if col is None:
                rg = QRadialGradient(c - QPointF(8, 8), 34)
                rg.setColorAt(0, QColor("#ffffff"))
                rg.setColorAt(1, QColor("#8d939a"))
                p.setBrush(rg)
            else:
                p.setBrush(QColor(col))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawEllipse(c, r, r)
        gold = QRadialGradient(c, 4)
        gold.setColorAt(0, QColor("#ffe9a0"))
        gold.setColorAt(1, QColor("#b8901d"))
        p.setBrush(gold)
        p.drawEllipse(c, 3.5, 3.5)
        # number tag
        tag = QPainterPath()
        tag.addRoundedRect(QRectF(44, 0, 22, 30), 3, 3)
        p.fillPath(tag, self.color)
        p.setPen(QColor("#1a1a1a"))
        p.setFont(QFont("Segoe UI", 15, QFont.Weight.Bold))
        p.drawText(QRectF(44, 0, 22, 30), Qt.AlignmentFlag.AlignCenter, self.num)
        # label + warning
        p.setFont(QFont("Segoe UI", 7))
        p.setPen(QColor("#3c4a5e"))
        p.drawText(QRectF(0, 74, 64, 28), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, self.sub)
        tx, ty = 6, 82
        p.setPen(QPen(QColor("#222"), 1))
        p.setBrush(QColor("#f5c400"))
        p.drawPolygon(QPolygonF([QPointF(tx, ty + 12), QPointF(tx + 14, ty + 12), QPointF(tx + 7, ty)]))
        p.end()
