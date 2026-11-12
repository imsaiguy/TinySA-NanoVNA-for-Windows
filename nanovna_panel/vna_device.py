"""
NanoVNA-F V3 device layer: USB-CDC shell, simulator and worker thread.

Shell protocol (NanoVNA family): send "<cmd>\r", device echoes the line, prints the
reply, then the prompt "ch> ".
  scan <start> <stop> <points> 7  ->  "<freq> <s11re> <s11im> <s21re> <s21im>" per line
  (older firmware: scan without mask, then "data 0" / "data 1" / "frequencies")
  cal on|off, save/recall <0..6>, pause/resume, capture (800x480 RGB565 LE)
"""
import math
import queue
import threading
import time

import numpy as np
from PyQt6.QtCore import QThread, pyqtSignal

try:
    import serial
    import serial.tools.list_ports
except ImportError:  # pragma: no cover
    serial = None

ST_VID, ST_PID = 0x0483, 0x5740
PROMPT = b"ch> "
SIM_PORT = "Simulator"
FMIN, FMAX = 1e6, 6e9
SCREEN = (800, 480)


class PointsError(Exception):
    pass


def list_ports():
    out = []
    if serial is None:
        return out
    for p in serial.tools.list_ports.comports():
        is_v = (p.vid == ST_VID and p.pid == ST_PID)
        out.append((p.device, p.description or "", is_v))
    out.sort(key=lambda x: (not x[2], x[0]))
    return out


def _floats(text, ncol):
    rows = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) != ncol:
            continue
        try:
            rows.append([float(x) for x in parts])
        except ValueError:
            continue
    return np.array(rows) if rows else np.zeros((0, ncol))


# --------------------------------------------------------------------------
class SerialTransport:
    rx = b""

    def __init__(self, port):
        if serial is None:
            raise RuntimeError("pyserial is not installed")
        self.ser = serial.Serial(port, 115200, timeout=0.05, write_timeout=2)
        time.sleep(0.1)
        self.resync()

    def close(self):
        try:
            self.ser.close()
        except Exception:
            pass

    def _fill(self):
        chunk = self.ser.read(max(1, self.ser.in_waiting))
        if chunk:
            self.rx += chunk
            return True
        return False

    def _read_until(self, token, idle):
        last = time.monotonic()
        start = 0
        while True:
            i = self.rx.find(token, start)
            if i >= 0:
                out, self.rx = self.rx[:i + len(token)], self.rx[i + len(token):]
                return out
            start = max(0, len(self.rx) - len(token) + 1)
            if self._fill():
                last = time.monotonic()
            elif time.monotonic() - last > idle:
                raise TimeoutError("timeout waiting for %r (got %r)" % (token, self.rx[-60:]))

    def _read_exact(self, n, idle):
        last = time.monotonic()
        while len(self.rx) < n:
            if self._fill():
                last = time.monotonic()
            elif time.monotonic() - last > idle:
                raise TimeoutError("timeout reading %d bytes (got %d)" % (n, len(self.rx)))
        out, self.rx = self.rx[:n], self.rx[n:]
        return out

    def resync(self):
        self.ser.reset_input_buffer()
        self.rx = b""
        self.ser.write(b"\r")
        try:
            self._read_until(PROMPT, 1.0)
        except TimeoutError:
            pass
        time.sleep(0.05)
        self.ser.reset_input_buffer()
        self.rx = b""

    def command(self, cmd, idle=3.0):
        self.ser.write((cmd + "\r").encode())
        resp = self._read_until(PROMPT, idle)[:-len(PROMPT)]
        lines = resp.decode(errors="replace").replace("\r", "").split("\n")
        if lines and lines[0].strip() == cmd.strip():
            lines = lines[1:]
        return "\n".join(lines).strip()

    def scan(self, start, stop, n):
        resp = self.command("scan %d %d %d 7" % (int(start), int(stop), int(n)), idle=15.0)
        low = resp.lower()
        if "usage" in low or "invalid" in low or "error" in low or "out of range" in low:
            raise PointsError(resp[:100])
        a = _floats(resp, 5)
        if len(a) == n:
            return a[:, 0], a[:, 1] + 1j * a[:, 2], a[:, 3] + 1j * a[:, 4]
        if len(a) == 0:
            # firmware without the outmask: data is left in the sweep buffers
            d0 = _floats(self.command("data 0", 8.0), 2)
            d1 = _floats(self.command("data 1", 8.0), 2)
            fr = _floats(self.command("frequencies", 8.0), 1)
            if len(d0) == n and len(d1) == n:
                f = fr[:, 0] if len(fr) == n else np.linspace(start, stop, n)
                return f, d0[:, 0] + 1j * d0[:, 1], d1[:, 0] + 1j * d1[:, 1]
        raise ValueError("scan returned %d of %d points: %r" % (len(a), n, resp[:80]))

    def capture(self, w, h):
        self.ser.write(b"capture\r")
        self._read_until(b"capture\r\n", 2.0)
        data = self._read_exact(w * h * 2, 5.0)
        self._read_until(PROMPT, 3.0)
        return data


# --------------------------------------------------------------------------
def abcd_series(z):
    o, zz = np.ones_like(z), np.zeros_like(z)
    return np.array([[o, z], [zz, o]])


def abcd_shunt(y):
    o, zz = np.ones_like(y), np.zeros_like(y)
    return np.array([[o, zz], [y, o]])


def abcd_line(f, length_s):
    th = 2 * np.pi * f * length_s
    c, s = np.cos(th) + 0j, 1j * np.sin(th)
    return np.array([[c, 50 * s], [s / 50, c]])


def mm(a, b):
    return np.einsum("ijn,jkn->ikn", a, b)


def abcd_to_s(m, z0=50.0):
    A, B, C, D = m[0, 0], m[0, 1], m[1, 0], m[1, 1]
    den = A + B / z0 + C * z0 + D
    s11 = (A + B / z0 - C * z0 - D) / den
    s21 = 2 / den
    s22 = (-A + B / z0 - C * z0 + D) / den
    return s11, s21, s22


class SimTransport:
    """Synthetic NanoVNA with realistic port errors, for testing without hardware."""
    MAX_POINTS = 201

    def __init__(self):
        self.cal_on = True
        self.dut = "filter"
        self.t0 = time.monotonic()

    def close(self):
        pass

    def resync(self):
        pass

    def command(self, cmd, idle=3.0):
        a = cmd.split()
        if not a:
            return ""
        c = a[0]
        if c == "version":
            return "NanoVNA-F V3 SIM 0.0.0"
        if c == "info":
            return "NanoVNA-F V3 simulator\n1 MHz - 6 GHz\nDUT: %s" % self.dut
        if c == "SN":
            return "SIM-000000"
        if c == "cal":
            if len(a) > 1 and a[1] in ("on", "off"):
                self.cal_on = a[1] == "on"
            return "" if len(a) > 1 else "cal %s" % ("on" if self.cal_on else "off")
        if c == "simdut" and len(a) > 1:
            self.dut = a[1]
            return ""
        if c in ("pause", "resume", "recall", "save", "edelay", "cwfreq", "beep"):
            return ""
        if c == "help":
            return "Commands: help version info SN cal save recall pause resume scan data frequencies capture"
        return "%s?" % c

    def _dut(self, f):
        if self.dut == "open":
            g = np.ones_like(f, complex)
            return g, np.zeros_like(g), np.zeros_like(g)
        if self.dut == "short":
            g = -np.ones_like(f, complex)
            return g, np.zeros_like(g), np.zeros_like(g)
        if self.dut == "load":
            g = np.zeros_like(f, complex)
            return g, g.copy(), g.copy()
        if self.dut == "thru":
            z = np.zeros_like(f, complex)
            return z, np.ones_like(f, complex), z.copy()
        # 3rd-order Butterworth bandpass, 1 GHz, 10 % FBW, Q=150 resonators, 20 cm of cable-ish delay
        w = 2 * np.pi * np.maximum(f, 1.0)
        f0, fbw, z0, q = 1.0e9, 0.10, 50.0, 150.0
        w0 = 2 * np.pi * f0
        g1, g2, g3 = 1.0, 2.0, 1.0

        def shunt(g):
            C = g / (fbw * w0 * z0)
            L = fbw * z0 / (w0 * g)
            Y = 1j * w * C + 1 / (1j * w * L) + w0 * C / q
            return abcd_shunt(Y)

        def series(g):
            L = g * z0 / (fbw * w0)
            C = fbw / (w0 * g * z0)
            Z = 1j * w * L + 1 / (1j * w * C) + w0 * L / q
            return abcd_series(Z)
        m = mm(abcd_line(f, 0.4e-9), mm(shunt(g1), mm(series(g2), mm(shunt(g3), abcd_line(f, 0.4e-9)))))
        s11, s21, s22 = abcd_to_s(m)
        return s11, s21, s22

    def _errors(self, f):
        x = f / 6e9
        e00 = 0.06 * np.exp(1j * 2 * np.pi * f * 0.21e-9) * (0.5 + x)
        e11 = 0.12 * np.exp(-1j * 2 * np.pi * f * 0.35e-9) * (0.4 + x)
        e10e01 = 0.85 * (1 - 0.35 * x) * np.exp(-1j * 2 * np.pi * f * 2 * 0.6e-9)
        e30 = 2e-4 * np.exp(1j * f / 3e8)
        e22 = 0.09 * np.exp(-1j * 2 * np.pi * f * 0.5e-9) * (0.5 + x)
        e10e32 = 0.7 * (1 - 0.4 * x) * np.exp(-1j * 2 * np.pi * f * 1.3e-9)
        return e00, e11, e10e01, e30, e22, e10e32

    def scan(self, start, stop, n):
        if n > self.MAX_POINTS or n < 11:
            raise PointsError("usage: scan {start(Hz)} {stop(Hz)} [points 11..%d] [outmask]" % self.MAX_POINTS)
        f = np.round(np.linspace(start, stop, n))
        s11, s21, s22 = self._dut(f)
        s12 = s21
        rng = np.random.default_rng()
        nz = lambda s: s * (rng.normal(size=n) + 1j * rng.normal(size=n))
        if self.cal_on:
            m11 = s11 + nz(6e-4)
            m21 = s21 + nz(2.5e-4)
        else:
            e00, e11, e10e01, e30, e22, e10e32 = self._errors(f)
            gin = s11 + s21 * s12 * e22 / (1 - s22 * e22)
            m11 = e00 + e10e01 * gin / (1 - e11 * gin) + nz(6e-4)
            den = (1 - e11 * s11) * (1 - e22 * s22) - e11 * e22 * s21 * s12
            m21 = e30 + e10e32 * s21 / den + nz(2.5e-4)
        time.sleep(n / 600.0)
        return f, m11, m21

    def capture(self, w, h):
        y, x = np.mgrid[0:h, 0:w]
        r = (x * 31 // w).astype(np.uint16)
        g = (y * 63 // h).astype(np.uint16)
        b = np.full_like(r, 20)
        return ((r << 11) | (g << 5) | b).astype("<u2").tobytes()


# --------------------------------------------------------------------------
class VNAWorker(QThread):
    sweep_ready = pyqtSignal(object, object, object, float)   # freqs, s11, s21, seconds
    connected = pyqtSignal(str)
    disconnected = pyqtSignal(str)
    reply = pyqtSignal(int, object)
    status = pyqtSignal(str)

    def __init__(self, port):
        super().__init__()
        self.port = port
        self.q = queue.Queue()
        self.lock = threading.Lock()
        self.sweep = (1e6, 1.5e9, 201)
        self.chunk = 201
        self.running = True
        self.single = False
        self._stop = False
        self.gen = 0          # bumps when sweep settings change so stale sweeps are dropped

    def set_sweep(self, start, stop, n):
        with self.lock:
            self.sweep = (float(start), float(stop), int(n))
            self.gen += 1

    def set_chunk(self, n):
        self.chunk = int(n)

    def set_running(self, run):
        self.running = bool(run)

    def single_sweep(self):
        self.single = True

    def send(self, cmd, token=None):
        self.q.put(("cmd", cmd, token))

    def capture(self, token):
        self.q.put(("capture", None, token))

    def stop(self):
        self._stop = True

    def _drain(self, tr):
        try:
            while True:
                kind, arg, token = self.q.get_nowait()
                if kind == "cmd":
                    r = tr.command(arg)
                else:
                    w, h = SCREEN
                    r = (w, h, tr.capture(w, h))
                if token is not None:
                    self.reply.emit(token, r)
        except queue.Empty:
            pass

    def run(self):
        tr = None
        try:
            tr = SimTransport() if self.port == SIM_PORT else SerialTransport(self.port)
            ver = tr.command("version")
            self.connected.emit(ver)
        except Exception as e:
            if tr:
                tr.close()
            self.disconnected.emit("Connect failed: %s" % e)
            return
        reason = "Disconnected"
        errors = 0
        try:
            while not self._stop:
                self._drain(tr)
                if not (self.running or self.single):
                    time.sleep(0.02)
                    continue
                with self.lock:
                    start, stop, n = self.sweep
                    gen = self.gen
                f_all = np.round(np.linspace(start, stop, n))
                s11 = np.zeros(n, complex)
                s21 = np.zeros(n, complex)
                t0 = time.perf_counter()
                i0 = 0
                ok = True
                while i0 < n and not self._stop:
                    m = min(self.chunk, n - i0)
                    if m < 11:                       # keep every segment >= 11 points
                        i0 = max(0, n - 11)
                        m = n - i0
                    try:
                        f, a, b = tr.scan(f_all[i0], f_all[i0 + m - 1], m)
                        errors = 0
                    except PointsError as ex:
                        if self.chunk > 101:
                            self.chunk = 101
                            self.status.emit("Device limits scan length - using 101-point segments")
                            continue
                        raise RuntimeError("scan rejected: %s" % ex)
                    except (ValueError, TimeoutError) as ex:
                        errors += 1
                        self.status.emit("Sweep error: %s (resyncing)" % ex)
                        tr.resync()
                        if errors > 5:
                            raise
                        ok = False
                        break
                    s11[i0:i0 + m] = a
                    s21[i0:i0 + m] = b
                    i0 += m
                    self._drain(tr)          # stay responsive between segments
                    if gen != self.gen:
                        ok = False
                        break
                if ok and not self._stop:
                    self.single = False
                    self.sweep_ready.emit(f_all, s11, s21, time.perf_counter() - t0)
        except Exception as e:
            reason = "Connection lost: %s" % e
        finally:
            tr.close()
        self.disconnected.emit(reason)
