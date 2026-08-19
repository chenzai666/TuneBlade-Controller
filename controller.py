"""
TuneBlade Controller — tray-resident volume bridge.

- Volume keys control the device slider named in config (default: 游戏室).
- Step is exactly volume_step percent (default: 5) via RangeValuePattern.
- Only intercept when Master is ON and system has speakers; otherwise pass through.
- Low-level hook is fail-safe so normal typing never breaks.
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
    Many laptops never deliver VK_VOLUME_* to WH_KEYBOARD_LL — they only
    change the Windows endpoint volume. While armed we:
      1) detect that system volume/mute changed
      2) snap it back to the baseline
      3) return 'up' / 'down' / 'mute' so TuneBlade can mirror the intent
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._active = False
        self._baseline = None  # float 0..1
        self._baseline_mute = None
        self._ep = None
        self._cooldown_until = 0.0

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
            return None

    def arm(self):
        with self._lock:
            ep = self._endpoint()
            if ep is None:
                self._active = False
                return
            try:
                self._baseline = float(ep.GetMasterVolumeLevelScalar())
                self._baseline_mute = int(ep.GetMute())
                self._active = True
                self._cooldown_until = 0.0
                _log(
                    f"[sysvol] armed baseline={self._baseline:.3f} mute={self._baseline_mute}"
                )
            except Exception as e:
                _log(f"[sysvol] arm failed: {e}")
                self._active = False

    def disarm(self):
        with self._lock:
            self._active = False
            self._baseline = None
            _log("[sysvol] disarmed")

    @property
    def active(self) -> bool:
        return self._active

    def freeze(self) -> bool:
        """Restore baseline only (no command)."""
        with self._lock:
            return self._restore_unlocked()

    def _restore_unlocked(self) -> bool:
        if not self._active or self._baseline is None:
            return False
        ep = self._endpoint()
        if ep is None:
            return False
        try:
            ep.SetMasterVolumeLevelScalar(self._baseline, None)
            ep.SetMute(int(self._baseline_mute or 0), None)
            return True
        except Exception as e:
            _log(f"[sysvol] restore: {e}")
            return False

    def poll_redirect(self) -> str | None:
        """
        If OS volume changed since baseline, restore it and return
        'up' | 'down' | 'mute'. None if nothing happened.
        """
        with self._lock:
            if not self._active or self._baseline is None:
                return None
            now = time.monotonic()
            if now < self._cooldown_until:
                # still settle after our own restore
                try:
                    self._restore_unlocked()
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
                return None

            d_vol = cur - float(self._baseline)
            d_mute = muted - int(self._baseline_mute or 0)

            if abs(d_vol) < 0.0015 and d_mute == 0:
                return None

            # Classify intent before restoring
            cmd = None
            if d_mute != 0 and abs(d_vol) < 0.02:
                # pure mute toggle
                cmd = "mute"
            elif d_vol > 0.0015 or (d_mute < 0 and d_vol >= -0.001):
                # louder or unmute-via-vol-up
                cmd = "up"
            elif d_vol < -0.0015:
                cmd = "down"
            else:
                cmd = "mute"

            self._restore_unlocked()
            self._cooldown_until = now + 0.25
            _log(f"[sysvol] redirect {cmd} (d_vol={d_vol:+.3f} d_mute={d_mute:+d})")
            # Dismiss Windows volume flyout (appears before we can swallow Fn-keys)
            try:
                _osd_burst_hide()
            except Exception:
                pass
            return cmd


# Longer cooldown so one media-key press cannot enqueue multiple redirects


_sysvol = SystemVolumeLock()


def hide_volume_osd() -> None:
    """
    Hide the Windows volume flyout/OSD that appears on media-key presses.
    We cannot always swallow Fn-keys before Explorer shows it, so we dismiss
    the OSD window as soon as we redirect the volume change.
    """
    try:
        import win32api
        import win32gui
        import win32process
        import win32con
    except Exception:
        return

    targets = []

    def _pid_name(pid: int) -> str:
        try:
            import win32api as wapi
            from win32com.client import GetObject

            # fallback via CreateToolhelp — keep simple
        except Exception:
            pass
        try:
            h = win32api.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            try:
                # QueryFullProcessImageName
                buf = ctypes.create_unicode_buffer(260)
                size = ctypes.c_uint(260)
                if ctypes.windll.kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
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
            # Win10 classic host + Win10/11 XAML / CoreWindow flyouts
            if cls not in (
                "NativeHWNDHost",
                "Windows.UI.Core.CoreWindow",
                "XamlExplorerHostIslandWindow",
                "Windows.Internal.Shell.TabProxyWindow",
            ):
                return
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            path = _pid_name(pid)
            # Volume OSD lives in explorer or ShellExperienceHost
            if (
                "shellexperiencehost" in path
                or path.endswith("\\explorer.exe")
                or "explorer.exe" in path
                or not path
            ):
                rect = win32gui.GetWindowRect(hwnd)
                w, h = rect[2] - rect[0], rect[3] - rect[1]
                # Volume flyout is a small floating panel, not a full screen window
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
            # also try closing/destroying the flyout host gently
            win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
        except Exception:
            pass


def _osd_burst_hide():
    """Hide OSD several times over ~400ms (it can reappear once)."""

    def _run():
        for _ in range(8):
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


# ── keyboard hook (Page Up / Page Down — fail-safe) ───────────────
# Laptop: Fn+PgUp / Fn+PgDown usually arrive as VK_PRIOR / VK_NEXT.
# Using these avoids the Windows volume OSD that media keys trigger.

WH_KEYBOARD_LL = 13
WM_KEYDOWN = 0x0100
WM_SYSKEYDOWN = 0x0104
VK_PRIOR = 0x21  # Page Up   → volume up
VK_NEXT = 0x22   # Page Down → volume down
VK_CONTROL = 0x11
VK_MENU = 0x12  # Alt
VK_Q = 0x51
VK_M = 0x4D     # Ctrl+Alt+M → mute
LLKHF_INJECTED = 0x10

# 64-bit: WPARAM/LPARAM are pointer-sized. Wrong ctypes types → OverflowError
# and the hook breaks (volume keys appear to "do nothing").
LRESULT = ctypes.c_ssize_t
HHOOK = ctypes.c_void_p
WPARAM_T = ctypes.c_size_t
LPARAM_T = ctypes.c_ssize_t

user32 = ctypes.windll.user32
user32.CallNextHookEx.argtypes = [HHOOK, ctypes.c_int, WPARAM_T, LPARAM_T]
user32.CallNextHookEx.restype = LRESULT
user32.SetWindowsHookExW.argtypes = [
    ctypes.c_int,
    ctypes.c_void_p,
    ctypes.c_void_p,
    wt.DWORD,
]
user32.SetWindowsHookExW.restype = HHOOK
user32.UnhookWindowsHookEx.argtypes = [HHOOK]
user32.UnhookWindowsHookEx.restype = wt.BOOL
user32.PostThreadMessageW.argtypes = [wt.DWORD, wt.UINT, WPARAM_T, LPARAM_T]
user32.PostThreadMessageW.restype = wt.BOOL
user32.PeekMessageW.argtypes = [
    ctypes.POINTER(wt.MSG),
    wt.HWND,
    wt.UINT,
    wt.UINT,
    wt.UINT,
]
user32.PeekMessageW.restype = wt.BOOL


class _KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wt.DWORD),
        ("scanCode", wt.DWORD),
        ("flags", wt.DWORD),
        ("time", wt.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


_LowLevelKeyboardProc = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, WPARAM_T, LPARAM_T)
_vol_hook_handle = None
_vol_hook_proc = None
_intercept_flag = ctypes.c_int(0)
_cmd_queue = None  # queue.Queue set in main — hook only enqueues, never touches UIA
_last_vol_cmd_at = 0.0
_last_vol_cmd = None
# One physical tap often repeats KEYDOWN several times → was jumping 0→20 (4×5).
_VOL_DEBOUNCE_SEC = 0.22


def _call_next(nCode, wParam, lParam):
    # Must return a plain Python int from the hook callback (not c_longlong),
    # otherwise ctypes raises: converting result of callback ... c_longlong
    r = user32.CallNextHookEx(None, int(nCode), WPARAM_T(wParam), LPARAM_T(lParam))
    try:
        return int(r)
    except Exception:
        return 0


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
        _cmd_queue.put_nowait(cmd)
        return True
    except Exception:
        return False


def install_volume_hook(cmd_queue):
    """
    Hook only decides suppress vs pass-through and enqueues 'up'/'down'/'mute'/'quit'.
    All UIA work happens on a dedicated worker thread (COM-safe).
    """
    global _vol_hook_handle, _vol_hook_proc, _cmd_queue
    _cmd_queue = cmd_queue

    def _handler(nCode, wParam, lParam):
        try:
            if nCode >= 0 and wParam in (WM_KEYDOWN, WM_SYSKEYDOWN):
                kb = ctypes.cast(lParam, ctypes.POINTER(_KBDLLHOOKSTRUCT)).contents
                if not (kb.flags & LLKHF_INJECTED):
                    vk = int(kb.vkCode)

                    ctrl_down = bool(user32.GetAsyncKeyState(VK_CONTROL) & 0x8000)
                    alt_down = bool(user32.GetAsyncKeyState(VK_MENU) & 0x8000)

                    # Quit: Ctrl+Alt+Q
                    if vk == VK_Q and ctrl_down and alt_down:
                        try:
                            _cmd_queue.put_nowait("quit")
                        except Exception:
                            pass
                        return 1

                    # Mute: Ctrl+Alt+M (only when TuneBlade owns volume)
                    if vk == VK_M and ctrl_down and alt_down and _intercept_flag.value:
                        _enqueue_vol("mute")
                        return 1

                    # Volume: Page Up / Page Down  (Fn+PgUp / Fn+PgDown on many laptops)
                    if vk in (VK_PRIOR, VK_NEXT):
                        if _intercept_flag.value:
                            cmd = "up" if vk == VK_PRIOR else "down"
                            _enqueue_vol(cmd)
                            return 1  # suppress while armed (incl. key-repeat)
                        # Master OFF → let PgUp/PgDn scroll pages as usual
        except Exception:
            pass
        try:
            return _call_next(nCode, wParam, lParam)
        except Exception:
            return 0

    _vol_hook_proc = _LowLevelKeyboardProc(_handler)
    _vol_hook_handle = user32.SetWindowsHookExW(WH_KEYBOARD_LL, _vol_hook_proc, None, 0)
    if not _vol_hook_handle:
        err = ctypes.windll.kernel32.GetLastError()
        raise RuntimeError(f"SetWindowsHookExW failed (error {err})")
    return _vol_hook_handle


def remove_volume_hook():
    global _vol_hook_handle
    if _vol_hook_handle:
        try:
            user32.UnhookWindowsHookEx(_vol_hook_handle)
        except Exception:
            pass
        _vol_hook_handle = None


_main_thread_id = None  # Win32 thread id that runs GetMessage
WM_QUIT = 0x0012
_quit_event = threading.Event()


def run_message_loop():
    msg = wt.MSG()
    while True:
        # Also wake periodically so we can exit via _quit_event if PostThreadMessage fails
        if _quit_event.is_set():
            break
        # Peek first so we can poll quit_event without blocking forever
        has_msg = user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0x0001)  # PM_REMOVE
        if has_msg:
            if msg.message == WM_QUIT:
                break
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        else:
            time.sleep(0.05)


def post_quit():
    """Signal exit from ANY thread (tray / worker / hook)."""
    _quit_event.set()
    set_intercept(False)
    try:
        _sysvol.disarm()
    except Exception:
        pass
    tid = _main_thread_id
    if tid:
        try:
            # Must target the main UI thread — PostQuitMessage from tray thread is ignored
            user32.PostThreadMessageW(int(tid), WM_QUIT, 0, 0)
        except Exception as e:
            _log(f"[quit] PostThreadMessage: {e}")
    try:
        user32.PostQuitMessage(0)
    except Exception:
        pass


def set_intercept(enabled: bool) -> None:
    _intercept_flag.value = 1 if enabled else 0


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
    state = [None]

    def walk(ctrl, in_master=False, depth=0):
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
                u = cname.upper()
                if u == "ON":
                    state[0] = True
                    return
                if u == "OFF":
                    state[0] = False
                    return
            child = ctrl.GetFirstChildControl()
            while child:
                walk(child, in_master, depth + 1)
                child = child.GetNextSiblingControl()
        except Exception:
            pass

    walk(root)
    return state[0]


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
        self._mute_bk = 50.0
        self._lock = threading.RLock()
        self._slider = None
        self._audio_ok = True
        self._tb_found = False
        self._master_on = False
        self._device_status = None
        self._last_vol = None
        self._device_list: list[str] = []

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
            try:
                devices = _list_all_devices(root)
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

        # Active = Master ON + device slider found + has speakers
        disconnected = (status or "").strip().lower() == "disconnected"
        active = bool(audio_ok and tb_found and master_on and slider is not None and not disconnected)

        was_active = bool(_intercept_flag.value)
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

        set_intercept(active)
        if active and not was_active:
            _sysvol.arm()
        elif not active and was_active:
            _sysvol.disarm()
        elif active:
            _sysvol.freeze()

        return {
            "audio_ok": audio_ok,
            "tb_found": tb_found,
            "master_on": master_on,
            "device_status": status,
            "active": active,
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
        if _slider_set(slider, new_v):
            time.sleep(0.05)
            vol = _slider_get(slider)
            with self._lock:
                self._last_vol = vol
                self.muted = bool(vol is not None and vol <= 0)
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
            self._mute_bk = vol
            if _slider_set(slider, 0):
                self.muted = True
                with self._lock:
                    self._last_vol = 0
                _log("muted")
        else:
            target = max(float(self._mute_bk or self.step), float(self.step))
            if _slider_set(slider, target):
                self.muted = False
                with self._lock:
                    self._last_vol = target
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

        return pystray.Menu(
            pystray.MenuItem(lambda item: self.ctrl.mode_label(), None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("选择设备", device_menu),
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

def main():
    import queue

    global _main_thread_id
    # Load config first so we know whether to enable the log file
    cfg = load_config()
    _setup_logging(bool(cfg.get("debug_log")))
    _main_thread_id = ctypes.windll.kernel32.GetCurrentThreadId()
    _log(f"[main] thread_id={_main_thread_id}")

    if cfg.get("autostart") and not is_autostart_enabled():
        set_autostart(True)
    elif is_autostart_enabled():
        cfg["autostart"] = True

    ctrl = TuneBladeController(cfg)
    st = ctrl.refresh_routing_state()
    _volume_osd.start()

    _log(APP_NAME)
    _log(f"  device        : {cfg.get('device_name')!r}")
    _log(f"  step          : {cfg.get('volume_step')}")
    _log(f"  audio         : {st['audio_ok']}")
    _log(f"  TuneBlade     : {st['tb_found']}")
    _log(f"  Master        : {st['master_on']}")
    _log(f"  device status : {st['device_status']!r}")
    _log(f"  active        : {st['active']}")
    _log(f"  volume        : {st['volume']}")
    _log(f"  mode          : {ctrl.mode_label()}")
    _log(f"  intercept     : {_intercept_flag.value}")

    cmd_queue: queue.Queue = queue.Queue(maxsize=32)
    stop_event = threading.Event()
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
            time.sleep(float(cfg.get("poll_interval_sec", 1.0) or 1.0))
            try:
                cmd_queue.put_nowait("refresh")
            except Exception:
                try:
                    ctrl.refresh_routing_state()
                except Exception:
                    pass

    def _sysvol_watch():
        """
        Primary capture path for Fn/media volume keys that never show up as
        VK_VOLUME_* in WH_KEYBOARD_LL — watch the Windows endpoint instead.
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

    try:
        install_volume_hook(cmd_queue)
        _log("[hook] installed (optional; sysvol watcher is primary)")
    except Exception as e:
        _log(f"[hook] install failed (sysvol watcher still works): {e}")
    try:
        signal.signal(signal.SIGINT, lambda s, f: post_quit())
    except Exception:
        pass

    try:
        run_message_loop()
    finally:
        _log("[quit] cleaning up…")
        stop_event.set()
        _quit_event.set()
        remove_volume_hook()
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
