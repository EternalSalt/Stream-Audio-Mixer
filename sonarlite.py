"""
SonarLite - a Sonar-style per-app audio mixer for Windows 10/11.

Each channel = one virtual audio cable. Apps are dragged onto a channel; the
app then forces that program's output to the channel's virtual device (via
NirSoft SoundVolumeView). Each channel's audio is captured and played to TWO
real output devices with independent volume / mute.
"""
import csv
import faulthandler
import json
import traceback
import os
import shutil
import subprocess
import sys
import tempfile
import re
import threading
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

SR = 48000
BLOCK = 480                 # 10 ms
MAX_LAT = int(SR * 0.15)    # if buffer grows beyond this, drop back to TARGET
TARGET = int(SR * 0.04)

APP_DIR = Path(os.environ.get("APPDATA", str(Path.home()))) / "SonarLite"
CFG_PATH = APP_DIR / "config.json"
if getattr(sys, "frozen", False):
    HERE = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))  # bundled files
    EXE_DIR = Path(sys.executable).resolve().parent                       # next to the .exe
else:
    HERE = EXE_DIR = Path(__file__).resolve().parent


# --------------------------------------------------------------------------
# Audio engine
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
    """capture device -> two output devices."""

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


def wasapi_devices():
    apis = sd.query_hostapis()
    wi = next((i for i, h in enumerate(apis) if "WASAPI" in h["name"]), None)
    ins, outs = {}, {}
    for i, d in enumerate(sd.query_devices()):
        if d["hostapi"] != wi:
            continue
        if d["max_input_channels"] > 0:
            ins[d["name"]] = i
        if d["max_output_channels"] > 0:
            outs[d["name"]] = i
    return ins, outs


# --------------------------------------------------------------------------
# Per-app routing (SoundVolumeView) and session discovery
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
        if base in self.ids:
            return self.ids[base]
        for k, v in self.ids.items():
            if k in sd_name:
                return v
        return base

    def set_app(self, sd_name, proc):
        if not self.exe:
            return
        with self.lock:
            if sd_name is None:
                self._run("/SetAppDefault", "DefaultRenderDevice", "all", proc)
                return
            if sd_name.split(" (")[0].strip() not in self.ids:
                self.refresh()
            self._run("/SetAppDefault", self.resolve(sd_name), "all", proc)

    def set_app_async(self, sd_name, proc):
        threading.Thread(target=self.set_app, args=(sd_name, proc), daemon=True).start()


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
OWNER, REPO = "EternalSalt", "Stream-Audio-Mixer"
FROZEN = bool(getattr(sys, "frozen", False))


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
                headers={"User-Agent": "SonarLite", "Accept": "application/vnd.github+json"})
            with urllib.request.urlopen(req, timeout=15) as r:
                data = json.load(r)
            tag = data["tag_name"].lstrip("vV")
            asset = next((a for a in data.get("assets", []) if a["name"] == "SonarLite-Portable.exe"), None)
            if asset and vtuple(tag) > vtuple(VERSION):
                self.available.emit(tag, asset["browser_download_url"], int(asset["size"]))
            elif not silent:
                self.status.emit(f"You're up to date (v{VERSION})")
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
            req = urllib.request.Request(url, headers={"User-Agent": "SonarLite"})
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
            self.failed.emit("Can't write to the app's folder. Move SonarLite somewhere you own and retry.")
        except Exception as e:  # noqa
            self.failed.emit(f"Download failed: {e}")


def launch_swap(new_path):
    """Detached helper: wait for this exe to unlock, replace it, relaunch, delete itself."""
    target = sys.executable
    bat = Path(tempfile.gettempdir()) / "sonarlite_update.bat"
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
QWidget { background:#16181d; color:#e6e8ec; font-size:13px; }
QFrame#card { background:#1f2229; border:1px solid #2c303a; border-radius:10px; }
QLineEdit#title { background:transparent; border:none; font-size:16px; font-weight:600; }
QComboBox, QPushButton { background:#2a2e38; border:1px solid #373c49; border-radius:6px; padding:4px 8px; }
QPushButton:hover, QComboBox:hover { background:#343947; }
QPushButton:checked { background:#b3412f; }
QListWidget { background:#14161a; border:1px dashed #3a3f4d; border-radius:8px; padding:4px; }
QListWidget::item { background:#2d3a63; border-radius:6px; padding:6px; margin:2px; }
QListWidget::item:selected { background:#4a63c0; }
QProgressBar { background:#101216; border:none; border-radius:3px; }
QProgressBar::chunk { background:#3ecf8e; border-radius:3px; }
QSlider::groove:horizontal { height:6px; background:#2c303a; border-radius:3px; }
QSlider::sub-page:horizontal { background:#5b7cff; border-radius:3px; }
QSlider::handle:horizontal { width:14px; margin:-5px 0; background:#e6e8ec; border-radius:7px; }
QLabel#status { color:#ff8a7a; }
QLabel#hint { color:#8b91a0; }
"""


def make_combo(names, current):
    cb = QtWidgets.QComboBox()
    cb.setSizeAdjustPolicy(QtWidgets.QComboBox.AdjustToMinimumContentsLengthWithIcon)
    cb.setMinimumContentsLength(22)
    cb.addItem("— none —", "")
    for n in names:
        cb.addItem(n, n)
    if current and current not in names:
        cb.addItem(f"(missing) {current}", current)
    cb.setCurrentIndex(max(cb.findData(current or ""), 0))
    return cb


class AppList(QtWidgets.QListWidget):
    dropped = QtCore.Signal(str, str)  # channel_id, process

    def __init__(self, channel_id=""):
        super().__init__()
        self.channel_id = channel_id
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDragDropMode(QtWidgets.QAbstractItemView.DragDrop)
        self.setDefaultDropAction(Qt.CopyAction)
        self.setMinimumHeight(120)

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


def vol_curve(v):
    return (v / 100.0) ** 2


class ChannelCard(QtWidgets.QFrame):
    changed = QtCore.Signal()
    removeRequested = QtCore.Signal(str)

    def __init__(self, ch, ins, outs):
        super().__init__()
        self.setObjectName("card")
        self.setFixedWidth(310)
        self.ch, self.ins, self.outs = ch, ins, outs
        self.engine = Engine()
        self.lvl = 0.0

        v = QtWidgets.QVBoxLayout(self)
        top = QtWidgets.QHBoxLayout()
        self.name = QtWidgets.QLineEdit(ch["name"])
        self.name.setObjectName("title")
        self.name.editingFinished.connect(self._cfg)
        close = QtWidgets.QPushButton("✕")
        close.setFixedWidth(30)
        close.clicked.connect(lambda: self.removeRequested.emit(self.ch["id"]))
        top.addWidget(self.name)
        top.addWidget(close)
        v.addLayout(top)

        self.setup_btn = QtWidgets.QPushButton("Virtual device setup ▾")
        self.setup_btn.clicked.connect(lambda: self.setup.setVisible(not self.setup.isVisible()))
        v.addWidget(self.setup_btn)
        self.setup = QtWidgets.QWidget()
        form = QtWidgets.QFormLayout(self.setup)
        form.setContentsMargins(0, 0, 0, 0)
        self.render_cb = make_combo(list(outs), ch["render"])
        self.cap_cb = make_combo(list(ins), ch["capture"])
        form.addRow("Apps play to", self.render_cb)
        form.addRow("Read from", self.cap_cb)
        self.setup.setVisible(not (ch["render"] and ch["capture"]))
        v.addWidget(self.setup)
        self.render_cb.currentIndexChanged.connect(self._render_changed)
        self.cap_cb.currentIndexChanged.connect(self._dev_changed)

        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel("Master"))
        self.master = QtWidgets.QSlider(Qt.Horizontal)
        self.master.setRange(0, 100)
        self.master.setValue(ch["vol"])
        self.master.valueChanged.connect(self._gains)
        row.addWidget(self.master)
        v.addLayout(row)
        self.meter = QtWidgets.QProgressBar()
        self.meter.setRange(0, 100)
        self.meter.setTextVisible(False)
        self.meter.setFixedHeight(6)
        v.addWidget(self.meter)

        self.out_cb, self.out_sl, self.out_mute = [], [], []
        for i in (0, 1):
            box = QtWidgets.QGroupBox(f"Output {'AB'[i]}")
            bl = QtWidgets.QVBoxLayout(box)
            cb = make_combo(list(outs), ch["out"][i])
            cb.currentIndexChanged.connect(self._dev_changed)
            r = QtWidgets.QHBoxLayout()
            sl = QtWidgets.QSlider(Qt.Horizontal)
            sl.setRange(0, 100)
            sl.setValue(ch["ov"][i])
            sl.valueChanged.connect(self._gains)
            m = QtWidgets.QPushButton("M")
            m.setCheckable(True)
            m.setChecked(ch["mute"][i])
            m.setFixedWidth(30)
            m.toggled.connect(self._gains)
            r.addWidget(sl)
            r.addWidget(m)
            bl.addWidget(cb)
            bl.addLayout(r)
            v.addWidget(box)
            self.out_cb.append(cb)
            self.out_sl.append(sl)
            self.out_mute.append(m)

        v.addWidget(QtWidgets.QLabel("Apps on this channel (drop here)"))
        self.apps = AppList(ch["id"])
        v.addWidget(self.apps, 1)
        self.status = QtWidgets.QLabel("")
        self.status.setObjectName("status")
        self.status.setWordWrap(True)
        v.addWidget(self.status)

        self._gains()
        self.restart()

    # -- config / engine ---------------------------------------------------
    def _cfg(self):
        c = self.ch
        c["name"] = self.name.text() or "Channel"
        c["render"] = self.render_cb.currentData()
        c["capture"] = self.cap_cb.currentData()
        c["out"] = [cb.currentData() for cb in self.out_cb]
        c["vol"] = self.master.value()
        c["ov"] = [s.value() for s in self.out_sl]
        c["mute"] = [m.isChecked() for m in self.out_mute]
        self.changed.emit()

    def _gains(self, *_):
        self.engine.master = vol_curve(self.master.value())
        for i in (0, 1):
            self.engine.gain[i] = vol_curve(self.out_sl[i].value())
            self.engine.mute[i] = self.out_mute[i].isChecked()
        self._cfg()

    def _render_changed(self):
        r = self.render_cb.currentData()
        if r and not self.cap_cb.currentData():  # guess the matching capture side
            guess = r.replace("Input", "Output")
            i = self.cap_cb.findData(guess)
            if i >= 0:
                self.cap_cb.setCurrentIndex(i)
        self._dev_changed()

    def _dev_changed(self, *_):
        self._cfg()
        self.restart()

    def restart(self):
        self.engine.stop()
        cap, outs = self.ch["capture"], self.ch["out"]
        if not cap or not any(outs):
            self.status.setText("Pick a 'Read from' device and at least one output.")
            return
        if self.ch["render"] and self.ch["render"] in outs:
            self.status.setText("An output is the same as the channel's virtual device (feedback loop).")
            return
        idx_in = self.ins.get(cap)
        idx_out = [self.outs.get(o) if o else None for o in outs]
        if idx_in is None:
            self.status.setText("Capture device not found - click Rescan.")
            return
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
        self.setWindowTitle(f"SonarLite v{VERSION}")
        self.resize(1250, 780)
        self.cfg = self.load()
        self.router = Router()
        self.active, self.applied, self._sig = {}, set(), None
        self.cards = {}

        root = QtWidgets.QHBoxLayout(self)
        left = QtWidgets.QVBoxLayout()
        left.addWidget(QtWidgets.QLabel("<b>Other apps</b> (not routed)"))
        self.free = AppList("")
        self.free.dropped.connect(self.on_drop)
        self.free.setFixedWidth(200)
        left.addWidget(self.free, 1)
        rescan = QtWidgets.QPushButton("Rescan devices")
        rescan.clicked.connect(self.rebuild)
        left.addWidget(rescan)
        self.up_btn = QtWidgets.QPushButton("Check for updates")
        self.up_btn.clicked.connect(self.on_update_click)
        left.addWidget(self.up_btn)
        self.up_bar = QtWidgets.QProgressBar()
        self.up_bar.setRange(0, 100)
        self.up_bar.setFixedHeight(8)
        self.up_bar.setTextVisible(False)
        self.up_bar.hide()
        left.addWidget(self.up_bar)
        self.up_lbl = QtWidgets.QLabel(f"v{VERSION}")
        self.up_lbl.setObjectName("hint")
        self.up_lbl.setWordWrap(True)
        left.addWidget(self.up_lbl)
        self.hint = QtWidgets.QLabel("")
        self.hint.setObjectName("status")
        self.hint.setWordWrap(True)
        left.addWidget(self.hint)
        root.addLayout(left)

        self.scroll = QtWidgets.QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.holder = QtWidgets.QWidget()
        self.row = QtWidgets.QHBoxLayout(self.holder)
        self.row.setAlignment(Qt.AlignLeft)
        self.scroll.setWidget(self.holder)
        root.addWidget(self.scroll, 1)

        self.rebuild()
        if not self.router.exe:
            self.hint.setText("SoundVolumeView.exe not found. Put it next to sonarlite.py "
                              "(see README) - drag & drop routing needs it.")
        t = QtCore.QTimer(self)
        t.timeout.connect(self.poll)
        t.start(2000)
        m = QtCore.QTimer(self)
        m.timeout.connect(lambda: [c.tick() for c in self.cards.values()])
        m.start(40)
        self.poll()

        self.up_state, self.up_info = "idle", None
        self.up = Updater()
        self.up.status.connect(self.up_lbl.setText)
        self.up.available.connect(self.on_update_available)
        self.up.progress.connect(self.up_bar.setValue)
        self.up.ready.connect(self.on_update_ready)
        self.up.failed.connect(self.on_update_failed)
        cleanup_leftovers()
        QtCore.QTimer.singleShot(3000, lambda: self.up.check(silent=True))

    # -- updates -----------------------------------------------------------
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
        self.up_lbl.setText(f"v{VERSION} - update v{ver} available")

    def on_update_ready(self, path):
        self.up_state, self.up_path = "ready", path
        self.up_bar.hide()
        self.up_btn.setEnabled(True)
        self.up_btn.setText("Restart to finish update")
        self.up_lbl.setText("Update downloaded and ready.")
        r = QtWidgets.QMessageBox.question(
            self, "Update ready",
            f"SonarLite v{self.up_info[0]} is downloaded.\nRestart now to finish? Audio pauses for a few seconds.")
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

    # -- persistence -------------------------------------------------------
    def load(self):
        try:
            return json.loads(CFG_PATH.read_text())
        except Exception:
            return {"channels": [self.new_channel(n) for n in ("Game", "Chat", "Media")], "assign": {}}

    def save(self):
        APP_DIR.mkdir(parents=True, exist_ok=True)
        CFG_PATH.write_text(json.dumps(self.cfg, indent=2))

    @staticmethod
    def new_channel(name):
        return {"id": uuid.uuid4().hex[:8], "name": name, "render": "", "capture": "",
                "out": ["", ""], "vol": 100, "ov": [100, 100], "mute": [False, False]}

    # -- channel management -------------------------------------------------
    def rebuild(self):
        for c in self.cards.values():
            c.shutdown()
            c.setParent(None)
        while self.row.count():
            it = self.row.takeAt(0)
            if it.widget():
                it.widget().setParent(None)
        self.cards = {}
        ins, outs = wasapi_devices()
        for ch in self.cfg["channels"]:
            card = ChannelCard(ch, ins, outs)
            card.changed.connect(self.save)
            card.changed.connect(self.sync_render_changes)
            card.removeRequested.connect(self.remove_channel)
            card.apps.dropped.connect(self.on_drop)
            self.cards[ch["id"]] = card
            self.row.addWidget(card)
        add = QtWidgets.QPushButton("+ Add channel")
        add.setFixedSize(130, 40)
        add.clicked.connect(self.add_channel)
        self.row.addWidget(add, 0, Qt.AlignTop)
        self._sig = None
        self.refresh_lists()

    def add_channel(self):
        n = len(self.cfg["channels"]) + 1
        self.cfg["channels"].append(self.new_channel(f"Channel {n}"))
        self.save()
        self.rebuild()

    def remove_channel(self, cid):
        for proc, c in list(self.cfg["assign"].items()):
            if c == cid:
                self.cfg["assign"].pop(proc)
                self.router.set_app_async(None, proc)
        self.cfg["channels"] = [c for c in self.cfg["channels"] if c["id"] != cid]
        self.save()
        self.rebuild()

    def channel(self, cid):
        return next((c for c in self.cfg["channels"] if c["id"] == cid), None)

    def sync_render_changes(self):
        self.apply_assignments()

    # -- drag & drop / routing -----------------------------------------------
    def on_drop(self, cid, proc):
        if cid:
            self.cfg["assign"][proc] = cid
            ch = self.channel(cid)
            if not ch or not ch["render"]:
                self.hint.setText(f"Set 'Apps play to' on channel '{ch['name'] if ch else '?'}' first.")
        else:
            self.cfg["assign"].pop(proc, None)
            self.router.set_app_async(None, proc)
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
                self.router.set_app_async(ch["render"], proc)

    def poll(self):
        try:
            self.active = list_audio_apps()
        except Exception:
            self.active = {}
        self.apply_assignments()
        self.refresh_lists()

    def refresh_lists(self, force=False):
        if QtWidgets.QApplication.mouseButtons() & Qt.LeftButton and not force:
            return  # don't rebuild lists in the middle of a drag
        groups = {c["id"]: [] for c in self.cfg["channels"]}
        groups[""] = []
        for proc in sorted(set(self.active) | set(self.cfg["assign"]), key=str.lower):
            cid = self.cfg["assign"].get(proc, "")
            if cid not in groups:
                self.cfg["assign"].pop(proc, None)
                cid = ""
            if cid == "" and proc not in self.active:
                continue
            groups[cid].append(proc)
        sig = (tuple((k, tuple(v)) for k, v in groups.items()), tuple(sorted(self.active)))
        if sig == self._sig and not force:
            return
        self._sig = sig
        for cid, procs in groups.items():
            w = self.free if cid == "" else self.cards[cid].apps
            w.clear()
            for p in procs:
                it = QtWidgets.QListWidgetItem(p.replace(".exe", "") + ("" if p in self.active else "  (idle)"))
                it.setData(Qt.UserRole, p)
                w.addItem(it)

    def closeEvent(self, e):
        for c in self.cards.values():
            c.shutdown()
        self.save()
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
