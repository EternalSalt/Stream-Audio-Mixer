"""
Salty's Stream Mixer - a Sonar-style audio mixer for Windows 10/11.

* Each channel (Game, Chat, Media, Aux...) is a virtual audio cable that apps play into.
* Two mixes: Personal Mix and Stream Mix. Each plays to the output device you choose.
* Every channel has a fader + on/off + mute for each mix. The Mic channel feeds the mixes too.
* The first channel is the Windows default output; drag apps between channels to route them.
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
import time
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
LOG_PATH = APP_DIR / "app.log"


def log(msg):
    """Breadcrumbs for diagnosing problems: %APPDATA%\\SaltysStreamMixer\\app.log"""
    try:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(time.strftime("%H:%M:%S ") + str(msg) + "\n")
    except Exception:
        pass

FROZEN = bool(getattr(sys, "frozen", False))
if FROZEN:
    HERE = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))  # bundled files
    EXE_DIR = Path(sys.executable).resolve().parent                       # next to the .exe
else:
    HERE = EXE_DIR = Path(__file__).resolve().parent


# --------------------------------------------------------------------------
# Audio engine: channels (capture) -> two mix buses (output devices)
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

    def clear(self):
        with self.lock:
            self.r = self.w = self.n = 0

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


class ChannelAudio:
    """One input (virtual cable or mic) feeding both mix buses."""

    def __init__(self):
        self.rings = [Ring(), Ring()]
        self.on = [True, True]
        self.gain = [1.0, 1.0]
        self.mute = [False, False]
        self.stream = None

    def start(self, in_idx):
        self.stop()
        ws = sd.WasapiSettings(auto_convert=True)
        last = "unknown error"
        for nch in (2, 1):
            def cb(indata, frames, t, status, nch=nch):
                x = np.repeat(indata, 2, axis=1) if nch == 1 else indata
                for i in (0, 1):
                    if self.on[i]:
                        self.rings[i].write(x)
            try:
                s = sd.InputStream(device=in_idx, channels=nch, samplerate=SR, blocksize=BLOCK,
                                   dtype="float32", callback=cb, extra_settings=ws)
                s.start()
                self.stream = s
                return None
            except Exception as e:  # noqa
                last = str(e)
        return last

    def stop(self):
        if self.stream:
            try:
                self.stream.stop()
                self.stream.close()
            except Exception:
                pass
        self.stream = None


class Bus:
    """A mix (Personal / Stream): sums every channel and plays it to one output device."""

    def __init__(self, i):
        self.i = i
        self.channels = []
        self.master = 1.0
        self.level = 0.0
        self.stream = None

    def start(self, dev_idx):
        self.stop()
        if dev_idx is None:
            return None
        i = self.i

        def cb(outdata, frames, t, status):
            acc = np.zeros((frames, 2), np.float32)
            for c in self.channels:
                if c.on[i]:
                    d = c.rings[i].read(frames)
                    if not c.mute[i]:
                        acc += d * c.gain[i]
            acc *= self.master
            self.level = max(self.level, float(np.abs(acc).max()))
            np.clip(acc, -1.0, 1.0, out=acc)
            outdata[:] = acc

        try:
            s = sd.OutputStream(device=dev_idx, channels=2, samplerate=SR, blocksize=BLOCK,
                                dtype="float32", callback=cb, extra_settings=sd.WasapiSettings(auto_convert=True))
            s.start()
            self.stream = s
            return None
        except Exception as e:  # noqa
            return str(e)

    def stop(self):
        if self.stream:
            try:
                self.stream.stop()
                self.stream.close()
            except Exception:
                pass
        self.stream = None


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
        self.defaults = set()
        self.lock = threading.Lock()

    def _run(self, *args):
        flags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
        return subprocess.run([self.exe, *args], capture_output=True, timeout=20, creationflags=flags)

    def refresh(self):
        tmp = tempfile.mktemp(suffix=".csv")
        try:
            self._run("/scomma", tmp)
            with open(tmp, newline="", encoding="utf-8-sig", errors="replace") as f:
                ids, defaults = {}, set()
                for row in csv.DictReader(f):
                    if row.get("Type") == "Device" and row.get("Direction") == "Render":
                        key = next((k for k in row if "Friendly ID" in k), None)
                        if key:
                            ids[row["Name"]] = row[key]
                        if (row.get("Default") or "").strip():
                            defaults.add(row["Name"])
                self.ids, self.defaults = ids, defaults
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
            try:
                args = ("/SetAppDefault", self.resolve(sd_name), "all", proc)
                r = self._run(*args)
                log(f"SVV {args} rc={r.returncode} {r.stderr[:150]!r}")
            except Exception as e:  # noqa
                log(f"SVV set_app failed: {e}")

    def set_default(self, sd_name):
        if self.exe:
            try:
                args = ("/SetDefault", self.resolve(sd_name), "all")
                r = self._run(*args)
                log(f"SVV {args} rc={r.returncode} {r.stderr[:150]!r}")
            except Exception as e:  # noqa
                log(f"SVV set_default failed: {e}")

    def default_render_names(self):
        """Names of render devices Windows currently uses as default (empty = unknown)."""
        if not self.exe:
            return set()
        with self.lock:
            self.refresh()
        return set(self.defaults)

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
QWidget#root { background:#1a1d24; }
QLabel, QCheckBox { background:transparent; }
QScrollArea, QScrollArea > QWidget > QWidget { background:transparent; border:none; }

QFrame#defaultBar { background:#2b303a; border-bottom:1px solid #1a1d24; }
QLabel#defaultIcon { background:#e5484d; font-size:20px; }
QLabel#defaultText { font-size:12px; }
QPushButton#linkBtn { background:transparent; border:none; padding:0; text-decoration:underline; font-size:13px; }
QPushButton#linkBtn:hover { color:#9db2ff; }
QFrame#master { background:#14171d; border-right:1px solid #262b34; }
QLabel#masterTitle { font-size:20px; font-weight:800; color:#5b7cff; }
QLabel#appName { color:#8b93a5; }
QLabel#caption { color:#8b93a5; font-size:11px; font-weight:700; letter-spacing:1px; }
QLabel#hint { color:#8b93a5; font-size:12px; }
QLabel#err { color:#ff8a7a; font-size:12px; }
QLabel#banner { background:#3a2f12; color:#ffd479; border-radius:8px; padding:8px 10px; font-size:12px; }

QFrame#channel { background:#252a33; border-right:1px solid #1a1d24; }
QFrame#chHead { background:#1f232b; }
QLabel#chName { font-size:14px; font-weight:800; letter-spacing:1px; }
QLabel#tag { color:#e0b341; font-size:10px; font-weight:700; }
QLabel#note { color:#ff8a7a; font-size:11px; }

QPushButton { background:#2a2e38; border:1px solid #3a3f4d; border-radius:6px; padding:5px 12px; }
QPushButton:hover { background:#343947; }
QPushButton:disabled { color:#6b7285; }
QPushButton#primary { background:#4a63c0; border-color:#5b7cff; font-weight:600; }
QPushButton#primary:hover { background:#5873d6; }
QPushButton#gear { background:#2b303a; border:none; border-radius:13px; padding:0; min-width:26px; max-width:26px; min-height:26px; max-height:26px; }
QPushButton#gear:hover { background:#3a4050; }
QPushButton#dot { background:#555b69; border:none; border-radius:6px; padding:0; min-width:12px; max-width:12px; min-height:12px; max-height:12px; }
QPushButton#muteBtn { padding:0; background:#2b303a; border:1px solid #3a3f4d; border-radius:5px; }
QPushButton#muteBtn:checked { background:#b3412f; border-color:#d9604c; }
QPushButton#addCh { background:transparent; border:2px dashed #353b48; border-radius:10px; color:#8b93a5; padding:0; }
QPushButton#addCh:hover { border-color:#5b7cff; color:#e8eaee; }

QComboBox { background:#262b34; border:1px solid #3a3f4d; border-radius:6px; padding:5px 8px; }
QComboBox:hover { background:#2f3540; }
QComboBox[needs="true"] { border:1px solid #e5484d; }
QComboBox QAbstractItemView { background:#14171d; selection-background-color:#3b4a8a; border:1px solid #3a3f4d; }
QMenu { background:#1c1f27; border:1px solid #3a3f4d; padding:4px; }
QMenu::item { padding:6px 18px; border-radius:4px; }
QMenu::item:selected { background:#3b4a8a; }
QMenu::item:disabled { color:#6b7285; }

QListWidget { background:#1a1d24; border:1px solid #1a1d24; border-radius:6px; padding:3px; outline:none; }
QListWidget::item { background:#323a55; border-radius:5px; padding:6px 8px; margin:2px; }
QListWidget::item:selected { background:#4a63c0; }

QProgressBar { background:#0f1115; border:none; border-radius:2px; }
QProgressBar::chunk { background:#3ecf8e; border-radius:2px; }
QProgressBar#updateBar::chunk { background:#5b7cff; }
QSlider::groove:horizontal { height:6px; background:#0f1115; border-radius:3px; }
QSlider::sub-page:horizontal { background:#5b7cff; border-radius:3px; }
QSlider::handle:horizontal { width:14px; margin:-5px 0; background:#f1f2f5; border-radius:7px; }
QSlider::groove:vertical { width:6px; background:#12151a; border-radius:3px; }
QSlider::sub-page:vertical { background:#12151a; border-radius:3px; }
QSlider::add-page:vertical { background:#5b7cff; border-radius:3px; }
QSlider::handle:vertical { height:18px; width:26px; margin:0 -10px; background:#f4f5f7; border-radius:4px; }
QMessageBox, QDialog { background:#1c1f27; }
"""

DEFAULT_CHANNELS = [  # name, icon, color, kind
    ("Game", "🎮", "#19d3a2", "cable"),
    ("Chat", "💬", "#2ea8ff", "cable"),
    ("Media", "🎵", "#ff3d8b", "cable"),
    ("Aux", "🔷", "#8b5cf6", "cable"),
    ("Mic", "🎤", "#f5a524", "mic"),
]
EXTRA_COLORS = ["#e0b341", "#4ad0ff", "#ff7a59", "#9be564"]
SCHEMA = 2


def caption(text):
    lbl = QtWidgets.QLabel(text.upper())
    lbl.setObjectName("caption")
    return lbl


def repolish(w):
    w.style().unpolish(w)
    w.style().polish(w)


def fill_combo(cb, items, current, placeholder):
    """items: list of (label, data). Keeps a (missing) entry if the saved device is gone."""
    cb.blockSignals(True)
    cb.clear()
    cb.addItem(placeholder, "")
    for label, data in items:
        cb.addItem(label, data)
    if current and cb.findData(current) < 0:
        cb.addItem(f"(missing) {current}", current)
    cb.setCurrentIndex(max(cb.findData(current or ""), 0))
    cb.blockSignals(False)


def new_combo():
    cb = QtWidgets.QComboBox()
    cb.setSizeAdjustPolicy(QtWidgets.QComboBox.AdjustToMinimumContentsLengthWithIcon)
    cb.setMinimumContentsLength(14)
    cb.view().setMinimumWidth(380)
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
        self.setMinimumHeight(90)

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
            p.drawText(self.viewport().rect(), Qt.AlignCenter | Qt.TextWordWrap, "Drop apps here")


class ChannelWidget(QtWidgets.QFrame):
    changed = QtCore.Signal()
    removeRequested = QtCore.Signal(str)

    def __init__(self, ch, cables, ins, taken_fn, can_remove):
        super().__init__()
        self.setObjectName("channel")
        self.setFixedWidth(178)
        self.ch, self.cables, self.ins = ch, cables, ins
        self.taken_fn, self.can_remove = taken_fn, can_remove
        self.audio = ChannelAudio()
        col = ch["color"]
        self.setStyleSheet(f"QLabel#chName{{color:{col};}} QSlider::add-page:vertical{{background:{col};}} "
                           f"QPushButton#dot:checked{{background:{col};}}")

        v = QtWidgets.QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 10)
        v.setSpacing(6)

        # header
        head = QtWidgets.QFrame()
        head.setObjectName("chHead")
        hl = QtWidgets.QHBoxLayout(head)
        hl.setContentsMargins(12, 10, 10, 10)
        self.title = QtWidgets.QLabel()
        self.title.setObjectName("chName")
        gear = QtWidgets.QPushButton("⚙")
        gear.setObjectName("gear")
        gear.setToolTip("Channel settings")
        gear.clicked.connect(self._gear)
        hl.addWidget(self.title, 1)
        hl.addWidget(gear)
        v.addWidget(head)

        self.info = QtWidgets.QLabel("")
        self.info.setWordWrap(True)
        self.info.setAlignment(Qt.AlignCenter)
        self.info.setFixedHeight(34)
        v.addWidget(self.info)
        self.is_default, self.note_text = False, ""

        # two fader columns: Personal / Stream
        faders = QtWidgets.QHBoxLayout()
        faders.setContentsMargins(8, 0, 8, 0)
        self.dot, self.sl, self.mute, self.icon = [], [], [], []
        for i, (sym, tip) in enumerate((("🎧", "Personal Mix"), ("📡", "Stream Mix"))):
            c = QtWidgets.QVBoxLayout()
            c.setSpacing(7)
            ic = QtWidgets.QLabel(sym)
            ic.setAlignment(Qt.AlignCenter)
            ic.setStyleSheet("font-size:18px;")
            ic.setToolTip(tip)
            dot = QtWidgets.QPushButton()
            dot.setObjectName("dot")
            dot.setCheckable(True)
            dot.setChecked(ch["on"][i])
            dot.setToolTip(f"Send this channel to {tip} (on/off)")
            sl = QtWidgets.QSlider(Qt.Vertical)
            sl.setRange(0, 100)
            sl.setValue(ch["vol"][i])
            sl.setMinimumHeight(190)
            sl.setToolTip(f"{tip} volume")
            mu = QtWidgets.QPushButton("🔊")
            mu.setObjectName("muteBtn")
            mu.setCheckable(True)
            mu.setChecked(ch["mute"][i])
            mu.setFixedSize(32, 26)
            mu.setToolTip(f"Mute in {tip}")
            dot.toggled.connect(lambda on, i=i: self._dot(i, on))
            sl.valueChanged.connect(self._gains)
            mu.toggled.connect(self._gains)
            c.addWidget(ic, 0, Qt.AlignHCenter)
            c.addWidget(dot, 0, Qt.AlignHCenter)
            c.addWidget(sl, 1, Qt.AlignHCenter)
            c.addWidget(mu, 0, Qt.AlignHCenter)
            faders.addLayout(c, 1)
            self.dot.append(dot)
            self.sl.append(sl)
            self.mute.append(mu)
            self.icon.append(ic)
        v.addLayout(faders, 1)

        # apps
        if ch["type"] == "cable":
            ap = QtWidgets.QLabel("Apps")
            ap.setObjectName("hint")
            ap.setContentsMargins(10, 0, 0, 0)
            v.addWidget(ap)
            self.apps = AppList(ch["id"])
            lw = QtWidgets.QHBoxLayout()
            lw.setContentsMargins(8, 0, 8, 0)
            lw.addWidget(self.apps)
            v.addLayout(lw, 1)
        else:
            self.apps = None
            ap = QtWidgets.QLabel("Input")
            ap.setObjectName("hint")
            ap.setContentsMargins(10, 0, 0, 0)
            v.addWidget(ap)
            hint = QtWidgets.QLabel("Your microphone.\nChoose the device\nin Master.")
            hint.setObjectName("hint")
            hint.setAlignment(Qt.AlignCenter)
            lw = QtWidgets.QHBoxLayout()
            lw.setContentsMargins(8, 0, 8, 0)
            lw.addWidget(hint)
            v.addLayout(lw, 1)

        self.set_default(False)
        self._refresh()

    # ---- state -----------------------------------------------------------
    def _refresh(self):
        c = self.ch
        self.title.setText(f"{c['icon']}  {c['name'].upper()}")
        for i in (0, 1):
            self.mute[i].setText("🔇" if self.mute[i].isChecked() else "🔊")
            self.icon[i].setEnabled(self.dot[i].isChecked())
            self.audio.on[i] = self.dot[i].isChecked()
            self.audio.gain[i] = vol_curve(self.sl[i].value())
            self.audio.mute[i] = self.mute[i].isChecked()

    def set_default(self, is_default):
        self.is_default = is_default
        self._info()

    def _info(self):
        if self.note_text:
            self.info.setStyleSheet("color:#ff8a7a; font-size:11px;")
            self.info.setText(self.note_text)
        elif self.is_default:
            self.info.setStyleSheet("color:#e0b341; font-size:10px; font-weight:700;")
            self.info.setText("★ WINDOWS DEFAULT")
        else:
            self.info.setText("")

    def _cfg(self):
        c = self.ch
        c["on"] = [d.isChecked() for d in self.dot]
        c["vol"] = [s.value() for s in self.sl]
        c["mute"] = [m.isChecked() for m in self.mute]
        self.changed.emit()

    def _gains(self, *_):
        self._refresh()
        self._cfg()

    def _dot(self, i, on):
        if not on:
            self.audio.rings[i].clear()
        self._gains()

    def set_note(self, text):
        self.note_text = text.replace("\n", " ")
        self._info()

    def restart(self, mic_name=""):
        self.audio.stop()
        c = self.ch
        if c["type"] == "mic":
            if not mic_name:
                self.set_note("Choose a mic\nin Master")
                return
            idx = self.ins.get(mic_name)
            if idx is None:
                self.set_note("Mic not found.\nTry Rescan.")
                return
        else:
            if not c["render"]:
                self.set_note("No virtual cable.\nUse ⚙ to pick one.")
                return
            idx = self.ins.get(capture_of(c["render"]))
            if idx is None:
                self.set_note("Cable not found.\nTry Rescan.")
                return
        err = self.audio.start(idx)
        log(f"channel {c['name']}: {'ERROR ' + str(err) if err else 'capture ok'}")
        self.set_note(f"Audio error:\n{err}" if err else "")

    def shutdown(self):
        self.audio.stop()

    # ---- ⚙ menu ------------------------------------------------------------
    def _gear(self):
        c = self.ch
        m = QtWidgets.QMenu(self)
        rename = m.addAction("Rename...")
        cable_actions = {}
        if c["type"] == "cable":
            sub = m.addMenu("Virtual cable")
            none = sub.addAction("(none)")
            none.setCheckable(True)
            none.setChecked(not c["render"])
            cable_actions[none] = ""
            for cab in self.cables:
                owner = self.taken_fn(cab, c["id"])
                a = sub.addAction(cable_label(cab) + (f"  (used by {owner})" if owner else ""))
                a.setCheckable(True)
                a.setChecked(cab == c["render"])
                a.setEnabled(not owner)
                cable_actions[a] = cab
            if not self.cables:
                sub.addAction("No cables found").setEnabled(False)
        m.addSeparator()
        remove = m.addAction("Remove channel")
        remove.setEnabled(self.can_remove)
        act = m.exec(QtGui.QCursor.pos())
        if act is None:
            return
        if act == rename:
            text, ok = QtWidgets.QInputDialog.getText(self, "Rename channel", "Name:", text=c["name"])
            if ok and text.strip():
                c["name"] = text.strip()
                self._refresh()
                self.changed.emit()
        elif act == remove:
            self.removeRequested.emit(c["id"])
        elif act in cable_actions:
            c["render"] = cable_actions[act]
            c["capture"] = capture_of(c["render"]) if c["render"] else ""
            self.restart()
            self.changed.emit()


class Main(QtWidgets.QWidget):
    def __init__(self):
        super().__init__()
        self.setObjectName("root")
        self.setWindowTitle(f"{APP_NAME}  v{VERSION}")
        self.resize(1280, 700)
        self.cfg = self.load()
        self.router = Router()
        self.active, self.applied, self._sig = {}, set(), None
        self.cards = {}
        self.buses = [Bus(0), Bus(1)]
        self._default_applied = None
        self.up_state, self.up_info, self.up_path = "idle", None, None

        top = QtWidgets.QVBoxLayout(self)
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(0)
        self.default_bar = self.build_default_bar()
        top.addWidget(self.default_bar)
        self._default_ok, self._checking, self._tick = None, False, 0
        outer = QtWidgets.QHBoxLayout()
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        top.addLayout(outer, 1)
        outer.addWidget(self.build_master())

        self.scroll = QtWidgets.QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.holder = QtWidgets.QWidget()
        self.row = QtWidgets.QHBoxLayout(self.holder)
        self.row.setContentsMargins(0, 0, 0, 0)
        self.row.setSpacing(0)
        self.row.setAlignment(Qt.AlignLeft)
        self.scroll.setWidget(self.holder)
        outer.addWidget(self.scroll, 1)

        self.record_original_default()
        self.scan()
        self.populate_master()
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
        m.timeout.connect(self.tick_meters)
        m.start(40)
        self.poll()

    # ---- "set Windows defaults" bar (like Sonar's) --------------------------------
    def build_default_bar(self):
        bar = QtWidgets.QFrame()
        bar.setObjectName("defaultBar")
        h = QtWidgets.QHBoxLayout(bar)
        h.setContentsMargins(0, 0, 14, 0)
        h.setSpacing(12)
        icon = QtWidgets.QLabel("🔇")
        icon.setObjectName("defaultIcon")
        icon.setFixedSize(48, 44)
        icon.setAlignment(Qt.AlignCenter)
        self.bar_text = QtWidgets.QLabel()
        self.bar_text.setObjectName("defaultText")
        self.bar_btn = QtWidgets.QPushButton("Click to auto set defaults")
        self.bar_btn.setObjectName("linkBtn")
        self.bar_btn.setCursor(Qt.PointingHandCursor)
        self.bar_btn.clicked.connect(self.force_default)
        h.addWidget(icon)
        h.addWidget(self.bar_text)
        h.addWidget(self.bar_btn)
        h.addStretch(1)
        bar.hide()
        return bar

    def update_default_bar(self):
        ch = self.default_channel()
        text, link = "", False
        if self.router.exe and ch is not None:
            if not (self.cfg["personal"] or self.cfg["stream"]):
                text = "Choose a Personal Mix device in Master, then Windows can use this mixer as its output."
            elif not ch["render"]:
                text = "The Windows default channel has no virtual cable. Use its ⚙ menu to pick one."
            elif self._default_ok is False:
                text = "Set Salty's Stream Mixer as your default audio output in your Windows sound settings."
                link = True
        self.bar_text.setText(text)
        self.bar_btn.setVisible(link)
        self.default_bar.setVisible(bool(text))

    def force_default(self):
        self.bar_text.setText("Setting Windows defaults...")
        self.bar_btn.hide()
        self._default_ok = None
        self.apply_default(force=True)

    def _check_default(self):
        try:
            ch = self.default_channel()
            names = self.router.default_render_names() if (ch and ch["render"]) else set()
            base = ch["render"].split(" (")[0].strip() if ch and ch["render"] else ""
            self._default_ok = (base in names) if names else None
        except Exception as e:  # noqa
            log(f"default check failed: {e}")
        finally:
            self._checking = False

    # ---- master panel ---------------------------------------------------------
    def build_master(self):
        mp = QtWidgets.QFrame()
        mp.setObjectName("master")
        mp.setFixedWidth(270)
        v = QtWidgets.QVBoxLayout(mp)
        v.setContentsMargins(16, 16, 16, 14)
        v.setSpacing(6)

        t = QtWidgets.QLabel("🎚  MASTER")
        t.setObjectName("masterTitle")
        v.addWidget(t)
        nm = QtWidgets.QLabel(f"{APP_NAME}  ·  v{VERSION}")
        nm.setObjectName("appName")
        v.addWidget(nm)
        v.addSpacing(10)

        v.addWidget(caption("Devices"))
        self.dev = {}
        for key, label, ph in (("personal", "🎧  Personal Mix", "Select Personal Mix"),
                               ("stream", "📡  Stream Mix", "Select Stream Mix (optional)"),
                               ("mic", "🎤  Mic Input", "Select Mic Input")):
            lbl = QtWidgets.QLabel(label)
            lbl.setObjectName("hint")
            v.addWidget(lbl)
            cb = new_combo()
            cb.currentIndexChanged.connect(self.on_master_changed)
            v.addWidget(cb)
            entry = {"cb": cb, "ph": ph}
            if key in ("personal", "stream"):
                r = QtWidgets.QHBoxLayout()
                sl = QtWidgets.QSlider(Qt.Horizontal)
                sl.setRange(0, 100)
                sl.setValue(self.cfg["pvol" if key == "personal" else "svol"])
                sl.valueChanged.connect(self.on_master_changed)
                pct = QtWidgets.QLabel()
                pct.setMinimumWidth(36)
                pct.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
                r.addWidget(sl, 1)
                r.addWidget(pct)
                v.addLayout(r)
                meter = QtWidgets.QProgressBar()
                meter.setRange(0, 100)
                meter.setTextVisible(False)
                meter.setFixedHeight(4)
                v.addWidget(meter)
                entry.update(sl=sl, pct=pct, meter=meter, lvl=0.0)
            err = QtWidgets.QLabel("")
            err.setObjectName("err")
            err.setWordWrap(True)
            err.hide()
            v.addWidget(err)
            entry["err"] = err
            self.dev[key] = entry
            v.addSpacing(6)

        v.addWidget(caption("Windows output"))
        hint = QtWidgets.QLabel("Everything on your PC plays through this channel unless you move it.")
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        v.addWidget(hint)
        self.dflt_cb = new_combo()
        self.dflt_cb.currentIndexChanged.connect(self.on_default_picked)
        v.addWidget(self.dflt_cb)

        v.addStretch(1)
        self.banner = QtWidgets.QLabel()
        self.banner.setObjectName("banner")
        self.banner.setWordWrap(True)
        self.banner.setOpenExternalLinks(True)
        self.banner.hide()
        v.addWidget(self.banner)

        rescan = QtWidgets.QPushButton("Rescan devices")
        rescan.clicked.connect(lambda: self.rebuild(rescan=True))
        v.addWidget(rescan)
        self.up_btn = QtWidgets.QPushButton("Check for updates")
        self.up_btn.clicked.connect(self.on_update_click)
        v.addWidget(self.up_btn)
        self.up_bar = QtWidgets.QProgressBar()
        self.up_bar.setObjectName("updateBar")
        self.up_bar.setRange(0, 100)
        self.up_bar.setFixedHeight(5)
        self.up_bar.setTextVisible(False)
        self.up_bar.hide()
        v.addWidget(self.up_bar)
        self.up_lbl = QtWidgets.QLabel("")
        self.up_lbl.setObjectName("hint")
        self.up_lbl.setWordWrap(True)
        v.addWidget(self.up_lbl)
        return mp

    def scan(self):
        self.ins, self.outs = wasapi_devices()
        self.cables = find_cables(self.ins, self.outs)
        self.real_outs = [n for n in self.outs if n not in self.cables and not is_virtual(n)]
        self.real_ins = [n for n in self.ins if not is_virtual(n)]

    def populate_master(self):
        for key, lst in (("personal", self.real_outs), ("stream", self.real_outs), ("mic", self.real_ins)):
            e = self.dev[key]
            fill_combo(e["cb"], [(n, n) for n in lst], self.cfg[key], e["ph"])
        self.update_master_labels()

    def update_master_labels(self):
        for key, vk in (("personal", "pvol"), ("stream", "svol")):
            e = self.dev[key]
            e["pct"].setText(f"{e['sl'].value()}%")
        pc = self.dev["personal"]["cb"]
        pc.setProperty("needs", not self.cfg["personal"])
        repolish(pc)

    def on_master_changed(self, *_):
        for key in ("personal", "stream", "mic"):
            self.cfg[key] = self.dev[key]["cb"].currentData() or ""
        self.cfg["pvol"] = self.dev["personal"]["sl"].value()
        self.cfg["svol"] = self.dev["stream"]["sl"].value()
        self.save()
        self.update_master_labels()
        self.start_buses()
        for c in self.cards.values():
            if c.ch["type"] == "mic":
                c.restart(self.cfg["mic"])
        self.apply_default()

    def start_buses(self):
        for i, key in enumerate(("personal", "stream")):
            b = self.buses[i]
            b.channels = [c.audio for c in self.cards.values()]
            b.master = vol_curve(self.dev[key]["sl"].value())
            name = self.cfg[key]
            idx = self.outs.get(name) if name else None
            err = b.start(idx) if (name and idx is not None) else None
            if name:
                log(f"bus {key} -> {name!r}: {'ERROR ' + str(err) if err else 'ok'}")
            if name and idx is None:
                err = "Device not found. Try Rescan."
            e = self.dev[key]["err"]
            e.setText(f"Audio error: {err}" if err else "")
            e.setVisible(bool(err))

    def tick_meters(self):
        for i, key in enumerate(("personal", "stream")):
            e = self.dev[key]
            e["lvl"] = max(self.buses[i].level, e["lvl"] * 0.85)
            self.buses[i].level = 0.0
            e["meter"].setValue(int(min(1.0, e["lvl"]) * 100))

    # ---- persistence ------------------------------------------------------------
    def load(self):
        old = {}
        try:
            old = json.loads(CFG_PATH.read_text())
            if old.get("schema") == SCHEMA:
                return old
        except Exception:
            pass
        return {"schema": SCHEMA, "assign": {}, "default": "",
                "orig_default": old.get("orig_default", ""),
                "personal": "", "stream": "", "mic": "", "pvol": 100, "svol": 100,
                "channels": [self.new_channel(n, i, c, k) for n, i, c, k in DEFAULT_CHANNELS]}

    def save(self):
        try:
            APP_DIR.mkdir(parents=True, exist_ok=True)
            CFG_PATH.write_text(json.dumps(self.cfg, indent=2))
        except Exception:
            pass

    @staticmethod
    def new_channel(name, icon, color, kind="cable"):
        return {"id": uuid.uuid4().hex[:8], "name": name, "icon": icon, "color": color, "type": kind,
                "render": "", "capture": "", "vol": [100, 100],
                "on": [True, True] if kind == "cable" else [False, True], "mute": [False, False]}

    def channel(self, cid):
        return next((c for c in self.cfg["channels"] if c["id"] == cid), None)

    def cable_channels(self):
        return [c for c in self.cfg["channels"] if c["type"] == "cable"]

    # ---- Windows default output ---------------------------------------------------
    def record_original_default(self):
        name = default_output_name()
        if name and not is_virtual(name):
            self.cfg["orig_default"] = name  # the user's real output, restored on exit
            self.save()

    def default_channel(self):
        return self.channel(self.cfg.get("default"))

    def apply_default(self, force=False):
        ch = self.default_channel()
        # don't hijack Windows' output until the mixer has somewhere to send it
        if ch and ch["render"] and (self.cfg["personal"] or self.cfg["stream"]) \
                and (force or ch["render"] != self._default_applied):
            self._default_applied = ch["render"]
            log(f"setting Windows default output -> {ch['render']}")
            self.router.async_(self.router.set_default, ch["render"])

    def on_default_picked(self, *_):
        cid = self.dflt_cb.currentData()
        ch = self.channel(cid)
        if not ch or not ch["render"]:
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
            card.set_default(cid == d and card.ch["type"] == "cable")
        fill_combo(self.dflt_cb, [(c["name"], c["id"]) for c in self.cable_channels() if c["render"]],
                   d, "- none -")

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

    # ---- channels -----------------------------------------------------------------------
    def auto_setup(self):
        used = {c["render"] for c in self.cfg["channels"] if c["render"]}
        free = [c for c in self.cables if c not in used]
        for ch in self.cable_channels():
            if not ch["render"] and free:
                ch["render"] = free.pop(0)
                ch["capture"] = capture_of(ch["render"])
        orig = self.cfg.get("orig_default", "")
        if not self.cfg["personal"] and orig in self.real_outs:
            self.cfg["personal"] = orig  # sensible start: play to your normal output
            fill_combo(self.dev["personal"]["cb"], [(n, n) for n in self.real_outs], orig,
                       self.dev["personal"]["ph"])
            self.update_master_labels()
        cc = self.cable_channels()
        if not self.channel(self.cfg.get("default")) or self.channel(self.cfg["default"])["type"] != "cable":
            self.cfg["default"] = cc[0]["id"] if cc else ""
        self.save()

    def taken_by(self, cable, my_id):
        for c in self.cfg["channels"]:
            if c["id"] != my_id and c["render"] == cable:
                return c["name"]
        return None

    def rebuild(self, rescan=False):
        for b in self.buses:
            b.stop()
        for c in self.cards.values():
            c.shutdown()
        if rescan:
            try:
                sd._terminate()
                sd._initialize()
            except Exception:
                pass
            self.scan()
            self.populate_master()
        for c in self.cards.values():
            c.setParent(None)
        while self.row.count():
            it = self.row.takeAt(0)
            if it.widget():
                it.widget().setParent(None)
        self.cards = {}

        self.auto_setup()
        n = len(self.cfg["channels"])
        for ch in self.cfg["channels"]:
            w = ChannelWidget(ch, self.cables, self.ins, self.taken_by, can_remove=n > 1)
            w.changed.connect(self.save)
            w.changed.connect(self.apply_default)
            w.changed.connect(self.refresh_default_badges)
            w.removeRequested.connect(self.remove_channel)
            if w.apps is not None:
                w.apps.dropped.connect(self.on_drop)
            self.cards[ch["id"]] = w
            self.row.addWidget(w)
        add = QtWidgets.QPushButton("+\nAdd\nchannel")
        add.setObjectName("addCh")
        add.setFixedWidth(64)
        add.setMinimumHeight(160)
        add.clicked.connect(self.add_channel)
        self.row.addWidget(add, 0, Qt.AlignTop)
        self.row.setContentsMargins(0, 0, 10, 0)

        try:
            self.start_buses()
            for w in self.cards.values():
                w.restart(self.cfg["mic"])
        except Exception:
            log("audio start error:\n" + traceback.format_exc())
        log(f"rebuild: {len(self.cables)} cables, outs={self.real_outs}, ins={self.real_ins}, "
            f"personal={self.cfg['personal']!r}, stream={self.cfg['stream']!r}, mic={self.cfg['mic']!r}")
        self.refresh_default_badges()

        msgs = []
        if not self.router.exe:
            msgs.append("SoundVolumeView.exe not found: routing apps and the Windows default won't work.")
        need = len(self.cable_channels())
        if not self.cables:
            msgs.append('No virtual cables found. Install VB-Cable from '
                        '<a style="color:#ffd479" href="https://vb-audio.com/Cable/">vb-audio.com/Cable</a>, '
                        'restart your PC, then click Rescan.')
        elif len(self.cables) < need:
            msgs.append(f"{len(self.cables)} virtual cable(s) for {need} channels. Each channel needs its own "
                        "cable (VB-Cable A+B / C+D packs add more).")
        self.banner.setText("<br><br>".join(msgs))
        self.banner.setVisible(bool(msgs))

        self._default_applied = None
        self.apply_default()
        self._sig = None
        self.refresh_lists()

    def add_channel(self):
        k = len(self.cfg["channels"])
        self.cfg["channels"].append(
            self.new_channel(f"Channel {k + 1}", "🔊", EXTRA_COLORS[k % len(EXTRA_COLORS)], "cable"))
        self.save()
        self.rebuild()

    def remove_channel(self, cid):
        if len(self.cfg["channels"]) <= 1:
            return
        self.cfg["channels"] = [c for c in self.cfg["channels"] if c["id"] != cid]
        if self.cfg.get("default") == cid:
            self.cfg["default"] = ""
            self._default_applied = None
        self.save()
        self.auto_setup()
        dflt = self.default_channel()
        for proc, c in list(self.cfg["assign"].items()):
            if c == cid:
                self.cfg["assign"].pop(proc)
                if dflt and dflt["render"]:
                    self.router.async_(self.router.set_app, dflt["render"], proc)
        self.save()
        self.rebuild()

    # ---- drag & drop / app routing -----------------------------------------------------------
    def on_drop(self, cid, proc):
        ch = self.channel(cid)
        if not ch or ch["type"] != "cable":
            return
        if not ch["render"]:
            self.cards[cid].set_note("Pick a cable first\n(use ⚙)")
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
        try:
            self.apply_assignments()
            self.refresh_lists()
            self._tick += 1
            if self._tick % 2 == 1 and not self._checking:
                self._checking = True
                self.router.async_(self._check_default)
            self.update_default_bar()
        except Exception:
            log("poll error:\n" + traceback.format_exc())

    def refresh_lists(self):
        if QtWidgets.QApplication.mouseButtons() & Qt.LeftButton:
            return  # never rebuild lists in the middle of a drag
        dflt = self.cfg.get("default")
        groups = {c["id"]: [] for c in self.cable_channels()}
        for proc in sorted(set(self.active) | set(self.cfg["assign"]), key=str.lower):
            cid = self.cfg["assign"].get(proc)
            if cid not in groups:
                self.cfg["assign"].pop(proc, None)
                cid = dflt
            if cid not in groups:
                continue
            if proc not in self.active and proc not in self.cfg["assign"]:
                continue
            groups[cid].append(proc)
        sig = (tuple((k, tuple(v)) for k, v in groups.items()), tuple(sorted(self.active)))
        if sig == self._sig:
            return
        self._sig = sig
        for cid, procs in groups.items():
            w = self.cards[cid].apps
            if w is None:
                continue
            w.clear()
            for p in procs:
                idle = p not in self.active
                it = QtWidgets.QListWidgetItem(p.replace(".exe", "") + ("  (idle)" if idle else ""))
                it.setData(Qt.UserRole, p)
                if idle:
                    it.setForeground(QtGui.QColor("#8b93a5"))
                w.addItem(it)

    # ---- updates ---------------------------------------------------------------------------------
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
        for b in self.buses:
            b.stop()
        for c in self.cards.values():
            c.shutdown()
        self.save()
        self.restore_windows_output()
        super().closeEvent(e)


def main():
    APP_DIR.mkdir(parents=True, exist_ok=True)
    try:
        if LOG_PATH.exists() and LOG_PATH.stat().st_size > 400_000:
            LOG_PATH.unlink()
    except Exception:
        pass
    crash = open(APP_DIR / "crash.log", "a", buffering=1)
    faulthandler.enable(file=crash)
    log(f"=== start v{VERSION} frozen={FROZEN} ===")
    shown = {"n": 0}

    def hook(et, ev, tb):
        txt = "".join(traceback.format_exception(et, ev, tb))
        crash.write(txt + "\n")
        log("EXCEPTION:\n" + txt)
        if shown["n"] < 3:  # don't spam dialogs
            shown["n"] += 1
            try:
                QtWidgets.QMessageBox.critical(
                    None, APP_NAME, f"Something went wrong:\n\n{ev}\n\nDetails were saved to:\n{APP_DIR}")
            except Exception:
                pass

    sys.excepthook = hook
    threading.excepthook = lambda a: log(f"THREAD EXCEPTION: {a.exc_type.__name__}: {a.exc_value}")
    app = QtWidgets.QApplication(sys.argv)
    app.setStyleSheet(STYLE)
    try:
        w = Main()
    except Exception:
        hook(*sys.exc_info())
        sys.exit(1)
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
