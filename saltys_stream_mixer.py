"""
Salty's Stream Mixer - per-app audio mixer for Windows 10/11.

* Every channel uses its own virtual audio cable (VB-Cable).
* One channel is the "Windows default": the app makes it the PC's default
  output, so everything plays through the mixer.
* Drag an app to another channel to route just that app there.
* Every channel plays to TWO real outputs at once (e.g. headphones + speakers).
"""
import csv
import faulthandler
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import traceback
import urllib.request
import uuid
from ctypes import POINTER, cast
from pathlib import Path

import numpy as np
import psutil
import sounddevice as sd
from PySide6 import QtCore, QtGui, QtWidgets
from PySide6.QtCore import Qt

import comtypes
from comtypes import CLSCTX_ALL
from pycaw.constants import CLSID_MMDeviceEnumerator
from pycaw.pycaw import IAudioSessionControl2, IAudioSessionManager2, IMMDeviceEnumerator

APP_NAME = "Salty's Stream Mixer"
APP_ID = "SaltysStreamMixer"
OWNER, REPO = "EternalSalt", "Stream-Audio-Mixer"
PORTABLE_ASSET = f"{APP_ID}-Portable.exe"

SR = 48000
BLOCK = 480                 # 10 ms
MAX_LAT = int(SR * 0.15)    # if a buffer grows beyond this, trim back to TARGET
TARGET = int(SR * 0.04)

APP_DIR = Path(os.environ.get("APPDATA", str(Path.home()))) / APP_ID
CFG_PATH = APP_DIR / "config.json"
FROZEN = bool(getattr(sys, "frozen", False))
if FROZEN:
    HERE = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))  # bundled files
    EXE_DIR = Path(sys.executable).resolve().parent                       # next to the .exe
else:
    HERE = EXE_DIR = Path(__file__).resolve().parent


# --------------------------------------------------------------------------
# Audio engine: one capture device -> two output devices
# --------------------------------------------------------------------------
class Ring:
    def __init__(self, cap=SR):
        self.buf = np.zeros((cap, 2), np.float32)
        self.cap, self.r, self.w, self.n = cap, 0, 0, 0
        self.lock = threading.Lock()

    def write(self, x):
        with self.lock:
            m = len(x)
            if m >= self.cap:
                x, m = x[-self.cap:], self.cap
            over = self.n + m - self.cap
            if over > 0:
                self.r = (self.r + over) % self.cap
                self.n -= over
            end = self.w + m
            if end <= self.cap:
                self.buf[self.w:end] = x
            else:
                k = self.cap - self.w
                self.buf[self.w:] = x[:k]
                self.buf[:m - k] = x[k:]
            self.w = end % self.cap
            self.n += m

    def read(self, m):
        out = np.zeros((m, 2), np.float32)
        with self.lock:
            if self.n > MAX_LAT:  # clock drift between devices: trim latency
                d = self.n - TARGET
                self.r = (self.r + d) % self.cap
                self.n -= d
            k = min(m, self.n)
            if k:
                end = self.r + k
                if end <= self.cap:
                    out[:k] = self.buf[self.r:end]
                else:
                    j = self.cap - self.r
                    out[:j] = self.buf[self.r:]
                    out[j:k] = self.buf[:end - self.cap]
                self.r = end % self.cap
                self.n -= k
        return out


class Engine:
    def __init__(self):
        self.rings = [Ring(), Ring()]
        self.gain = [1.0, 1.0]
        self.mute = [False, False]
        self.master = 1.0
        self.level = 0.0
        self.active = [False, False]
        self.streams = []

    def start(self, in_idx, out_idxs):
        self.stop()
        self.rings = [Ring(), Ring()]
        self.active = [i is not None for i in out_idxs]
        ws = sd.WasapiSettings(auto_convert=True)

        def in_cb(indata, frames, t, status):
            self.level = max(self.level, float(np.abs(indata).max()))
            for i in (0, 1):
                if self.active[i]:
                    self.rings[i].write(indata)

        def make_out_cb(i):
            def cb(outdata, frames, t, status):
                g = 0.0 if self.mute[i] else self.gain[i] * self.master
                outdata[:] = np.clip(self.rings[i].read(frames) * g, -1.0, 1.0)
            return cb

        try:
            for i, idx in enumerate(out_idxs):
                if idx is None:
                    continue
                s = sd.OutputStream(device=idx, channels=2, samplerate=SR, blocksize=BLOCK,
                                    dtype="float32", callback=make_out_cb(i), extra_settings=ws)
                s.start()
                self.streams.append(s)
            s = sd.InputStream(device=in_idx, channels=2, samplerate=SR, blocksize=BLOCK,
                               dtype="float32", callback=in_cb, extra_settings=ws)
            s.start()
            self.streams.append(s)
            return None
        except Exception as e:  # noqa
            self.stop()
            return str(e)

    def stop(self):
        for s in self.streams:
            try:
                s.stop()
                s.close()
            except Exception:
                pass
        self.streams = []


# --------------------------------------------------------------------------
# Devices
# --------------------------------------------------------------------------
def _wasapi_index():
    apis = sd.query_hostapis()
    return next((i for i, h in enumerate(apis) if "WASAPI" in h["name"]), None)


def wasapi_devices():
    """({capture name: idx}, {render name: idx}) for WASAPI."""
    wi = _wasapi_index()
    ins, outs = {}, {}
    for i, d in enumerate(sd.query_devices()):
        if d["hostapi"] != wi:
            continue
        if d["max_input_channels"] > 0:
            ins[d["name"]] = i
        if d["max_output_channels"] > 0:
            outs[d["name"]] = i
    return ins, outs


def default_output_name():
    try:
        wi = _wasapi_index()
        idx = sd.query_hostapis()[wi]["default_output_device"]
        return sd.query_devices(idx)["name"]
    except Exception:
        return ""


def is_virtual(name):
    return "vb-audio" in name.lower()


def capture_of(render_name):
    """VB-Cable pairs 'CABLE-A Input' (apps play to) with 'CABLE-A Output' (we read from)."""
    return render_name.replace("Input", "Output", 1)


def find_cables(ins, outs):
    return sorted(n for n in outs if is_virtual(n) and "Input" in n and capture_of(n) in ins)


def cable_label(render_name):
    return render_name.split(" (")[0].replace(" Input", "").strip()


# --------------------------------------------------------------------------
# Windows routing via NirSoft SoundVolumeView
# --------------------------------------------------------------------------
class Router:
    def __init__(self):
        self.exe = None
        for c in (HERE / "SoundVolumeView.exe", EXE_DIR / "SoundVolumeView.exe", APP_DIR / "SoundVolumeView.exe"):
            if c.exists():
                self.exe = str(c)
                break
        self.exe = self.exe or shutil.which("SoundVolumeView.exe")
        self.ids = {}
        self.lock = threading.Lock()

    def _run(self, *args):
        flags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
        return subprocess.run([self.exe, *args], capture_output=True, timeout=20, creationflags=flags)

    def refresh(self):
        tmp = tempfile.mktemp(suffix=".csv")
        try:
            self._run("/scomma", tmp)
            with open(tmp, newline="", encoding="utf-8-sig", errors="replace") as f:
                ids = {}
                for row in csv.DictReader(f):
                    if row.get("Type") == "Device" and row.get("Direction") == "Render":
                        key = next((k for k in row if "Friendly ID" in k), None)
                        if key:
                            ids[row["Name"]] = row[key]
                self.ids = ids
        except Exception:
            pass
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)

    def resolve(self, sd_name):
        base = sd_name.split(" (")[0].strip()
        if base not in self.ids:
            with self.lock:
                self.refresh()
        if base in self.ids:
            return self.ids[base]
        for k, v in self.ids.items():
            if k in sd_name:
                return v
        return base

    def set_app(self, sd_name, proc):
        if self.exe:
            self._run("/SetAppDefault", self.resolve(sd_name), "all", proc)

    def set_default(self, sd_name):
        if self.exe:
            self._run("/SetDefault", self.resolve(sd_name), "all")

    def async_(self, fn, *a):
        t = threading.Thread(target=fn, args=a, daemon=True)
        t.start()
        return t


def list_audio_apps():
    """{process_name: {pids}} for every audio session on every active render device."""
    found = {}
    me = os.getpid()
    enum = comtypes.CoCreateInstance(CLSID_MMDeviceEnumerator, IMMDeviceEnumerator,
                                     comtypes.CLSCTX_INPROC_SERVER)
    coll = enum.EnumAudioEndpoints(0, 1)  # eRender, DEVICE_STATE_ACTIVE
    for i in range(coll.GetCount()):
        try:
            dev = coll.Item(i)
            iface = dev.Activate(IAudioSessionManager2._iid_, CLSCTX_ALL, None)
            mgr = cast(iface, POINTER(IAudioSessionManager2))
            se = mgr.GetSessionEnumerator()
            for j in range(se.GetCount()):
                ctl = se.GetSession(j).QueryInterface(IAudioSessionControl2)
                pid = ctl.GetProcessId()
                if not pid or pid == me:
                    continue
                try:
                    found.setdefault(psutil.Process(pid).name(), set()).add(pid)
                except Exception:
                    pass
        except Exception:
            continue
    return found


# --------------------------------------------------------------------------
# Self-updater (GitHub releases)
# --------------------------------------------------------------------------
def read_version():
    for p in (HERE / "version.txt", EXE_DIR / "version.txt"):
        try:
            return p.read_text().strip()
        except Exception:
            pass
    return "0.0.0"


VERSION = read_version()


def vtuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", v)[:4]) or (0,)


def cleanup_leftovers():
    if FROZEN:
        for suffix in (".new", ".old"):
            try:
                Path(sys.executable + suffix).unlink()
            except Exception:
                pass


class Updater(QtCore.QObject):
    status = QtCore.Signal(str)
    available = QtCore.Signal(str, str, int)  # version, url, size
    progress = QtCore.Signal(int)
    ready = QtCore.Signal(str)
    failed = QtCore.Signal(str)

    def check(self, silent=False):
        threading.Thread(target=self._check, args=(silent,), daemon=True).start()

    def _check(self, silent):
        try:
            req = urllib.request.Request(
                f"https://api.github.com/repos/{OWNER}/{REPO}/releases/latest",
                headers={"User-Agent": APP_ID, "Accept": "application/vnd.github+json"})
            with urllib.request.urlopen(req, timeout=15) as r:
                data = json.load(r)
            tag = data["tag_name"].lstrip("vV")
            asset = next((a for a in data.get("assets", []) if a["name"] == PORTABLE_ASSET), None)
            if asset and vtuple(tag) > vtuple(VERSION):
                self.available.emit(tag, asset["browser_download_url"], int(asset["size"]))
            elif not silent:
                self.status.emit(f"Up to date (v{VERSION})")
        except Exception as e:  # noqa
            if not silent:
                self.failed.emit(f"Update check failed: {e}")

    def download(self, url, size):
        threading.Thread(target=self._download, args=(url, size), daemon=True).start()

    def _download(self, url, size):
        try:
            if not FROZEN:
                raise RuntimeError("self-update only works in the built .exe")
            dest = Path(sys.executable + ".new")
            req = urllib.request.Request(url, headers={"User-Agent": APP_ID})
            got = 0
            with urllib.request.urlopen(req, timeout=30) as r, open(dest, "wb") as f:
                while True:
                    chunk = r.read(1 << 16)
                    if not chunk:
                        break
                    f.write(chunk)
                    got += len(chunk)
                    if size:
                        self.progress.emit(min(100, int(got * 100 / size)))
            if size and got != size:
                raise IOError("download incomplete")
            with open(dest, "rb") as f:
                if f.read(2) != b"MZ":
                    raise IOError("downloaded file is not a valid .exe")
            self.ready.emit(str(dest))
        except PermissionError:
            self.failed.emit("Can't write to the app's folder. Move the app somewhere you own and retry.")
        except Exception as e:  # noqa
            self.failed.emit(f"Download failed: {e}")


def launch_swap(new_path):
    """Detached helper: wait for this exe to unlock, replace it, relaunch, delete itself."""
    target = sys.executable
    bat = Path(tempfile.gettempdir()) / f"{APP_ID}_update.bat"
    bat.write_text(
        "@echo off\r\n"
        "set n=0\r\n"
        ":retry\r\n"
        f'move /Y "{new_path}" "{target}" >nul 2>&1\r\n'
        "if not errorlevel 1 goto done\r\n"
        "set /a n+=1\r\n"
        "if %n% GEQ 90 exit /b 1\r\n"
        "ping -n 2 127.0.0.1 >nul\r\n"
        "goto retry\r\n"
        ":done\r\n"
        f'start "" "{target}"\r\n'
        'del "%~f0"\r\n')
    env = {k: v for k, v in os.environ.items() if not k.startswith("_PYI") and k != "_MEIPASS2"}
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"  # new instance must not reuse our temp dir
    subprocess.Popen(["cmd", "/c", str(bat)], env=env, cwd=tempfile.gettempdir(),
                     creationflags=0x08000000 | 0x00000200, close_fds=True)


# --------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------
STYLE = """
* { color:#e8eaee; font-family:"Segoe UI",sans-serif; font-size:13px; }
QWidget#root { background:#14161b; }
QLabel, QCheckBox { background:transparent; }
QScrollArea, QScrollArea > QWidget > QWidget { background:transparent; border:none; }

QFrame#header { background:#1a1d24; border-bottom:1px solid #2a2e38; }
QLabel#appTitle { font-size:20px; font-weight:700; }
QLabel#hint { color:#8b93a5; }
QLabel#banner { background:#3a2f12; color:#ffd479; border-radius:8px; padding:8px 12px; }
QLabel#footer { color:#8b93a5; padding:2px 4px; }

QFrame#card { background:#1c1f27; border:1px solid #2b303b; border-radius:12px; }
QFrame#card[isDefault="true"] { border:1px solid #e0b341; }
QFrame#outbox { background:#15181e; border-radius:8px; }
QLabel#caption { color:#8b93a5; font-size:11px; font-weight:700; letter-spacing:1px; }
QLabel#status { color:#ff8a7a; }
QLineEdit#title { background:transparent; border:none; font-size:17px; font-weight:700; padding:0; }
QLineEdit#title:focus { border-bottom:1px solid #5b7cff; }

QPushButton { background:#2a2e38; border:1px solid #3a3f4d; border-radius:6px; padding:5px 12px; }
QPushButton:hover { background:#343947; }
QPushButton:disabled { color:#6b7285; }
QPushButton#primary { background:#4a63c0; border-color:#5b7cff; font-weight:600; }
QPushButton#primary:hover { background:#5873d6; }
QPushButton#defaultBtn { text-align:left; padding:6px 10px; }
QPushButton#defaultBtn[isDefault="true"] { background:#4a3d12; border-color:#e0b341; color:#ffd479; font-weight:600; }
QPushButton#muteBtn { padding:4px 8px; min-width:44px; }
QPushButton#muteBtn:checked { background:#b3412f; border-color:#d9604c; }
QPushButton#close { padding:2px 6px; min-width:22px; background:transparent; border:none; color:#8b93a5; }
QPushButton#close:hover { color:#ff8a7a; }

QComboBox { background:#2a2e38; border:1px solid #3a3f4d; border-radius:6px; padding:4px 8px; }
QComboBox:hover { background:#343947; }
QComboBox QAbstractItemView { background:#14161b; selection-background-color:#3b4a8a; border:1px solid #3a3f4d; }

QListWidget { background:#14161b; border:1px dashed #3a3f4d; border-radius:8px; padding:4px; outline:none; }
QListWidget::item { background:#27325a; border-radius:6px; padding:7px 8px; margin:2px; }
QListWidget::item:selected { background:#4a63c0; }

QProgressBar { background:#101216; border:none; border-radius:3px; }
QProgressBar::chunk { background:#3ecf8e; border-radius:3px; }
QProgressBar#updateBar::chunk { background:#5b7cff; }
QSlider::groove:horizontal { height:6px; background:#2c303a; border-radius:3px; }
QSlider::sub-page:horizontal { background:#5b7cff; border-radius:3px; }
QSlider::handle:horizontal { width:14px; margin:-5px 0; background:#e8eaee; border-radius:7px; }
QMessageBox, QDialog { background:#1c1f27; }
"""


def caption(text):
    lbl = QtWidgets.QLabel(text.upper())
    lbl.setObjectName("caption")
    return lbl


def repolish(w):
    w.style().unpolish(w)
    w.style().polish(w)


def make_combo(items, current, placeholder="- none -"):
    """items: list of (label, data)."""
    cb = QtWidgets.QComboBox()
    cb.setSizeAdjustPolicy(QtWidgets.QComboBox.AdjustToMinimumContentsLengthWithIcon)
    cb.setMinimumContentsLength(16)
    cb.view().setMinimumWidth(380)
    cb.addItem(placeholder, "")
    for label, data in items:
        cb.addItem(label, data)
    if current and cb.findData(current) < 0:
        cb.addItem(f"(missing) {current}", current)
    cb.setCurrentIndex(max(cb.findData(current or ""), 0))
    return cb


def vol_curve(v):
    return (v / 100.0) ** 2


class AppList(QtWidgets.QListWidget):
    dropped = QtCore.Signal(str, str)  # channel_id, process

    def __init__(self, channel_id=""):
        super().__init__()
        self.channel_id = channel_id
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDragDropMode(QtWidgets.QAbstractItemView.DragDrop)
        self.setDefaultDropAction(Qt.CopyAction)
        self.setMinimumHeight(110)

    def startDrag(self, supportedActions):
        item = self.currentItem()
        if item is None:
            return
        md = QtCore.QMimeData()
        md.setText(item.data(Qt.UserRole) or "")
        drag = QtGui.QDrag(self)
        drag.setMimeData(md)
        drag.exec(Qt.CopyAction)  # copy only: Qt never deletes our items

    def dragEnterEvent(self, e):
        if e.mimeData().hasText():
            e.acceptProposedAction()

    def dragMoveEvent(self, e):
        if e.mimeData().hasText():
            e.acceptProposedAction()

    def dropEvent(self, e):
        proc = e.mimeData().text()
        e.setDropAction(Qt.CopyAction)
        e.accept()
        if proc:
            self.dropped.emit(self.channel_id, proc)

    def paintEvent(self, e):
        super().paintEvent(e)
        if self.count() == 0:
            p = QtGui.QPainter(self.viewport())
            p.setPen(QtGui.QColor("#6b7285"))
            p.drawText(self.viewport().rect(), Qt.AlignCenter | Qt.TextWordWrap,
                       "No apps here.\nDrop an app on this box.")


class ChannelCard(QtWidgets.QFrame):
    changed = QtCore.Signal()
    removeRequested = QtCore.Signal(str)
    defaultRequested = QtCore.Signal(str)

    def __init__(self, ch, cables, ins, outs, taken_fn, can_remove):
        super().__init__()
        self.setObjectName("card")
        self.setFixedWidth(330)
        self.ch, self.ins, self.outs, self.taken_fn = ch, ins, outs, taken_fn
        self.engine = Engine()
        self.lvl = 0.0
        real_outs = [n for n in outs if n not in cables and not is_virtual(n)]

        v = QtWidgets.QVBoxLayout(self)
        v.setContentsMargins(14, 14, 14, 14)
        v.setSpacing(7)

        # --- header: name + remove
        top = QtWidgets.QHBoxLayout()
        self.name = QtWidgets.QLineEdit(ch["name"])
        self.name.setObjectName("title")
        self.name.setToolTip("Click to rename")
        self.name.editingFinished.connect(self._cfg)
        close = QtWidgets.QPushButton("✕")
        close.setObjectName("close")
        close.setToolTip("Remove channel")
        close.setEnabled(can_remove)
        close.clicked.connect(lambda: self.removeRequested.emit(self.ch["id"]))
        top.addWidget(self.name, 1)
        top.addWidget(close)
        v.addLayout(top)

        self.default_btn = QtWidgets.QPushButton()
        self.default_btn.setObjectName("defaultBtn")
        self.default_btn.clicked.connect(lambda: self.defaultRequested.emit(self.ch["id"]))
        v.addWidget(self.default_btn)

        # --- virtual cable
        v.addWidget(caption("Virtual cable"))
        self.cable_cb = make_combo([(cable_label(c), c) for c in cables], ch["render"], "- choose a cable -")
        self.cable_cb.currentIndexChanged.connect(self._cable_changed)
        v.addWidget(self.cable_cb)

        # --- apps
        v.addWidget(caption("Apps on this channel"))
        self.apps = AppList(ch["id"])
        v.addWidget(self.apps, 1)

        # --- volume
        v.addWidget(caption("Channel volume"))
        row = QtWidgets.QHBoxLayout()
        self.master = QtWidgets.QSlider(Qt.Horizontal)
        self.master.setRange(0, 100)
        self.master.setValue(ch["vol"])
        self.master.valueChanged.connect(self._gains)
        self.master_lbl = QtWidgets.QLabel()
        self.master_lbl.setMinimumWidth(34)
        self.master_lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        row.addWidget(self.master, 1)
        row.addWidget(self.master_lbl)
        v.addLayout(row)
        self.meter = QtWidgets.QProgressBar()
        self.meter.setRange(0, 100)
        self.meter.setTextVisible(False)
        self.meter.setFixedHeight(6)
        v.addWidget(self.meter)

        # --- two outputs
        v.addWidget(caption("Plays through (both at once)"))
        self.out_cb, self.out_sl, self.out_mute = [], [], []
        for i in (0, 1):
            box = QtWidgets.QFrame()
            box.setObjectName("outbox")
            bl = QtWidgets.QVBoxLayout(box)
            bl.setContentsMargins(8, 8, 8, 8)
            bl.setSpacing(5)
            cb = make_combo([(n, n) for n in real_outs], ch["out"][i], f"Output {i + 1}: - none -")
            cb.currentIndexChanged.connect(self._dev_changed)
            r = QtWidgets.QHBoxLayout()
            sl = QtWidgets.QSlider(Qt.Horizontal)
            sl.setRange(0, 100)
            sl.setValue(ch["ov"][i])
            sl.valueChanged.connect(self._gains)
            m = QtWidgets.QPushButton("Mute")
            m.setObjectName("muteBtn")
            m.setCheckable(True)
            m.setChecked(ch["mute"][i])
            m.toggled.connect(self._gains)
            r.addWidget(sl, 1)
            r.addWidget(m)
            bl.addWidget(cb)
            bl.addLayout(r)
            v.addWidget(box)
            self.out_cb.append(cb)
            self.out_sl.append(sl)
            self.out_mute.append(m)

        self.status = QtWidgets.QLabel("")
        self.status.setObjectName("status")
        self.status.setWordWrap(True)
        v.addWidget(self.status)

        self.set_default(False)
        self._gains()
        self.restart()

    # -- state --------------------------------------------------------------
    def set_default(self, is_default):
        self.setProperty("isDefault", is_default)
        self.default_btn.setProperty("isDefault", is_default)
        self.default_btn.setText("★  Windows default output" if is_default else "☆  Make Windows default output")
        self.default_btn.setToolTip("Everything on your PC plays through this channel unless you move it."
                                    if is_default else "Click to make this the channel Windows plays through.")
        repolish(self)
        repolish(self.default_btn)

    def set_output(self, i, name):
        j = self.out_cb[i].findData(name)
        if j >= 0:
            self.out_cb[i].setCurrentIndex(j)

    def _cfg(self):
        c = self.ch
        c["name"] = self.name.text() or "Channel"
        c["render"] = self.cable_cb.currentData() or ""
        c["capture"] = capture_of(c["render"]) if c["render"] else ""
        c["out"] = [cb.currentData() or "" for cb in self.out_cb]
        c["vol"] = self.master.value()
        c["ov"] = [s.value() for s in self.out_sl]
        c["mute"] = [m.isChecked() for m in self.out_mute]
        self.changed.emit()

    def _gains(self, *_):
        self.engine.master = vol_curve(self.master.value())
        self.master_lbl.setText(f"{self.master.value()}%")
        for i in (0, 1):
            self.engine.gain[i] = vol_curve(self.out_sl[i].value())
            self.engine.mute[i] = self.out_mute[i].isChecked()
        self._cfg()

    def _cable_changed(self, *_):
        new = self.cable_cb.currentData() or ""
        owner = self.taken_fn(new, self.ch["id"]) if new else None
        if owner:
            self.cable_cb.blockSignals(True)
            self.cable_cb.setCurrentIndex(max(self.cable_cb.findData(self.ch["render"] or ""), 0))
            self.cable_cb.blockSignals(False)
            self.status.setText(f"That cable is already used by '{owner}'.")
            return
        self._dev_changed()

    def _dev_changed(self, *_):
        self._cfg()
        self.restart()

    def restart(self):
        self.engine.stop()
        c = self.ch
        if not c["render"]:
            self.status.setText("Choose a virtual cable for this channel.")
            return
        if not any(c["out"]):
            self.status.setText("Choose at least one output.")
            return
        idx_in = self.ins.get(c["capture"])
        if idx_in is None:
            self.status.setText("Cable's recording side not found. Try 'Rescan devices'.")
            return
        idx_out = [self.outs.get(o) if o else None for o in c["out"]]
        err = self.engine.start(idx_in, idx_out)
        self.status.setText(f"Audio error: {err}" if err else "")

    def tick(self):
        self.lvl = max(self.engine.level, self.lvl * 0.85)
        self.engine.level = 0.0
        self.meter.setValue(int(min(1.0, self.lvl) * 100))

    def shutdown(self):
        self.engine.stop()


class Main(QtWidgets.QWidget):
    def __init__(self):
        super().__init__()
        self.setObjectName("root")
        self.setWindowTitle(f"{APP_NAME}  v{VERSION}")
        self.resize(1250, 800)
        self.cfg = self.load()
        self.router = Router()
        self.active, self.applied, self._sig = {}, set(), None
        self.cards = {}
        self._default_applied = None
        self.up_state, self.up_info, self.up_path = "idle", None, None

        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 8)
        root.setSpacing(8)

        # ---- header bar
        header = QtWidgets.QFrame()
        header.setObjectName("header")
        h = QtWidgets.QHBoxLayout(header)
        h.setContentsMargins(18, 12, 18, 12)
        title = QtWidgets.QLabel(APP_NAME)
        title.setObjectName("appTitle")
        ver = QtWidgets.QLabel(f"v{VERSION}")
        ver.setObjectName("hint")
        h.addWidget(title)
        h.addWidget(ver)
        h.addStretch()
        self.up_lbl = QtWidgets.QLabel("")
        self.up_lbl.setObjectName("hint")
        self.up_btn = QtWidgets.QPushButton("Check for updates")
        self.up_btn.clicked.connect(self.on_update_click)
        rescan = QtWidgets.QPushButton("Rescan devices")
        rescan.clicked.connect(lambda: self.rebuild(rescan=True))
        add = QtWidgets.QPushButton("+ Add channel")
        add.setObjectName("primary")
        add.clicked.connect(self.add_channel)
        for w in (self.up_lbl, self.up_btn, rescan, add):
            h.addWidget(w)
        root.addWidget(header)

        self.up_bar = QtWidgets.QProgressBar()
        self.up_bar.setObjectName("updateBar")
        self.up_bar.setRange(0, 100)
        self.up_bar.setFixedHeight(5)
        self.up_bar.setTextVisible(False)
        self.up_bar.hide()
        root.addWidget(self.up_bar)

        # ---- body
        self.banner = QtWidgets.QLabel()
        self.banner.setObjectName("banner")
        self.banner.setWordWrap(True)
        self.banner.setOpenExternalLinks(True)
        self.banner.hide()
        bw = QtWidgets.QHBoxLayout()
        bw.setContentsMargins(18, 0, 18, 0)
        bw.addWidget(self.banner)
        root.addLayout(bw)

        self.scroll = QtWidgets.QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.holder = QtWidgets.QWidget()
        self.row = QtWidgets.QHBoxLayout(self.holder)
        self.row.setContentsMargins(18, 4, 18, 4)
        self.row.setSpacing(14)
        self.row.setAlignment(Qt.AlignLeft)
        self.scroll.setWidget(self.holder)
        root.addWidget(self.scroll, 1)

        foot = QtWidgets.QLabel(
            "★ Windows plays everything through the default channel. Drag an app between channels to route just "
            "that app. Each channel plays to two outputs at once.")
        foot.setObjectName("footer")
        foot.setWordWrap(True)
        fw = QtWidgets.QHBoxLayout()
        fw.setContentsMargins(18, 0, 18, 0)
        fw.addWidget(foot)
        root.addLayout(fw)

        # ---- go
        self.record_original_default()
        self.rebuild()

        self.up = Updater()
        self.up.status.connect(self.up_lbl.setText)
        self.up.available.connect(self.on_update_available)
        self.up.progress.connect(self.up_bar.setValue)
        self.up.ready.connect(self.on_update_ready)
        self.up.failed.connect(self.on_update_failed)
        cleanup_leftovers()
        QtCore.QTimer.singleShot(3000, lambda: self.up.check(silent=True))

        t = QtCore.QTimer(self)
        t.timeout.connect(self.poll)
        t.start(2000)
        m = QtCore.QTimer(self)
        m.timeout.connect(lambda: [c.tick() for c in self.cards.values()])
        m.start(40)
        self.poll()

    # ---- persistence ------------------------------------------------------
    def load(self):
        try:
            cfg = json.loads(CFG_PATH.read_text())
            cfg.setdefault("assign", {})
            return cfg
        except Exception:
            return {"channels": [self.new_channel(n) for n in ("Media", "Game", "Chat")],
                    "assign": {}, "default": "", "orig_default": ""}

    def save(self):
        try:
            APP_DIR.mkdir(parents=True, exist_ok=True)
            CFG_PATH.write_text(json.dumps(self.cfg, indent=2))
        except Exception:
            pass

    @staticmethod
    def new_channel(name):
        return {"id": uuid.uuid4().hex[:8], "name": name, "render": "", "capture": "",
                "out": ["", ""], "vol": 100, "ov": [100, 100], "mute": [False, False]}

    def channel(self, cid):
        return next((c for c in self.cfg["channels"] if c["id"] == cid), None)

    # ---- Windows default output --------------------------------------------
    def record_original_default(self):
        name = default_output_name()
        if name and not is_virtual(name):
            self.cfg["orig_default"] = name  # the user's real output, restored on exit
            self.save()

    def default_channel(self):
        return self.channel(self.cfg.get("default"))

    def apply_default(self):
        ch = self.default_channel()
        # never hijack Windows' output until the channel actually has somewhere to play
        if ch and ch["render"] and any(ch["out"]) and ch["render"] != self._default_applied:
            self._default_applied = ch["render"]
            self.router.async_(self.router.set_default, ch["render"])

    def set_default(self, cid):
        ch = self.channel(cid)
        if not ch or not ch["render"]:
            if cid in self.cards:
                self.cards[cid].status.setText("Choose a virtual cable first.")
            return
        self.cfg["default"] = cid
        self._default_applied = None
        self.save()
        self.apply_default()
        self.refresh_default_badges()
        self._sig = None
        self.refresh_lists()

    def refresh_default_badges(self):
        d = self.cfg.get("default")
        for cid, card in self.cards.items():
            card.set_default(cid == d)

    def restore_windows_output(self):
        """On exit: put Windows (and routed apps) back on the user's real output."""
        orig = self.cfg.get("orig_default")
        if not orig or not self.router.exe:
            return
        jobs = [self.router.async_(self.router.set_default, orig)]
        for proc in list(self.cfg["assign"]):
            jobs.append(self.router.async_(self.router.set_app, orig, proc))
        for j in jobs:
            j.join(timeout=6)

    # ---- channels ------------------------------------------------------------
    def auto_setup(self, cables, real_outs):
        used = {c["render"] for c in self.cfg["channels"] if c["render"]}
        free = [c for c in cables if c not in used]
        orig = self.cfg.get("orig_default", "")
        for ch in self.cfg["channels"]:
            if not ch["render"] and free:
                ch["render"] = free.pop(0)
                ch["capture"] = capture_of(ch["render"])
            if not any(ch["out"]) and orig in real_outs:
                ch["out"][0] = orig  # sensible start: play to your normal output
        if not self.channel(self.cfg.get("default")) and self.cfg["channels"]:
            self.cfg["default"] = self.cfg["channels"][0]["id"]
        self.save()

    def taken_by(self, cable, my_id):
        for c in self.cfg["channels"]:
            if c["id"] != my_id and c["render"] == cable:
                return c["name"]
        return None

    def rebuild(self, rescan=False):
        for c in self.cards.values():
            c.shutdown()
        if rescan:
            try:
                sd._terminate()
                sd._initialize()
            except Exception:
                pass
        for c in self.cards.values():
            c.setParent(None)
        while self.row.count():
            it = self.row.takeAt(0)
            if it.widget():
                it.widget().setParent(None)
        self.cards = {}

        ins, outs = wasapi_devices()
        cables = find_cables(ins, outs)
        real_outs = [n for n in outs if n not in cables and not is_virtual(n)]
        self.auto_setup(cables, real_outs)

        n = len(self.cfg["channels"])
        for ch in self.cfg["channels"]:
            card = ChannelCard(ch, cables, ins, outs, self.taken_by, can_remove=n > 1)
            card.changed.connect(self.save)
            card.changed.connect(self.apply_default)
            card.removeRequested.connect(self.remove_channel)
            card.defaultRequested.connect(self.set_default)
            card.apps.dropped.connect(self.on_drop)
            self.cards[ch["id"]] = card
            self.row.addWidget(card)
        self.refresh_default_badges()

        msgs = []
        if not self.router.exe:
            msgs.append("SoundVolumeView.exe was not found, so routing apps and setting the Windows default "
                        "will not work.")
        if not cables:
            msgs.append('No virtual cables found. Install VB-Cable from '
                        '<a style="color:#ffd479" href="https://vb-audio.com/Cable/">vb-audio.com/Cable</a>, '
                        'restart your PC, then click "Rescan devices".')
        elif len(cables) < n:
            msgs.append(f"You have {len(cables)} virtual cable(s) but {n} channels. Each channel needs its own "
                        "cable (VB-Cable A+B / C+D packs add more), or remove a channel.")
        self.banner.setText("<br>".join(msgs))
        self.banner.setVisible(bool(msgs))

        self._default_applied = None
        self.apply_default()
        self._sig = None
        self.refresh_lists()

    def add_channel(self):
        self.cfg["channels"].append(self.new_channel(f"Channel {len(self.cfg['channels']) + 1}"))
        self.save()
        self.rebuild()

    def remove_channel(self, cid):
        if len(self.cfg["channels"]) <= 1:
            return
        self.cfg["channels"] = [c for c in self.cfg["channels"] if c["id"] != cid]
        if self.cfg.get("default") == cid:
            self.cfg["default"] = self.cfg["channels"][0]["id"]
            self._default_applied = None
        dflt = self.default_channel()
        for proc, c in list(self.cfg["assign"].items()):
            if c == cid:
                self.cfg["assign"].pop(proc)
                if dflt and dflt["render"]:
                    self.router.async_(self.router.set_app, dflt["render"], proc)
        self.save()
        self.rebuild()

    # ---- drag & drop / app routing ---------------------------------------------
    def on_drop(self, cid, proc):
        ch = self.channel(cid)
        if not ch:
            return
        if not ch["render"]:
            self.cards[cid].status.setText("Choose a virtual cable first.")
            return
        self.cfg["assign"][proc] = cid
        self.applied = {k for k in self.applied if k[0] != proc}
        self.save()
        self.apply_assignments()
        self._sig = None
        QtCore.QTimer.singleShot(250, self.refresh_lists)  # after the drag has fully ended

    def apply_assignments(self):
        for proc, pids in self.active.items():
            ch = self.channel(self.cfg["assign"].get(proc))
            if not ch or not ch["render"]:
                continue
            keys = {(proc, pid, ch["render"]) for pid in pids}
            if keys - self.applied:
                self.applied |= keys
                self.router.async_(self.router.set_app, ch["render"], proc)

    def poll(self):
        try:
            self.active = list_audio_apps()
        except Exception:
            self.active = {}
        self.apply_assignments()
        self.refresh_lists()

    def refresh_lists(self):
        if QtWidgets.QApplication.mouseButtons() & Qt.LeftButton:
            return  # never rebuild lists in the middle of a drag
        dflt = self.cfg.get("default")
        groups = {c["id"]: [] for c in self.cfg["channels"]}
        for proc in sorted(set(self.active) | set(self.cfg["assign"]), key=str.lower):
            cid = self.cfg["assign"].get(proc)
            if cid not in groups:
                self.cfg["assign"].pop(proc, None)
                cid = dflt
            if cid not in groups:
                continue
            if proc not in self.active and cid == dflt and proc not in self.cfg["assign"]:
                continue
            groups[cid].append(proc)
        sig = (tuple((k, tuple(v)) for k, v in groups.items()), tuple(sorted(self.active)))
        if sig == self._sig:
            return
        self._sig = sig
        for cid, procs in groups.items():
            w = self.cards[cid].apps
            w.clear()
            for p in procs:
                idle = p not in self.active
                it = QtWidgets.QListWidgetItem(p.replace(".exe", "") + ("   (idle)" if idle else ""))
                it.setData(Qt.UserRole, p)
                if idle:
                    it.setForeground(QtGui.QColor("#8b93a5"))
                w.addItem(it)

    # ---- updates -----------------------------------------------------------------
    def on_update_click(self):
        if self.up_state == "available":
            self.up_state = "downloading"
            self.up_btn.setEnabled(False)
            self.up_btn.setText("Downloading...")
            self.up_bar.setValue(0)
            self.up_bar.show()
            self.up.download(self.up_info[1], self.up_info[2])
        elif self.up_state == "ready":
            self.finish_update()
        elif self.up_state == "idle":
            self.up_lbl.setText("Checking...")
            self.up.check()

    def on_update_available(self, ver, url, size):
        self.up_state, self.up_info = "available", (ver, url, size)
        self.up_btn.setText(f"Download && install v{ver}")
        self.up_btn.setObjectName("primary")
        repolish(self.up_btn)
        self.up_lbl.setText(f"Update v{ver} available")

    def on_update_ready(self, path):
        self.up_state, self.up_path = "ready", path
        self.up_bar.hide()
        self.up_btn.setEnabled(True)
        self.up_btn.setText("Restart to finish update")
        self.up_lbl.setText("Update downloaded")
        r = QtWidgets.QMessageBox.question(
            self, "Update ready",
            f"v{self.up_info[0]} is downloaded.\nRestart now to finish? Audio pauses for a few seconds.")
        if r == QtWidgets.QMessageBox.Yes:
            self.finish_update()

    def on_update_failed(self, msg):
        self.up_state = "idle"
        self.up_bar.hide()
        self.up_btn.setEnabled(True)
        self.up_btn.setText("Check for updates")
        self.up_lbl.setText(msg)

    def finish_update(self):
        try:
            launch_swap(self.up_path)
        except Exception as e:  # noqa
            self.on_update_failed(f"Couldn't start update: {e}")
            return
        self.close()
        QtWidgets.QApplication.quit()

    def closeEvent(self, e):
        for c in self.cards.values():
            c.shutdown()
        self.save()
        self.restore_windows_output()
        super().closeEvent(e)


def main():
    APP_DIR.mkdir(parents=True, exist_ok=True)
    log = open(APP_DIR / "crash.log", "a", buffering=1)
    faulthandler.enable(file=log)

    def hook(et, ev, tb):
        log.write("".join(traceback.format_exception(et, ev, tb)) + "\n")

    sys.excepthook = hook
    app = QtWidgets.QApplication(sys.argv)
    app.setStyleSheet(STYLE)
    w = Main()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
