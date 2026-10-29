from __future__ import annotations

import io
import sys
import threading
import time
from pathlib import Path

import qrcode
from PySide6.QtCore import Qt, QTimer, QSize, QThread, Signal, QObject, QPointF
from PySide6.QtCore import QUrl
from PySide6.QtGui import (QPixmap, QFont, QIcon, QGuiApplication,
                           QDesktopServices, QPainter, QPolygonF, QColor,
                           QPen)
from PySide6.QtWidgets import (
    QApplication, QWidget, QLabel, QPushButton, QVBoxLayout, QHBoxLayout,
    QFrame, QScrollArea, QListWidget, QListWidgetItem, QLineEdit, QComboBox,
    QStackedWidget, QButtonGroup, QDoubleSpinBox, QCheckBox,
)

from . import __version__, config, game, recording
from .core import Orchestrator

ASSETS = config.ASSETS_DIR

BG = "#000000"
SHEET = "#1c1c1e"
CELL = "#2c2c2e"
LINE = "#38383a"
TXT = "#ffffff"
MUTED = "#98989f"
ORANGE = "#f59e3b"
GOOD = "#30d158"
BAD = "#ff453a"
WARN = "#ffd60a"
BLUE = "#0a84ff"

FONT = "'Segoe UI Variable Text','Segoe UI',sans-serif"
EDITION_NAMES = {game.STEAM: "Steam", game.STORE: "Microsoft Store"}


def qr_pixmap(data: str, size: int = 232) -> QPixmap:
    qr = qrcode.QRCode(version=None, border=3, box_size=12,
                       error_correction=qrcode.constants.ERROR_CORRECT_L)
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    buf = io.BytesIO(); img.save(buf, format="PNG")
    pm = QPixmap(); pm.loadFromData(buf.getvalue(), "PNG")
    return pm.scaled(size, size, Qt.KeepAspectRatio, Qt.FastTransformation)


DISCORD_URL = "https://discord.gg/fh6"


def button(text, kind="secondary", height=40) -> QPushButton:
    b = QPushButton(text)
    b.setCursor(Qt.PointingHandCursor)
    b.setMinimumHeight(height)
    b.setStyleSheet(button_style(kind, height))
    return b


def button_style(kind="secondary", height=40) -> str:
    styles = {
        "primary": (f"color:#000;background:{ORANGE};border:0;",
                    f"background:#ffb05a;"),
        "danger": (f"color:{BAD};background:{CELL};border:1px solid {LINE};",
                   f"background:#343437;"),
        "secondary": (f"color:{TXT};background:{CELL};border:1px solid {LINE};",
                      f"background:#343437;"),
        "link": (f"color:{BLUE};background:transparent;border:0;", "color:#409cff;"),
    }
    base, hover = styles[kind]
    return (f"QPushButton{{{base}border-radius:{height // 2 if kind != 'link' else 0}px;"
            f"padding:0 18px;font:600 13px {FONT};}}"
            f"QPushButton:hover{{{hover}}}"
            f"QPushButton:disabled{{color:#636366;"
            f"background:{'#252527' if kind != 'link' else 'transparent'};"
            f"border:1px solid {'#2c2c2e' if kind != 'link' else 'transparent'};}}")


def card() -> QFrame:
    f = QFrame()
    f.setObjectName("card")
    f.setStyleSheet(f"QFrame#card{{background:{SHEET};border-radius:22px;}}")
    return f


def label(text, size=13, color=TXT, weight=400) -> QLabel:
    l = QLabel(text)
    l.setStyleSheet(f"color:{color};font:{weight} {size}px {FONT};background:transparent;")
    return l


class StatusRow(QWidget):

    def __init__(self, title: str, last=False):
        super().__init__()
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setStyleSheet(
            "background:transparent;" if last else
            f"background:transparent;border-bottom:1px solid {LINE};")
        lay = QHBoxLayout(self); lay.setContentsMargins(0, 13, 0, 13); lay.setSpacing(12)
        self.dot = QLabel(); self.dot.setFixedSize(10, 10)
        self.title = label(title, 14)
        self.title.setStyleSheet(self.title.styleSheet() + "border:0;")
        self.value = label("—", 13, MUTED)
        self.value.setStyleSheet(self.value.styleSheet() + "border:0;")
        lay.addWidget(self.dot); lay.addWidget(self.title); lay.addStretch(1)
        lay.addWidget(self.value)
        self.set(None, "—")

    def set(self, ok, text="", warn=False):
        color = GOOD if ok else (WARN if warn else (MUTED if ok is None else BAD))
        self.dot.setStyleSheet(f"background:{color};border-radius:5px;border:0;")
        self.value.setText(text)


class StatusPoller:

    def __init__(self, orch):
        self.orch = orch
        self._lock = threading.Lock()
        self._snap = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="hl-status")
        self._thread.start()

    def snapshot(self):
        with self._lock:
            return self._snap

    def poke(self):
        self._wake.set()

    def stop(self):
        self._stop.set()
        self._wake.set()

    def _run(self):
        while not self._stop.is_set():
            try:
                gs = self.orch.game_status()
                st = None
                if self.orch.running:
                    self.orch.refresh_companion()
                    st = self.orch.status()
                fullscreen = game.fullscreen()
                with self._lock:
                    self._snap = {"game": gs, "status": st, "fullscreen": fullscreen,
                                  "running": self.orch.running}
            except Exception:
                pass
            self._wake.wait(1.0)
            self._wake.clear()


class _BgRelay(QObject):
    done = Signal(object, object)

    def __init__(self):
        super().__init__()
        self.done.connect(self._call, Qt.QueuedConnection)

    def _call(self, callback, result):
        callback(result)


class MainWindow(QWidget):
    attachProgress = Signal(str)

    def __init__(self, orch: Orchestrator):
        super().__init__()
        self.orch = orch
        self._attaching = False
        self._busy = False
        self._rec_busy = False
        self._qr_code = None
        self._rec_started = None
        self.poller = StatusPoller(orch)
        self._bg_relay = _BgRelay()
        self.setWindowTitle("HorizonLens")
        self.setMinimumSize(QSize(900, 640))
        self.setStyleSheet(f"QWidget{{background:{BG};}}")
        ico = ASSETS / "horizonlens.ico"
        if ico.is_file():
            self.setWindowIcon(QIcon(str(ico)))
        self._build()
        self.attachProgress.connect(self.hint.setText, Qt.QueuedConnection)
        self._timer = QTimer(self); self._timer.timeout.connect(self._refresh)
        self._timer.start(1000)
        self._seqTimer = QTimer(self); self._seqTimer.timeout.connect(self._seq_ui_tick)
        self._seqTimer.start(500)
        self._refresh()

    def _build(self):
        root = QVBoxLayout(self); root.setContentsMargins(28, 22, 28, 22); root.setSpacing(18)

        head = QHBoxLayout(); head.setSpacing(12)
        logo = QLabel(); logo.setFixedSize(40, 40)
        tile = ASSETS / "icon_tile.png"
        if tile.is_file():
            logo.setPixmap(QPixmap(str(tile)).scaled(40, 40, Qt.KeepAspectRatio,
                                                     Qt.SmoothTransformation))
        head.addWidget(logo)
        names = QVBoxLayout(); names.setSpacing(0)
        row = QHBoxLayout(); row.setSpacing(8)
        row.addWidget(label("HorizonLens", 22, TXT, 700))
        badge = (f"BETA {__version__.rsplit('beta.', 1)[-1]}" if "beta" in __version__
                 else f"v{__version__}")
        beta = label(badge, 10, ORANGE, 700)
        beta.setToolTip(f"HorizonLens {__version__}")
        beta.setStyleSheet(beta.styleSheet() +
                           f"border:1px solid {ORANGE};border-radius:9px;padding:1px 7px;")
        row.addWidget(beta, 0, Qt.AlignVCenter); row.addStretch(1)
        names.addLayout(row)
        names.addWidget(label("Your phone as a camera in Forza Horizon 6", 12, MUTED))
        head.addLayout(names); head.addStretch(1)

        self.pages = QStackedWidget()
        seg = QFrame(); seg.setObjectName("seg")
        seg.setStyleSheet(f"QFrame#seg{{background:{SHEET};border-radius:19px;}}")
        sl = QHBoxLayout(seg); sl.setContentsMargins(3, 3, 3, 3); sl.setSpacing(2)
        self._pageButtons = QButtonGroup(self)
        for i, name in enumerate(("Camera", "Sequencer", "Recordings", "Help")):
            b = QPushButton(name); b.setCheckable(True); b.setCursor(Qt.PointingHandCursor)
            b.setMinimumHeight(32)
            b.setStyleSheet(
                f"QPushButton{{color:{MUTED};background:transparent;border:0;border-radius:16px;"
                f"padding:0 16px;font:600 12px {FONT};}}"
                f"QPushButton:checked{{color:{ORANGE};background:#3a3a3e;}}")
            self._pageButtons.addButton(b, i); sl.addWidget(b)
        self._pageButtons.button(0).setChecked(True)
        self._pageButtons.idClicked.connect(self._on_page)
        head.addWidget(seg, 0, Qt.AlignVCenter)
        discord = QPushButton("Join Discord"); discord.setCursor(Qt.PointingHandCursor)
        discord.setMinimumHeight(38)
        discord.setToolTip("Help, updates and feedback: discord.gg/fh6")
        discord.setStyleSheet(
            f"QPushButton{{color:#fff;background:#5865F2;border:0;border-radius:19px;"
            f"padding:0 16px;font:600 12px {FONT};}}"
            "QPushButton:hover{background:#4752C4;}")
        discord.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(DISCORD_URL)))
        head.addWidget(discord, 0, Qt.AlignVCenter)
        root.addLayout(head)

        self.pages.addWidget(self._build_dashboard())
        self.pages.addWidget(self._build_sequencer())
        self.pages.addWidget(self._build_editor())
        self.pages.addWidget(self._build_help())
        root.addWidget(self.pages, 1)

    def _build_dashboard(self) -> QWidget:
        page = QWidget(); outer = QVBoxLayout(page)
        outer.setContentsMargins(0, 0, 0, 0); outer.setSpacing(18)
        body = QHBoxLayout(); body.setSpacing(18)

        sess = card(); sl = QVBoxLayout(sess); sl.setContentsMargins(24, 20, 24, 20); sl.setSpacing(4)
        sl.addWidget(label("Session", 13, MUTED, 600))
        self.rowGame = StatusRow("Forza Horizon 6")
        self.rowCompanion = StatusRow("Camera control")
        self.rowLive = StatusRow("Live view")
        self.rowPhone = StatusRow("Phone", last=True)
        for r in (self.rowGame, self.rowCompanion, self.rowLive, self.rowPhone):
            sl.addWidget(r)
        sl.addSpacing(10)
        acts = QHBoxLayout(); acts.setSpacing(10)
        self.editionBox = QComboBox()
        self.editionBox.setMinimumHeight(40)
        self.editionBox.setStyleSheet(
            f"QComboBox{{color:{TXT};background:{CELL};border:1px solid {LINE};border-radius:20px;"
            f"padding:0 14px;font:600 13px {FONT};}}"
            f"QComboBox::drop-down{{border:0;width:22px;}}"
            f"QComboBox QAbstractItemView{{color:{TXT};background:{CELL};"
            f"selection-background-color:#3a3a3e;border:1px solid {LINE};}}")
        self.launchBtn = button("Launch game")
        self.attachBtn = button("Take camera")
        self.launchBtn.clicked.connect(self._on_launch_game)
        self.attachBtn.clicked.connect(self._on_attach)
        acts.addWidget(self.editionBox); acts.addWidget(self.launchBtn, 1)
        acts.addWidget(self.attachBtn, 1)
        sl.addLayout(acts)
        sl.addStretch(1)
        self.hint = label("", 12, MUTED); self.hint.setWordWrap(True)
        sl.addWidget(self.hint)
        body.addWidget(sess, 11)

        pair = card(); pl = QVBoxLayout(pair); pl.setContentsMargins(24, 20, 24, 20); pl.setSpacing(10)
        pl.addWidget(label("Connect your phone", 13, MUTED, 600))
        self.qr = QLabel(); self.qr.setAlignment(Qt.AlignCenter); self.qr.setFixedSize(252, 252)
        self.qr.setStyleSheet(f"background:{CELL};border-radius:18px;color:{MUTED};"
                              f"font:13px {FONT};")
        self.qr.setText("Press Start to show\nthe pairing code")
        pl.addWidget(self.qr, 0, Qt.AlignHCenter)
        self.codeLab = label("— — — —", 24, TXT, 700)
        self.codeLab.setAlignment(Qt.AlignCenter)
        self.codeLab.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.codeLab.setStyleSheet(self.codeLab.styleSheet() +
                                   "letter-spacing:5px;font-family:'Cascadia Mono',Consolas,monospace;")
        pl.addWidget(self.codeLab)
        linkRow = QHBoxLayout(); linkRow.setSpacing(6)
        self.urlLab = label("Scan with the phone's camera, same Wi-Fi as this PC", 12, MUTED)
        self.urlLab.setAlignment(Qt.AlignCenter)
        self.urlLab.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.copyBtn = button("Copy link", "link", 26); self.copyBtn.setVisible(False)
        self.copyBtn.clicked.connect(self._copy_link)
        linkRow.addStretch(1); linkRow.addWidget(self.urlLab); linkRow.addWidget(self.copyBtn)
        linkRow.addStretch(1)
        pl.addLayout(linkRow)
        pl.addStretch(1)
        body.addWidget(pair, 9)
        outer.addLayout(body, 1)

        bar = QHBoxLayout(); bar.setSpacing(12)
        self.startBtn = button("Start", "primary", 48)
        self.startBtn.clicked.connect(self._on_start_stop)
        self.recBtn = button("●  Record", "secondary", 48)
        self.recBtn.clicked.connect(self._on_record)
        self.recBtn.setEnabled(False)
        bar.addWidget(self.startBtn, 2); bar.addWidget(self.recBtn, 1)
        outer.addLayout(bar)
        return page

    def _build_help(self) -> QWidget:
        scroll = QScrollArea(); scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setStyleSheet(
            f"QScrollArea{{background:transparent;}}"
            f"QScrollBar:vertical{{background:transparent;width:8px;}}"
            f"QScrollBar::handle:vertical{{background:{LINE};border-radius:4px;min-height:30px;}}"
            f"QScrollBar::add-line,QScrollBar::sub-line{{height:0;}}"
            f"QScrollBar::add-page,QScrollBar::sub-page{{background:transparent;}}")
        inner = QWidget()
        wrap = QVBoxLayout(inner); wrap.setContentsMargins(0, 0, 8, 0); wrap.setSpacing(12)

        def section(title, rows):
            c = card(); cl = QVBoxLayout(c); cl.setContentsMargins(24, 18, 24, 18); cl.setSpacing(10)
            cl.addWidget(label(title, 15, TXT, 600))
            for num, head, text in rows:
                r = QHBoxLayout(); r.setSpacing(14)
                badge = label(str(num), 12, "#000", 700); badge.setFixedSize(24, 24)
                badge.setAlignment(Qt.AlignCenter)
                badge.setStyleSheet(badge.styleSheet() + f"background:{ORANGE};border-radius:12px;")
                r.addWidget(badge, 0, Qt.AlignTop)
                t = QLabel(f"<b style='color:{TXT}'>{head}</b><br>{text}")
                t.setWordWrap(True); t.setTextFormat(Qt.RichText)
                t.setStyleSheet(f"color:{MUTED};font:13px {FONT};background:transparent;")
                r.addWidget(t, 1)
                cl.addLayout(r)
            wrap.addWidget(c)

        section("Get started", [
            (1, "Start Forza Horizon 6",
             "Steam or Microsoft Store (Xbox app / Game Pass) both work. Use "
             "<b>Launch game</b> or start it yourself. Any display mode works; "
             "only the game is captured, never your desktop."),
            (2, "Press Start",
             "HorizonLens starts the live view and shows a QR code. When you're "
             "driving, it takes the game camera on its own (<b>Camera control</b> "
             "turns green). If it doesn't, press <b>Take camera</b>."),
            (3, "Scan the QR code with your phone",
             "Same Wi-Fi as this PC. Safari will warn about the certificate the "
             "first time: tap <b>Show Details → visit this website</b>. Then tap "
             "<b>Tap to Start</b> and allow <b>Motion &amp; Orientation</b>."),
        ])
        section("On the phone", [
            (1, "Recenter",
             "Hold the phone the way 'forward' should be and tap the round arrow. "
             "Only the heading resets; the horizon always stays level."),
            (2, "Car View",
             "Pick Passenger, Driver, Window, Bumper, Wheel or GAP (a wide fishbowl "
             "camera behind the car). Use the stick and "
             "HEIGHT to move that view, then <b>SAVE</b> to keep it. The sliders "
             "button hides those controls."),
            (3, "Freecam",
             "Walk around the car with the stick and HEIGHT. The ⋯ menu has Run, "
             "Steadycam, Drone Inertia and the optional motion effects."),
            (4, "Record",
             "The red button records on this PC, with the game's sound, cropped "
             "to exactly what the phone shows. The photos button lists and "
             "downloads recordings."),
        ])
        tips = card(); tl = QVBoxLayout(tips); tl.setContentsMargins(24, 18, 24, 18); tl.setSpacing(8)
        tl.addWidget(label("If something's off", 15, TXT, 600))
        t = QLabel(
            "• <b>Phone can't open the link</b>: same Wi-Fi, and allow HorizonLens "
            "through Windows Firewall on Private networks.<br>"
            "• <b>Live view black</b>: make sure the game isn't minimized. On an "
            "NVIDIA PC, update the graphics driver.<br>"
            "• <b>Camera won't take</b>: get into free roam behind the wheel, then "
            "press Take camera. After a game update it can stay red until "
            "HorizonLens supports that version.<br>"
            "• <b>Camera turns the wrong way</b>: tap Recenter while holding the "
            "phone upright.")
        t.setWordWrap(True); t.setTextFormat(Qt.RichText)
        t.setStyleSheet(f"color:{MUTED};font:13px {FONT};background:transparent;")
        tl.addWidget(t)
        wrap.addWidget(tips)
        wrap.addStretch(1)
        scroll.setWidget(inner)
        return scroll

    def _build_editor(self) -> QWidget:
        w = QWidget(); outer = QHBoxLayout(w)
        outer.setContentsMargins(0, 0, 0, 0); outer.setSpacing(18)

        listCard = card(); ll = QVBoxLayout(listCard)
        ll.setContentsMargins(20, 18, 20, 18); ll.setSpacing(10)
        top = QHBoxLayout()
        top.addWidget(label("Recordings", 15, TXT, 600)); top.addStretch(1)
        refresh = button("Refresh", "link", 26); refresh.clicked.connect(self._load_recordings)
        folder = button("Open folder", "link", 26); folder.clicked.connect(self._open_recordings_folder)
        top.addWidget(refresh); top.addWidget(folder)
        ll.addLayout(top)
        self.clipList = QListWidget()
        self.clipList.setStyleSheet(
            f"QListWidget{{background:transparent;border:0;color:{TXT};font:13px {FONT};outline:0;}}"
            f"QListWidget::item{{padding:12px 10px;border-bottom:1px solid {LINE};}}"
            f"QListWidget::item:selected{{background:#3a3a3e;color:{TXT};border-radius:10px;}}")
        self.clipList.currentRowChanged.connect(self._on_clip_selected)
        ll.addWidget(self.clipList, 1)
        outer.addWidget(listCard, 3)

        ctrl = card(); cl = QVBoxLayout(ctrl)
        cl.setContentsMargins(20, 18, 20, 18); cl.setSpacing(10)
        field_css = (f"QLineEdit{{background:{CELL};border:1px solid {LINE};border-radius:12px;"
                     f"color:{TXT};padding:9px 12px;font:13px {FONT};}}")
        cl.addWidget(label("Split into scenes", 15, TXT, 600))
        help_ = label("Cut points in seconds, comma separated. Each scene is saved "
                      "losslessly in a *_scenes folder next to the clip.", 12, MUTED)
        help_.setWordWrap(True); cl.addWidget(help_)
        self.cutsEdit = QLineEdit(); self.cutsEdit.setPlaceholderText("12, 47.5, 90")
        self.cutsEdit.setStyleSheet(field_css); cl.addWidget(self.cutsEdit)
        self.splitBtn = button("Split", "primary"); self.splitBtn.clicked.connect(self._on_split)
        cl.addWidget(self.splitBtn)
        cl.addSpacing(12)
        cl.addWidget(label("Trim", 15, TXT, 600))
        trimRow = QHBoxLayout(); trimRow.setSpacing(8)
        self.trimStart = QLineEdit(); self.trimStart.setPlaceholderText("Start (s)")
        self.trimEnd = QLineEdit(); self.trimEnd.setPlaceholderText("End (s)")
        for e in (self.trimStart, self.trimEnd):
            e.setStyleSheet(field_css); trimRow.addWidget(e)
        cl.addLayout(trimRow)
        self.trimBtn = button("Export trim"); self.trimBtn.clicked.connect(self._on_trim)
        cl.addWidget(self.trimBtn)
        cl.addStretch(1)
        self.editorStatus = label("", 12, MUTED); self.editorStatus.setWordWrap(True)
        cl.addWidget(self.editorStatus)
        outer.addWidget(ctrl, 2)
        return w

    def _build_sequencer(self) -> QWidget:
        w = QWidget()
        outer = QHBoxLayout(w); outer.setContentsMargins(0, 0, 0, 0); outer.setSpacing(16)

        main = card(); ml = QVBoxLayout(main); ml.setContentsMargins(28, 22, 28, 22); ml.setSpacing(10)
        ml.addWidget(label("Cinematic Sequencer", 22, TXT, 700))
        intro = label("Plan smooth camera moves on a timeline and play them back in the game. "
                      "The sequencer opens as a menu on top of Forza, so you edit and watch "
                      "in the same place.", 13, MUTED)
        intro.setWordWrap(True)
        ml.addWidget(intro)
        self.seqLaunch = button("Open in-game sequencer", "primary", 48)
        self.seqLaunch.clicked.connect(self._open_overlay)
        ml.addWidget(self.seqLaunch)
        self.seqState = label("", 12, MUTED)
        self.seqState.setWordWrap(True)
        ml.addWidget(self.seqState)
        self.fsCard = QFrame(); self.fsCard.setObjectName("fs")
        self.fsCard.setStyleSheet(f"QFrame#fs{{background:#3a2a10;border:1px solid {ORANGE};"
                                  f"border-radius:14px;}}")
        fl = QVBoxLayout(self.fsCard); fl.setContentsMargins(16, 12, 16, 12); fl.setSpacing(4)
        fl.addWidget(label("Forza is in fullscreen mode", 14, ORANGE, 700))
        fsText = label("The sequencer menu can't show on top of a fullscreen game. In Forza, open "
                       "Settings › Video and turn Fullscreen off (windowed / borderless), then "
                       "open the sequencer again.", 12, TXT)
        fsText.setWordWrap(True)
        fsText.setMinimumHeight(52)
        fl.addWidget(fsText)
        self.fsCard.setVisible(False)
        ml.addWidget(self.fsCard)
        steps = [("1", "Get the camera", "Get into free roam, open the sequencer and press Take camera "
                                         "(it presses Start for you)."),
                 ("2", "Fly", "Press F2 and fly the camera (arrow keys, Page Up / Down, right mouse "
                              "to look, + / - to zoom). Fly stays on until you press F2 again."),
                 ("3", "Key it", "Press F9: a key at the playhead, exactly what you see. The playhead "
                                 "moves on, so fly to the next spot and press F9 again."),
                 ("4", "Edit like a video", "Click a key to go back to it (F9 there updates it). Drag "
                                            "a shot's edge to trim it, drag a shot to move it, Split cuts it."),
                 ("5", "Play", "Press F6. The camera glides through your keys; F7 hands it back.")]
        for num, head, text in steps:
            r = QHBoxLayout(); r.setSpacing(14)
            n = label(num, 15, ORANGE, 700); n.setFixedWidth(18); n.setAlignment(Qt.AlignTop)
            col = QVBoxLayout(); col.setSpacing(2)
            col.addWidget(label(head, 14, TXT, 600))
            t = label(text, 12, MUTED); t.setWordWrap(True); col.addWidget(t)
            r.addWidget(n); r.addLayout(col, 1)
            ml.addLayout(r)
        ml.addStretch(1)
        outer.addWidget(main, 3)

        side = card(); sl = QVBoxLayout(side); sl.setContentsMargins(24, 22, 24, 22); sl.setSpacing(8)
        sl.addWidget(label("Hotkeys", 15, TXT, 600))
        for key, text in (("F8", "Show / hide the menu"), ("F2", "Fly the camera on / off"),
                          ("F9", "Key at the playhead (updates a key there)"),
                          ("F11", "Set the selected key from the camera"), ("F5", "Go to the selected key"),
                          ("F6", "Play / pause"), ("F7", "Stop, camera back to you"),
                          ("F3 / F4", "Previous / next key"), ("+ / −", "Zoom while flying")):
            r = QHBoxLayout()
            k = label(key, 12, ORANGE, 700); k.setFixedWidth(58)
            r.addWidget(k); r.addWidget(label(text, 12, TXT), 1)
            sl.addLayout(r)
        sl.addSpacing(10)
        sl.addWidget(label("Good to know", 15, TXT, 600))
        for text in ("Run Forza with Fullscreen off (Settings › Video) so the menu can sit on top of it.",
                     "Shots either stay in place in the world (even if the car drives off) or "
                     "follow the car. Switch any time; the keys stay.",
                     "Tracks: + Keyframe puts a Zoom, Air suspension or Forzavista (doors, "
                     "lights, ...) keyframe at the playhead, each on its own coloured track. Air "
                     "and Forzavista toggle: the 1st keyframe drops / turns on, the next raises / "
                     "turns off.",
                     "⚙ Settings in the menu: switch each track on or off, and set the zoom, air "
                     "suspension and camber curves, times and heights.",
                     "Sequences save automatically. Library in the menu keeps named ones."):
            t = label("•  " + text, 12, MUTED); t.setWordWrap(True); sl.addWidget(t)
        sl.addStretch(1)
        outer.addWidget(side, 2)
        return w

    def closeEvent(self, event):
        overlay = getattr(self, "_overlay", None)
        if overlay is not None:
            try:
                overlay.shutdown()
            except Exception:
                pass
        self.poller.stop()
        event.accept()
        QApplication.instance().quit()

    def _open_overlay(self):
        if not hasattr(self, "_overlay"):
            from .overlay import SequencerOverlay
            self._overlay = SequencerOverlay(self.orch)
        if self._overlay.launched and self._overlay.isVisible():
            self._overlay.raise_()
            return
        self._overlay.launch()

    def _seq_ui_tick(self):
        if self.pages.currentIndex() != 1 or not hasattr(self, "seqState"):
            return
        from .overlay import game_client_rect
        snap = self.poller.snapshot() or {}
        self.fsCard.setVisible(bool(snap.get("fullscreen")) and bool((snap.get("game") or {}).get("running")))
        eng = self.orch.sequencer
        if not self.orch.running:
            text = "Not started yet: the sequencer's Take camera starts HorizonLens for you."
        elif self.orch.link is None:
            text = "Waiting for camera control: get into free roam in Forza."
        elif game_client_rect() is None:
            text = "Forza's window isn't visible (minimised?)."
        else:
            text = (f"Ready. {eng.seq.name}: {len(eng.seq.shots)} shot(s), "
                    f"{sum(len(s.keys) for s in eng.seq.shots)} keys, {eng.seq.duration:.1f} s.")
        self.seqState.setText(text)
        overlay = getattr(self, "_overlay", None)
        self.seqLaunch.setText("Show the in-game sequencer" if overlay and overlay.launched
                               else "Open in-game sequencer")

    def _load_recordings(self):
        if getattr(self, "_listing", False):
            return
        self._listing = True
        self.editorStatus.setText("Loading recordings…")
        self._run_bg(recording.list_recordings, self._show_recordings)

    def _show_recordings(self, clips):
        self._listing = False
        self.clipList.clear()
        self._clips = clips or []
        for c in self._clips:
            dur = c["duration"]
            dtxt = f"{int(dur // 60)}:{int(dur % 60):02d}" if dur else "–:––"
            self.clipList.addItem(QListWidgetItem(
                f"{c['name'].replace('HorizonLens_', '').replace('.mp4', '').replace('_', '  ')}"
                f"      {dtxt}  ·  {c['size_mb']} MB"))
        self.editorStatus.setText("" if self._clips else
                                  "No recordings yet. Press Record here or on the phone.")

    def _selected_clip(self):
        i = self.clipList.currentRow()
        clips = getattr(self, "_clips", [])
        return clips[i] if 0 <= i < len(clips) else None

    def _on_clip_selected(self, _row):
        c = self._selected_clip()
        if c and c["duration"]:
            self.trimEnd.setPlaceholderText(f"End (≤ {c['duration']:.1f} s)")

    def _on_split(self):
        c = self._selected_clip()
        if not c:
            self.editorStatus.setText("Select a recording first."); return
        try:
            cuts = [float(x) for x in self.cutsEdit.text().split(",") if x.strip()]
        except ValueError:
            self.editorStatus.setText("Cut points must be numbers of seconds."); return
        if not cuts:
            self.editorStatus.setText("Enter at least one cut point."); return
        self.editorStatus.setText("Splitting…"); self.splitBtn.setEnabled(False)
        self._run_bg(lambda: recording.split_at(c["path"], cuts), self._after_split)

    def _after_split(self, result):
        self.splitBtn.setEnabled(True)
        n = len(result or [])
        self.editorStatus.setText(f"Saved {n} scene{'s' if n != 1 else ''}." if n
                                  else "Couldn't split that recording.")

    def _on_trim(self):
        c = self._selected_clip()
        if not c:
            self.editorStatus.setText("Select a recording first."); return
        try:
            s = float(self.trimStart.text()); e = float(self.trimEnd.text())
        except ValueError:
            self.editorStatus.setText("Start and end must be numbers of seconds."); return
        self.editorStatus.setText("Exporting…"); self.trimBtn.setEnabled(False)
        self._run_bg(lambda: recording.trim(c["path"], s, e), self._after_trim)

    def _after_trim(self, result):
        self.trimBtn.setEnabled(True)
        self.editorStatus.setText(f"Saved {Path(result).name}" if result
                                  else "Couldn't export that trim.")
        self._load_recordings()

    def _open_recordings_folder(self):
        import os
        try:
            os.startfile(str(config.recordings_dir()))
        except Exception:
            pass

    def _run_bg(self, fn, done):
        relay = self._bg_relay

        def work():
            try:
                r = fn()
            except Exception:
                import traceback; traceback.print_exc()
                r = None
            relay.done.emit(done, r)
        threading.Thread(target=work, daemon=True, name="hl-bg").start()

    def _on_page(self, idx):
        self.pages.setCurrentIndex(idx)
        if idx == 2:
            self._load_recordings()

    def _on_launch_game(self):
        self.launchBtn.setEnabled(False)
        edition = self.editionBox.currentData()
        self.orch.launch_game(edition)
        self.hint.setText(f"Starting Forza Horizon 6 ({EDITION_NAMES.get(edition, 'Steam')})… "
                          "this can take a minute.")

    def _on_attach(self):
        if self._attaching:
            return
        self._attaching = True
        self.attachBtn.setEnabled(False)
        self.hint.setText("Taking the camera… (reading game memory)")
        self._run_bg(lambda: self.orch.attach_companion(progress=self.attachProgress.emit),
                     self._attach_done)

    AUTO_RETRY_SECONDS = 45
    AUTO_MAX_TRIES = 12
    AUTO_FINAL = ("isn't supported", "already loaded", "is missing", "doesn't match")

    def _attach_done(self, res, auto=False):
        ok, msg = res if res else (False, "Something went wrong taking the camera.")
        if ok or not auto:
            self.hint.setText(msg)
        elif any(f in msg for f in self.AUTO_FINAL):
            self._auto_give_up = True
            self.hint.setText(msg)
        else:
            self.hint.setText(f"{msg}  Trying again in {self.AUTO_RETRY_SECONDS} s "
                              "(or press Take camera).")
        self._attaching = False

    def _try_auto_attach(self, gs):
        pid = gs["pid"]
        if getattr(self, "_auto_pid", None) != pid:
            self._auto_pid, self._auto_tries, self._auto_next = pid, 0, 0.0
            self._auto_give_up = False
        if self._attaching or self._auto_give_up or self._auto_tries >= self.AUTO_MAX_TRIES                 or time.monotonic() < self._auto_next:
            return
        self._attaching = True
        self._auto_tries += 1
        self._auto_next = time.monotonic() + self.AUTO_RETRY_SECONDS
        self.hint.setText("Game found — taking the camera… (about 20 s)")
        self._run_bg(lambda: self.orch.attach_companion(progress=self.attachProgress.emit),
                     lambda res: self._attach_done(res, auto=True))

    def _on_start_stop(self):
        if self._busy:
            return
        self._busy = True
        self.startBtn.setEnabled(False)
        stopping = self.orch.running
        self.startBtn.setText("Stopping…" if stopping else "Starting…")
        if stopping:
            self._set_rec_button(False)

        def done(_):
            self._busy = False
            self.startBtn.setEnabled(True)
            self.poller.poke()
            self._refresh()
        self._run_bg(self.orch.stop if stopping else (lambda: self.orch.start(start_capture=True)), done)

    def _sync_qr(self):
        code = self.orch.pair_code() if self.orch.running and self.orch.server else None
        key = (code, self.orch.client_url()) if code else None
        if key == self._qr_code:
            return
        self._qr_code = key
        if code is None:
            self.qr.clear(); self.qr.setText("Press Start to show\nthe pairing code")
            self.codeLab.setText("— — — —")
            self.urlLab.setText("Scan with the phone's camera, same Wi-Fi as this PC")
            self.copyBtn.setVisible(False)
            return
        url = self.orch.client_url()
        self._link = f"{url}?code={code}"
        from . import tls
        fp = tls.cert_fingerprint()
        qr_link = f"{self._link}&fp={fp}" if fp else self._link
        self.qr.setPixmap(qr_pixmap(qr_link))
        self.codeLab.setText(f"{code[:4]} {code[4:]}")
        self.urlLab.setText(url.replace("https://", ""))
        self.copyBtn.setVisible(True)

    def _copy_link(self):
        QGuiApplication.clipboard().setText(getattr(self, "_link", ""))
        self.copyBtn.setText("Copied"); QTimer.singleShot(1500, lambda: self.copyBtn.setText("Copy link"))

    def _on_record(self):
        if self._rec_busy:
            return
        self._rec_busy = True
        self.recBtn.setEnabled(False)

        def done(rec):
            self._rec_busy = False
            self.recBtn.setEnabled(self.orch.running)
            self._set_rec_button(bool(rec))
            if not rec:
                err = self.orch.recorder.last_error
                if err and not self.orch.recorder.current_path:
                    self.hint.setText(f"Recording: {err}")
                if self.pages.currentIndex() == 2:
                    self._load_recordings()
        self._run_bg(self.orch.toggle_recording, done)

    def _set_rec_button(self, on: bool):
        self._rec_started = time.time() if on else None
        self.recBtn.setText("■  Stop  0:00" if on else "●  Record")
        self.recBtn.setStyleSheet(button_style("danger" if on else "secondary", 48))

    def _refresh(self):
        snap = self.poller.snapshot()
        running = self.orch.running
        self._sync_qr()
        if snap is None:
            self.rowGame.set(None, "checking…")
            return
        gs = snap["game"]
        editions = gs.get("editions") or []

        if [self.editionBox.itemData(i) for i in range(self.editionBox.count())] != editions:
            self.editionBox.clear()
            for e in editions:
                self.editionBox.addItem(EDITION_NAMES[e], e)
        self.editionBox.setVisible(len(editions) > 1)

        if gs["running"]:
            self.rowGame.set(True, f"{EDITION_NAMES.get(gs['edition'], 'Steam')} · running")
        elif gs["installed"]:
            self.rowGame.set(False, "not running", warn=True)
        else:
            self.rowGame.set(False, "not installed")
        self.launchBtn.setEnabled(gs["installed"] and not gs["running"])

        if not self._busy and self.startBtn.text() != ("Stop" if running else "Start"):
            self.startBtn.setText("Stop" if running else "Start")
            self.startBtn.setStyleSheet(button_style("secondary" if running else "primary", 48))
        if self.recBtn.isEnabled() != running and not self._busy and not self._rec_busy:
            self.recBtn.setEnabled(running)
        if self._rec_started:
            s = int(time.time() - self._rec_started)
            self.recBtn.setText(f"■  Stop  {s // 60}:{s % 60:02d}")
        if not self.orch.recorder.is_recording() and self._rec_started:
            self._set_rec_button(False)
            threading.Thread(target=self.orch.recorder.stop, daemon=True).start()

        if not running:
            self.rowCompanion.set(None, "—"); self.rowLive.set(None, "—")
            self.rowPhone.set(None, "—")
            self.attachBtn.setEnabled(False)
            self.hint.setText("Start Forza Horizon 6, then press Start." if not gs["running"]
                              else "Press Start, then scan the code with your phone.")
            return

        st = snap["status"]
        if st is None:
            return
        if st.get("server_error"):
            self.rowPhone.set(False, "phone page not running")
            self.hint.setText(st["server_error"])
            return
        if st["companion"]:
            self.rowCompanion.set(True, "ready")
        elif self._attaching:
            self.rowCompanion.set(False, "connecting…", warn=True)
        else:
            self.rowCompanion.set(False, "not connected", warn=True)
        if st["mediamtx"] and st["capture"]:
            self.rowLive.set(True, st.get("capture_mode") or "streaming")
        elif st["mediamtx"]:
            self.rowLive.set(False, "waiting for the game window", warn=True)
        else:
            self.rowLive.set(False, "media server down")
        self.rowPhone.set(st["clients"] > 0, "connected" if st["clients"] else "waiting",
                          warn=not st["clients"])

        self.attachBtn.setEnabled(gs["running"] and not st["companion"] and not self._attaching)
        if gs["running"] and not st["companion"]:
            self._try_auto_attach(gs)
        if not gs["running"]:
            self.hint.setText("Waiting for Forza Horizon 6.")
        elif st["companion"] and st["clients"]:
            self.hint.setText("All set. The phone has the camera.")
        elif st["companion"]:
            self.hint.setText("Ready. Scan the code with your phone.")


def _set_app_user_model_id():
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("HorizonLens.App")
        except Exception:
            pass


_instance_mutex = None


def _already_running() -> bool:
    global _instance_mutex
    try:
        import ctypes
        k32, u32 = ctypes.windll.kernel32, ctypes.windll.user32
        _instance_mutex = k32.CreateMutexW(None, False, r"Local\HorizonLens.App")
        if k32.GetLastError() != 183:
            return False
        hwnd = u32.FindWindowW(None, "HorizonLens")
        if hwnd:
            u32.ShowWindow(hwnd, 9)
            u32.SetForegroundWindow(hwnd)
        return True
    except Exception:
        return False


_crash_file = None


def _crash_log():
    global _crash_file
    import faulthandler
    import traceback
    try:
        path = config.data_dir() / "crash.log"
        if path.is_file() and path.stat().st_size > 500_000:
            path.replace(path.with_suffix(".old.log"))
        _crash_file = open(path, "a", encoding="utf-8")
        stamp = time.strftime('%Y-%m-%d %H:%M:%S')
        _crash_file.write(f"\n=== HorizonLens {__version__} started {stamp}\n")
        _crash_file.flush()
        faulthandler.enable(file=_crash_file, all_threads=True)

        def hook(kind, value, tb):
            _crash_file.write("".join(traceback.format_exception(kind, value, tb)))
            _crash_file.flush()
        sys.excepthook = hook
        threading.excepthook = lambda a: hook(a.exc_type, a.exc_value, a.exc_traceback)
    except OSError:
        pass


def run():
    if _already_running():
        sys.exit(0)
    _crash_log()
    _set_app_user_model_id()
    app = QApplication(sys.argv)
    app.setFont(QFont("Segoe UI", 10))
    ico = ASSETS / "horizonlens.ico"
    if ico.is_file():
        app.setWindowIcon(QIcon(str(ico)))
    orch = Orchestrator()
    win = MainWindow(orch)
    win.resize(1000, 720)
    win.show()
    code = app.exec()
    orch.stop()
    sys.exit(code)


if __name__ == "__main__":
    run()
