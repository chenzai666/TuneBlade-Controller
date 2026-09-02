"""
TuneBlade Controller — tray-resident volume bridge.

- Volume keys control the device slider named in config (default: 游戏室).
- Step is exactly volume_step percent (default: 5) via RangeValuePattern.
- Only intercept when Master is ON and system has speakers; otherwise pass through.
- Uses RegisterHotKey (no WH_KEYBOARD_LL) so typing / mouse never freeze.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import json
import os
import signal
import sys
import threading
import time
import winreg
from pathlib import Path

import win32con
import win32gui
import uiautomation as auto

try:
    import pystray
    from PIL import Image, ImageDraw
except ImportError:  # pragma: no cover
    pystray = None
    Image = ImageDraw = None


# ── paths ─────────────────────────────────────────────────────────

def _app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


APP_DIR = _app_dir()
CONFIG_PATH = APP_DIR / "config.json"
LOG_PATH = APP_DIR / "TuneBladeController.log"
APP_NAME = "TuneBlade Controller"
RUN_REG_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE_NAME = "TuneBladeController"

# Empty device_name = auto-pick the first AirPlay receiver found in TuneBlade
DEFAULT_CFG = {
    "device_name": "",
    "volume_step": 5,
    "window_title": "TuneBlade",
    "poll_interval_sec": 1.0,
    "autostart": False,
    "debug_log": False,  # True 才写 TuneBladeController.log，日常请保持关闭
    "auto_enable_master": True,  # 开机启动时自动开 Master（仅一次窗口）
    "auto_connect_device": True,  # 开机启动时若设备 Disconnected 则自动点连接
    # 可自定义，格式如 Ctrl+Alt+PgUp / Ctrl+Shift+Up / Ctrl+Alt+F1
    "hotkey_vol_up": "Ctrl+Alt+PgUp",
    "hotkey_vol_down": "Ctrl+Alt+PgDn",
    "hotkey_mute": "Ctrl+Alt+M",
    "hotkey_quit": "Ctrl+Alt+Q",
}

_debug_log_enabled = False

# Text labels that are NOT device names
_DEVICE_NAME_SKIP = {
    "",
    "master",
    "eq",
    "on",
    "off",
    "latency",
    "ms",
    "volume",
    "volume:",
    "connected",
    "disconnected",
    "connection standby",
    "searching for airplay receivers..",
    "searching for airplay receivers...",
}

UIA_InvokePatternId = 10000


class _NullIO:
    def write(self, *a, **k):
        pass

    def flush(self):
        pass


def _setup_logging(enable: bool = False) -> None:
    """Only write TuneBladeController.log when debug_log=True (keeps daily use quiet)."""
    global _debug_log_enabled
    _debug_log_enabled = bool(enable)
    if not getattr(sys, "frozen", False):
        return
    if not enable:
        sys.stdout = _NullIO()  # type: ignore
        sys.stderr = _NullIO()  # type: ignore
        return
    try:
        fp = open(LOG_PATH, "w", encoding="utf-8", buffering=1)  # truncate each debug run
        sys.stdout = fp
        sys.stderr = fp
        print(f"===== start {time.strftime('%Y-%m-%d %H:%M:%S')} =====")
    except Exception:
        sys.stdout = _NullIO()  # type: ignore
        sys.stderr = _NullIO()  # type: ignore


def _msgbox(text: str, title: str = APP_NAME) -> None:
    try:
        ctypes.windll.user32.MessageBoxW(None, str(text), title, 0x10)
    except Exception:
        pass


def _log(msg: str) -> None:
    if getattr(sys, "frozen", False) and not _debug_log_enabled:
        return
    try:
        print(msg, flush=True)
    except Exception:
        pass


# ── config ────────────────────────────────────────────────────────

def load_config() -> dict:
    cfg = dict(DEFAULT_CFG)
    if CONFIG_PATH.exists():
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                cfg.update(data)
        except Exception as e:
            _log(f"[Warning] config.json: {e}")
    cfg["volume_step"] = max(1, int(cfg.get("volume_step") or 5))
    name = str(cfg.get("device_name") or "").strip()
    if "\ufffd" in name:
        _log("[Warning] device_name corrupted in config, reset to auto")
        name = ""
    cfg["device_name"] = name  # "" means auto
    return cfg


def save_config(cfg: dict) -> None:
    try:
        CONFIG_PATH.write_text(
            json.dumps(cfg, indent=4, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    except Exception as e:
        _log(f"[Warning] save config: {e}")


def has_system_audio() -> bool:
    try:
        return ctypes.windll.winmm.waveOutGetNumDevs() > 0
    except Exception:
        return True


# ── system volume redirect (primary path for laptop Fn volume keys) ─

class SystemVolumeLock:
    """
    When TuneBlade is active (armed):
      - Remember the user's previous system volume/mute
      - Force the Windows speakers muted (so local PC is silent)
      - If media keys still nudge the OS volume, snap mute back and
        optionally report 'up'/'down'/'mute' for TuneBlade

    When disarmed (Master OFF / device gone):
      - Restore the previous system volume/mute
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._active = False
        # While armed we keep the OS at this frozen state (muted)
        self._hold_vol = None
        self._hold_mute = 1
        # User's real system state before we took over — restored on disarm
        self._saved_vol = None
        self._saved_mute = None
        # True after we muted OS until we successfully release speakers
        self._we_muted = False
        self._ep = None
        self._cooldown_until = 0.0
        # TUN/VPN/adapter flips can make EndpointVolume briefly report garbage
        # (often a big drop). Ignore those as TuneBlade volume keys.
        self._glitch_vol_jump = 0.06  # ≥6% in one poll ≈ not a key step

    def _invalidate_endpoint(self):
        self._ep = None

    def _endpoint(self):
        if self._ep is not None:
            return self._ep
        try:
            from pycaw.pycaw import AudioUtilities

            spk = AudioUtilities.GetSpeakers()
            self._ep = spk.EndpointVolume
            return self._ep
        except Exception as e:
            _log(f"[sysvol] endpoint: {e}")
            self._ep = None
            return None

    def arm(self):
        """Take over: save system volume, then mute speakers."""
        with self._lock:
            ep = self._endpoint()
            if ep is None:
                self._active = False
                return
            try:
                cur_vol = float(ep.GetMasterVolumeLevelScalar())
                cur_mute = int(ep.GetMute())
                # Only snapshot once per arm session
                if not self._active:
                    self._saved_vol = cur_vol
                    self._saved_mute = cur_mute
                # Hold point while TB is on: keep level, force muted
                self._hold_vol = cur_vol if self._saved_vol is None else float(self._saved_vol)
                self._hold_mute = 1
                ep.SetMasterVolumeLevelScalar(self._hold_vol, None)
                ep.SetMute(1, None)
                self._active = True
                self._we_muted = True
                self._cooldown_until = time.monotonic() + 0.35
                _log(
                    f"[sysvol] armed — system muted "
                    f"(saved vol={self._saved_vol:.3f} mute={self._saved_mute})"
                )
            except Exception as e:
                _log(f"[sysvol] arm failed: {e}")
                self._active = False

    def disarm(self):
        """Give speakers back: restore volume and unmute.

        Safe to call every poll while inactive — only touches OS when we
        actually had muted it (or still have a saved snapshot).
        Always SetMute(0) on release (never restore a stale mute=1).
        """
        with self._lock:
            ep = self._endpoint()
            was = self._active
            saved = self._saved_vol
            release = was or self._we_muted or saved is not None
            self._active = False
            self._hold_vol = None
            self._saved_vol = None
            self._saved_mute = None
            if ep is None or not release:
                return
            try:
                if saved is not None:
                    ep.SetMasterVolumeLevelScalar(float(saved), None)
                ep.SetMute(0, None)
                self._we_muted = False
                _log(
                    f"[sysvol] disarmed — unmuted"
                    + (f" vol={saved:.3f}" if saved is not None else "")
                )
            except Exception as e:
                _log(f"[sysvol] disarm restore failed: {e}")

    def clear_leftover_mute(self):
        """Unmute once if OS is still muted while we are not armed (stale state)."""
        with self._lock:
            if self._active:
                return False
            ep = self._endpoint()
            if ep is None:
                return False
            try:
                if int(ep.GetMute()) != 1:
                    self._we_muted = False
                    return False
                if self._saved_vol is not None:
                    ep.SetMasterVolumeLevelScalar(float(self._saved_vol), None)
                ep.SetMute(0, None)
                self._we_muted = False
                self._saved_vol = None
                self._saved_mute = None
                self._hold_vol = None
                _log("[sysvol] cleared leftover system mute")
                return True
            except Exception as e:
                _log(f"[sysvol] clear leftover failed: {e}")
                return False

    @property
    def active(self) -> bool:
        return self._active

    def freeze(self) -> bool:
        """Keep OS muted at hold point (no TuneBlade command)."""
        with self._lock:
            return self._hold_unlocked()

    def _hold_unlocked(self) -> bool:
        if not self._active or self._hold_vol is None:
            return False
        ep = self._endpoint()
        if ep is None:
            return False
        try:
            ep.SetMasterVolumeLevelScalar(float(self._hold_vol), None)
            ep.SetMute(1, None)
            return True
        except Exception as e:
            _log(f"[sysvol] hold: {e}")
            return False

    def poll_redirect(self) -> str | None:
        """
        If OS volume/mute drifted from the muted hold point, snap it back
        and return 'up' | 'down' | 'mute' for TuneBlade.

        Ignores endpoint glitches from TUN/VPN/adapter switches (sudden big
        jumps, or downward drift while we still force mute).
        """
        with self._lock:
            if not self._active or self._hold_vol is None:
                return None
            now = time.monotonic()
            if now < self._cooldown_until:
                try:
                    self._hold_unlocked()
                except Exception:
                    pass
                return None

            ep = self._endpoint()
            if ep is None:
                return None
            try:
                cur = float(ep.GetMasterVolumeLevelScalar())
                muted = int(ep.GetMute())
            except Exception as e:
                _log(f"[sysvol] read: {e}")
                self._invalidate_endpoint()
                self._cooldown_until = now + 1.0
                return None

            if not (0.0 <= cur <= 1.0):
                self._invalidate_endpoint()
                self._cooldown_until = now + 1.0
                return None

            d_vol = cur - float(self._hold_vol)
            # hold mute is always 1; if unmuted → user tried to raise volume
            if muted == 1 and abs(d_vol) < 0.0015:
                return None

            # Sudden jump (e.g. TUN on/off): restore hold, do NOT nudge TuneBlade
            if abs(d_vol) >= float(self._glitch_vol_jump):
                try:
                    self._hold_unlocked()
                except Exception:
                    pass
                self._invalidate_endpoint()
                self._cooldown_until = now + 1.2
                _log(f"[sysvol] ignore glitch d_vol={d_vol:+.3f} muted={muted}")
                return None

            if muted == 0 and abs(d_vol) < 0.02:
                cmd = "up"  # unmute / vol-up while we forced mute
            elif d_vol > 0.0015:
                cmd = "up"
            elif d_vol < -0.0015:
                # While we force mute, downward scalar drift is usually adapter
                # noise (TUN), not a volume key — just re-freeze.
                if muted == 1:
                    try:
                        self._hold_unlocked()
                    except Exception:
                        pass
                    self._cooldown_until = now + 0.45
                    return None
                cmd = "down"
            else:
                cmd = "mute"

            self._hold_unlocked()
            self._cooldown_until = now + 0.25
            _log(f"[sysvol] redirect {cmd} (d_vol={d_vol:+.3f} muted={muted})")
            try:
                _osd_burst_hide()
            except Exception:
                pass
            return cmd


_sysvol = SystemVolumeLock()


def hide_volume_osd() -> None:
    """
    Dismiss only the Windows volume flyout (ShellExperienceHost).
    Never WM_CLOSE explorer XAML islands — that kills menus / mouse clicks.
    """
    try:
        import win32gui
        import win32process
        import win32con
        import win32api
    except Exception:
        return

    targets = []

    def _pid_name(pid: int) -> str:
        try:
            h = win32api.OpenProcess(0x1000, False, pid)
            try:
                buf = ctypes.create_unicode_buffer(260)
                size = ctypes.c_uint(260)
                if ctypes.windll.kernel32.QueryFullProcessImageNameW(
                    h, 0, buf, ctypes.byref(size)
                ):
                    return buf.value.lower()
            finally:
                win32api.CloseHandle(h)
        except Exception:
            pass
        return ""

    def enum_cb(hwnd, _):
        try:
            if not win32gui.IsWindowVisible(hwnd):
                return
            cls = win32gui.GetClassName(hwnd) or ""
            if cls != "NativeHWNDHost":
                return
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            path = _pid_name(pid)
            if "shellexperiencehost" not in path:
                return
            rect = win32gui.GetWindowRect(hwnd)
            w, h = rect[2] - rect[0], rect[3] - rect[1]
            if 40 <= w <= 900 and 40 <= h <= 500:
                targets.append(hwnd)
        except Exception:
            pass

    try:
        win32gui.EnumWindows(enum_cb, None)
    except Exception:
        return

    for hwnd in targets:
        try:
            win32gui.ShowWindow(hwnd, win32con.SW_HIDE)
        except Exception:
            pass


def _osd_burst_hide():
    """Hide the volume flyout a few times (it can reappear once)."""

    def _run():
        for _ in range(4):
            hide_volume_osd()
            time.sleep(0.05)

    threading.Thread(target=_run, daemon=True).start()


def _autostart_command() -> str:
    if getattr(sys, "frozen", False):
        return f'"{Path(sys.executable).resolve()}"'
    pyw = APP_DIR / "venv" / "Scripts" / "pythonw.exe"
    py = APP_DIR / "venv" / "Scripts" / "python.exe"
    exe = pyw if pyw.exists() else py if py.exists() else Path(sys.executable)
    return f'"{exe}" "{APP_DIR / "controller.py"}"'


def is_autostart_enabled() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_REG_KEY, 0, winreg.KEY_READ) as k:
            winreg.QueryValueEx(k, RUN_VALUE_NAME)
            return True
    except OSError:
        return False


def set_autostart(enabled: bool) -> None:
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_REG_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if enabled:
            winreg.SetValueEx(k, RUN_VALUE_NAME, 0, winreg.REG_SZ, _autostart_command())
        else:
            try:
                winreg.DeleteValue(k, RUN_VALUE_NAME)
            except FileNotFoundError:
                pass


# ── hotkeys (NO low-level keyboard hook) ──────────────────────────
# RegisterHotKey only steals the configured combos. Default volume keys are
# Ctrl+Alt+PgUp/PgDn so plain PgUp/PgDn still scroll while typing.

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000
WM_QUIT = 0x0012
WM_HOTKEY = 0x0312
WM_APP_RELOAD_HOTKEYS = 0x8002

HOTKEY_QUIT = 1
HOTKEY_MUTE = 2
HOTKEY_VOLUP = 3
HOTKEY_VOLDOWN = 4

_VK_ALIASES = {
    "pgup": 0x21,
    "pageup": 0x21,
    "prior": 0x21,
    "pgdn": 0x22,
    "pagedown": 0x22,
    "pagedn": 0x22,
    "next": 0x22,
    "up": 0x26,
    "down": 0x28,
    "left": 0x25,
    "right": 0x27,
    "space": 0x20,
    "tab": 0x09,
    "esc": 0x1B,
    "escape": 0x1B,
    "enter": 0x0D,
    "return": 0x0D,
    "home": 0x24,
    "end": 0x23,
    "insert": 0x2D,
    "ins": 0x2D,
    "delete": 0x2E,
    "del": 0x2E,
    "backspace": 0x08,
    "bksp": 0x08,
}
for _i in range(1, 13):
    _VK_ALIASES[f"f{_i}"] = 0x70 + _i - 1
for _i in range(10):
    _VK_ALIASES[str(_i)] = 0x30 + _i
for _c in "abcdefghijklmnopqrstuvwxyz":
    _VK_ALIASES[_c] = ord(_c.upper())

WPARAM_T = ctypes.c_size_t
LPARAM_T = ctypes.c_ssize_t

user32 = ctypes.windll.user32
user32.PostThreadMessageW.argtypes = [wt.DWORD, wt.UINT, WPARAM_T, LPARAM_T]
user32.PostThreadMessageW.restype = wt.BOOL
user32.GetMessageW.argtypes = [
    ctypes.POINTER(wt.MSG),
    wt.HWND,
    wt.UINT,
    wt.UINT,
]
user32.GetMessageW.restype = ctypes.c_int
user32.RegisterHotKey.argtypes = [wt.HWND, ctypes.c_int, wt.UINT, wt.UINT]
user32.RegisterHotKey.restype = wt.BOOL
user32.UnregisterHotKey.argtypes = [wt.HWND, ctypes.c_int]
user32.UnregisterHotKey.restype = wt.BOOL
user32.TranslateMessage.argtypes = [ctypes.POINTER(wt.MSG)]
user32.DispatchMessageW.argtypes = [ctypes.POINTER(wt.MSG)]

_intercept_flag = ctypes.c_int(0)
_cmd_queue = None
_last_vol_cmd_at = 0.0
_last_vol_cmd = None
_VOL_DEBOUNCE_SEC = 0.22
_hotkey_thread_id = None
_main_thread_id = None
_quit_event = threading.Event()
_hotkey_labels: dict[str, str] = {}


def parse_hotkey(spec: str) -> tuple[int, int] | None:
    """Parse 'Ctrl+Alt+PgUp' → (modifiers, vk)."""
    if not spec or not str(spec).strip():
        return None
    parts = [p.strip().lower() for p in str(spec).replace("-", "+").split("+") if p.strip()]
    if not parts:
        return None
    mods = 0
    key = None
    for p in parts:
        if p in ("ctrl", "control", "ctl"):
            mods |= MOD_CONTROL
        elif p in ("alt", "menu"):
            mods |= MOD_ALT
        elif p in ("shift",):
            mods |= MOD_SHIFT
        elif p in ("win", "windows", "cmd", "super", "meta"):
            mods |= MOD_WIN
        else:
            key = p
    if not key:
        return None
    vk = _VK_ALIASES.get(key)
    if vk is None and len(key) == 1:
        vk = ord(key.upper())
    if vk is None:
        return None
    return mods, int(vk)


def hotkey_label(cfg: dict | None = None) -> dict[str, str]:
    c = cfg or load_config()
    return {
        "vol_up": str(c.get("hotkey_vol_up") or DEFAULT_CFG["hotkey_vol_up"]),
        "vol_down": str(c.get("hotkey_vol_down") or DEFAULT_CFG["hotkey_vol_down"]),
        "mute": str(c.get("hotkey_mute") or DEFAULT_CFG["hotkey_mute"]),
        "quit": str(c.get("hotkey_quit") or DEFAULT_CFG["hotkey_quit"]),
    }


def _enqueue_vol(cmd: str) -> bool:
    """Enqueue at most one up/down/mute per debounce window. Returns True if accepted."""
    global _last_vol_cmd_at, _last_vol_cmd
    now = time.monotonic()
    if cmd in ("up", "down", "mute"):
        if (now - _last_vol_cmd_at) < _VOL_DEBOUNCE_SEC and _last_vol_cmd == cmd:
            return False
        _last_vol_cmd_at = now
        _last_vol_cmd = cmd
    try:
        if _cmd_queue is None:
            return False
        _cmd_queue.put_nowait(cmd)
        return True
    except Exception:
        return False


def reload_hotkeys() -> None:
    tid = _hotkey_thread_id
    if tid:
        try:
            user32.PostThreadMessageW(int(tid), WM_APP_RELOAD_HOTKEYS, 0, 0)
        except Exception as e:
            _log(f"[hotkey] reload post failed: {e}")


def _hotkey_loop(cmd_queue, stop_event: threading.Event):
    """Dedicated thread: blocking GetMessage + RegisterHotKey only."""
    global _hotkey_thread_id, _cmd_queue, _hotkey_labels
    _cmd_queue = cmd_queue
    _hotkey_thread_id = ctypes.windll.kernel32.GetCurrentThreadId()
    _log(f"[hotkey] thread_id={_hotkey_thread_id}")

    registered: list[int] = []

    def _unregister_all() -> None:
        for hid in list(registered):
            try:
                user32.UnregisterHotKey(None, hid)
            except Exception:
                pass
        registered.clear()

    def _register_all() -> None:
        global _hotkey_labels
        nonlocal registered
        _unregister_all()
        cfg = load_config()
        labels = hotkey_label(cfg)
        _hotkey_labels = dict(labels)
        specs = [
            (HOTKEY_QUIT, labels["quit"], "quit"),
            (HOTKEY_MUTE, labels["mute"], "mute"),
            (HOTKEY_VOLUP, labels["vol_up"], "vol_up"),
            (HOTKEY_VOLDOWN, labels["vol_down"], "vol_down"),
        ]
        for hid, spec, name in specs:
            parsed = parse_hotkey(spec)
            if not parsed:
                _log(f"[hotkey] invalid {name}={spec!r}")
                continue
            mods, vk = parsed
            ok = bool(user32.RegisterHotKey(None, hid, mods | MOD_NOREPEAT, vk))
            if ok:
                registered.append(hid)
                _log(f"[hotkey] registered {name}={spec}")
            else:
                err = ctypes.windll.kernel32.GetLastError()
                _log(f"[hotkey] RegisterHotKey failed {name}={spec} err={err}")

    _register_all()

    msg = wt.MSG()
    while not stop_event.is_set() and not _quit_event.is_set():
        r = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
        if r == 0 or r == -1:
            break
        if msg.message == WM_HOTKEY:
            kid = int(msg.wParam)
            if kid == HOTKEY_QUIT:
                try:
                    cmd_queue.put_nowait("quit")
                except Exception:
                    post_quit()
            elif kid == HOTKEY_MUTE:
                if _intercept_flag.value:
                    _enqueue_vol("mute")
            elif kid == HOTKEY_VOLUP:
                if _intercept_flag.value:
                    _enqueue_vol("up")
            elif kid == HOTKEY_VOLDOWN:
                if _intercept_flag.value:
                    _enqueue_vol("down")
        elif msg.message == WM_APP_RELOAD_HOTKEYS:
            _register_all()
            _log("[hotkey] reloaded from config")
        else:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    _unregister_all()
    _log("[hotkey] loop ended")


def post_quit():
    """Signal exit from ANY thread (tray / worker / hotkey)."""
    _quit_event.set()
    set_intercept(False)
    try:
        _sysvol.disarm()
    except Exception:
        pass
    for tid in (_hotkey_thread_id, _main_thread_id):
        if tid:
            try:
                user32.PostThreadMessageW(int(tid), WM_QUIT, 0, 0)
            except Exception as e:
                _log(f"[quit] PostThreadMessage: {e}")


def set_intercept(enabled: bool) -> None:
    # Volume/mute hotkeys stay registered (with modifiers); actions gated here.
    _intercept_flag.value = 1 if enabled else 0


def open_hotkey_settings(cfg: dict) -> None:
    """Simple dialog to edit hotkeys; saves config.json and reloads bindings."""
    try:
        import tkinter as tk
        from tkinter import messagebox
    except Exception as e:
        _log(f"[hotkey] tkinter unavailable: {e}")
        try:
            os.startfile(str(CONFIG_PATH))
        except Exception:
            pass
        return

    def _run():
        root = tk.Tk()
        root.title("TuneBlade 快捷键")
        root.attributes("-topmost", True)
        root.resizable(False, False)
        labels = hotkey_label(cfg)
        fields = [
            ("升高音量", "hotkey_vol_up", labels["vol_up"]),
            ("降低音量", "hotkey_vol_down", labels["vol_down"]),
            ("静音切换", "hotkey_mute", labels["mute"]),
            ("退出程序", "hotkey_quit", labels["quit"]),
        ]
        entries: dict[str, tk.Entry] = {}
        frm = tk.Frame(root, padx=14, pady=12)
        frm.pack(fill="both", expand=True)
        tk.Label(
            frm,
            text="格式示例：Ctrl+Alt+PgUp、Ctrl+Shift+Up、Ctrl+Alt+F1",
            fg="#555",
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))
        for i, (title, key, val) in enumerate(fields, start=1):
            tk.Label(frm, text=title, width=10, anchor="w").grid(row=i, column=0, sticky="w", pady=3)
            ent = tk.Entry(frm, width=28)
            ent.insert(0, val)
            ent.grid(row=i, column=1, sticky="we", pady=3)
            entries[key] = ent

        def on_save():
            patch = {}
            for key, ent in entries.items():
                spec = ent.get().strip()
                if not parse_hotkey(spec):
                    messagebox.showerror("无效快捷键", f"{key}: {spec}\n请用 Ctrl/Alt/Shift + 键名", parent=root)
                    return
                patch[key] = spec
            cfg.update(patch)
            save_config(cfg)
            reload_hotkeys()
            messagebox.showinfo("已保存", "快捷键已更新并重新注册", parent=root)
            root.destroy()

        def on_reset():
            for key, ent in entries.items():
                ent.delete(0, "end")
                ent.insert(0, str(DEFAULT_CFG[key]))

        btns = tk.Frame(frm)
        btns.grid(row=len(fields) + 1, column=0, columnspan=2, pady=(12, 0), sticky="e")
        tk.Button(btns, text="恢复默认", command=on_reset).pack(side="left", padx=(0, 8))
        tk.Button(btns, text="取消", command=root.destroy).pack(side="left", padx=(0, 8))
        tk.Button(btns, text="保存", command=on_save).pack(side="left")
        root.mainloop()

    threading.Thread(target=_run, daemon=True, name="hotkey-ui").start()


def uia_worker_loop(ctrl: "TuneBladeController", cmd_queue, stop_event: threading.Event):
    """
    Single worker for all UIA / volume changes.
    CoInitialize so COM calls from this thread are valid.
    """
    try:
        ctypes.windll.ole32.CoInitialize(None)
    except Exception:
        pass
    _log("[worker] UIA worker started")
    while not stop_event.is_set():
        try:
            cmd = cmd_queue.get(timeout=0.2)
        except Exception:
            continue
        try:
            if cmd == "quit":
                post_quit()
            elif cmd == "up":
                ctrl.volume_up()
            elif cmd == "down":
                ctrl.volume_down()
            elif cmd == "mute":
                ctrl.toggle_mute()
            elif cmd == "refresh":
                ctrl.refresh_routing_state()
        except Exception as e:
            _log(f"[worker] {cmd}: {e}")
    try:
        ctypes.windll.ole32.CoUninitialize()
    except Exception:
        pass
    _log("[worker] stopped")


# ── UIA: find device "游戏室" slider ───────────────────────────────

def _parse_device_parent(parent):
    """
    From a device row parent, extract (name, status, slider, vol_text).
    name = first TextControl that isn't status/volume chrome.
    """
    status = None
    slider = None
    vol = None
    name = None
    texts = []
    try:
        child = parent.GetFirstChildControl()
        while child:
            ct = child.ControlTypeName
            cn = (child.Name or "").strip()
            if ct == "TextControl" and cn:
                texts.append(cn)
                if "Volume" in cn:
                    gc = child.GetFirstChildControl()
                    if gc and gc.ControlTypeName == "TextControl":
                        try:
                            vol = int(gc.Name.strip())
                        except Exception:
                            pass
                else:
                    low = cn.lower()
                    if low in _DEVICE_NAME_SKIP or low.endswith("%"):
                        if low in (
                            "connected",
                            "disconnected",
                            "connection standby",
                        ):
                            status = cn
                    elif name is None:
                        name = cn
            if ct == "SliderControl":
                slider = child
            child = child.GetNextSiblingControl()
    except Exception:
        pass
    # status might appear before we classified it
    if status is None:
        for cn in texts:
            if cn.lower() in ("connected", "disconnected", "connection standby"):
                status = cn
                break
    return name, status, slider, vol


def _list_all_devices(root) -> list[dict]:
    """
    Discover AirPlay receiver rows (each has a SliderControl + a name label).
    Skips the Master panel slider.
    """
    devices = []
    seen_names = set()

    def walk(ctrl, depth=0, under_master=False):
        if depth > 45:
            return
        try:
            caid = ""
            try:
                caid = ctrl.AutomationId or ""
            except Exception:
                pass
            ctype = ctrl.ControlTypeName

            if ctype == "ButtonControl" and caid == "masterPanel":
                under_master = True

            if (
                not under_master
                and ctype in ("ButtonControl", "PaneControl", "CustomControl", "GroupControl")
            ):
                # candidate row if it directly contains a SliderControl
                has_slider = False
                try:
                    c = ctrl.GetFirstChildControl()
                    while c:
                        if c.ControlTypeName == "SliderControl":
                            has_slider = True
                            break
                        c = c.GetNextSiblingControl()
                except Exception:
                    pass
                if has_slider:
                    name, status, slider, vol = _parse_device_parent(ctrl)
                    if name and name not in seen_names and slider is not None:
                        seen_names.add(name)
                        devices.append(
                            {
                                "name": name,
                                "status": status,
                                "slider": slider,
                                "volume": vol,
                                "row": ctrl,
                            }
                        )

            child = ctrl.GetFirstChildControl()
            while child:
                # children of masterPanel stay under_master
                walk(child, depth + 1, under_master)
                child = child.GetNextSiblingControl()
        except Exception:
            pass

    walk(root)
    return devices


def _find_device_block(root, device_name: str):
    """
    Find device by name (exact). If device_name empty, return the first device.
    Returns (parent_or_None, status, slider, vol).
    """
    devices = _list_all_devices(root)
    if not devices:
        return None, None, None, None
    want = (device_name or "").strip()
    if want:
        for d in devices:
            if d["name"] == want:
                return None, d["status"], d["slider"], d["volume"]
    # auto / not found → first device
    d = devices[0]
    return None, d["status"], d["slider"], d["volume"]


def _read_master_on(root) -> bool | None:
    """
    Master connected? Prefer masterConnectDisconnectButton HelpText:
      - 'Disconnect All' → currently ON (connected)
      - 'Connect All'    → currently OFF (disconnected)
    Do NOT use the 'ON'/'OFF' TextControl under masterPanel — that is the EQ label.
    """
    btn = _find_by_automation_id(root, "masterConnectDisconnectButton")
    if btn is not None:
        try:
            help_txt = (getattr(btn, "HelpText", None) or "").strip().lower()
            if not help_txt:
                try:
                    help_txt = (btn.GetPropertyValue(30013) or "").strip().lower()  # HelpTextProperty
                except Exception:
                    help_txt = ""
            if "disconnect" in help_txt:
                return True
            if "connect" in help_txt:
                return False
        except Exception:
            pass

    # Fallback: legacy ON/OFF text, but skip the EQ row's ON
    state = [None]

    def walk(ctrl, in_master=False, after_eq=False, depth=0):
        if state[0] is not None or depth > 30:
            return
        try:
            ctype = ctrl.ControlTypeName
            cname = (ctrl.Name or "").strip()
            caid = ""
            try:
                caid = ctrl.AutomationId or ""
            except Exception:
                pass
            if not in_master and ctype == "ButtonControl" and caid == "masterPanel":
                in_master = True
            if in_master and ctype == "TextControl":
                if cname.upper() == "EQ":
                    after_eq = True
                elif cname.upper() in ("ON", "OFF"):
                    if after_eq:
                        # EQ's own ON/OFF — ignore
                        after_eq = False
                    else:
                        state[0] = cname.upper() == "ON"
                        return
            child = ctrl.GetFirstChildControl()
            while child:
                walk(child, in_master, after_eq, depth + 1)
                child = child.GetNextSiblingControl()
        except Exception:
            pass

    walk(root)
    return state[0]


def _find_by_automation_id(root, automation_id: str, depth_limit: int = 40):
    """DFS find — avoids uiautomation Refind timeouts."""
    found = [None]

    def walk(ctrl, depth=0):
        if found[0] is not None or depth > depth_limit:
            return
        try:
            caid = ""
            try:
                caid = ctrl.AutomationId or ""
            except Exception:
                pass
            if caid == automation_id:
                found[0] = ctrl
                return
            child = ctrl.GetFirstChildControl()
            while child:
                walk(child, depth + 1)
                child = child.GetNextSiblingControl()
        except Exception:
            pass

    walk(root)
    return found[0]


def ensure_master_on(root) -> bool:
    """
    If Master shows OFF, click masterConnectDisconnectButton once to turn ON.
    Does NOT ShowWindow/Hide — that can white-screen TuneBlade.
    Returns True if Master is ON afterwards (or already was).
    """
    btn = _find_by_automation_id(root, "masterConnectDisconnectButton")
    on = _read_master_on(root)
    if on is True:
        return True
    if btn is None:
        _log("[master] connect button not found")
        return False

    help_txt = ""
    try:
        help_txt = (getattr(btn, "HelpText", None) or "").strip().lower()
    except Exception:
        help_txt = ""
    if "disconnect" in help_txt:
        return True
    if on is None and "connect" not in help_txt:
        # UI not ready — try again later
        _log("[master] state unknown, skip this tick")
        return False

    try:
        pattern = btn.GetInvokePattern()
        if pattern is None:
            _log("[master] no InvokePattern")
            return False
        # Invoke only — never mouse Click. Hidden TuneBlade rows often report
        # BoundingRectangle at (0,0); Click would yank the cursor to top-left
        # every boot poll.
        pattern.Invoke()
        time.sleep(0.8)
        _log("[master] invoked connect button to enable")
        return True
    except Exception as e:
        _log(f"[master] enable failed: {e}")
        return False


def _find_device_connect_button(row):
    """Small square button on the right of a device row (connect/disconnect)."""
    if row is None:
        return None
    candidates = []

    def walk(ctrl, depth=0):
        if depth > 6:
            return
        try:
            if ctrl.ControlTypeName == "ButtonControl":
                aid = ""
                try:
                    aid = ctrl.AutomationId or ""
                except Exception:
                    pass
                if aid in ("IncreaseLarge", "DecreaseLarge"):
                    return
                try:
                    r = ctrl.BoundingRectangle
                    w, h = int(r.width()), int(r.height())
                except Exception:
                    return
                # Device connect is a small icon button (~25–40px), not the whole row
                if 16 <= w <= 48 and 16 <= h <= 48:
                    candidates.append((r.left, ctrl))
            child = ctrl.GetFirstChildControl()
            while child:
                walk(child, depth + 1)
                child = child.GetNextSiblingControl()
        except Exception:
            pass

    walk(row)
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]


def ensure_device_connected(device: dict) -> bool:
    """
    If device status is Disconnected, click its connect button once.
    Leaves Connected / Connection Standby alone.
    """
    if not device:
        return False
    status = (device.get("status") or "").strip().lower()
    if status in ("connected", "connection standby"):
        return True
    if status and status != "disconnected":
        # Unknown status — don't click
        return False
    btn = _find_device_connect_button(device.get("row"))
    if btn is None:
        _log(f"[device] connect button not found for {device.get('name')!r}")
        return False
    try:
        pattern = btn.GetInvokePattern()
        if pattern is None:
            return False
        pattern.Invoke()
        time.sleep(0.9)
        _log(f"[device] clicked connect for {device.get('name')!r} (was {device.get('status')!r})")
        return True
    except Exception as e:
        _log(f"[device] connect failed: {e}")
        return False


def _slider_get(slider) -> float | None:
    if slider is None:
        return None
    try:
        rp = slider.GetRangeValuePattern()
        if rp:
            return float(rp.Value)
    except Exception:
        pass
    return None


def _slider_set(slider, value: float) -> bool:
    if slider is None:
        return False
    try:
        rp = slider.GetRangeValuePattern()
        if not rp:
            return False
        lo = float(rp.Minimum)
        hi = float(rp.Maximum)
        v = max(lo, min(hi, float(value)))
        rp.SetValue(v)
        return True
    except Exception as e:
        _log(f"[slider_set] {e}")
        return False


# ── Controller ────────────────────────────────────────────────────

class TuneBladeController:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.device_name = str(cfg.get("device_name") or "").strip()
        self.step = max(1, int(cfg.get("volume_step") or 5))
        self.muted = False
        # Volume to restore on unmute — never default to 50%.
        self._mute_bk: float | None = None
        self._last_nonzero_vol: float | None = None
        self._lock = threading.RLock()
        self._slider = None
        self._audio_ok = True
        self._tb_found = False
        self._master_on = False
        self._device_status = None
        self._last_vol = None
        self._device_list: list[str] = []
        # Boot window: auto Master + auto device connect (time-based, not try-count).
        # After the user has had Master ON once, never force it back on.
        self._seen_master_on = False
        self._boot_deadline = time.monotonic() + 120.0
        self._device_connect_attempts = 0

        self.refresh_routing_state()

    def list_device_names(self) -> list[str]:
        """Return receiver names currently visible in TuneBlade UI."""
        _hwnd, root = self._root()
        if root is None:
            return []
        try:
            return [d["name"] for d in _list_all_devices(root)]
        except Exception as e:
            _log(f"[list_devices] {e}")
            return []

    def set_device_name(self, name: str, persist: bool = True) -> None:
        name = (name or "").strip()
        with self._lock:
            self.device_name = name
            self.cfg["device_name"] = name
            self._slider = None
        if persist:
            save_config(self.cfg)
        _log(f"[device] selected {name!r}")
        self.refresh_routing_state()

    def _root(self):
        title = self.cfg.get("window_title", "TuneBlade")
        hwnd = win32gui.FindWindow(None, title)
        if not hwnd:
            return None, None
        try:
            return hwnd, auto.ControlFromHandle(hwnd)
        except Exception:
            return hwnd, None

    def refresh_routing_state(self) -> dict:
        audio_ok = has_system_audio()
        tb_found = False
        master_on = False
        status = None
        vol = None
        slider = None

        hwnd, root = self._root()
        devices = []
        if root is not None:
            tb_found = True
            on = _read_master_on(root)
            master_on = bool(on) if on is not None else False

            # Auto-enable Master during boot window only.
            # If user later turns Master OFF, leave it OFF and restore system volume.
            in_boot = time.monotonic() < self._boot_deadline
            if on is True:
                self._seen_master_on = True
            elif (
                self.cfg.get("auto_enable_master", True)
                and not self._seen_master_on
                and in_boot
            ):
                try:
                    ensure_master_on(root)
                    hwnd, root = self._root()
                    if root is not None:
                        on = _read_master_on(root)
                        master_on = bool(on) if on is not None else False
                        if on is True:
                            self._seen_master_on = True
                            _log("[master] auto-enabled at startup")
                except Exception as e:
                    _log(f"[master] auto_enable: {e}")

            try:
                devices = _list_all_devices(root) if root is not None else []
            except Exception as e:
                _log(f"[devices] {e}")
                devices = []

            # Resolve which device to use
            want = (self.device_name or "").strip()
            chosen = None
            if want:
                for d in devices:
                    if d["name"] == want:
                        chosen = d
                        break
            if chosen is None and devices:
                chosen = devices[0]
                # Auto-select first receiver and remember it
                if want != chosen["name"]:
                    _log(
                        f"[device] {want!r} not found or empty → auto {chosen['name']!r}"
                    )
                    with self._lock:
                        self.device_name = chosen["name"]
                        self.cfg["device_name"] = chosen["name"]
                    try:
                        save_config(self.cfg)
                    except Exception:
                        pass

            # Auto-connect selected device while Disconnected (boot window)
            if (
                chosen is not None
                and self.cfg.get("auto_connect_device", True)
                and master_on
                and in_boot
                and self._device_connect_attempts < 15
            ):
                st0 = (chosen.get("status") or "").strip().lower()
                # Only when clearly Disconnected. Standby/Connected = TuneBlade
                # Auto Connect already did its job after Master came on.
                if st0 == "disconnected":
                    self._device_connect_attempts += 1
                    try:
                        ensure_device_connected(chosen)
                        # refresh device list after click
                        try:
                            devices = _list_all_devices(root) if root is not None else devices
                        except Exception:
                            pass
                        want2 = (self.device_name or "").strip()
                        for d in devices:
                            if d["name"] == want2 or (not want2 and d is devices[0]):
                                chosen = d
                                break
                    except Exception as e:
                        _log(f"[device] auto_connect: {e}")

            if chosen is not None:
                status = chosen.get("status")
                slider = chosen.get("slider")
                vol_txt = chosen.get("volume")
                if slider is not None:
                    v = _slider_get(slider)
                    if v is not None:
                        vol = v
                    elif vol_txt is not None:
                        vol = float(vol_txt)

        # Hotkeys as soon as Master is ON and the device row/slider exists
        # (boot: AirPlay may still be connecting). Mute local speakers only when
        # the device is clearly Connected / Connection Standby.
        status_l = (status or "").strip().lower()
        device_routable = status_l in ("connected", "connection standby")
        keys_ok = bool(audio_ok and tb_found and master_on and slider is not None)
        mute_ok = bool(keys_ok and device_routable)
        active = mute_ok  # tray "green" = fully routing (muted + connected)

        was_keys = bool(_intercept_flag.value)
        was_muted = bool(_sysvol.active)
        with self._lock:
            self._audio_ok = audio_ok
            self._tb_found = tb_found
            self._master_on = master_on
            self._device_status = status
            self._slider = slider
            self._last_vol = vol
            self._device_list = [d["name"] for d in devices]
            if vol == 0:
                self.muted = True
            elif vol and vol > 0:
                self.muted = False
                self._last_nonzero_vol = float(vol)
                # Keep a restore point even if mute was entered via vol-down to 0
                if self._mute_bk is None:
                    self._mute_bk = float(vol)

        set_intercept(keys_ok)
        if mute_ok:
            if not was_muted:
                _sysvol.arm()
            else:
                _sysvol.freeze()
        else:
            # Not fully routing — never leave the PC muted.
            _sysvol.disarm()
            _sysvol.clear_leftover_mute()

        if keys_ok and not was_keys:
            _log("[keys] volume hotkeys armed (Master ON + slider)")
        elif not keys_ok and was_keys:
            _log("[keys] volume hotkeys released")

        return {
            "audio_ok": audio_ok,
            "tb_found": tb_found,
            "master_on": master_on,
            "device_status": status,
            "active": active,
            "keys_ok": keys_ok,
            "volume": vol,
            "devices": [d["name"] for d in devices],
        }

    def should_intercept(self) -> bool:
        return bool(_intercept_flag.value)

    def mode_label(self) -> str:
        with self._lock:
            if not self._audio_ok:
                return "无音响 · 系统音量"
            if not self._tb_found:
                return "TuneBlade 未运行 · 系统音量"
            if not self._master_on:
                return "Master OFF · 系统音量"
            st = self._device_status or "?"
            if (st or "").lower() == "disconnected":
                return f"{self.device_name} 未连接 · 系统音量"
            vol = self._last_vol
            if self.muted:
                return f"{self.device_name} · 静音"
            if vol is not None:
                return f"{self.device_name} · {int(round(vol))}%"
            return f"{self.device_name} · ON"

    def _ensure_slider(self):
        with self._lock:
            if self._slider is not None:
                try:
                    _ = self._slider.ControlTypeName
                    return self._slider
                except Exception:
                    self._slider = None
        self.refresh_routing_state()
        with self._lock:
            return self._slider

    def get_volume(self) -> float | None:
        slider = self._ensure_slider()
        return _slider_get(slider)

    def _nudge(self, delta: float):
        # Always freeze system volume first when we own keys (keys may still hit OS)
        if self.should_intercept():
            _sysvol.freeze()
        else:
            return
        slider = self._ensure_slider()
        if slider is None:
            _log("[nudge] no slider")
            return
        cur = _slider_get(slider)
        if cur is None:
            _log("[nudge] cannot read slider")
            return
        new_v = cur + delta
        # About to hit 0 via keys — remember level for unmute
        if cur > 0 and new_v <= 0:
            with self._lock:
                self._mute_bk = float(cur)
                self._last_nonzero_vol = float(cur)
        if _slider_set(slider, new_v):
            time.sleep(0.05)
            vol = _slider_get(slider)
            with self._lock:
                self._last_vol = vol
                self.muted = bool(vol is not None and vol <= 0)
                if vol is not None and vol > 0:
                    self._last_nonzero_vol = float(vol)
                    self._mute_bk = float(vol)
            _sysvol.freeze()
            if vol is not None:
                _log(f"volume {cur} -> {vol} (step {delta})")
                show_volume_osd(self.device_name, vol, self.muted)
                if _status_callback:
                    try:
                        _status_callback(self.mode_label())
                    except Exception:
                        pass
        else:
            _log(f"[nudge] SetValue failed cur={cur} delta={delta}")

    def volume_up(self):
        self._nudge(float(self.step))
        self.muted = False

    def volume_down(self):
        self._nudge(-float(self.step))

    def _unmute_target(self) -> float:
        """Restore volume from before mute — prefer backup, never invent 50%."""
        for candidate in (self._mute_bk, self._last_nonzero_vol, self._last_vol):
            try:
                v = float(candidate) if candidate is not None else 0.0
            except (TypeError, ValueError):
                continue
            if v > 0:
                return max(v, float(self.step))
        # Last resort if we never saw a non-zero level this session
        return max(25.0, float(self.step))

    def toggle_mute(self):
        if self.should_intercept():
            _sysvol.freeze()
        else:
            return
        slider = self._ensure_slider()
        if slider is None:
            return
        vol = _slider_get(slider)
        if vol is None:
            return
        if not self.muted and vol > 0:
            self._mute_bk = float(vol)
            self._last_nonzero_vol = float(vol)
            if _slider_set(slider, 0):
                self.muted = True
                with self._lock:
                    self._last_vol = 0
                _log(f"muted (backup {self._mute_bk})")
        else:
            target = self._unmute_target()
            if _slider_set(slider, target):
                self.muted = False
                with self._lock:
                    self._last_vol = target
                    self._last_nonzero_vol = float(target)
                    self._mute_bk = float(target)
                _log(f"unmuted -> {target}")
        _sysvol.freeze()
        show_volume_osd(self.device_name, self._last_vol, self.muted)
        if _status_callback:
            try:
                _status_callback(self.mode_label())
            except Exception:
                pass


_status_callback = None


def set_status_callback(cb):
    global _status_callback
    _status_callback = cb


# ── on-screen volume popup (like Windows volume flyout) ───────────

class VolumeOSD:
    """Small topmost popup: device name + bar + percent."""

    def __init__(self):
        self._q = None
        self._thread = None
        self._started = False

    def start(self):
        if self._started:
            return
        import queue as _queue

        self._q = _queue.Queue()
        self._started = True
        self._thread = threading.Thread(target=self._run, daemon=True, name="volume-osd")
        self._thread.start()

    def show(self, device: str, volume: float | None, muted: bool = False):
        if not self._started:
            self.start()
        try:
            self._q.put_nowait((device, volume, muted))
        except Exception:
            pass

    def _run(self):
        try:
            import tkinter as tk
            from tkinter import font as tkfont
        except Exception as e:
            _log(f"[osd] tkinter unavailable: {e}")
            return

        root = tk.Tk()
        root.withdraw()
        win = tk.Toplevel(root)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        try:
            win.attributes("-alpha", 0.95)
        except Exception:
            pass
        bg = "#202020"
        win.configure(bg=bg)
        win.withdraw()

        # Prefer fonts that render CJK well on Windows
        ui = "Microsoft YaHei UI"
        try:
            tkfont.Font(family=ui, size=11)
        except Exception:
            ui = "Segoe UI"

        W, H = 340, 118
        PAD_X, PAD_Y = 20, 16
        BAR_W, BAR_H = 220, 10

        frame = tk.Frame(win, bg=bg, width=W, height=H)
        frame.pack(fill="both", expand=True)
        frame.pack_propagate(False)

        title_lbl = tk.Label(
            frame, text="TuneBlade", fg="#e5e5e5", bg=bg,
            font=(ui, 12), anchor="w",
        )
        title_lbl.place(x=PAD_X, y=PAD_Y, width=W - PAD_X * 2 - 70, height=24)

        pct_lbl = tk.Label(
            frame, text="0%", fg="#4ade80", bg=bg,
            font=(ui, 16, "bold"), anchor="e",
        )
        pct_lbl.place(x=W - PAD_X - 70, y=PAD_Y - 2, width=70, height=28)

        # Drawn progress bar (avoids font/glyph clipping of █░)
        bar_bg = tk.Canvas(
            frame, width=BAR_W, height=BAR_H, bg="#3f3f46",
            highlightthickness=0, bd=0,
        )
        bar_bg.place(x=PAD_X, y=PAD_Y + 40)
        bar_fill = bar_bg.create_rectangle(0, 0, 0, BAR_H, fill="#4ade80", width=0)

        tip_lbl = tk.Label(
            frame, text="", fg="#a1a1aa", bg=bg,
            font=(ui, 9), anchor="w",
        )
        tip_lbl.place(x=PAD_X, y=PAD_Y + 58, width=W - PAD_X * 2, height=20)

        hide_job = {"id": None}

        def _place():
            sw = win.winfo_screenwidth()
            sh = win.winfo_screenheight()
            x = max(0, (sw - W) // 2)
            y = max(0, sh - H - 100)
            win.geometry(f"{W}x{H}+{x}+{y}")

        def _hide():
            try:
                win.withdraw()
            except Exception:
                pass

        def _show(device, volume, muted):
            name = (device or "TuneBlade").strip() or "TuneBlade"
            title_lbl.config(text=name)
            if muted or (volume is not None and volume <= 0):
                v = 0
                pct_lbl.config(text="静音", fg="#f87171")
                tip_lbl.config(text="已静音")
                bar_bg.itemconfig(bar_fill, fill="#f87171")
                bar_bg.coords(bar_fill, 0, 0, 0, BAR_H)
            else:
                v = 0 if volume is None else int(round(max(0, min(100, float(volume)))))
                pct_lbl.config(text=f"{v}%", fg="#4ade80")
                tip_lbl.config(text="TuneBlade 音量")
                bar_bg.itemconfig(bar_fill, fill="#4ade80")
                fw = int(BAR_W * (v / 100.0))
                bar_bg.coords(bar_fill, 0, 0, fw, BAR_H)

            _place()
            win.deiconify()
            win.lift()
            try:
                win.attributes("-topmost", True)
            except Exception:
                pass
            if hide_job["id"] is not None:
                try:
                    root.after_cancel(hide_job["id"])
                except Exception:
                    pass
            hide_job["id"] = root.after(1500, _hide)

        def _poll():
            try:
                while True:
                    device, volume, muted = self._q.get_nowait()
                    try:
                        while True:
                            device, volume, muted = self._q.get_nowait()
                    except Exception:
                        pass
                    _show(device, volume, muted)
            except Exception:
                pass
            root.after(50, _poll)

        root.after(50, _poll)
        try:
            root.mainloop()
        except Exception as e:
            _log(f"[osd] mainloop: {e}")


_volume_osd = VolumeOSD()


def show_volume_osd(device: str, volume: float | None, muted: bool = False):
    try:
        _volume_osd.show(device, volume, muted)
    except Exception as e:
        _log(f"[osd] {e}")


# ── tray ──────────────────────────────────────────────────────────

def _make_icon(active: bool):
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    color = (46, 204, 113, 255) if active else (149, 165, 166, 255)
    draw.ellipse((4, 4, 60, 60), fill=color)
    draw.rectangle((18, 26, 28, 38), fill=(255, 255, 255, 230))
    draw.polygon([(28, 26), (40, 18), (40, 46), (28, 38)], fill=(255, 255, 255, 230))
    return img


class TrayApp:
    def __init__(self, ctrl: TuneBladeController, cfg: dict):
        self.ctrl = ctrl
        self.cfg = cfg
        self.icon = None
        self._stop = threading.Event()

    def _menu(self):
        def toggle_autostart(icon, item):
            new_val = not is_autostart_enabled()
            set_autostart(new_val)
            self.cfg["autostart"] = new_val
            save_config(self.cfg)
            self._refresh_menu()

        def do_quit(icon, item):
            _log("[quit] tray exit clicked")
            self._stop.set()
            post_quit()
            try:
                icon.stop()
            except Exception:
                pass

            def _force():
                time.sleep(0.8)
                _log("[quit] force os._exit")
                os._exit(0)

            threading.Thread(target=_force, daemon=True).start()

        def refresh(icon, item):
            st = self.ctrl.refresh_routing_state()
            self._apply_icon(st["active"])
            if self.icon:
                self.icon.title = f"{APP_NAME}\n{self.ctrl.mode_label()}"
            self._refresh_menu()

        def _device_items():
            # Must only use callbacks with 0–2 positional args (pystray requirement)
            try:
                names = list(self.ctrl._device_list) or self.ctrl.list_device_names()
            except Exception as e:
                _log(f"[tray] list devices: {e}")
                names = []
            if not names:
                return (
                    pystray.MenuItem(
                        "(未检测到设备 — 请先打开 TuneBlade)", None, enabled=False
                    ),
                )
            items = []
            for name in names:
                # Closure with exactly (icon, item) — do NOT add extra params
                def _make_handler(device):
                    def handler(icon, item):
                        try:
                            self.ctrl.set_device_name(device)
                            self._apply_icon(self.ctrl.should_intercept())
                            if self.icon:
                                self.icon.title = (
                                    f"{APP_NAME}\n{self.ctrl.mode_label()}"
                                )
                            self._refresh_menu()
                        except Exception as ex:
                            _log(f"[tray] select device: {ex}")

                    return handler

                def _make_checked(device):
                    def checked(item):
                        return self.ctrl.device_name == device

                    return checked

                items.append(
                    pystray.MenuItem(
                        name,
                        _make_handler(name),
                        checked=_make_checked(name),
                        radio=True,
                    )
                )
            return tuple(items)

        try:
            device_menu = pystray.Menu(_device_items)
        except Exception as e:
            _log(f"[tray] device menu: {e}")
            device_menu = pystray.Menu(
                pystray.MenuItem("(设备菜单不可用)", None, enabled=False)
            )

        def open_hotkeys(icon, item):
            open_hotkey_settings(self.cfg)
            self._refresh_menu()

        def reload_keys(icon, item):
            # Re-read config from disk (user may have edited config.json)
            try:
                self.cfg.update(load_config())
            except Exception:
                pass
            reload_hotkeys()
            self._refresh_menu()

        def open_cfg(icon, item):
            try:
                os.startfile(str(CONFIG_PATH))
            except Exception as e:
                _log(f"[tray] open config: {e}")

        hk = hotkey_label(self.cfg)
        hotkey_menu = pystray.Menu(
            pystray.MenuItem(f"升高：{hk['vol_up']}", None, enabled=False),
            pystray.MenuItem(f"降低：{hk['vol_down']}", None, enabled=False),
            pystray.MenuItem(f"静音：{hk['mute']}", None, enabled=False),
            pystray.MenuItem(f"退出：{hk['quit']}", None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("设置快捷键…", open_hotkeys),
            pystray.MenuItem("打开 config.json", open_cfg),
            pystray.MenuItem("重新加载快捷键", reload_keys),
        )

        return pystray.Menu(
            pystray.MenuItem(lambda item: self.ctrl.mode_label(), None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("选择设备", device_menu),
            pystray.MenuItem("快捷键", hotkey_menu),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "开机自启",
                toggle_autostart,
                checked=lambda item: is_autostart_enabled(),
            ),
            pystray.MenuItem("立即刷新状态", refresh),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出", do_quit),
        )

    def _refresh_menu(self):
        if self.icon:
            self.icon.menu = self._menu()
            try:
                self.icon.update_menu()
            except Exception:
                pass

    def _apply_icon(self, active: bool):
        if self.icon:
            try:
                self.icon.icon = _make_icon(active)
            except Exception:
                pass

    def _poller(self):
        interval = float(self.cfg.get("poll_interval_sec", 1.0) or 1.0)
        while not self._stop.wait(interval):
            try:
                st = self.ctrl.refresh_routing_state()
                self._apply_icon(st["active"])
                if self.icon:
                    self.icon.title = f"{APP_NAME}\n{self.ctrl.mode_label()}"
            except Exception as e:
                _log(f"[poll] {e}")

    def run_async(self):
        if pystray is None:
            _log("[Warning] pystray not installed")
            return None
        st = self.ctrl.refresh_routing_state()

        def _run_tray():
            try:
                menu = self._menu()
            except Exception as e:
                _log(f"[tray] menu build failed, using minimal menu: {e}")
                menu = pystray.Menu(
                    pystray.MenuItem("退出", lambda icon, item: (post_quit(), icon.stop()))
                )
            try:
                self.icon = pystray.Icon(
                    "tuneblade_controller",
                    _make_icon(st["active"]),
                    f"{APP_NAME}\n{self.ctrl.mode_label()}",
                    menu,
                )
                set_status_callback(
                    lambda t: setattr(self.icon, "title", f"{APP_NAME}\n{t}")
                    if self.icon
                    else None
                )
                self.icon.run()
            except Exception as e:
                _log(f"[tray] run failed: {e}")

        threading.Thread(target=self._poller, daemon=True, name="tray-poller").start()
        threading.Thread(target=_run_tray, daemon=True, name="tray").start()
        return True


# ── main ──────────────────────────────────────────────────────────

_instance_mutex = None  # keep alive for process lifetime


def _acquire_single_instance() -> bool:
    """Return False if another TuneBladeController is already running."""
    global _instance_mutex
    try:
        import win32api
        import win32event
        import winerror

        handle = win32event.CreateMutex(
            None, False, "Local\\TuneBladeController_SingleInstance"
        )
        _instance_mutex = handle
        if win32api.GetLastError() == winerror.ERROR_ALREADY_EXISTS:
            return False
        return True
    except Exception as e:
        _log(f"[main] single-instance mutex failed: {e}")
        return True


def main():
    import queue

    global _main_thread_id
    # Load config first so we know whether to enable the log file
    cfg = load_config()
    _setup_logging(bool(cfg.get("debug_log")))
    _main_thread_id = ctypes.windll.kernel32.GetCurrentThreadId()
    _log(f"[main] thread_id={_main_thread_id}")

    if not _acquire_single_instance():
        _log("[main] another instance is already running — exit")
        return

    if cfg.get("autostart") and not is_autostart_enabled():
        set_autostart(True)
    elif is_autostart_enabled():
        cfg["autostart"] = True

    # Do NOT block here waiting for TuneBlade — that left hotkeys dead for
    # several seconds after login. Poller will pick TB up as soon as it appears.
    hwnd0 = win32gui.FindWindow(None, cfg.get("window_title", "TuneBlade"))
    if not hwnd0:
        _log("[main] TuneBlade window not found yet (will retry in poller)")

    ctrl = TuneBladeController(cfg)
    st = ctrl.refresh_routing_state()
    if not st.get("active"):
        # Previous run may have left Windows muted after a missed disarm
        _sysvol.clear_leftover_mute()
    _volume_osd.start()

    _log(APP_NAME)
    _log(f"  device        : {cfg.get('device_name')!r}")
    _log(f"  step          : {cfg.get('volume_step')}")
    _log(f"  audio         : {st['audio_ok']}")
    _log(f"  TuneBlade     : {st['tb_found']}")
    _log(f"  Master        : {st['master_on']}")
    _log(f"  device status : {st['device_status']!r}")
    _log(f"  active        : {st['active']}")
    _log(f"  keys_ok       : {st.get('keys_ok')}")
    _log(f"  volume        : {st['volume']}")
    _log(f"  mode          : {ctrl.mode_label()}")
    _log(f"  intercept     : {_intercept_flag.value}")

    cmd_queue: queue.Queue = queue.Queue(maxsize=32)
    stop_event = threading.Event()
    boot_deadline = time.monotonic() + 90.0  # fast-poll window after login
    threading.Thread(
        target=uia_worker_loop,
        args=(ctrl, cmd_queue, stop_event),
        daemon=True,
        name="uia-worker",
    ).start()

    tray = TrayApp(ctrl, cfg)
    tray.run_async()

    def _poll():
        while not stop_event.is_set() and not _quit_event.is_set():
            # Boot: poll faster so Master auto-on + hotkeys engage ASAP
            if time.monotonic() < boot_deadline and not _intercept_flag.value:
                delay = 0.25
            else:
                delay = float(cfg.get("poll_interval_sec", 1.0) or 1.0)
            time.sleep(delay)
            try:
                cmd_queue.put_nowait("refresh")
            except Exception:
                try:
                    ctrl.refresh_routing_state()
                except Exception:
                    pass

    def _sysvol_watch():
        """
        Primary capture path for Fn/media volume keys — watch the Windows
        endpoint (those keys often never appear as hotkeys).
        """
        _log("[sysvol] watcher started")
        while not stop_event.is_set() and not _quit_event.is_set():
            try:
                if _intercept_flag.value and _sysvol.active:
                    cmd = _sysvol.poll_redirect()
                    if cmd:
                        # same debounce as keyboard — one physical action → one ±5
                        _enqueue_vol(cmd)
                time.sleep(0.04)
            except Exception as e:
                _log(f"[sysvol] watcher: {e}")
                time.sleep(0.2)
        _log("[sysvol] watcher stopped")

    threading.Thread(target=_poll, daemon=True, name="poller").start()
    threading.Thread(target=_sysvol_watch, daemon=True, name="sysvol-watch").start()
    threading.Thread(
        target=_hotkey_loop,
        args=(cmd_queue, stop_event),
        daemon=True,
        name="hotkeys",
    ).start()
    _log("[hotkey] RegisterHotKey thread started (no WH_KEYBOARD_LL)")

    try:
        signal.signal(signal.SIGINT, lambda s, f: post_quit())
    except Exception:
        pass

    try:
        _quit_event.wait()
    finally:
        _log("[quit] cleaning up…")
        stop_event.set()
        _quit_event.set()
        set_intercept(False)
        try:
            _sysvol.disarm()
        except Exception:
            pass
        if tray.icon:
            try:
                tray.icon.stop()
            except Exception:
                pass
        _log("Exited.")
        # Ensure no orphan process left in Task Manager
        os._exit(0)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import traceback

        err = f"{e}\n\n{traceback.format_exc()}"
        try:
            _setup_logging(True)
            _log(err)
        except Exception:
            pass
        _msgbox(f"启动失败：{e}")
        raise
