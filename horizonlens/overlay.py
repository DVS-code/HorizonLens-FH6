from __future__ import annotations

import ctypes
import math
import os
import threading
import time
from ctypes import wintypes

from PySide6.QtCore import QEvent, QObject, QPoint, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (QColor, QFocusEvent, QFont, QKeyEvent, QPainter, QPainterPath, QPen,
                           QPixmap, QPolygonF)
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDoubleSpinBox, QFrame, QGridLayout,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMenu, QPushButton, QScrollArea, QSizePolicy, QSlider, QStackedWidget, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from . import camworld, config, curves, game, relay, sequencer as seqmod, vista

SHEET = "#1c1c1e"
CELL = "#2c2c2e"
CELL2 = "#3a3a3c"
LINE = "#38383a"
TXT = "#ffffff"
MUTED = "#98989f"
ORANGE = "#f59e3b"
GOOD = "#30d158"
BAD = "#ff453a"
FONT = "'Segoe UI Variable Text','Segoe UI',sans-serif"

HOTKEYS = [
    (0x77, "toggle", "F8  Show / hide the menu"),
    (0x71, "flytoggle", "F2  Fly camera on / off"),
    (0x78, "add", "F9  Add key"),
    (0x7A, "update", "F11  Set key from camera"),
    (0x74, "fly", "F5  Fly to key"),
    (0x75, "play", "F6  Play / pause"),
    (0x76, "stop", "F7  Stop (camera back)"),
    (0x72, "prev", "F3  Previous key"),
    (0x73, "next", "F4  Next key"),
]
VK = {"FWD": 0x26, "LEFT": 0x25, "BACK": 0x28, "RIGHT": 0x27, "UP": 0x21, "DOWN": 0x22,
      "SHIFT": 0x10, "CTRL": 0x11, "RBUTTON": 0x02}

_u32 = ctypes.WinDLL("user32", use_last_error=True)
_u32.GetForegroundWindow.restype = wintypes.HWND
_u32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
_u32.GetAsyncKeyState.argtypes = [ctypes.c_int]
_u32.GetAsyncKeyState.restype = ctypes.c_short
_u32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
_u32.SetForegroundWindow.argtypes = [wintypes.HWND]
_u32.IsWindow.argtypes = [wintypes.HWND]
_u32.IsIconic.argtypes = [wintypes.HWND]
_u32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
_u32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
_u32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
_u32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
_u32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
_u32.SetWindowDisplayAffinity.argtypes = [wintypes.HWND, wintypes.DWORD]
_u32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                              ctypes.c_int, ctypes.c_int, wintypes.UINT]
GWL_EXSTYLE, GWLP_HWNDPARENT = -20, -8
WS_EX_TRANSPARENT, WS_EX_LAYERED, WS_EX_TOOLWINDOW = 0x20, 0x80000, 0x80
WDA_EXCLUDEFROMCAPTURE = 0x11


def _foreground_pid():
    hwnd = _u32.GetForegroundWindow()
    if not hwnd:
        return None
    pid = wintypes.DWORD()
    _u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


_win_cache = [0.0, None]


def game_client_rect():
    now = time.monotonic()
    if now - _win_cache[0] > 1.0 or not _win_cache[1] or not _u32.IsWindow(_win_cache[1][0]):
        pid = game.game_pid_cached()
        _win_cache[:] = [now, game.main_window(pid) if pid else None]
    win = _win_cache[1]
    if not win:
        return None
    hwnd = win[0]
    rect = wintypes.RECT()
    if _u32.IsIconic(hwnd) or not _u32.GetClientRect(hwnd, ctypes.byref(rect)):
        return None
    w, h = rect.right - rect.left, rect.bottom - rect.top
    pt = wintypes.POINT(0, 0)
    if not _u32.ClientToScreen(hwnd, ctypes.byref(pt)):
        return None
    return hwnd, pt.x, pt.y, w, h


def focus_game():
    got = game_client_rect()
    if got:
        try:
            _u32.SetForegroundWindow(got[0])
        except Exception:
            pass


_k32 = ctypes.WinDLL("kernel32")
def _key(vk):
    return bool(_u32.GetAsyncKeyState(vk) & 0x8000)


def _fmt_time(t):
    t = max(0.0, t)
    return f"{int(t // 60)}:{t % 60:05.2f}"


def look_rows(yaw_deg, pitch_deg):
    y, p = math.radians(yaw_deg), math.radians(pitch_deg)
    fwd = [math.sin(y) * math.cos(p), math.sin(p), math.cos(y) * math.cos(p)]
    right = [math.cos(y), 0.0, -math.sin(y)]
    up = [fwd[1] * right[2] - fwd[2] * right[1], fwd[2] * right[0] - fwd[0] * right[2],
          fwd[0] * right[1] - fwd[1] * right[0]]
    return [right, up, fwd]


class FlyCam:

    RAMP_S = 0.18
    MAX_SPEED = 60.0
    ZOOM_RATE = 1.8

    def __init__(self):
        self.space = "car"
        self.pos = list(seqmod.DEFAULT_POSE["pos"])
        self.yaw, self.pitch = 0.0, -8.0
        self.zoom = 1.0
        self.speed = 6.0
        self.vel = [0.0, 0.0, 0.0]

    @property
    def fov(self):
        return seqmod.zoom_to_fov(self.zoom)

    @fov.setter
    def fov(self, degrees):
        self.zoom = seqmod.fov_to_zoom(degrees)

    def effective_speed(self, keys):
        mult = 4.0 if keys.get("SHIFT") else 0.25 if keys.get("CTRL") else 1.0
        return min(self.MAX_SPEED, self.speed * mult)

    def start_from(self, pose=None, *_ignored, **_kw):
        pose = pose if pose and pose.get("pos") is not None else seqmod.DEFAULT_POSE
        self.vel = [0.0, 0.0, 0.0]
        self.pos = list(pose["pos"])
        self.fov = float(pose.get("fov", 70.0))
        m = camworld.quat_to_mat(pose["quat"])
        fwd = [m[0][2], m[1][2], m[2][2]]
        self.yaw = math.degrees(math.atan2(fwd[0], fwd[2]))
        self.pitch = math.degrees(math.asin(max(-1.0, min(1.0, fwd[1]))))

    def _wanted(self, keys):
        d = [(1.0 if keys.get("RIGHT") else 0.0) - (1.0 if keys.get("LEFT") else 0.0),
             (1.0 if keys.get("UP") else 0.0) - (1.0 if keys.get("DOWN") else 0.0),
             (1.0 if keys.get("FWD") else 0.0) - (1.0 if keys.get("BACK") else 0.0)]
        n = math.sqrt(sum(v * v for v in d))
        return [v / n for v in d] if n > 1e-6 else [0.0, 0.0, 0.0]

    def step(self, dt, keys, look_dx=0.0, look_dy=0.0):
        self.yaw = math.remainder(self.yaw + look_dx * 0.15, 360.0)
        self.pitch = max(-89.0, min(89.0, self.pitch - look_dy * 0.15))
        zdir = (1 if keys.get("ZIN") else 0) - (1 if keys.get("ZOUT") else 0)
        if zdir and dt > 0:
            self.zoom = max(seqmod.ZOOM_MIN, min(seqmod.ZOOM_MAX,
                                                 self.zoom * self.ZOOM_RATE ** (zdir * dt)))
        speed = self.effective_speed(keys)
        want = [v * speed for v in self._wanted(keys)]
        rows = look_rows(self.yaw, self.pitch)
        target = [want[0] * rows[0][i] + want[2] * rows[2][i] for i in range(3)]
        target[1] += want[1]
        a = 1.0 if dt <= 0 else min(1.0, dt / self.RAMP_S)
        self.vel = [v + (t - v) * a for v, t in zip(self.vel, target)]
        if dt > 0:
            self.pos = [p + v * dt for p, v in zip(self.pos, self.vel)]

    @property
    def moving(self):
        return any(abs(v) > 1e-3 for v in self.vel)

    def sample(self):
        rows = look_rows(self.yaw, self.pitch)
        pos = tuple(self.pos)
        return {"space": "car", "pos": pos,
                "quat": camworld.mat_to_quat(camworld.transpose(rows)), "fov": self.fov,
                "camera_target": seqmod.cabin_target(pos)}


STYLE = f"""
QWidget {{ color:{TXT}; font: 12px {FONT}; }}
QLabel#title {{ font: 600 15px {FONT}; }}
QLabel#muted, QLabel.muted {{ color:{MUTED}; }}
QLabel#sec {{ color:{MUTED}; font: 600 11px {FONT}; letter-spacing:.6px; }}
QPushButton {{ background:{CELL}; border:1px solid {LINE}; border-radius:9px; padding:5px 11px; }}
QPushButton:hover {{ background:{CELL2}; }}
QPushButton:pressed {{ background:#48484a; }}
QPushButton:disabled {{ color:#5a5a5e; }}
QPushButton#accent {{ background:{ORANGE}; color:#000; border:0; font-weight:600; }}
QPushButton#accent:hover {{ background:#ffb05a; }}
QPushButton#seg {{ border-radius:8px; padding:6px 12px; }}
QPushButton#seg:checked {{ background:{ORANGE}; color:#000; border:0; font-weight:600; }}
QPushButton#icon {{ border:0; background:transparent; padding:2px 6px; font-size:15px; }}
QPushButton#tool, QPushButton#toolseg {{ padding:7px 12px; font-weight:600; }}
QPushButton#toolseg:checked {{ background:#64d2ff; color:#000; border:0; }}
QPushButton#accent {{ padding:7px 14px; }}
QFrame#card {{ background:{CELL}; border-radius:12px; }}
QFrame#card QLabel, QFrame#card QCheckBox {{ background:transparent; }}
QLabel#guide {{ color:{ORANGE}; font: 600 13px {FONT}; }}
QPushButton#icon:hover {{ background:{CELL}; }}
QLineEdit, QDoubleSpinBox, QComboBox {{ background:{CELL}; border:1px solid {LINE};
    border-radius:8px; padding:4px 8px; selection-background-color:{ORANGE}; }}
QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {{ width:0; }}
QComboBox QAbstractItemView {{ background:{CELL}; border:1px solid {LINE};
    selection-background-color:{ORANGE}; selection-color:#000; }}
QCheckBox::indicator {{ width:16px; height:16px; border-radius:5px; border:1px solid {LINE};
    background:{CELL}; }}
QCheckBox::indicator:checked {{ background:{ORANGE}; border-color:{ORANGE}; }}
QListWidget {{ background:{CELL}; border:1px solid {LINE}; border-radius:10px; padding:4px; }}
QListWidget::item {{ padding:6px 8px; border-radius:7px; }}
QListWidget::item:selected {{ background:{ORANGE}; color:#000; }}
QTableWidget {{ background:{CELL}; border:1px solid {LINE}; border-radius:10px;
    gridline-color:{LINE}; selection-background-color:rgba(245,158,59,.35); }}
QHeaderView::section {{ background:{SHEET}; color:{MUTED}; border:0; padding:4px;
    font: 600 11px {FONT}; }}
QTableCornerButton::section {{ background:{SHEET}; border:0; }}
QMenu {{ background:{CELL}; border:1px solid {LINE}; border-radius:8px; padding:4px; }}
QMenu::item {{ padding:6px 18px; border-radius:6px; }}
QMenu::item:selected {{ background:{ORANGE}; color:#000; }}
QMenu::separator {{ height:1px; background:{LINE}; margin:4px 6px; }}
QScrollBar:vertical {{ background:transparent; width:8px; }}
QScrollBar::handle:vertical {{ background:#4a4a4e; border-radius:4px; min-height:24px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height:0; }}
"""


class _Bridge(QObject):
    changed = Signal()


_ACTION_ORDER = {a: i for i, (a, _, _) in enumerate(vista.ACTIONS)}


ZOOM_COLOR = "#5ac8fa"
AIR_COLOR = "#bf5af2"
CAMBER_COLOR = "#ff9f0a"
TRACK_LABELS = {seqmod.TRACK_ZOOM: "Zoom", seqmod.TRACK_AIR: "Air suspension"}


def action_color(action) -> QColor:
    if action == seqmod.TRACK_ZOOM:
        return QColor(ZOOM_COLOR)
    if action == seqmod.TRACK_AIR:
        return QColor(AIR_COLOR)
    i = _ACTION_ORDER.get(action, 0)
    return QColor.fromHsvF(((i * 137.508) % 360) / 360.0, 0.62, 0.98)


def track_label(action):
    return TRACK_LABELS.get(action) or vista.ACTION_LABELS.get(action, action)


class CurveEditor(QWidget):

    committed = Signal(list)
    ML, MR, MT, MB = 64, 12, 10, 22

    def __init__(self, color, top="End", bottom="Start", parent=None):
        super().__init__(parent)
        self.color = QColor(color)
        self.top, self.bottom = top, bottom
        self._values = list(curves.DEFAULT_CURVE)
        self._drag = None
        self._hover = None
        self.setFixedHeight(124)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setMouseTracking(True)
        self.setToolTip("Drag the middle points. Left to right is the transition time; "
                        "bottom to top is how far it has got.")

    def values(self):
        return list(self._values)

    def set_values(self, values):
        if self._drag is None:
            self._values = curves.normalise(values)
            self.update()

    def _plot(self):
        return QRectF(self.ML, self.MT, max(1.0, self.width() - self.ML - self.MR),
                      max(1.0, self.height() - self.MT - self.MB))

    def _point(self, i):
        r = self._plot()
        return QPointF(r.left() + r.width() * i / (len(self._values) - 1),
                       r.bottom() - r.height() * self._values[i])

    def _nearest(self, pos):
        best, dist = None, 12.0 ** 2
        for i in range(1, len(self._values) - 1):
            pt = self._point(i)
            d = (pt.x() - pos.x()) ** 2 + (pt.y() - pos.y()) ** 2
            if d <= dist:
                best, dist = i, d
        return best

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = self._plot()
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(CELL))
        p.drawRoundedRect(QRectF(self.rect()).adjusted(0, 0, -1, -1), 10, 10)
        p.setPen(QPen(QColor(LINE), 1))
        for k in range(5):
            y = r.top() + r.height() * k / 4
            p.drawLine(QPointF(r.left(), y), QPointF(r.right(), y))
        for k in range(len(self._values)):
            x = r.left() + r.width() * k / (len(self._values) - 1)
            p.drawLine(QPointF(x, r.top()), QPointF(x, r.bottom()))
        path = QPainterPath(self._point(0))
        for i in range(1, len(self._values)):
            path.lineTo(self._point(i))
        fill = QPainterPath(path)
        fill.lineTo(r.bottomRight())
        fill.lineTo(r.bottomLeft())
        fill.closeSubpath()
        shade = QColor(self.color)
        shade.setAlpha(45 if self.isEnabled() else 15)
        p.fillPath(fill, shade)
        line = QColor(self.color if self.isEnabled() else MUTED)
        p.setPen(QPen(line, 2.0))
        p.drawPath(path)
        p.setPen(QPen(QColor(SHEET), 1.5))
        for i in range(len(self._values)):
            p.setBrush(QColor(TXT) if i == self._hover else line)
            rad = 5.5 if i == self._hover else 4.5
            p.drawEllipse(self._point(i), rad, rad)
        p.setPen(QColor(MUTED))
        p.setFont(QFont("Segoe UI", 8))
        p.drawText(QRectF(4, r.top() - 6, self.ML - 10, 16), Qt.AlignRight, self.top)
        p.drawText(QRectF(4, r.bottom() - 10, self.ML - 10, 16), Qt.AlignRight, self.bottom)
        p.drawText(QRectF(r.left(), r.bottom() + 3, r.width(), self.MB - 3), Qt.AlignCenter,
                   "Transition time")
        p.end()

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton and self.isEnabled():
            self._drag = self._nearest(e.position())
            self._move(e.position().y())

    def mouseMoveEvent(self, e):
        if self._drag is not None:
            self._move(e.position().y())
            return
        hover = self._nearest(e.position())
        if hover != self._hover:
            self._hover = hover
            self.update()

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton and self._drag is not None:
            self._drag = None
            self.committed.emit(self.values())

    def leaveEvent(self, _):
        if self._hover is not None:
            self._hover = None
            self.update()

    def _move(self, y):
        if self._drag is None:
            return
        r = self._plot()
        self._values[self._drag] = max(0.0, min(1.0, (r.bottom() - y) / max(1.0, r.height())))
        self.update()


def action_icon(action):
    from PySide6.QtGui import QIcon
    pm = QPixmap(14, 14)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(Qt.NoPen)
    p.setBrush(action_color(action))
    p.drawEllipse(QRectF(1, 1, 12, 12))
    p.end()
    return QIcon(pm)


class Timeline(QWidget):

    RULER_H = 22
    BAR_Y = 28
    BAR_H = 44
    KEY_Y = 62
    LANE_Y0 = 80
    LANE_H = 18
    GRAPH_LANE_H = 30
    EDGE_PX = 7
    SNAP_PX = 8

    def __init__(self, panel):
        super().__init__()
        self.panel = panel
        self.eng = panel.eng
        self.setMouseTracking(True)
        self.view0, self.view_len = 0.0, None
        self._drag = None
        self._graphs = {}
        self._fit_height()

    def lanes(self):
        cfg = self.eng.seq.settings
        out = [a for a in seqmod.SPECIAL_TRACKS if cfg[a]["enabled"]]
        if cfg["animations"]["enabled"]:
            used = {g.action for g in self.eng.seq.triggers if g.action not in seqmod.SPECIAL_TRACKS}
            out += sorted(used, key=lambda a: _ACTION_ORDER.get(a, 99))
        return out

    def lane_rows(self):
        rows, y = [], self.LANE_Y0
        for a in self.lanes():
            h = self.GRAPH_LANE_H if a in seqmod.SPECIAL_TRACKS else self.LANE_H
            rows.append((a, y, h))
            y += h
        return rows

    def _fit_height(self):
        rows = self.lane_rows()
        bottom = rows[-1][1] + rows[-1][2] if rows else self.LANE_Y0 + self.LANE_H
        h = int(bottom + 6)
        if self.minimumHeight() != h:
            self.setMinimumHeight(h)
            self.setMaximumHeight(h)

    def _span(self):
        total = max(1.0, self.eng.seq.duration)
        length = self.view_len or total * 1.05
        return self.view0, max(0.5, length)

    def t2x(self, t):
        v0, length = self._span()
        return 10 + (t - v0) / length * (self.width() - 20)

    def x2t(self, x):
        v0, length = self._span()
        return v0 + (x - 10) / max(1, self.width() - 20) * length

    def _clips(self):
        shots = self.eng.seq.shots
        order = list(range(len(shots)))
        d = self._drag
        if d and d[0] == "shot" and d[3]:
            order.insert(d[4], order.pop(d[1]))
        out, start = [], 0.0
        for i in order:
            dur, tin = shots[i].duration, shots[i].trim_in
            if d and d[0] == "trim" and d[1] == i:
                tin, dur = d[6], d[7]
            out.append((i, start, dur, tin))
            start += dur
        return out

    def _snap(self, t, also=()):
        targets = [self.eng.time()] + list(also)
        start = 0.0
        for s in self.eng.seq.shots:
            targets.append(start)
            start += s.duration
        targets.append(start)
        px = self.SNAP_PX * self._span()[1] / max(1, self.width() - 20)
        best = min(targets, key=lambda v: abs(v - t))
        return best if abs(best - t) <= px else t

    def paintEvent(self, _):
        self._fit_height()
        eng = self.eng
        seq = eng.seq
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(CELL))
        p.drawRoundedRect(QRectF(0, 0, w, h), 10, 10)
        v0, length = self._span()
        step = next(s for s in (0.25, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300) if length / s <= 14)
        p.setPen(QPen(QColor(MUTED)))
        p.setFont(QFont("Segoe UI", 8))
        t = math.floor(v0 / step) * step
        while t <= v0 + length:
            x = self.t2x(t)
            if x >= 6:
                p.drawLine(QPointF(x, self.RULER_H - 6), QPointF(x, self.RULER_H))
                p.drawText(QPointF(x + 3, 13), _fmt_ruler(t))
            t += step
        drag = self._drag
        for i, start, dur, tin in self._clips():
            shot = seq.shots[i]
            x0, x1 = self.t2x(start), self.t2x(start + dur)
            current = i == eng.shot_index
            moving = drag and drag[0] == "shot" and drag[3] and drag[1] == i
            rect = QRectF(x0 + 1, self.BAR_Y, max(4, x1 - x0 - 2), self.BAR_H)
            base = QColor("#2f6f73") if shot.mode == "world" else QColor("#5b4a8a")
            if moving:
                base.setAlpha(150)
            p.setPen(Qt.NoPen)
            p.setBrush(base)
            p.drawRoundedRect(rect, 6, 6)
            p.setBrush(QColor(0, 0, 0, 70))
            p.drawRoundedRect(QRectF(rect.x(), rect.y(), rect.width(), 16), 6, 6)
            p.setPen(QColor(TXT))
            p.setFont(QFont("Segoe UI", 8, QFont.DemiBold))
            mode = "Stays in place" if shot.mode == "world" else "Follows car"
            p.drawText(rect.adjusted(7, 1, -4, -(self.BAR_H - 16)), Qt.AlignVCenter | Qt.AlignLeft,
                       f"{shot.name} · {mode} · {dur:.1f} s")
            if current:
                p.setPen(QPen(QColor(TXT), 2))
                p.setBrush(Qt.NoBrush)
                p.drawRoundedRect(rect, 6, 6)
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(TXT))
                for hx in (rect.left(), rect.right() - 6):
                    p.drawRoundedRect(QRectF(hx, rect.top(), 6, rect.height()), 3, 3)
                p.setBrush(QColor("#000"))
                for hx in (rect.left() + 2.5, rect.right() - 3.5):
                    p.drawRect(QRectF(hx, rect.center().y() - 6, 1, 12))
            p.save()
            p.setClipRect(rect)
            for k_i, key in enumerate(shot.keys):
                if not (tin - 1e-6 <= key.t <= tin + dur + 1e-6):
                    continue
                kx = self.t2x(start + key.t - tin)
                if drag and drag[0] == "key" and current and drag[1] == k_i and drag[2] is not None:
                    kx = self.t2x(start + drag[2])
                kx = self._inside(kx, start, dur)
                selected = current and k_i == eng.key_index
                size = 6 if selected else 4.5
                poly = QPolygonF([QPointF(kx, self.KEY_Y - size), QPointF(kx + size, self.KEY_Y),
                                  QPointF(kx, self.KEY_Y + size), QPointF(kx - size, self.KEY_Y)])
                p.setPen(QPen(QColor(ORANGE if selected else "#000"), 1.5))
                p.setBrush(QColor(TXT if current else "#c7c7cc"))
                p.drawPolygon(poly)
            p.restore()
        if drag and drag[0] == "shot" and drag[3]:
            land = next(s for i, s, _, _ in self._clips() if i == drag[1])
            x = self.t2x(land)
            p.setPen(QPen(QColor(ORANGE), 2))
            p.drawLine(QPointF(x, self.BAR_Y - 3), QPointF(x, self.BAR_Y + self.BAR_H + 3))
        rows = self.lane_rows()
        total = seq.duration
        p.setFont(QFont("Segoe UI", 7, QFont.DemiBold))
        if not rows:
            p.setPen(QColor(MUTED))
            p.drawText(QRectF(12, self.LANE_Y0, w - 24, self.LANE_H), Qt.AlignVCenter | Qt.AlignLeft,
                       "Every track is off: turn them on in Settings")
        for row, (action, y, lh) in enumerate(rows):
            color = action_color(action)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(255, 255, 255, 10 if row % 2 else 18))
            p.drawRect(QRectF(4, y, w - 8, lh))
            mine = [(i, g) for i, g in enumerate(seq.triggers) if g.action == action]
            times = [self._trigger_t(i, g) for i, g in mine]
            one_shot = vista_one_shot(action)
            if action in seqmod.SPECIAL_TRACKS:
                self._paint_graph(p, action, y, lh, color)
                if not mine:
                    p.setPen(QColor(MUTED))
                    p.drawText(QRectF(130, y, w - 140, lh), Qt.AlignVCenter | Qt.AlignLeft,
                               "+ Keyframe › " + track_label(action) + " adds one at the playhead")
            elif not one_shot:
                fill = QColor(color)
                fill.setAlpha(70)
                p.setBrush(fill)
                srt = sorted(times)
                for a in range(0, len(srt), 2):
                    end = srt[a + 1] if a + 1 < len(srt) else total
                    p.drawRect(QRectF(self.t2x(srt[a]), y + 4, max(1.0, self.t2x(end) - self.t2x(srt[a])),
                                      lh - 8))
            label = track_label(action)
            if action == seqmod.TRACK_AIR and seq.settings["air"]["camber"]:
                label += " + camber"
            tw = p.fontMetrics().horizontalAdvance(label)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(28, 28, 30, 215))
            p.drawRoundedRect(QRectF(6, y + lh / 2 - 7, tw + 10, 14), 4, 4)
            p.setPen(color.lighter(120))
            p.drawText(QRectF(11, y, tw + 4, lh), Qt.AlignVCenter | Qt.AlignLeft, label)
            order = sorted(range(len(mine)), key=lambda j: times[j])
            for rank, j in enumerate(order):
                i, g = mine[j]
                x, cy = self.t2x(times[j]), y + lh / 2
                selected = i == eng.trigger_index
                p.setPen(QPen(QColor(TXT) if selected else color.darker(160), 2 if selected else 1.2))
                if action == seqmod.TRACK_ZOOM:
                    p.setBrush(color)
                    p.drawPolygon(QPolygonF([QPointF(x, cy - 6), QPointF(x + 6, cy),
                                             QPointF(x, cy + 6), QPointF(x - 6, cy)]))
                    p.setPen(QColor(TXT))
                    p.drawText(QPointF(x + 8, cy + 4), f"{(g.value or 1.0):.1f}×")
                    continue
                if one_shot:
                    p.setBrush(color)
                    p.drawPolygon(QPolygonF([QPointF(x, cy - 6), QPointF(x + 6, cy + 5),
                                             QPointF(x - 6, cy + 5)]))
                else:
                    on = rank % 2 == 0
                    p.setBrush(color if on else QColor(CELL))
                    if not on:
                        p.setPen(QPen(QColor(TXT) if selected else color, 2.2))
                    p.drawEllipse(QPointF(x, cy), 5.5, 5.5)
        x = self.t2x(eng.time())
        p.setPen(QPen(QColor(ORANGE), 2))
        p.drawLine(QPointF(x, 2), QPointF(x, h - 2))
        p.setBrush(QColor(ORANGE))
        p.setPen(Qt.NoPen)
        p.drawPolygon(QPolygonF([QPointF(x - 5, 0), QPointF(x + 5, 0), QPointF(x, 7)]))
        p.end()

    def _paint_graph(self, p, action, y, lh, color):
        seq = self.eng.seq
        v0, length = self._span()
        key = (action, self.eng.revision, self.width(), round(v0, 4), round(length, 4), y, lh)
        lines = self._graphs.get(key)
        if lines is None:
            total = seq.duration
            pts_a, pts_b = [], []
            top, span = y + 4, lh - 8
            for x in range(10, max(11, self.width() - 9), 3):
                t = self.x2t(x)
                if t < 0 or t > total:
                    continue
                if action == seqmod.TRACK_ZOOM:
                    z = seq.zoom_at(t)
                    if z is None:
                        got = seq.sample_keys(t)
                        z = seqmod.fov_to_zoom(got[2]) if got else 1.0
                    frac = math.log(z / seqmod.ZOOM_MIN) / math.log(seqmod.ZOOM_MAX / seqmod.ZOOM_MIN)
                    pts_a.append(QPointF(x, top + span * (1.0 - frac)))
                else:
                    st = seq.air_at(t)
                    hts = st[0] if st else [0.0] * 4
                    pts_a.append(QPointF(x, top + span * (hts[0] + hts[1]) / 2))
                    pts_b.append(QPointF(x, top + span * (hts[2] + hts[3]) / 2))
            if action == seqmod.TRACK_AIR and seq.settings["air"]["sequence"] == "together":
                pts_b = []
            lines = (pts_a, pts_b)
            if len(self._graphs) > 16:
                self._graphs.clear()
            self._graphs[key] = lines
        pts_a, pts_b = lines
        if len(pts_a) > 1:
            fill = QPainterPath(QPointF(pts_a[0].x(), y + lh - 2))
            for pt in pts_a:
                fill.lineTo(pt)
            fill.lineTo(QPointF(pts_a[-1].x(), y + lh - 2))
            fill.closeSubpath()
            shade = QColor(color)
            shade.setAlpha(40)
            p.fillPath(fill, shade)
            p.setPen(QPen(color, 1.6))
            p.drawPolyline(QPolygonF(pts_a))
        if len(pts_b) > 1:
            pen = QPen(color.lighter(130), 1.2)
            pen.setStyle(Qt.DashLine)
            p.setPen(pen)
            p.drawPolyline(QPolygonF(pts_b))

    def _trigger_t(self, i, g):
        d = self._drag
        if d and d[0] == "trig" and d[1] == i and d[3] is not None:
            return d[3]
        return g.t

    def _clip_at(self, pos):
        if not (self.BAR_Y <= pos.y() <= self.BAR_Y + self.BAR_H):
            return None
        t = self.x2t(pos.x())
        for i, start, dur, tin in self._clips():
            if start <= t <= start + dur:
                return i, start, dur, tin
        return None

    def _key_at(self, pos):
        if abs(pos.y() - self.KEY_Y) > 9:
            return None
        eng = self.eng
        shot = eng.shot
        start = eng.seq.shot_start(eng.shot_index)
        for i, key in enumerate(shot.keys):
            kx = self._inside(self.t2x(start + key.t - shot.trim_in), start, shot.duration)
            if shot.in_window(key) and abs(kx - pos.x()) <= 7:
                return i
        return None

    def _inside(self, kx, start, dur):
        lo, hi = self.t2x(start) + 11, self.t2x(start + dur) - 11
        return kx if hi <= lo else max(lo, min(hi, kx))

    def _edge_at(self, pos):
        if not (self.BAR_Y <= pos.y() <= self.BAR_Y + self.BAR_H):
            return None
        for i, start, dur, _ in self._clips():
            if i != self.eng.shot_index:
                continue
            if abs(pos.x() - self.t2x(start)) <= self.EDGE_PX:
                return "left"
            if abs(pos.x() - self.t2x(start + dur)) <= self.EDGE_PX:
                return "right"
        return None

    def _trigger_at(self, pos):
        lane = next((a for a, y, h in self.lane_rows() if y <= pos.y() < y + h), None)
        if lane is None:
            return None
        best = None
        for i, g in enumerate(self.eng.seq.triggers):
            if g.action == lane:
                dx = abs(self.t2x(g.t) - pos.x())
                if dx <= 7 and (best is None or dx < best[1]):
                    best = (i, dx)
        return best[0] if best else None

    def mousePressEvent(self, e):
        pos = e.position()
        eng = self.eng
        if e.button() == Qt.MiddleButton:
            self._drag = ("pan", pos.x(), self._span()[0])
            return
        if e.button() == Qt.RightButton:
            self._context_menu(pos, e.globalPosition().toPoint())
            return
        if e.button() != Qt.LeftButton:
            return
        trig = self._trigger_at(pos)
        if trig is not None:
            eng.trigger_index = trig
            eng.key_index = -1
            self._drag = ("trig", trig, pos.x(), None)
            self.panel.refresh()
            return
        key = self._key_at(pos)
        if key is not None:
            eng.key_index = key
            eng.trigger_index = -1
            shot = eng.shot
            eng.scrub(eng.seq.shot_start(eng.shot_index) + shot.keys[key].t - shot.trim_in)
            self._drag = ("key", key, None)
            self.panel.refresh()
            return
        edge = self._edge_at(pos)
        if edge:
            shot = eng.shot
            self._drag = ("trim", eng.shot_index, edge, pos.x(), shot.trim_in, shot.duration,
                          shot.trim_in, shot.duration)
            return
        clip = self._clip_at(pos)
        if clip:
            i = clip[0]
            if i != eng.shot_index:
                eng.select_shot(i)
            self._drag = ("shot", i, pos.x(), False, i)
            return
        self._drag = ("scrub",)
        eng.scrub(self.x2t(pos.x()))

    def mouseMoveEvent(self, e):
        pos = e.position()
        if not self._drag:
            edge = self._edge_at(pos)
            self.setCursor(Qt.SizeHorCursor if edge else
                           Qt.OpenHandCursor if self._clip_at(pos) and self._key_at(pos) is None
                           else Qt.ArrowCursor)
            return
        d = self._drag
        kind = d[0]
        per_px = self._span()[1] / max(1, self.width() - 20)
        if kind == "scrub":
            self.eng.scrub(self.x2t(pos.x()))
        elif kind == "pan":
            _, x0, v0 = d
            self.view0 = max(0.0, v0 - (pos.x() - x0) * per_px)
            if self.view_len is None:
                self.view_len = self._span()[1]
            self.update()
        elif kind == "key":
            start = self.eng.seq.shot_start(self.eng.shot_index)
            t = self._snap(self.x2t(pos.x()))
            self._drag = ("key", d[1], max(0.0, t - start))
            self.update()
        elif kind == "trim":
            _, i, edge, x0, tin0, dur0, _, _ = d
            dt = (pos.x() - x0) * per_px
            if edge == "right":
                start = self.eng.seq.shot_start(i)
                end = self._snap(start + dur0 + dt)
                tin, dur = tin0, max(seqmod.MIN_SHOT, end - start)
            else:
                tin = max(0.0, tin0 + dt)
                tin = min(tin, tin0 + dur0 - seqmod.MIN_SHOT)
                dur = dur0 - (tin - tin0)
            self._drag = d[:6] + (tin, dur)
            self.update()
        elif kind == "shot":
            _, i, x0, moved, _ = d
            if not moved and abs(pos.x() - x0) < 6:
                return
            self.setCursor(Qt.ClosedHandCursor)
            t = self.x2t(pos.x())
            shots = self.eng.seq.shots
            to, start = 0, 0.0
            for j, s in enumerate(shots):
                if j == i:
                    continue
                if start + s.duration / 2 < t:
                    to += 1
                start += s.duration
            self._drag = ("shot", i, x0, True, to)
            self.update()
        elif kind == "trig":
            t = self._snap(self.x2t(pos.x()))
            self._drag = ("trig", d[1], d[2], max(0.0, min(self.eng.seq.duration, t)))
            self.update()

    def mouseReleaseEvent(self, e):
        d, self._drag = self._drag, None
        self.setCursor(Qt.ArrowCursor)
        if not d:
            return
        eng = self.eng
        if d[0] == "key" and d[2] is not None:
            eng.move_key(d[1], d[2])
        elif d[0] == "trim":
            _, i, _, _, tin0, dur0, tin, dur = d
            if abs(tin - tin0) > 1e-4 or abs(dur - dur0) > 1e-4:
                eng.trim_shot(i, tin, dur)
        elif d[0] == "shot" and d[3]:
            eng.move_shot(d[1], d[4])
        elif d[0] == "trig" and d[3] is not None:
            eng.move_trigger(d[1], d[3])
        self.update()

    def wheelEvent(self, e):
        v0, length = self._span()
        at = self.x2t(e.position().x())
        factor = 0.8 if e.angleDelta().y() > 0 else 1.25
        new_len = max(0.5, min(max(1.0, self.eng.seq.duration) * 3, length * factor))
        self.view_len = new_len
        self.view0 = max(0.0, at - (at - v0) * new_len / length)
        self.update()

    def _context_menu(self, pos, global_pos):
        eng = self.eng
        menu = self.panel._menu()
        trig = self._trigger_at(pos)
        if trig is not None:
            g = eng.seq.triggers[trig]
            eng.trigger_index = trig
            eng.key_index = -1
            menu.addAction(f"Delete this {track_label(g.action)} keyframe",
                           lambda: eng.delete_trigger(trig))
            if g.action == seqmod.TRACK_ZOOM:
                menu.addAction(f"Set to the Zoom slider ({self.panel.zoomSlider.value() / 100:.1f}×)",
                               lambda: eng.set_trigger_value(trig, self.panel.zoomSlider.value() / 100))
            menu.addAction("Move to the playhead", lambda: eng.move_trigger(trig, eng.time()))
        else:
            clip = self._clip_at(pos)
            if clip and clip[0] != eng.shot_index:
                eng.select_shot(clip[0])
            menu.addAction("Split at the playhead", self.panel.split)
            menu.addAction("Duplicate shot", eng.duplicate_shot)
            menu.addAction("Delete shot", self.panel._delete_shot)
            menu.addSeparator()
            menu.addAction("Fit the whole sequence", self.fit_view)
        self.update()
        menu.exec(global_pos)

    def fit_view(self):
        self.view0, self.view_len = 0.0, None
        self.update()


def _fmt_ruler(t):
    if t >= 60:
        return f"{int(t // 60)}:{int(t % 60):02d}"
    return f"{t:g}"


def vista_one_shot(action):
    return action in vista.ONE_SHOT_ACTIONS


class _Stack(QStackedWidget):

    def sizeHint(self):
        w = self.currentWidget()
        return w.sizeHint() if w else super().sizeHint()

    def minimumSizeHint(self):
        w = self.currentWidget()
        return w.minimumSizeHint() if w else super().minimumSizeHint()


class SequencerPanel(QFrame):

    COLS = ["#", "time", "X", "Y", "Z", "yaw", "pitch", "roll", "zoom", "lin"]

    def __init__(self, host, parent=None):
        super().__init__(parent)
        self.host = host
        self.orch = host.orch
        self.eng = host.eng
        self._loading = False
        self._drag_from = None
        self.setStyleSheet(STYLE)
        self.resize(900, 830)
        self._build()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 16, 16)
        p.fillPath(path, QColor(28, 28, 30, 244))
        p.setPen(QPen(QColor(LINE), 1))
        p.drawPath(path)
        p.end()

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton and e.position().y() < 46:
            self._drag_from = e.globalPosition().toPoint() - self.window().pos()

    def mouseMoveEvent(self, e):
        if self._drag_from is not None:
            self.window().move(e.globalPosition().toPoint() - self._drag_from)
            self.host.remember_panel_offset()

    def mouseReleaseEvent(self, _):
        self._drag_from = None

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 12, 16, 12)
        root.setSpacing(8)

        head = QHBoxLayout(); head.setSpacing(10)
        logo = QLabel()
        tile = config.ASSETS_DIR / "icon_tile.png"
        if tile.is_file():
            logo.setPixmap(QPixmap(str(tile)).scaled(26, 26, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        head.addWidget(logo)
        title = QLabel("Sequencer"); title.setObjectName("title")
        head.addWidget(title)
        self.chip = QLabel("CAMERA: GAME")
        head.addWidget(self.chip)
        head.addStretch(1)
        self.takeBtn = QPushButton("Take camera"); self.takeBtn.setObjectName("accent")
        self.takeBtn.setToolTip("Give HorizonLens control of the game camera (be in free roam)")
        self.takeBtn.clicked.connect(self.host.take_camera)
        head.addWidget(self.takeBtn)
        hideBtn = QPushButton("Hide"); hideBtn.setToolTip("Clear the screen for filming (F8 brings the menu back)")
        hideBtn.clicked.connect(self.host.hide_for_shot)
        head.addWidget(hideBtn)
        close = QPushButton("✕"); close.setObjectName("icon"); close.setToolTip("Close the sequencer")
        close.clicked.connect(self.host.close_sequencer)
        head.addWidget(close)
        root.addLayout(head)

        tb = QHBoxLayout(); tb.setSpacing(6)

        def tool(text, tip, fn, obj="tool", checkable=False):
            b = QPushButton(text); b.setObjectName(obj); b.setToolTip(tip)
            b.setCheckable(checkable)
            if not checkable:
                b.clicked.connect(fn)
            tb.addWidget(b)
            return b
        self.flyBtn = tool("✈  Fly  F2", "Fly the camera: arrow keys move, Page Up / Down up / down, hold the "
                           "right mouse button to look, wheel = speed, + / − zoom. Stays on until you "
                           "turn it off.", None, obj="toolseg", checkable=True)
        self.flyBtn.toggled.connect(lambda on: None if self._loading else self.host.set_flying(on))
        self.keyBtn = tool("◆  Key  F9", "Set a key at the playhead from what the camera shows now. "
                           "On an existing key: updates it. Then the playhead moves on.",
                           self.host.add_key, obj="accent")
        self.btnPlay = tool("▶  Play  F6", "Play / pause from the playhead", self.host.play)
        self.btnStop = tool("■  Stop  F7", "Stop and give the camera back to the game", self.host.stop)
        sep = QFrame(); sep.setFixedWidth(1); sep.setStyleSheet(f"background:{LINE};"); tb.addWidget(sep)
        self.btnSplit = tool("✂  Split", "Cut the shot at the playhead in two (the move stays seamless)",
                             self.split)
        tool("↶", "Undo", self.eng.undo)
        tool("↷", "Redo", self.eng.redo)
        self.loopBox = tool("⟲  Loop", "Play the sequence on repeat", None, obj="toolseg", checkable=True)
        self.loopBox.toggled.connect(lambda v: None if self._loading else self.eng.set_loop(v))
        tb.addStretch(1)
        self.timeLab = QLabel("0:00.00 / 0:00.00")
        self.timeLab.setStyleSheet("font: 600 13px 'Consolas';")
        tb.addWidget(self.timeLab)
        self.speed = QPushButton("1×"); self.speed.setToolTip("Playback speed (click to change)")
        self.speed.clicked.connect(self._cycle_speed)
        tb.addWidget(self.speed)
        root.addLayout(tb)

        self.timeline = Timeline(self)
        root.addWidget(self.timeline)
        gl = QHBoxLayout(); gl.setSpacing(8)
        self.guide = QLabel(""); self.guide.setObjectName("guide"); self.guide.setWordWrap(True)
        gl.addWidget(self.guide, 3)
        self.msg = QLabel(""); self.msg.setObjectName("muted"); self.msg.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        gl.addWidget(self.msg, 2)
        root.addLayout(gl)

        an = QHBoxLayout(); an.setSpacing(8)
        an.addWidget(self._sec("TRACKS"))
        self.animAdd = QPushButton("+ Keyframe ▾")
        self.animAdd.setToolTip("Add a keyframe at the playhead: Zoom, Air suspension (drop / raise) "
                                "or a Forzavista action (doors, lights, ...).")
        self.animAdd.clicked.connect(lambda: self._anim_menu(self.animAdd))
        an.addWidget(self.animAdd)
        self.animDel = QPushButton("Delete keyframe")
        self.animDel.clicked.connect(lambda: self.eng.delete_trigger())
        an.addWidget(self.animDel)
        self.animReset = QPushButton("Reset car")
        self.animReset.setToolTip("Put the car back to default (Forzavista's Reset) before a take")
        self.animReset.clicked.connect(self.host.reset_car)
        an.addWidget(self.animReset)
        self.animState = QLabel(""); self.animState.setObjectName("muted")
        an.addWidget(self.animState, 1)
        self.setBtn = QPushButton("⚙  Settings"); self.setBtn.setObjectName("seg")
        self.setBtn.setCheckable(True)
        self.setBtn.setToolTip("Zoom, Air suspension and Forzavista track settings, with their curves")
        self.setBtn.toggled.connect(self._show_settings)
        an.addWidget(self.setBtn)
        root.addLayout(an)

        self.editorPage = QWidget()
        ep = QVBoxLayout(self.editorPage); ep.setContentsMargins(0, 0, 0, 0); ep.setSpacing(8)
        body = QHBoxLayout(); body.setSpacing(12)

        def card():
            f = QFrame(); f.setObjectName("card")
            v = QVBoxLayout(f); v.setContentsMargins(12, 10, 12, 10); v.setSpacing(7)
            return f, v

        shotCard, sv = card()
        top = QHBoxLayout()
        top.addWidget(self._sec("SHOT"))
        self.shotName = QLineEdit(); self.shotName.setPlaceholderText("Shot name")
        self.shotName.editingFinished.connect(lambda: self.eng.set_shot_field("name", self.shotName.text()))
        top.addWidget(self.shotName, 1)
        sv.addLayout(top)
        modes = QHBoxLayout(); modes.setSpacing(6)
        self.modeBtns = {}
        for mode, text, tip in (("world", "Stays in place", "The camera keeps its spot in the world, "
                                 "even if the car drives off"),
                                ("car", "Follows the car", "The camera rides along with the car")):
            b = QPushButton(text); b.setObjectName("seg"); b.setCheckable(True); b.setToolTip(tip)
            b.clicked.connect(lambda _=False, m=mode: self._set_mode(m))
            self.modeBtns[mode] = b
            modes.addWidget(b, 1)
        sv.addLayout(modes)
        grid = QGridLayout(); grid.setHorizontalSpacing(10); grid.setVerticalSpacing(6)
        self.dur = self._spin(0.1, 3600, 2, " s", lambda v: self.eng.set_shot_field("duration", v))
        self.spacing = self._spin(0.1, 60, 2, " s", lambda v: self.eng.set_shot_field("spacing", v))
        self.spacing.setToolTip("How far the playhead moves on after each new key")
        self.easeIn = self._slider(0, 50, lambda v: self.eng.set_shot_field("ease_in", v / 100))
        self.easeOut = self._slider(0, 50, lambda v: self.eng.set_shot_field("ease_out", v / 100))
        self.easeIn.setToolTip("How gently the move starts"); self.easeOut.setToolTip("How gently it stops")
        for r, (text, wdg) in enumerate((("Length", self.dur), ("Key spacing", self.spacing),
                                         ("Ease in", self.easeIn), ("Ease out", self.easeOut))):
            lab = QLabel(text); lab.setObjectName("muted")
            grid.addWidget(lab, r // 2, (r % 2) * 2)
            grid.addWidget(wdg, r // 2, (r % 2) * 2 + 1)
        sv.addLayout(grid)
        self.smooth = QCheckBox("Smooth curved path")
        self.smooth.toggled.connect(lambda v: None if self._loading else self.eng.set_shot_field("smooth", v))
        sv.addWidget(self.smooth)
        sb = QHBoxLayout(); sb.setSpacing(6)
        for text, fn, tip in (("+ Shot", self.eng.add_shot, "A new shot after this one"),
                              ("Duplicate", self.eng.duplicate_shot, "A copy of this shot"),
                              ("Delete", self._delete_shot, "Delete this shot"),
                              ("Even speed", self.eng.retime_constant_speed,
                               "Space the keys so the camera moves at an even speed")):
            b = QPushButton(text); b.setToolTip(tip); b.clicked.connect(fn); sb.addWidget(b)
            if text == "Delete":
                self.delShotBtn = b
        sv.addLayout(sb)
        sv.addStretch(1)
        body.addWidget(shotCard, 1)

        keyCard, kv = card()
        kt = QHBoxLayout()
        kt.addWidget(self._sec("SELECTED KEY"))
        self.keyInfo = QLabel(""); self.keyInfo.setObjectName("muted")
        kt.addWidget(self.keyInfo, 1)
        kv.addLayout(kt)
        zr = QHBoxLayout(); zr.setSpacing(8)
        self.zoomTitle = QLabel("Zoom"); self.zoomTitle.setObjectName("muted"); self.zoomTitle.setFixedWidth(118)
        zr.addWidget(self.zoomTitle)
        self.zoomSlider = QSlider(Qt.Horizontal)
        self.zoomSlider.setRange(int(seqmod.ZOOM_MIN * 100), int(seqmod.ZOOM_MAX * 100))
        self.zoomSlider.setValue(100)
        self.zoomSlider.setToolTip("Like the phone's zoom (0.5x to 3x). While flying: the camera's zoom "
                                   "(also + / - keys). Otherwise: the selected key's (or zoom keyframe's).")
        self.zoomSlider.valueChanged.connect(self._zoom_changed)
        self.zoomSlider.sliderReleased.connect(lambda: self._zoom_changed(self.zoomSlider.value(), True))
        zr.addWidget(self.zoomSlider, 1)
        self.zoomLab = QLabel("1.0×"); self.zoomLab.setFixedWidth(40)
        zr.addWidget(self.zoomLab)
        z1 = QPushButton("1×"); z1.setToolTip("Back to 1x")
        z1.clicked.connect(lambda: self._zoom_changed(100, True))
        zr.addWidget(z1)
        kv.addLayout(zr)
        self.linBox = QCheckBox("Straight line to the next key")
        self.linBox.toggled.connect(lambda v: None if self._loading or self.eng.key_index < 0
                                    else self.eng.set_key_field(self.eng.key_index, "lin", v))
        kv.addWidget(self.linBox)
        kb = QHBoxLayout(); kb.setSpacing(6)
        self.keyBtns = []
        for text, fn, tip in (("◀", lambda: self.host._select_key(-1), "Previous key (F3)"),
                              ("Go to key  F5", self.host.fly_to_key, "Show this key (flying carries on from it)"),
                              ("Set from camera  F11", self.host.update_key,
                               "Replace this key with what the camera shows now"),
                              ("Delete key", lambda: self.eng.delete_key(), "Delete this key"),
                              ("▶", lambda: self.host._select_key(1), "Next key (F4)")):
            b = QPushButton(text); b.setToolTip(tip); b.clicked.connect(fn); kb.addWidget(b)
            self.keyBtns.append(b)
        kv.addLayout(kb)
        kl = QHBoxLayout()
        self.listBtn = QPushButton("Key list ▾"); self.listBtn.setCheckable(True)
        self.listBtn.setToolTip("Show every key of this shot as a table you can edit")
        self.listBtn.toggled.connect(self._show_list)
        kl.addWidget(self.listBtn); kl.addStretch(1)
        kv.addLayout(kl)
        kv.addStretch(1)
        body.addWidget(keyCard, 1)
        ep.addLayout(body)

        self.keysTitle = QLabel("")
        self.table = QTableWidget(0, len(self.COLS))
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.table.setMinimumHeight(110)
        self.table.itemSelectionChanged.connect(self._table_selected)
        self.table.itemChanged.connect(self._table_edited)
        self.table.setVisible(False)
        ep.addWidget(self.table, 1)
        ep.addStretch(0)
        self.bodyStack = _Stack()
        self.bodyStack.addWidget(self.editorPage)
        settings = self._build_settings()
        settings.setMinimumHeight(430)
        self.bodyStack.addWidget(settings)
        root.addWidget(self.bodyStack, 1)

        bot = QHBoxLayout()
        foot = QLabel("F8 menu · F2 fly · F9 key · F6 play · F7 stop · F3/F4 keys · F5 go to key")
        foot.setToolTip("On the timeline: drag a shot's edge to trim it, drag a shot to move it, "
                        "right-click for more")
        foot.setObjectName("muted")
        bot.addWidget(foot, 1)
        for text, fn, tip in (("🎬  Clean play", self.host.clean_play,
                               "Hide everything and play from the start, for the final take"),):
            b = QPushButton(text); b.setToolTip(tip); b.clicked.connect(fn); bot.addWidget(b)
        lib = QPushButton("Library ▾"); lib.clicked.connect(lambda: self._library_menu(lib))
        bot.addWidget(lib)
        self.nameEdit = QLineEdit(); self.nameEdit.setPlaceholderText("Sequence name")
        self.nameEdit.setFixedWidth(170)
        self.nameEdit.editingFinished.connect(self._name_done)
        bot.addWidget(self.nameEdit)
        root.addLayout(bot)

    def _slider(self, lo, hi, on_change):
        s = QSlider(Qt.Horizontal)
        s.setRange(lo, hi)
        s.valueChanged.connect(lambda v: None if self._loading or s.isSliderDown() else on_change(v))
        s.sliderReleased.connect(lambda: on_change(s.value()))
        return s

    def _show_list(self, on):
        self.table.setVisible(on)
        self.listBtn.setText("Key list ▴" if on else "Key list ▾")
        self._fit_window()

    def _show_settings(self, on):
        self.bodyStack.setCurrentIndex(1 if on else 0)
        self._fit_window()

    def _fit_window(self):
        self.bodyStack.updateGeometry()
        self.layout().activate()
        h = max(420, self.sizeHint().height())
        if abs(self.height() - h) > 2:
            self.resize(self.width(), h)
            if self.window() is not self:
                self.window().resize(self.width(), h)

    def _sec(self, text):
        lab = QLabel(text); lab.setObjectName("sec")
        return lab

    def _build_settings(self):
        self._set_w = {}
        scroll = QScrollArea(); scroll.setWidgetResizable(True); scroll.setFrameShape(QFrame.NoFrame)
        scroll.setStyleSheet("QScrollArea{background:transparent;}")
        inner = QWidget(); inner.setStyleSheet("background:transparent;")
        lay = QVBoxLayout(inner); lay.setContentsMargins(0, 0, 8, 0); lay.setSpacing(10)

        def card(title, module, color, hint):
            box = QFrame(); box.setObjectName("setcard")
            box.setStyleSheet(f"QFrame#setcard{{background:{CELL};border-radius:12px;"
                              f"border-left:4px solid {color};}}")
            v = QVBoxLayout(box); v.setContentsMargins(14, 10, 14, 12); v.setSpacing(7)
            head = QHBoxLayout()
            t = QLabel(title); t.setStyleSheet(f"font:600 14px {FONT};color:{color};background:transparent;")
            head.addWidget(t); head.addStretch(1)
            if module:
                on = QCheckBox("On"); on.setStyleSheet("background:transparent;")
                on.toggled.connect(lambda v_, m=module: None if self._loading
                                   else self.eng.set_setting(m, "enabled", v_))
                self._set_w[(module, "enabled")] = on
                head.addWidget(on)
                rst = QPushButton("Defaults")
                rst.setToolTip("Put this track's settings back to the defaults")
                rst.clicked.connect(lambda _=False, m=module: self.eng.reset_settings(m))
                head.addWidget(rst)
            v.addLayout(head)
            h = QLabel(hint); h.setWordWrap(True)
            h.setStyleSheet(f"color:{MUTED};font:11px {FONT};background:transparent;")
            v.addWidget(h)
            lay.addWidget(box)
            return v

        def slider(v, module, key, text, lo, hi, scale, fmt, tip=""):
            row = QHBoxLayout()
            lab = QLabel(text); lab.setFixedWidth(150); lab.setStyleSheet("background:transparent;")
            sl = QSlider(Qt.Horizontal); sl.setRange(int(round(lo * scale)), int(round(hi * scale)))
            sl.setToolTip(tip)
            val = QLabel(""); val.setFixedWidth(64); val.setStyleSheet("background:transparent;")

            def changed(raw, release=False):
                val.setText(fmt(raw / scale))
                if self._loading or (sl.isSliderDown() and not release):
                    return
                self.eng.set_setting(module, key, raw / scale)
            sl.valueChanged.connect(changed)
            sl.sliderReleased.connect(lambda: changed(sl.value(), True))
            row.addWidget(lab); row.addWidget(sl, 1); row.addWidget(val)
            v.addLayout(row)
            self._set_w[(module, key)] = (sl, val, scale, fmt)
            return sl

        def curve(v, module, key, color, top, bottom, title):
            row = QHBoxLayout()
            lab = QLabel(title); lab.setStyleSheet("background:transparent;")
            row.addWidget(lab); row.addStretch(1)
            for name, label in curves.PRESET_LABELS.items():
                b = QPushButton(label)
                b.clicked.connect(lambda _=False, n=name: self.eng.set_setting(module, key, curves.PRESETS[n]))
                row.addWidget(b)
                self._set_w.setdefault((module, key, "presets"), []).append(b)
            v.addLayout(row)
            ed = CurveEditor(color, top=top, bottom=bottom)
            ed.committed.connect(lambda vals: self.eng.set_setting(module, key, vals))
            v.addWidget(ed)
            self._set_w[(module, key)] = ed
            return ed

        z = card("Zoom track", "zoom", ZOOM_COLOR,
                 "Each Zoom keyframe sets a zoom (the Zoom slider's value when you add it; select "
                 "one and move the slider to change it). The camera eases from the zoom before it "
                 "to the new one over the transition time, shaped by the curve. Before the first "
                 "keyframe the camera keys' own zoom is used.")
        slider(z, "zoom", "transition", "Transition time", 0, 10, 10, lambda v: f"{v:.1f} s",
               "0 = cut straight to the new zoom")
        curve(z, "zoom", "curve", ZOOM_COLOR, "New zoom", "Old zoom", "Zoom curve")

        a = card("Air suspension track", "air", AIR_COLOR,
                 "The 1st Air keyframe drops the car, the 2nd raises it, the 3rd drops it again ... "
                 "Each move takes the transition time, shaped by the height curve, axles in the "
                 "chosen order. Before the first keyframe the car sits at its raised height. "
                 "Don't run Neptune's air ride at the same time.")
        slider(a, "air", "drop", "Drop (how low)", 0, 90, 1, lambda v: f"{v:.0f} %",
               "Share of each wheel's clearance the drop takes away")
        slider(a, "air", "front", "Front raised height", -90, 50, 1, lambda v: f"{v:+.0f} %",
               "Raised height of the front axle against stock")
        slider(a, "air", "rear", "Rear raised height", -90, 50, 1, lambda v: f"{v:+.0f} %",
               "Raised height of the rear axle against stock")
        slider(a, "air", "floor", "Lowest point", 0, 90, 1, lambda v: f"{v:.0f} %",
               "No wheel ever goes below this share of its clearance")
        slider(a, "air", "transition", "Transition time", 0, 8, 10, lambda v: f"{v:.1f} s",
               "How long a drop or a raise takes (0 = instant)")
        seq_row = QHBoxLayout()
        sl = QLabel("Axle order"); sl.setFixedWidth(150); sl.setStyleSheet("background:transparent;")
        seq_row.addWidget(sl)
        self._seqBtns = {}
        for val, text in (("together", "Together"), ("front", "Front first"), ("rear", "Rear first")):
            b = QPushButton(text); b.setObjectName("seg"); b.setCheckable(True)
            b.clicked.connect(lambda _=False, v_=val: self.eng.set_setting("air", "sequence", v_))
            seq_row.addWidget(b, 1)
            self._seqBtns[val] = b
        a.addLayout(seq_row)
        curve(a, "air", "curve", AIR_COLOR, "Lowered", "Raised", "Height curve")
        cam = QCheckBox("Camber control: tilt the wheels as the car drops")
        cam.setStyleSheet("background:transparent;")
        cam.toggled.connect(lambda v_: None if self._loading else self.eng.set_setting("air", "camber", v_))
        self._set_w[("air", "camber")] = cam
        a.addWidget(cam)
        slider(a, "air", "camber_front", "Front camber (dropped)", -20, 20, 10, lambda v: f"{v:+.1f}°",
               "Negative = top of the wheel in")
        slider(a, "air", "camber_rear", "Rear camber (dropped)", -20, 20, 10, lambda v: f"{v:+.1f}°")
        curve(a, "air", "camber_curve", CAMBER_COLOR, "Air camber", "Stock", "Camber curve")
        back = QCheckBox("Back to stock when the sequencer stops (off: the car stays as the sequence left it)")
        back.setStyleSheet("background:transparent;")
        back.toggled.connect(lambda v_: None if self._loading
                             else self.eng.set_setting("air", "return_on_stop", v_))
        self._set_w[("air", "return_on_stop")] = back
        a.addWidget(back)

        f = card("Forzavista tracks", "animations", ORANGE,
                 "Doors, hood, lights and more at the moments you pick (Forzavista must be running "
                 "next to Forza). The 1st keyframe of an action turns it on, the 2nd off. Scrub "
                 "before a keyframe and the car goes back to default.")
        row = QHBoxLayout()
        for text, fn in (("Set up key bindings for me", self.host.setup_vista),
                         ("Reset car", self.host.reset_car)):
            b = QPushButton(text); b.clicked.connect(fn); row.addWidget(b)
        row.addStretch(1)
        f.addLayout(row)
        rcp = QCheckBox("Reset car (Forzavista) before Clean play, so every take starts from stock")
        rcp.setStyleSheet("background:transparent;")
        rcp.toggled.connect(lambda v_: None if self._loading
                            else self.eng.set_setting("animations", "reset_on_clean_play", v_))
        self._set_w[("animations", "reset_on_clean_play")] = rcp
        f.addWidget(rcp)
        note = QLabel("Settings are saved with this sequence (and in the Library).")
        note.setStyleSheet(f"color:{MUTED};background:transparent;")
        lay.addWidget(note)
        lay.addStretch(1)
        scroll.setWidget(inner)
        return scroll

    def _sync_settings(self):
        cfg = self.eng.seq.settings
        for (module, key, *extra), w in self._set_w.items():
            if extra:
                continue
            value = cfg[module][key]
            if isinstance(w, tuple):
                sl, val, scale, fmt = w
                if not sl.isSliderDown():
                    sl.setValue(int(round(value * scale)))
                val.setText(fmt(value))
            elif isinstance(w, CurveEditor):
                w.set_values(value)
            else:
                w.setChecked(bool(value))
        for val, b in self._seqBtns.items():
            b.setChecked(cfg["air"]["sequence"] == val)
        for (module, key, *extra), w in self._set_w.items():
            if key == "enabled":
                continue
            on = cfg[module]["enabled"]
            if module == "air" and (key.startswith("camber_") or key == "camber_curve"):
                on = on and cfg["air"]["camber"]
            for widget in (w if extra else [w[0] if isinstance(w, tuple) else w]):
                widget.setEnabled(on)
        for b in self._seqBtns.values():
            b.setEnabled(cfg["air"]["enabled"])

    def _spin(self, lo, hi, decimals, suffix, on_change):
        s = QDoubleSpinBox()
        s.setRange(lo, hi); s.setDecimals(decimals); s.setSuffix(suffix)
        s.setKeyboardTracking(False)
        s.setMinimumHeight(26)
        s.valueChanged.connect(lambda v: None if self._loading else on_change(v))
        return s

    SPEEDS = (0.25, 0.5, 0.75, 1.0, 1.5, 2.0)

    def _cycle_speed(self):
        i = min(range(len(self.SPEEDS)), key=lambda j: abs(self.SPEEDS[j] - self.eng.speed))
        self.eng.set_speed(self.SPEEDS[(i + 1) % len(self.SPEEDS)])
        self.speed.setText(f"{self.eng.speed:g}×")

    def say(self, text, bad=False):
        self.msg.setText(text)
        self.msg.setStyleSheet(f"color:{BAD if bad else MUTED};")

    def split(self):
        self.eng.split_shot()

    def _zoom_changed(self, value, commit=False):
        z = value / 100.0
        self.zoomLab.setText(f"{z:.1f}×")
        if self._loading:
            return
        self.host.set_zoom(z, commit=commit or not self.zoomSlider.isSliderDown())

    def _anim_menu(self, anchor):
        menu = self._menu()
        cfg = self.eng.seq.settings
        z = self.zoomSlider.value() / 100.0
        if cfg["zoom"]["enabled"]:
            menu.addAction(action_icon(seqmod.TRACK_ZOOM), f"Zoom keyframe here ({z:.1f}×, the Zoom slider)",
                           lambda: self.eng.add_trigger(seqmod.TRACK_ZOOM, value=z))
        else:
            menu.addAction("Zoom track is off (Settings)").setEnabled(False)
        if cfg["air"]["enabled"]:
            t = self.eng.time()
            before = sum(1 for g in self.eng.seq.track(seqmod.TRACK_AIR) if g.t <= t + 1e-9)
            what = "drop the car" if before % 2 == 0 else "raise the car"
            menu.addAction(action_icon(seqmod.TRACK_AIR), f"Air suspension keyframe here ({what})",
                           lambda: self.eng.add_trigger(seqmod.TRACK_AIR))
        else:
            menu.addAction("Air suspension track is off (Settings)").setEnabled(False)
        menu.addSeparator()
        if not cfg["animations"]["enabled"]:
            menu.addAction("Forzavista tracks are off (Settings)").setEnabled(False)
            menu.exec(anchor.mapToGlobal(QPoint(0, anchor.height())))
            return
        binds = vista.load_bindings_cached()
        groups = (("panels", "Doors & panels"), ("windows", "Windows"), ("lights", "Lights"),
                  ("car", "Car"))
        for group, title in groups:
            sub = menu.addMenu(title)
            for action, label, g in vista.ACTIONS:
                if g != group:
                    continue
                bound = bool(binds.get(action) and vista.parse_gesture(binds[action]))
                text = label + ("" if bound else "   (no key yet)")
                if action in vista.ONE_SHOT_ACTIONS:
                    text += "   · fires once"
                sub.addAction(action_icon(action), text,
                              lambda a=action: self.eng.add_trigger(a))
        menu.addSeparator()
        menu.addAction("Set up Forzavista key bindings for me…", self.host.setup_vista)
        menu.exec(anchor.mapToGlobal(QPoint(0, anchor.height())))

    def _menu(self):
        return MenuBuilder(self.host)

    def _confirm(self, anchor, text, fn):
        menu = self._menu()
        menu.addAction(text, fn)
        menu.addAction("Cancel", self.refresh)
        menu.exec(anchor.mapToGlobal(QPoint(0, anchor.height())))

    def _delete_shot(self):
        if not self.eng.shot.keys:
            self.eng.delete_shot()
            return
        self._confirm(self.delShotBtn, f"Delete “{self.eng.shot.name}” and its "
                      f"{len(self.eng.shot.keys)} keys", self.eng.delete_shot)

    def _set_mode(self, mode):
        if mode != self.eng.shot.mode:
            self.eng.set_shot_field("mode", mode)
            self.say("Shot stays in place in the world" if mode == "world"
                     else "Shot follows the car")
        for m, b in self.modeBtns.items():
            b.setChecked(m == self.eng.shot.mode)

    def _library_menu(self, anchor):
        menu = self._menu()
        menu.addAction("New sequence", lambda: (self.eng.load(seqmod.Sequence()), self.say("New sequence")))
        menu.addAction("Save", self._save)
        menu.addAction("Save as…", self._save_as)
        items = seqmod.list_named()
        if items:
            menu.addSeparator()
            load = menu.addMenu("Open")
            for name, path in items[:30]:
                load.addAction(name, lambda p=path: self._open(p))
            delete = menu.addMenu("Delete")
            for name, path in items[:30]:
                delete.addAction(f"Delete “{name}”", lambda p=path, n=name: self._delete_saved(p, n),
                                 confirm=f"Click again: delete “{name}” for good")
        menu.exec(anchor.mapToGlobal(QPoint(0, anchor.height())))

    def _controls_menu(self, anchor):
        menu = self._menu()
        for _, _, label in HOTKEYS:
            menu.addAction(label).setEnabled(False)
        menu.addSeparator()
        for text in ("Fly camera: arrow keys move, Page Up / Page Down up / down",
                     "Hold right mouse and move to look around",
                     "Mouse wheel: flying speed · Shift fast · Ctrl slow"):
            menu.addAction(text).setEnabled(False)
        menu.exec(anchor.mapToGlobal(QPoint(0, anchor.height())))

    def _save(self):
        seqmod.save_named(self.eng.seq)
        self.say(f"Saved “{self.eng.seq.name}”")

    def _save_as(self):
        self._save_as_pending = True
        self.host._begin_edit(self.nameEdit)
        self.say("Type a name for the copy, then press Enter")

    def _name_done(self):
        self.eng.set_name(self.nameEdit.text())
        if getattr(self, "_save_as_pending", False):
            self._save_as_pending = False
            self._save()

    def _open(self, path):
        try:
            self.eng.load(seqmod.load_path(path))
            self.say(f"Opened “{self.eng.seq.name}”")
        except (OSError, ValueError) as e:
            self.say(f"Couldn't open it: {e}", bad=True)

    def _delete_saved(self, path, name):
        seqmod.delete_named(path)
        self.say(f"Deleted “{name}”")

    def _table_selected(self):
        if self._loading:
            return
        rows = self.table.selectionModel().selectedRows()
        if rows:
            self.eng.key_index = rows[0].row()
            self.eng.trigger_index = -1
            self.timeline.update()

    def _table_edited(self, item):
        if self._loading:
            return
        row, name = item.row(), self.COLS[item.column()]
        if name == "#":
            return
        if name == "lin":
            self.eng.set_key_field(row, "lin", item.checkState() == Qt.Checked)
            return
        try:
            value = float(item.text())
        except ValueError:
            self.refresh()
            return
        if name == "time":
            self.eng.move_key(row, value)
        elif name == "zoom":
            self.eng.set_key_field(row, "fov", seqmod.zoom_to_fov(value))
        else:
            self.eng.set_key_field(row, name.lower(), value)

    def _guide_text(self):
        eng, host = self.eng, self.host
        linked = getattr(self.orch, "link", None) is not None
        nkeys = sum(len(s.keys) for s in eng.seq.shots)
        if host.taking:
            return "Taking the camera… stay in free roam"
        if not linked:
            return "① Get into free roam, then press  Take camera"
        if eng.state == "playing":
            return "Playing…  F6 pauses · F7 stops and gives the camera back"
        if nkeys == 0:
            return ("③ Move to where the shot starts, then press  F9  to set the first key"
                    if host.flying else "② Press  F2  (Fly) and move the camera to where the shot starts")
        if nkeys == 1:
            return ("④ Fly to the next spot and press  F9  (the playhead moved on for it)"
                    if host.flying else "④ Press  F2  to fly to the next spot, then  F9")
        if host.flying:
            return "F9 adds a key at the playhead (or updates the key there) · F6 plays · F2 stops flying"
        return "⑤ Press  F6  to play · click a ◆ key to go back to it, then  F9  updates it"

    def update_live(self):
        eng = self.eng
        stamp = f"{_fmt_time(eng.time())} / {_fmt_time(eng.seq.duration)}"
        if stamp != self.timeLab.text():
            self.timeLab.setText(stamp)
            self.timeline.update()
        state = eng.state
        linked = getattr(self.orch, "link", None) is not None
        text, color = {"playing": ("PLAYING", GOOD), "paused": ("PREVIEW", ORANGE),
                       "flying": ("FLYING", "#64d2ff")}.get(state, ("CAMERA: GAME", MUTED))
        if not linked:
            text, color = "NO CAMERA CONTROL", BAD
        look = (text, color, state == "playing", linked, self.host.taking)
        if look != getattr(self, "_look", None):
            self._look = look
            self.chip.setText(text)
            self.chip.setStyleSheet(f"color:{color};border:1px solid {color};border-radius:9px;"
                                    "padding:2px 10px;font:700 11px;")
            self.btnPlay.setText("❚❚  Pause  F6" if state == "playing" else "▶  Play  F6")
            self.takeBtn.setVisible(not linked)
            self.takeBtn.setEnabled(not self.host.taking)
            self.takeBtn.setText("Taking camera…" if self.host.taking else "Take camera")
        if self.flyBtn.isChecked() != (state == "flying"):
            self._loading = True
            self.flyBtn.setChecked(state == "flying")
            self._loading = False
        guide = self._guide_text()
        if guide != self.guide.text():
            self.guide.setText(guide)
        g = eng.selected_trigger()
        k = eng.key_index
        if self.host.flying:
            ztitle = "Zoom (fly camera)"
        elif g is not None and g.action == seqmod.TRACK_ZOOM:
            ztitle = "Zoom (zoom keyframe)"
        elif 0 <= k < len(eng.shot.keys):
            ztitle = f"Zoom (key {k + 1})"
        else:
            ztitle = "Zoom (next fly)"
        if ztitle != self.zoomTitle.text():
            self.zoomTitle.setText(ztitle)
        if not self.zoomSlider.isSliderDown():
            z = self.host.shown_zoom()
            if z is not None and int(round(z * 100)) != self.zoomSlider.value():
                self._loading = True
                self.zoomSlider.setValue(int(round(z * 100)))
                self.zoomLab.setText(f"{z:.1f}×")
                self._loading = False
        anim = self.host.animation_status()
        if anim != getattr(self, "_anim_shown", None):
            self._anim_shown = anim
            text, bad = anim
            self.animState.setText(text)
            self.animState.setStyleSheet(f"color:{BAD if bad else MUTED};")
        has_trigger = 0 <= eng.trigger_index < len(eng.seq.triggers)
        if self.animDel.isEnabled() != has_trigger:
            self.animDel.setEnabled(has_trigger)
        if eng.message and eng.message != getattr(self, "_shown", ""):
            self._shown = eng.message
            self.say(eng.message, bad=any(w in eng.message for w in ("off", "Can't", "didn't")))

    def refresh(self):
        eng = self.eng
        self._loading = True
        try:
            seq, shot = eng.seq, eng.shot
            if not self.nameEdit.hasFocus():
                self.nameEdit.setText(seq.name)
            self.loopBox.setChecked(seq.loop)
            if not self.shotName.hasFocus():
                self.shotName.setText(shot.name)
            for mode, b in self.modeBtns.items():
                b.setChecked(mode == shot.mode)
            self.dur.setValue(shot.duration)
            self.easeIn.setValue(int(round(shot.ease_in * 100)))
            self.easeOut.setValue(int(round(shot.ease_out * 100)))
            self.spacing.setValue(shot.spacing)
            self.smooth.setChecked(shot.smooth)
            k = eng.key_index
            has_key = 0 <= k < len(shot.keys)
            if has_key:
                key = shot.keys[k]
                self.keyInfo.setText(f"key {k + 1} of {len(shot.keys)} · {key.t - shot.trim_in:.2f} s"
                                     + ("" if shot.in_window(key) else " · trimmed off"))
                self.linBox.setChecked(key.lin)
            else:
                self.keyInfo.setText(f"{len(shot.keys)} key{'s' if len(shot.keys) != 1 else ''} in this shot"
                                     " · click a ◆ on the timeline")
                self.linBox.setChecked(False)
            self.linBox.setEnabled(has_key)
            for b in self.keyBtns[1:4]:
                b.setEnabled(has_key)
            self.table.setRowCount(len(shot.keys))

            def num(v, d):
                text = f"{v:.{d}f}"
                return text[1:] if text.startswith("-") and float(text) == 0 else text
            for r, key in enumerate(shot.keys):
                yaw, pitch, roll = key.ypr
                vals = [str(r + 1), f"{key.t - shot.trim_in:.3f}", num(key.pos[0], 2), num(key.pos[1], 2),
                        num(key.pos[2], 2), num(yaw, 1), num(pitch, 1), num(roll, 1),
                        f"{seqmod.fov_to_zoom(key.fov):.2f}"]
                trimmed = not shot.in_window(key)
                for c, v in enumerate(vals):
                    item = QTableWidgetItem(v)
                    item.setTextAlignment(Qt.AlignCenter)
                    if c == 0:
                        item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                    if trimmed:
                        item.setForeground(QColor("#636366"))
                        item.setToolTip("Trimmed off: outside this shot's window (drag the shot's "
                                        "edge to bring it back)")
                    self.table.setItem(r, c, item)
                lin = QTableWidgetItem()
                lin.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable | Qt.ItemIsSelectable)
                lin.setCheckState(Qt.Checked if key.lin else Qt.Unchecked)
                self.table.setItem(r, len(self.COLS) - 1, lin)
            if has_key:
                self.table.selectRow(k)
            else:
                self.table.clearSelection()
            self._sync_settings()
        finally:
            self._loading = False
        self.timeline._fit_height()
        self.timeline.update()
        self._fit_window()
        self.update_live()


_u32.SetWindowsHookExW.restype = wintypes.HHOOK
_u32.SetWindowsHookExW.argtypes = [ctypes.c_int, ctypes.c_void_p, wintypes.HINSTANCE, wintypes.DWORD]
_u32.CallNextHookEx.restype = ctypes.c_ssize_t
_u32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
_u32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
_u32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
_u32.GetMessageW.restype = wintypes.BOOL
_u32.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT,
                              wintypes.UINT, wintypes.UINT]
_u32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
_u32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
_u32.GetKeyState.argtypes = [ctypes.c_int]
_u32.GetKeyState.restype = ctypes.c_short
_u32.GetKeyboardLayout.argtypes = [wintypes.DWORD]
_u32.GetKeyboardLayout.restype = ctypes.c_void_p
_u32.ToUnicodeEx.argtypes = [wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_ubyte),
                             wintypes.LPWSTR, ctypes.c_int, wintypes.UINT, ctypes.c_void_p]
_k32.GetModuleHandleW.restype = wintypes.HMODULE
_k32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
WS_EX_NOACTIVATE = 0x08000000


class GameWindow(QWidget):

    def __init__(self, click_through=False):
        flags = (Qt.Window | Qt.FramelessWindowHint | Qt.NoDropShadowWindowHint
                 | Qt.WindowDoesNotAcceptFocus)
        if click_through:
            flags |= Qt.WindowTransparentForInput
        super().__init__(None, flags)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self._owner = None
        self._topmost = None
        self._no_activate()

    def _no_activate(self):
        hwnd = int(self.winId())
        style = _u32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
        want = style | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW
        if want != style:
            _u32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, want)

    def set_topmost(self, on):
        if on == self._topmost:
            return
        self._topmost = on
        _u32.SetWindowPos(int(self.winId()), wintypes.HWND(-1 if on else -2), 0, 0, 0, 0,
                          0x0001 | 0x0002 | 0x0010)

    def own(self, game_hwnd):
        hwnd = int(self.winId())
        if self._owner != game_hwnd or _u32.GetWindowLongPtrW(hwnd, GWLP_HWNDPARENT) != game_hwnd:
            _u32.SetWindowLongPtrW(hwnd, GWLP_HWNDPARENT, game_hwnd)
            self._owner = game_hwnd
            self._topmost = None
        self._no_activate()

    def contains_physical(self, x, y):
        if not self.isVisible():
            return False
        r = wintypes.RECT()
        return bool(_u32.GetWindowRect(int(self.winId()), ctypes.byref(r))) \
            and r.left <= x < r.right and r.top <= y < r.bottom


class _MenuItem:
    def __init__(self, kind, text="", fn=None, icon=None, indent=0, confirm=None):
        self.kind, self.text, self.fn, self.icon = kind, text, fn, icon
        self.indent, self.confirm, self.enabled = indent, confirm, True

    def setEnabled(self, on):
        self.enabled = bool(on)
        return self


class MenuBuilder:

    def __init__(self, host, items=None, indent=0):
        self.host = host
        self.items = [] if items is None else items
        self.indent = indent

    def addAction(self, *args, confirm=None):
        icon = None
        if args and not isinstance(args[0], str):
            icon, args = args[0], args[1:]
        item = _MenuItem("action", args[0], args[1] if len(args) > 1 else None, icon,
                         self.indent, confirm)
        self.items.append(item)
        return item

    def addSeparator(self):
        self.items.append(_MenuItem("sep"))

    def addMenu(self, title):
        self.items.append(_MenuItem("header", title, indent=self.indent))
        return MenuBuilder(self.host, self.items, self.indent + 1)

    def exec(self, global_pos):
        self.host.show_menu(self.items, global_pos)


class OverlayMenu(GameWindow):

    MAX_H = 460

    def __init__(self, host):
        super().__init__()
        self.host = host
        self.setStyleSheet(STYLE + f"""
            QPushButton#mi {{ background:transparent; border:0; border-radius:7px;
                              text-align:left; padding:6px 14px 6px 10px; }}
            QPushButton#mi:hover {{ background:{ORANGE}; color:#000; }}
            QPushButton#mi:disabled {{ color:{MUTED}; }}
            QPushButton#arm {{ background:{BAD}; color:#fff; border:0; border-radius:7px;
                               text-align:left; padding:6px 14px 6px 10px; }}
            QScrollArea, QScrollArea > QWidget > QWidget {{ background:transparent; }}""")
        self.scroll = QScrollArea(self)
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.inner = None

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 10, 10)
        p.fillPath(path, QColor(44, 44, 46, 250))
        p.setPen(QPen(QColor(LINE), 1))
        p.drawPath(path)
        p.end()

    def populate(self, items):
        self.inner = QWidget()
        lay = QVBoxLayout(self.inner)
        lay.setContentsMargins(5, 5, 5, 5)
        lay.setSpacing(1)
        self.buttons = []
        for it in items:
            if it.kind == "sep":
                line = QFrame(); line.setFixedHeight(1)
                line.setStyleSheet(f"background:{LINE};margin:3px 6px;")
                lay.addWidget(line)
            elif it.kind == "header":
                lab = QLabel(it.text.upper())
                lab.setObjectName("sec")
                lab.setStyleSheet(f"padding:7px 10px 2px {10 + 14 * it.indent}px;background:transparent;")
                lay.addWidget(lab)
            else:
                b = QPushButton(it.text)
                b.setObjectName("mi")
                b.setCursor(Qt.PointingHandCursor)
                if it.icon is not None:
                    b.setIcon(it.icon)
                if it.indent:
                    b.setStyleSheet(f"padding-left:{10 + 14 * it.indent}px;")
                b.setEnabled(it.enabled and (it.fn is not None or it.text == "Cancel"
                                             or it.confirm is not None))
                b.clicked.connect(lambda _=False, it=it, b=b: self._chosen(it, b))
                lay.addWidget(b)
                self.buttons.append(b)
        self.scroll.setWidget(self.inner)
        hint = self.inner.sizeHint()
        w = max(200, min(520, hint.width() + 24))
        h = min(self.MAX_H, hint.height() + 4)
        self.resize(w, h)
        self.scroll.setGeometry(2, 2, w - 4, h - 4)

    def _chosen(self, it, button):
        if it.confirm and button.objectName() != "arm":
            button.setObjectName("arm")
            button.setText(it.confirm)
            button.setStyleSheet("")
            button.style().unpolish(button); button.style().polish(button)
            return
        self.host.close_menu(chosen=True)
        if it.fn is not None:
            QTimer.singleShot(0, it.fn)


class SoftCursor(GameWindow):

    def __init__(self):
        super().__init__(click_through=True)
        self.resize(22, 26)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        arrow = QPolygonF([QPointF(1.5, 1.5), QPointF(1.5, 19), QPointF(6, 14.6), QPointF(9.4, 22.5),
                           QPointF(12.6, 21.1), QPointF(9.3, 13.4), QPointF(15.5, 13.4)])
        p.setPen(QPen(QColor(0, 0, 0), 1.6))
        p.setBrush(QColor(255, 255, 255))
        p.drawPolygon(arrow)
        p.end()


class _CURSORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD),
                ("hCursor", wintypes.HANDLE), ("ptScreenPos", wintypes.POINT)]


_u32.GetCursorInfo.argtypes = [ctypes.POINTER(_CURSORINFO)]


def cursor_info():
    ci = _CURSORINFO()
    ci.cbSize = ctypes.sizeof(ci)
    if not _u32.GetCursorInfo(ctypes.byref(ci)):
        return None
    return bool(ci.flags & 1), ci.ptScreenPos.x, ci.ptScreenPos.y


class PanelWindow(GameWindow):
    def __init__(self, host):
        super().__init__()
        self.setWindowTitle("HorizonLens Sequencer")
        self.panel = SequencerPanel(host, self)
        self.resize(self.panel.size())

    def resizeEvent(self, _):
        self.panel.resize(self.size())


class HudWindow(GameWindow):

    def __init__(self, host):
        super().__init__(click_through=True)
        self.host = host
        self.label = QLabel(self)
        self.label.setStyleSheet(f"color:{TXT};font: 12px {FONT};background:transparent;")
        self._text = None
        self.resize(560, 30)

    def refresh(self):
        eng = self.host.eng
        state = {"playing": "▶", "paused": "❚❚", "flying": "✈"}.get(eng.state, "■")
        dot = {"playing": GOOD, "paused": ORANGE, "flying": "#64d2ff"}.get(eng.state, MUTED)
        if self.host.editing:
            extra = "typing: Enter confirms · Esc cancels"
        elif eng.state == "flying":
            extra = (f"✈ {self.host.fly.speed:.1f} m/s · {self.host.fly.zoom:.1f}× · arrows · PgUp/PgDn"
                     f" · + / − zoom · right-drag look · wheel speed")
        else:
            extra = "F8 menu · F2 fly · F9 key · F6 play · F7 stop"
        text = (f"<span style='color:{dot}'>●</span>  <b>{eng.shot.name}</b>  ·  keys "
                f"{len(eng.shot.keys)}/{seqmod.MAX_KEYS}  ·  {state} {_fmt_time(eng.time())}"
                f"  <span style='color:{MUTED}'>·  {extra}</span>")
        if text == self._text:
            return
        self._text = text
        self.label.setText(text)
        self.label.adjustSize()
        w = self.label.width() + 28
        if w != self.width():
            self.resize(w, 30)
        self.label.move(14, (30 - self.label.height()) // 2)
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 15, 15)
        p.fillPath(path, QColor(28, 28, 30, 225))
        p.setPen(QPen(QColor(LINE), 1))
        p.drawPath(path)
        p.end()


class CrossWindow(GameWindow):

    def __init__(self):
        super().__init__(click_through=True)
        self.resize(44, 44)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        c = QPointF(22, 22)
        for color, width in ((QColor(0, 0, 0, 120), 3.2), (QColor(255, 255, 255, 220), 1.6)):
            p.setPen(QPen(color, width))
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                p.drawLine(c + QPointF(dx * 6, dy * 6), c + QPointF(dx * 17, dy * 17))
        p.end()


class _KBD(ctypes.Structure):
    _fields_ = [("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD), ("flags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class _MSE(ctypes.Structure):
    _fields_ = [("pt", wintypes.POINT), ("mouseData", wintypes.DWORD), ("flags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


_HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
FLY_VKS = {0x26: "FWD", 0x28: "BACK", 0x25: "LEFT", 0x27: "RIGHT", 0x21: "UP", 0x22: "DOWN",
           0x10: "SHIFT", 0xA0: "SHIFT", 0xA1: "SHIFT", 0x11: "CTRL", 0xA2: "CTRL", 0xA3: "CTRL",
           0xBB: "ZIN", 0x6B: "ZIN", 0xBD: "ZOUT", 0x6D: "ZOUT"}
WM_REHOOK = 0x8001
_QT_KEYS = {0x08: Qt.Key_Backspace, 0x2E: Qt.Key_Delete, 0x25: Qt.Key_Left, 0x27: Qt.Key_Right,
            0x26: Qt.Key_Up, 0x28: Qt.Key_Down, 0x24: Qt.Key_Home, 0x23: Qt.Key_End,
            0x20: Qt.Key_Space}
_PASS_VKS = {0x12, 0xA4, 0xA5, 0x5B, 0x5C, 0x2C}
_MOD_VKS = {0x10, 0x11, 0xA0, 0xA1, 0xA2, 0xA3, 0x14}


class InputHook(QObject):

    hot = Signal(str)
    typed = Signal(int, str, bool, bool)
    clicked = Signal(int, int)
    pressed = Signal(int, int)
    escape = Signal()

    def __init__(self, host):
        super().__init__()
        self.host = host
        self.game_pid = None
        self.flying = False
        self.editing = False
        self.menu_open = False
        self._lock = threading.Lock()
        self._down = set()
        self._look = [0.0, 0.0]
        self._wheel = 0
        self._rb = None
        self._hot_held = set()
        self._thread = None
        self._tid = 0
        self.rehooks = 0
        self._kb_proc = _HOOKPROC(self._on_key)
        self._ms_proc = _HOOKPROC(self._on_mouse)
        self.hot.connect(host.hotkey, Qt.QueuedConnection)
        self.typed.connect(host.on_typed, Qt.QueuedConnection)
        self.clicked.connect(host.on_click_while_typing, Qt.QueuedConnection)
        self.pressed.connect(host.on_press_with_menu, Qt.QueuedConnection)
        self.escape.connect(host.close_menu, Qt.QueuedConnection)

    def install(self):
        if self._thread and self._thread.is_alive():
            return
        ready = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(ready,), daemon=True,
                                        name="hl-input-hook")
        self._thread.start()
        ready.wait(2.0)

    def _run(self, ready):
        msg = wintypes.MSG()
        self._tid = _k32.GetCurrentThreadId()
        _u32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)
        mod = _k32.GetModuleHandleW(None)

        def hook():
            return (_u32.SetWindowsHookExW(13, self._kb_proc, mod, 0),
                    _u32.SetWindowsHookExW(14, self._ms_proc, mod, 0))
        hooks = hook()
        ready.set()
        try:
            while _u32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == WM_REHOOK:
                    for h in hooks:
                        if h:
                            _u32.UnhookWindowsHookEx(h)
                    hooks = hook()
                    self.rehooks += 1
        finally:
            for h in hooks:
                if h:
                    _u32.UnhookWindowsHookEx(h)

    def reinstall(self):
        if self._thread and self._thread.is_alive() and self._tid:
            _u32.PostThreadMessageW(self._tid, WM_REHOOK, 0, 0)
        else:
            self.install()

    def remove(self):
        t = self._thread
        if t and t.is_alive():
            _u32.PostThreadMessageW(self._tid, 0x0012, 0, 0)
            t.join(1.0)
        self._thread = None
        self.reset_fly()

    def reset_fly(self):
        with self._lock:
            self._down.clear()
            self._look = [0.0, 0.0]
            self._wheel = 0
            self._rb = None

    def take(self):
        with self._lock:
            keys = {k: True for k in self._down}
            (dx, dy), self._look = self._look, [0.0, 0.0]
            wheel, self._wheel = self._wheel, 0
        return keys, dx, dy, wheel

    def _active(self):
        fg = _foreground_pid()
        return fg is not None and (fg == self.game_pid or fg == os.getpid())

    def _on_key(self, code, wparam, lparam):
        try:
            if code >= 0 and self._active():
                k = ctypes.cast(lparam, ctypes.POINTER(_KBD)).contents
                vk, down = k.vkCode, wparam in (0x100, 0x104)
                if k.dwExtraInfo == vista.INJECT_MARK:
                    return _u32.CallNextHookEx(None, code, wparam, lparam)
                if self.menu_open and vk == 0x1B and not self.editing:
                    if down:
                        self.escape.emit()
                    return 1
                if vk in HOTKEY_ACTIONS:
                    if down and vk not in self._hot_held:
                        self._hot_held.add(vk)
                        self.hot.emit(HOTKEY_ACTIONS[vk])
                    elif not down:
                        self._hot_held.discard(vk)
                    return 1
                if self.editing and vk not in _PASS_VKS and not (k.flags & 0x20):
                    if down and vk not in _MOD_VKS:
                        shift = bool(_u32.GetAsyncKeyState(0x10) & 0x8000)
                        ctrl = bool(_u32.GetAsyncKeyState(0x11) & 0x8000)
                        self.typed.emit(vk, "" if ctrl else self._text(vk, k.scanCode, shift),
                                        shift, ctrl)
                    return 1
                if self.flying and vk in FLY_VKS:
                    with self._lock:
                        (self._down.add if down else self._down.discard)(FLY_VKS[vk])
                    return 1
        except Exception:
            pass
        return _u32.CallNextHookEx(None, code, wparam, lparam)

    @staticmethod
    def _text(vk, scan, shift):
        state = (ctypes.c_ubyte * 256)()
        if shift:
            state[0x10] = 0x80
        if _u32.GetKeyState(0x14) & 1:
            state[0x14] = 0x01
        buf = ctypes.create_unicode_buffer(8)
        fg = _u32.GetForegroundWindow()
        layout = _u32.GetKeyboardLayout(_u32.GetWindowThreadProcessId(fg, None) if fg else 0)
        n = _u32.ToUnicodeEx(vk, scan, state, buf, 8, 0x4, layout)
        return buf.value[:n] if n > 0 else ""

    def _on_mouse(self, code, wparam, lparam):
        try:
            if code >= 0 and self._active():
                m = ctypes.cast(lparam, ctypes.POINTER(_MSE)).contents
                if self.editing and wparam in (0x201, 0x204, 0x207):
                    self.clicked.emit(m.pt.x, m.pt.y)
                if self.menu_open and wparam in (0x201, 0x204, 0x207):
                    self.pressed.emit(m.pt.x, m.pt.y)
                if self.flying:
                    if wparam == 0x204:
                        with self._lock:
                            self._rb = (m.pt.x, m.pt.y)
                        return 1
                    if wparam == 0x205:
                        with self._lock:
                            self._rb = None
                        return 1
                    if wparam == 0x200 and self._rb is not None:
                        with self._lock:
                            if self._rb is not None:
                                self._look[0] += m.pt.x - self._rb[0]
                                self._look[1] += m.pt.y - self._rb[1]
                        return 1
                    if wparam == 0x20A:
                        with self._lock:
                            self._wheel += ctypes.c_short(m.mouseData >> 16).value // 120
                        return 1
        except Exception:
            pass
        return _u32.CallNextHookEx(None, code, wparam, lparam)


HOTKEY_ACTIONS = {vk: action for vk, action, _ in HOTKEYS}


class SequencerOverlay(QObject):

    def __init__(self, orch):
        super().__init__()
        self.orch = orch
        self.eng = orch.sequencer
        self.launched = False
        self.menu_open = False
        self.clean = False
        self.taking = False
        self.fly = FlyCam()
        self._fly_t = time.monotonic()
        self._offset = None
        self._game = None
        self._edit = None
        self._async_down = set()
        self._resume_fly = False
        self._anim_status = ("", False)
        self._anim_checked = 0.0
        self._bridge = _Bridge()
        self._bridge.changed.connect(self._refresh, Qt.QueuedConnection)
        self.eng.listeners.append(self._bridge.changed.emit)
        self.win = PanelWindow(self)
        self.panel = self.win.panel
        self.hud = HudWindow(self)
        self.cross = CrossWindow()
        self.menu = OverlayMenu(self)
        self.softcur = SoftCursor()
        self.hook = InputHook(self)
        self._watch_fields()
        self._tick = QTimer(self)
        self._tick.timeout.connect(self._on_tick)
        self._tick.start(16)

    @property
    def flying(self):
        return self.eng.state == "flying"

    @property
    def editing(self):
        return self._edit is not None

    def isVisible(self):
        return self.launched and self.win.isVisible()

    def raise_(self):
        self.set_menu(True)

    def remember_panel_offset(self):
        if self._game:
            _, gx, gy, _, _ = self._game
            self._offset = (self.win.x() - gx, self.win.y() - gy)

    def _follow_game(self):
        got = game_client_rect()
        game_pid = game.game_pid_cached()
        self.hook.game_pid = game_pid
        self.hook.flying = self.flying
        if not got:
            self._end_edit(commit=True)
            self.close_menu()
            for w in (self.win, self.hud, self.cross, self.softcur):
                if w.isVisible():
                    w.hide()
            self._game = None
            return
        hwnd, x, y, w, h = got
        r = self.win.devicePixelRatioF() or 1.0
        gx, gy, gw, gh = x / r, y / r, w / r, h / r
        self._game = (hwnd, gx, gy, gw, gh)
        if self._offset is None:
            self._offset = (gw - self.win.width() - 24, 56)
        ox = max(0, min(self._offset[0], gw - 120))
        oy = max(0, min(self._offset[1], gh - 60))
        fg = _foreground_pid()
        in_game = fg is not None and fg == game_pid
        if self.editing and fg != game_pid and fg != os.getpid():
            self._end_edit(commit=True)
        want = {self.win: self.menu_open, self.hud: not self.clean,
                self.cross: self.flying and not self.clean}
        for wdg, show in want.items():
            if show:
                if not wdg.isVisible():
                    wdg.show()
                wdg.own(hwnd)
                wdg.set_topmost(in_game)
            elif wdg.isVisible():
                wdg.hide()
        self.win.move(int(gx + ox), int(gy + oy))
        self.hud.move(int(gx + (gw - self.hud.width()) / 2), int(gy + 12))
        self.cross.move(int(gx + gw / 2 - 22), int(gy + gh / 2 - 22))
        if self.menu.isVisible():
            self.menu.own(hwnd)
            self.menu.set_topmost(in_game)
        self._soft_cursor(hwnd, in_game)

    def _soft_cursor(self, hwnd, in_game):
        want = None
        if (self.menu_open or self.menu.isVisible()) and not self.clean \
                and not (self.flying and self.hook._rb is not None):
            got = cursor_info()
            if got and not got[0]:
                r = self.win.devicePixelRatioF() or 1.0
                want = (int(got[1] / r) - 1, int(got[2] / r) - 1)
        if want is None:
            if self.softcur.isVisible():
                self.softcur.hide()
            return
        self.softcur.move(*want)
        if not self.softcur.isVisible():
            self.softcur.show()
        self.softcur.own(hwnd)
        self.softcur._topmost = None
        self.softcur.set_topmost(True)

    def show_menu(self, items, global_pos):
        self._end_edit(commit=True)
        self.menu.populate(items)
        screen = QApplication.screenAt(global_pos) or QApplication.primaryScreen()
        g = screen.availableGeometry()
        x = max(g.left(), min(global_pos.x(), g.right() - self.menu.width()))
        y = global_pos.y()
        if y + self.menu.height() > g.bottom():
            y = max(g.top(), y - self.menu.height() - 34)
        self.menu.move(x, y)
        self.menu.show()
        if self._game:
            self.menu.own(self._game[0])
        self.menu._topmost = None
        self.menu.set_topmost(True)
        self.hook.menu_open = True

    def close_menu(self, chosen=False):
        was = self.menu.isVisible()
        if was:
            self.menu.hide()
        self.hook.menu_open = False
        if was and not chosen:
            self.panel.refresh()

    def on_press_with_menu(self, x, y):
        if self.menu.isVisible() and not self.menu.contains_physical(x, y):
            self.close_menu()

    def launch(self):
        self.launched = True
        self.clean = False
        self.menu_open = True
        self.hook.install()
        self._refresh()
        self._follow_game()
        if game.fullscreen():
            self.panel.say("Forza is in fullscreen: turn Fullscreen off in Settings › Video", bad=True)
        elif self._game:
            focus_game()

    def set_menu(self, open_):
        self.menu_open = bool(open_)
        if self.menu_open:
            self.clean = False
        else:
            self._end_edit(commit=True)
            self.close_menu()
        self._follow_game()

    def toggle_menu(self):
        if not self.launched:
            return
        self.set_menu(not self.menu_open)

    def hide_for_shot(self):
        self._end_edit(commit=True)
        self.close_menu()
        self.clean = True
        self.menu_open = False
        self._follow_game()

    def clean_play(self):
        if not self._ready_to_drive():
            return
        if not any(s.keys for s in self.eng.seq.shots):
            self.panel.say("Add some keys first (F9).", bad=True)
            return
        self.set_flying(False)
        drv = self._anim_driver()
        cfg = self.eng.seq.settings["animations"]
        if drv is not None and cfg["enabled"] and cfg.get("reset_on_clean_play"):
            drv.reset_car()
        self.eng.scrub(0.0)
        self.eng.play()
        self.hide_for_shot()

    def close_sequencer(self):
        self._end_edit(commit=True)
        self.close_menu()
        self.set_flying(False)
        self.eng.stop()
        self.hook.remove()
        self.launched = False
        self.menu_open = False
        for w in (self.win, self.hud, self.cross, self.softcur):
            w.hide()

    def shutdown(self):
        try:
            self.close_sequencer()
        finally:
            self._tick.stop()
            for w in (self.win, self.hud, self.cross, self.menu, self.softcur):
                w.close()

    def hotkey(self, action):
        if not self.launched:
            return
        self.close_menu()
        if action != "toggle":
            self._end_edit(commit=True)
        {"toggle": self.toggle_menu, "flytoggle": lambda: self.set_flying(not self.flying),
         "add": self.add_key, "update": self.update_key, "fly": self.fly_to_key,
         "play": self.play, "stop": self.stop, "prev": lambda: self._select_key(-1),
         "next": lambda: self._select_key(1)}[action]()

    def _watch_fields(self):
        p = self.panel
        for w in (p.nameEdit, p.shotName, p.dur, p.spacing):
            inner = w.lineEdit() if isinstance(w, QDoubleSpinBox) else w
            inner._edit_target = w
            inner.installEventFilter(self)
        p.table.viewport().installEventFilter(self)
        p.table.itemDelegate().closeEditor.connect(self._table_editor_closed)

    def eventFilter(self, obj, event):
        et = event.type()
        if et == QEvent.MouseButtonPress and isinstance(obj, QWidget):
            target = getattr(obj, "_edit_target", None)
            if target is not None:
                if target is not self._edit:
                    self._begin_edit(target)
            elif self._edit is not None and obj is not self._edit \
                    and not self._is_inside(obj, self._edit):
                self._end_edit(commit=True)
        elif et == QEvent.MouseButtonDblClick and obj is self.panel.table.viewport():
            QTimer.singleShot(0, self._arm_table_editor)
        return False

    @staticmethod
    def _is_inside(obj, widget):
        while obj is not None:
            if obj is widget:
                return True
            obj = obj.parent() if hasattr(obj, "parent") else None
        return False

    def _arm_table_editor(self):
        for ed in self.panel.table.viewport().findChildren(QLineEdit):
            if ed.isVisible():
                self._begin_edit(ed, table=True)
                return

    def _table_editor_closed(self, *_):
        if self._edit is not None and getattr(self._edit, "_table_editor", False):
            self._clear_edit()

    def _begin_edit(self, target, table=False):
        if self._edit is not None and self._edit is not target:
            self._end_edit(commit=True)
        self._edit = target
        target._table_editor = table
        self.hook.editing = True
        QApplication.instance().installEventFilter(self)
        QApplication.sendEvent(target, QFocusEvent(QEvent.FocusIn, Qt.MouseFocusReason))
        if not table:
            target.setStyleSheet(f"border:1px solid {ORANGE};")
        sel = target.lineEdit() if isinstance(target, QDoubleSpinBox) else target
        sel.selectAll()
        QTimer.singleShot(0, sel.selectAll)
        self.hud.refresh()

    def _clear_edit(self):
        target, self._edit = self._edit, None
        self.hook.editing = False
        QApplication.instance().removeEventFilter(self)
        if target is not None:
            try:
                if not getattr(target, "_table_editor", False):
                    target.setStyleSheet("")
                target.blockSignals(True)
                QApplication.sendEvent(target, QFocusEvent(QEvent.FocusOut, Qt.OtherFocusReason))
                target.blockSignals(False)
            except RuntimeError:
                pass
        self.hud.refresh()

    def _end_edit(self, commit=True):
        target = self._edit
        if target is None:
            return
        try:
            if commit:
                QApplication.sendEvent(target, QKeyEvent(QEvent.KeyPress, Qt.Key_Return, Qt.NoModifier))
            elif getattr(target, "_table_editor", False):
                QApplication.sendEvent(target, QKeyEvent(QEvent.KeyPress, Qt.Key_Escape, Qt.NoModifier))
        except RuntimeError:
            pass
        if not commit:
            self.panel._save_as_pending = False
            self.panel.refresh()
        if self._edit is target:
            self._clear_edit()

    def on_typed(self, vk, text, shift, ctrl):
        target = self._edit
        if target is None:
            return
        if vk == 0x1B:
            self._end_edit(commit=False)
            return
        if vk in (0x0D, 0x09):
            self._end_edit(commit=True)
            return
        if vk in _QT_KEYS:
            key = _QT_KEYS[vk]
        elif 0x41 <= vk <= 0x5A:
            key = Qt.Key(Qt.Key_A.value + vk - 0x41)
        elif 0x30 <= vk <= 0x39:
            key = Qt.Key(Qt.Key_0.value + vk - 0x30)
        else:
            key = Qt.Key_unknown
        mods = Qt.NoModifier
        if shift:
            mods |= Qt.ShiftModifier
        if ctrl:
            mods |= Qt.ControlModifier
        try:
            QApplication.sendEvent(target, QKeyEvent(QEvent.KeyPress, key, mods, text))
            QApplication.sendEvent(target, QKeyEvent(QEvent.KeyRelease, key, mods, text))
        except RuntimeError:
            self._clear_edit()

    def on_click_while_typing(self, x, y):
        if self._edit is not None and not self.win.contains_physical(x, y):
            self._end_edit(commit=True)

    def _ready_to_drive(self):
        if getattr(self.orch, "link", None) is None:
            self.panel.say("No camera control yet: press Take camera.", bad=True)
            return False
        return True

    def take_camera(self):
        if self.taking:
            return
        self.taking = True
        self.panel.say("Taking the camera… (stay in free roam)")

        def work():
            try:
                if not getattr(self.orch, "running", False):
                    self.orch.start(start_capture=True)
                if not getattr(self.orch, "running", False):
                    ok, msg = False, "HorizonLens didn't start; press Start in its window"
                else:
                    ok, msg = self.orch.attach_companion()
            except Exception as e:
                ok, msg = False, str(e)
            self.taking = False
            self.eng.message = "Camera control: ready" if ok else f"Can't take the camera: {msg}"
            self._bridge.changed.emit()
        threading.Thread(target=work, daemon=True, name="hl-take-camera").start()

    def set_flying(self, on):
        self._resume_fly = False
        if on:
            if not self._ready_to_drive():
                self.panel.flyBtn.setChecked(False)
                return
            if not self.flying:
                cur = self.eng.current_pose() if self.eng.state != "stopped" else None
                if not cur or cur.get("pos") is None:
                    cur = self.eng.timeline_pose()
                self.fly.start_from(cur)
                self.hook.reset_fly()
                self.eng.fly(self.fly.sample())
                self._fly_t = time.monotonic()
                self.panel.say("Flying: arrows move, Page Up / Down up / down, hold right mouse to look, "
                               "+ / − zoom, F9 sets a key. F2 stops flying.")
        elif self.flying:
            self.eng.pause()
        self.hook.flying = self.flying
        self._follow_game()

    def _capture(self):
        if self.flying:
            pose = dict(self.fly.sample())
        else:
            pose = self.eng.current_pose() if self.eng.state != "stopped" else None
            if not pose or pose.get("pos") is None:
                pose = self.eng.timeline_pose()
            if not pose:
                self.panel.say("Press F2 to fly the camera to a spot first, then F9.", bad=True)
                return None
        car = self.eng._car()
        pose["car"] = car.to_dict() if car is not None else None
        return pose

    def add_key(self):
        if not self._ready_to_drive():
            return
        pose = self._capture()
        if not pose:
            return
        what = self.eng.key_at_playhead(pose)
        k = self.eng.key_index
        t = self.eng.shot.keys[k].t - self.eng.shot.trim_in if 0 <= k < len(self.eng.shot.keys) else 0.0
        self.panel.say(f"Key {k + 1} {'updated' if what == 'updated' else 'added'} at {t:.2f} s"
                       + ("" if what == "updated" else " · fly on and press F9 for the next"))

    def update_key(self):
        if self.eng.key_index < 0:
            self.panel.say("Select a key first (click it on the timeline).", bad=True)
            return
        pose = self._capture()
        if pose and self.eng.update_key(pose):
            self.panel.say(f"Key {self.eng.key_index + 1} set from the camera")

    def fly_to_key(self):
        if not self._ready_to_drive():
            return
        if not self.eng.fly_to_key():
            self.panel.say("Select a key first (click it on the timeline).", bad=True)

    def play(self):
        if self.eng.state != "playing" and not self._ready_to_drive():
            return
        if not any(s.keys for s in self.eng.seq.shots):
            self.panel.say("Add some keys first: F2 to fly, F9 to set a key.", bad=True)
            return
        resume = self.flying
        self.eng.toggle_play()
        if resume:
            self._resume_fly = True
            self.hook.flying = False

    def stop(self):
        self._resume_fly = False
        if self.flying:
            self.eng.pause()
        self.eng.stop()
        self.hook.flying = False
        self.panel.say("Camera handed back to the game")

    def _select_key(self, delta):
        n = len(self.eng.shot.keys)
        if n:
            i = self.eng.key_index if self.eng.key_index >= 0 else (0 if delta > 0 else n)
            self.eng.key_index = max(0, min(n - 1, i + delta))
            self.eng.trigger_index = -1
            if getattr(self.orch, "link", None) is not None:
                self.eng.fly_to_key(self.eng.key_index)
            self._refresh()

    def set_zoom(self, z, commit=True):
        z = max(seqmod.ZOOM_MIN, min(seqmod.ZOOM_MAX, float(z)))
        self.fly.zoom = z
        if self.flying:
            self.eng.fly(self.fly.sample())
            return
        g = self.eng.selected_trigger()
        if g is not None and g.action == seqmod.TRACK_ZOOM:
            if commit:
                self.eng.set_trigger_value(self.eng.trigger_index, z)
                self.panel.say(f"Zoom keyframe {z:.1f}×")
            return
        k = self.eng.key_index
        if commit and 0 <= k < len(self.eng.shot.keys):
            self.eng.set_key_field(k, "fov", seqmod.zoom_to_fov(z))
            self.panel.say(f"Key {k + 1} zoom {z:.1f}×")

    def shown_zoom(self):
        if self.flying:
            return self.fly.zoom
        g = self.eng.selected_trigger()
        if g is not None and g.action == seqmod.TRACK_ZOOM:
            return g.value or 1.0
        k = self.eng.key_index
        if 0 <= k < len(self.eng.shot.keys):
            return seqmod.fov_to_zoom(self.eng.shot.keys[k].fov)
        return self.fly.zoom

    def _anim_driver(self):
        return getattr(self.orch, "anim", None)

    def reset_car(self):
        drv = self._anim_driver()
        if drv is None:
            return

        def work():
            ok, message = drv.reset_car()
            self.eng.message = "Car reset to default" if ok else f"Reset: {message}"
            self._bridge.changed.emit()
        threading.Thread(target=work, daemon=True, name="hl-reset-car").start()

    def setup_vista(self):
        def work():
            ok, message = vista.setup_bindings()
            self.eng.message = message
            self._bridge.changed.emit()
        threading.Thread(target=work, daemon=True, name="hl-vista-setup").start()

    def animation_status(self):
        seq = self.eng.seq
        triggers = [g for g in seq.triggers if g.action not in seqmod.SPECIAL_TRACKS]
        now = time.monotonic()
        if triggers and now - self._anim_checked > 5.0:
            self._anim_checked = now
            threading.Thread(target=vista.forzavista_running, daemon=True,
                             name="hl-vista-check").start()
        parts = []
        for track in seqmod.SPECIAL_TRACKS:
            n = len(seq.track(track))
            if n and seq.settings[track]["enabled"]:
                parts.append(f"{track_label(track)} {n}")
        air = getattr(self.orch, "air", None)
        if seq.track(seqmod.TRACK_AIR) and air is not None and air.driven and \
                air.status not in ("sequencer", "ready"):
            return (" · ".join(parts) + f" · air: {air.status}", True)
        if not triggers or not seq.settings["animations"]["enabled"]:
            return (" · ".join(parts) or "Zoom, air suspension and Forzavista keyframes at chosen "
                    "moments", False)
        n = len(triggers)
        head = " · ".join(parts + [f"Forzavista {n}"])
        if not vista._running_cache[1]:
            return (f"{head} · start Forzavista to play them", True)
        drv = self._anim_driver()
        if drv is not None and drv.last_error:
            return (f"{head} · {drv.last_error}", True)
        on = drv.snapshot() if drv is not None else {}
        if on:
            names = ", ".join(vista.ACTION_LABELS.get(a, a) for a in sorted(on))
            return (f"{head} · on now: {names}"[:110], False)
        return (f"{head} · Forzavista ready", False)

    def _check_hook(self, game_pid):
        fg = _foreground_pid()
        if fg is None or fg not in (game_pid, os.getpid()):
            self._async_down.clear()
            return
        if time.monotonic() - vista.last_injected[0] < 0.5:
            for vk, _, _ in HOTKEYS:
                if _key(vk):
                    self._async_down.add(vk)
            return
        for vk, action, _ in HOTKEYS:
            if _key(vk):
                if vk not in self._async_down:
                    self._async_down.add(vk)
                    self.hook.reinstall()
                    self.hotkey(action)
            else:
                self._async_down.discard(vk)

    def _on_tick(self):
        if not self.launched:
            return
        try:
            self._follow_game()
            self._check_hook(self.hook.game_pid)
            now = time.monotonic()
            dt, self._fly_t = min(0.1, now - self._fly_t), now
            if self._resume_fly and self.eng.state == "paused":
                self._resume_fly = False
                self.set_flying(True)
            elif self._resume_fly and self.eng.state == "stopped":
                self._resume_fly = False
            if self.flying and self.eng.fly_jump is not None:
                self.fly.start_from(self.eng.fly_jump)
                self.eng.fly_jump = None
            if self.flying:
                keys, dx, dy, wheel = self.hook.take()
                if wheel:
                    self.fly.speed = max(0.5, min(FlyCam.MAX_SPEED, self.fly.speed * 1.25 ** wheel))
                self.fly.step(dt, keys, dx, dy)
                self.eng.fly(self.fly.sample())
            if self.win.isVisible():
                self.panel.update_live()
            if self.hud.isVisible():
                self.hud.refresh()
        except Exception as e:
            import traceback
            if getattr(self, "_last_err", None) != repr(e):
                self._last_err = repr(e)
                traceback.print_exc()

    def _refresh(self):
        self.panel.refresh()
        self.hud.refresh()
