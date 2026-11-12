"""
App-side calibration for a 1.5-port VNA (NanoVNA: S11 and S21 only).

One-port error model (port 1), ideal standards:
    Gm = e00 + e10e01 * G / (1 - e11 * G)
    e00    = Ml
    e11    = (Mo + Ms - 2 Ml) / (Mo - Ms)
    e10e01 = -2 (Mo - Ml)(Ms - Ml) / (Mo - Ms)

Transmission, enhanced response (thru + optional isolation):
    e30    = isolation S21 (or 0)
    e22    = corrected reflection measured during thru (port-2 load match)
    e10e32 = (Mt21 - e30) (1 - e11 e22)
    S21    = (S21m - e30) (1 - e11 S11) / e10e32
"""
import numpy as np

STANDARDS = ("open", "short", "load", "thru", "isolation")


def interp_c(fn, f, y):
    return np.interp(fn, f, y.real) + 1j * np.interp(fn, f, y.imag)


class Calibration:
    def __init__(self):
        self.std = {}          # name -> (freqs, s11, s21)
        self.terms = None      # dict of arrays on self.freqs
        self.freqs = None
        self.enabled = False

    # ---------------------------------------------------------------
    def clear(self):
        self.std.clear()
        self.terms = None
        self.freqs = None
        self.enabled = False

    def add(self, name, freqs, s11, s21):
        self.std[name] = (np.asarray(freqs, float), np.asarray(s11, complex), np.asarray(s21, complex))

    def has(self, name):
        return name in self.std

    def kind(self):
        if self.terms is None:
            return "None"
        t = self.terms
        if t["oneport"] and t["thru"]:
            return "SOLT" if not t["iso"] else "SOLT+Iso"
        if t["oneport"]:
            return "SOL 1-Port"
        return "Thru Resp"

    def solve(self):
        """Compute error terms from whatever standards were measured."""
        ref = None
        for n in ("load", "open", "short", "thru"):
            if n in self.std:
                ref = self.std[n][0]
                break
        if ref is None:
            raise ValueError("No standards measured")
        f = ref
        for n, (fs, _, _) in self.std.items():
            if len(fs) != len(f) or np.max(np.abs(fs - f)) > 1:
                raise ValueError("Standard '%s' was measured with different sweep settings" % n)

        oneport = all(n in self.std for n in ("open", "short", "load"))
        thru = "thru" in self.std
        if not oneport and not thru:
            raise ValueError("Need Open+Short+Load and/or Thru")

        n = len(f)
        e00 = np.zeros(n, complex)
        e11 = np.zeros(n, complex)
        e10e01 = np.ones(n, complex)
        if oneport:
            Mo, Ms, Ml = self.std["open"][1], self.std["short"][1], self.std["load"][1]
            d = Mo - Ms
            d[np.abs(d) < 1e-12] = 1e-12
            e00 = Ml.copy()
            e11 = (Mo + Ms - 2 * Ml) / d
            e10e01 = -2 * (Mo - Ml) * (Ms - Ml) / d
        iso = "isolation" in self.std
        e30 = self.std["isolation"][2].copy() if iso else np.zeros(n, complex)
        if thru:
            Mt11, Mt21 = self.std["thru"][1], self.std["thru"][2]
            if oneport:
                e22 = (Mt11 - e00) / (e10e01 + e11 * (Mt11 - e00))
            else:
                e22 = np.zeros(n, complex)
            e10e32 = (Mt21 - e30) * (1 - e11 * e22)
        else:
            e22 = np.zeros(n, complex)
            e10e32 = np.ones(n, complex)
        self.freqs = f.copy()
        self.terms = dict(e00=e00, e11=e11, e10e01=e10e01, e30=e30, e22=e22, e10e32=e10e32,
                          oneport=oneport, thru=thru, iso=iso)
        self.enabled = True
        return self.kind()

    def covers(self, freqs):
        if self.terms is None:
            return False
        return freqs[0] >= self.freqs[0] - 1 and freqs[-1] <= self.freqs[-1] + 1

    def interpolated(self, freqs):
        """True if the sweep does not land exactly on the cal frequencies."""
        return not (len(freqs) == len(self.freqs) and np.max(np.abs(freqs - self.freqs)) < 1)

    def apply(self, freqs, s11, s21):
        t = self.terms
        if self.interpolated(freqs):
            g = {k: interp_c(freqs, self.freqs, t[k]) for k in ("e00", "e11", "e10e01", "e30", "e10e32")}
        else:
            g = t
        out11, out21 = s11, s21
        if t["oneport"]:
            d = s11 - g["e00"]
            out11 = d / (g["e10e01"] + g["e11"] * d)
        if t["thru"]:
            out21 = (s21 - g["e30"]) / g["e10e32"]
            if t["oneport"]:
                out21 = out21 * (1 - g["e11"] * out11)
        return out11, out21

    # ---------------------------------------------------------------
    def save(self, fn):
        d = {}
        for n, (f, a, b) in self.std.items():
            d[n + "_f"], d[n + "_s11"], d[n + "_s21"] = f, a, b
        np.savez(fn, **d)

    def load(self, fn):
        z = np.load(fn)
        self.clear()
        for n in STANDARDS:
            if n + "_f" in z:
                self.add(n, z[n + "_f"], z[n + "_s11"], z[n + "_s21"])
        return self.solve()
