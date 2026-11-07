from __future__ import annotations

import ctypes
import json
import logging
import logging.handlers
import math
import os
import queue
import re
import shutil
import sys
import threading
import time
import traceback
import uuid
import webbrowser
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk
from tkinter.scrolledtext import ScrolledText

from pynput import keyboard, mouse

# Bandeja (opcional). Si pystray/Pillow faltan, la X cierra normalmente.
try:
    import pystray
    from PIL import Image, ImageDraw
    TRAY_AVAILABLE = True
except Exception:
    pystray = None
    Image = None
    ImageDraw = None
    TRAY_AVAILABLE = False


# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------
APP_NAME = "ACC - AutoCallCenter"
APP_VERSION = "2.6.0"
AUTHOR = "Sebastián Bocek"
GITHUB_URL = "https://github.com/sebastianbocek"

BUTTONS = {"Izquierdo": "left", "Derecho": "right", "Central": "middle"}
BUTTON_LABELS = {v: k for k, v in BUTTONS.items()}
PROFILE_FORMAT = "acc-profile-v2"
LEGACY_FORMATS = ("autoclick-items-v1", "autoclick-points-v2", "autoclick-profile-v2")
MAX_POINTS = 1000
DEFAULT_PASTE_WAIT = 0.5

LEGACY_BASE_DIR = Path(os.getenv("APPDATA") or Path.home()) / "AutoCallCenter"


def program_directory() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


PROGRAM_DIR = program_directory()
BASE_DIR = PROGRAM_DIR / "Secuencias"
PROFILE_DIR = BASE_DIR
SETTINGS_FILE = BASE_DIR / "acc_settings.json"
SESSION_FILE = BASE_DIR / "acc_session.json"
LOG_FILE = BASE_DIR / "acc.log"


def ensure_data_directory() -> None:
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    if not LEGACY_BASE_DIR.exists():
        return
    for legacy_name, target in (
        ("acc_settings.json", SETTINGS_FILE),
        ("acc_session.json", SESSION_FILE),
    ):
        legacy = LEGACY_BASE_DIR / legacy_name
        if legacy.is_file() and not target.exists():
            try:
                shutil.copy2(legacy, target)
            except OSError:
                pass
    legacy_profiles = LEGACY_BASE_DIR / "automatizaciones"
    if legacy_profiles.is_dir():
        for old in legacy_profiles.glob("*.json"):
            dest = PROFILE_DIR / old.name
            if not dest.exists():
                try:
                    shutil.copy2(old, dest)
                except OSError:
                    pass


# ---------------------------------------------------------------------------
# Sistema de logs
# ---------------------------------------------------------------------------
class TkLogHandler(logging.Handler):
    def __init__(self, events_queue):
        super().__init__()
        self.events = events_queue

    def emit(self, record):
        try:
            msg = self.format(record)
            self.events.put(("log", (record.levelname, msg)))
        except Exception:
            pass


def setup_logging(events_queue):
    try:
        BASE_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    logger = logging.getLogger("acc")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    if logger.handlers:
        logger.handlers.clear()
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")
    try:
        file_handler = logging.handlers.RotatingFileHandler(
            LOG_FILE, maxBytes=1_000_000, backupCount=2, encoding="utf-8")
        file_handler.setFormatter(fmt)
        logger.addHandler(file_handler)
    except OSError:
        pass
    tk_handler = TkLogHandler(events_queue)
    tk_handler.setFormatter(fmt)
    logger.addHandler(tk_handler)
    return logger


# ---------------------------------------------------------------------------
# Tabla de teclas → VK
# ---------------------------------------------------------------------------
def _build_key_table():
    t = []
    for i in range(10):
        t.append((f"numpad{i}", f"Numpad {i}", 0x60 + i))
    t += [
        ("numpad_multiply", "Numpad *", 0x6A),
        ("numpad_add", "Numpad +", 0x6B),
        ("numpad_subtract", "Numpad -", 0x6D),
        ("numpad_decimal", "Numpad .", 0x6E),
        ("numpad_divide", "Numpad /", 0x6F),
    ]
    for i in range(10):
        t.append((str(i), str(i), 0x30 + i))
    for i in range(26):
        c = chr(ord("A") + i)
        t.append((c, c, 0x41 + i))
    for i in range(1, 13):
        t.append((f"F{i}", f"F{i}", 0x6F + i))
    t += [
        ("space", "Espacio", 0x20),
        ("tab", "Tab", 0x09),
        ("enter", "Enter", 0x0D),
        ("backspace", "Backspace", 0x08),
        ("insert", "Insert", 0x2D),
        ("delete", "Delete", 0x2E),
        ("home", "Home", 0x24),
        ("end", "End", 0x23),
        ("page_up", "Page Up", 0x21),
        ("page_down", "Page Down", 0x22),
        ("up", "Flecha Arriba", 0x26),
        ("down", "Flecha Abajo", 0x28),
        ("left", "Flecha Izquierda", 0x25),
        ("right", "Flecha Derecha", 0x27),
    ]
    return t


KEY_DEFS = _build_key_table()
KEY_NAME_TO_VK = {name: vk for name, _label, vk in KEY_DEFS}
KEY_LABELS = {name: label for name, label, _vk in KEY_DEFS}
KEY_LABEL_TO_NAME = {label: name for name, label, _vk in KEY_DEFS}
KEY_OPTIONS = [(name, label) for name, label, _vk in KEY_DEFS]


def key_label(name: str) -> str:
    return KEY_LABELS.get(name, name or "—")


def _pynput_key(name):
    if len(name) == 1:
        return keyboard.KeyCode.from_char(name.lower())
    if name.startswith("numpad") and name[6:].isdigit():
        return keyboard.KeyCode.from_char(name[6:])
    mapping = {f"F{i}": getattr(keyboard.Key, f"f{i}") for i in range(1, 13)}
    mapping.update({
        "space": keyboard.Key.space, "tab": keyboard.Key.tab,
        "enter": keyboard.Key.enter, "backspace": keyboard.Key.backspace,
        "insert": keyboard.Key.insert, "delete": keyboard.Key.delete,
        "home": keyboard.Key.home, "end": keyboard.Key.end,
        "page_up": keyboard.Key.page_up, "page_down": keyboard.Key.page_down,
        "up": keyboard.Key.up, "down": keyboard.Key.down,
        "left": keyboard.Key.left, "right": keyboard.Key.right,
    })
    return mapping.get(name)


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------
def enable_dpi_awareness():
    if os.name != "nt":
        return
    try:
        fn = ctypes.windll.user32.SetProcessDpiAwarenessContext
        fn.argtypes = [ctypes.c_void_p]
        fn.restype = ctypes.c_bool
        if fn(ctypes.c_void_p(-4)):
            return
    except (AttributeError, OSError):
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except (AttributeError, OSError):
        ctypes.windll.user32.SetProcessDPIAware()


def number(value, name, minimum, maximum):
    value = float(str(value).replace(",", "."))
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError(f"{name}: usá un número entre {minimum} y {maximum}.")
    return value


def integer(value, name, minimum, maximum):
    n = number(value, name, minimum, maximum)
    if n != int(n):
        raise ValueError(f"{name}: usá un número entero.")
    return int(n)


def write_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False, allow_nan=False), encoding="utf-8")
    tmp.replace(path)


# ---------------------------------------------------------------------------
# Instancia única
# ---------------------------------------------------------------------------
class SingleInstance:
    def __init__(self, lock_dir=None):
        self.lock_dir = lock_dir or BASE_DIR
        self.handle = None
        self.file = None
        self.kernel32 = None

    def acquire(self):
        if os.name == "nt":
            from ctypes import wintypes
            self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            self.kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
            self.kernel32.CreateMutexW.restype = wintypes.HANDLE
            self.kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            self.kernel32.CloseHandle.restype = wintypes.BOOL
            ctypes.set_last_error(0)
            self.handle = self.kernel32.CreateMutexW(None, False, "Local\\AutoCallCenter.SingleInstance.v2")
            error = ctypes.get_last_error()
            if not self.handle:
                raise ctypes.WinError(error)
            if error == 183:
                self.close()
                return False
        else:
            import fcntl
            try:
                self.lock_dir.mkdir(parents=True, exist_ok=True)
            except OSError:
                pass
            try:
                self.file = (self.lock_dir / "acc_v2.lock").open("a+")
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (BlockingIOError, OSError):
                if self.file:
                    self.file.close()
                    self.file = None
                return False
        return True

    def close(self):
        if self.handle:
            self.kernel32.CloseHandle(self.handle)
            self.handle = None
        if self.file:
            self.file.close()
            self.file = None


# ---------------------------------------------------------------------------
# Modelo de datos
# ---------------------------------------------------------------------------
@dataclass
class ClickPoint:
    id: str
    x: int
    y: int
    wait: float = 0.3
    button: str = "left"
    clicks: int = 1
    text: str = ""
    replace_text: bool = True

    @classmethod
    def create(cls, x, y, **kwargs):
        return cls(uuid.uuid4().hex, int(x), int(y), **kwargs)

    @property
    def is_paste(self):
        return bool(self.text)


@dataclass
class Profile:
    id: str
    name: str
    key: str
    points: list[ClickPoint] = field(default_factory=list)
    initial_delay: float = 0.0
    path: Path | None = None

    @classmethod
    def create(cls, name="Nuevo perfil", key=""):
        return cls(uuid.uuid4().hex, name, key)


def _point_from_dict(item):
    if not isinstance(item, dict):
        raise ValueError("Punto inválido.")
    ident = str(item.get("id") or uuid.uuid4().hex)
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", ident):
        raise ValueError("Identificador de punto inválido.")
    button = item.get("button", "left")
    if button not in BUTTON_LABELS:
        raise ValueError("Botón de mouse inválido.")
    text = item.get("text", "")
    if not isinstance(text, str):
        raise ValueError("Texto de punto inválido.")
    return ClickPoint(
        ident,
        integer(item.get("x"), "X", -1000000, 1000000),
        integer(item.get("y"), "Y", -1000000, 1000000),
        number(item.get("wait", 0.3), "Espera", 0, 86400),
        button,
        integer(item.get("clicks", 1), "Clics", 1, 100),
        text,
        bool(item.get("replace_text", True)),
    )


def profile_from_dict(data, path: Path | None = None) -> Profile:
    if not isinstance(data, dict):
        raise ValueError("Perfil inválido.")
    fmt = data.get("format")
    if fmt in LEGACY_FORMATS:
        raw = data.get("points") or []
        points = [_point_from_dict(p) for p in raw]
        return Profile(
            id=str(data.get("id") or uuid.uuid4().hex),
            name=str(data.get("name") or "Importado"),
            key=str(data.get("key") or ""),
            points=points,
            initial_delay=number(data.get("initial_delay", 0), "Demora inicial", 0, 60),
            path=path,
        )
    if fmt != PROFILE_FORMAT:
        raise ValueError("Este archivo no es un perfil de ACC - AutoCallCenter.")
    raw_points = data.get("points", [])
    if not isinstance(raw_points, list) or len(raw_points) > MAX_POINTS:
        raise ValueError(f"El perfil admite hasta {MAX_POINTS} puntos.")
    points = [_point_from_dict(p) for p in raw_points]
    key = data.get("key", "")
    if key and key not in KEY_NAME_TO_VK:
        raise ValueError(f"Tecla desconocida en el perfil: {key}")
    return Profile(
        id=str(data.get("id") or uuid.uuid4().hex),
        name=str(data.get("name") or "Sin nombre"),
        key=key,
        points=points,
        initial_delay=number(data.get("initial_delay", 0), "Demora inicial", 0, 60),
        path=path,
    )


def profile_to_dict(profile: Profile) -> dict:
    return {
        "format": PROFILE_FORMAT,
        "version": APP_VERSION,
        "id": profile.id,
        "name": profile.name,
        "key": profile.key,
        "points": [asdict(p) for p in profile.points],
        "initial_delay": profile.initial_delay,
    }


# ---------------------------------------------------------------------------
# Motor de clics
# ---------------------------------------------------------------------------
@dataclass
class ClipboardRequest:
    run_id: int
    text: str
    done: threading.Event = field(default_factory=threading.Event)
    error: str | None = None


class ClickEngine:
    def __init__(self, events, mouse_lock=None, logger=None):
        self.events = events
        self.mouse_lock = mouse_lock if mouse_lock is not None else threading.RLock()
        self.controller = mouse.Controller()
        self.key_controller = keyboard.Controller()
        self.stop_event = threading.Event()
        self.lock = threading.RLock()
        self.phase = "idle"
        self.generation = 0
        self.thread = None
        self.run_id = 0
        self.stop_reason = None
        self.paste_wait = DEFAULT_PASTE_WAIT
        self.current_profile_name = ""
        self.current_profile_id = ""
        self.log = logger or logging.getLogger("acc")

    @property
    def running(self):
        with self.lock:
            return self.phase in {"running", "stopping"} or bool(self.thread and self.thread.is_alive())

    @property
    def active(self):
        with self.lock:
            return self.phase != "idle" or bool(self.thread and self.thread.is_alive())

    def cancel(self, reason=None):
        with self.lock:
            if reason:
                self.stop_reason = reason
            self.generation += 1
            self.stop_event.set()
            if self.phase == "pending":
                self.phase = "idle"
            elif self.phase == "running":
                self.phase = "stopping"

    def wait_or_stop(self, seconds, stop_event):
        deadline = time.monotonic() + seconds
        while not stop_event.is_set():
            x, y = self.controller.position
            if 0 <= x <= 3 and 0 <= y <= 3:
                self.cancel("Detenido: cursor en la esquina superior izquierda (0–3).")
                stop_event.set()
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            if stop_event.wait(min(0.01, remaining)):
                return True
        return True

    def start(self, profile: Profile):
        with self.lock:
            old_thread = self.thread if (self.thread and self.thread.is_alive()) else None
            if old_thread is not None:
                self.generation += 1
                self.stop_event.set()
        if old_thread is not None:
            old_thread.join(timeout=2.0)
            if old_thread.is_alive():
                self.log.warning("El hilo anterior no terminó; abortando nuevo start.")
                return False

        snapshot = [ClickPoint(**asdict(p)) for p in profile.points]
        initial_delay = profile.initial_delay
        name = profile.name or "Perfil"
        profile_id = profile.id

        with self.lock:
            if not snapshot:
                return False
            self.generation += 1
            self.stop_event = threading.Event()
            self.stop_reason = None
            self.run_id += 1
            self.current_profile_name = name
            self.current_profile_id = profile_id
            self.phase = "running"
            self.thread = threading.Thread(
                target=self._worker,
                args=(snapshot, initial_delay, self.stop_event, self.run_id, name),
                daemon=True,
            )
            try:
                self.thread.start()
            except Exception:
                self.phase = "idle"
                self.generation += 1
                raise
        self.log.info("Iniciando perfil '%s' con %d puntos.", name, len(snapshot))
        return True

    def acknowledge_finished(self, run_id):
        with self.lock:
            if (run_id != self.run_id or self.phase != "finished"
                    or (self.thread and self.thread.is_alive())):
                return False
            self.phase = "idle"
            return True

    def _copy_text(self, text, stop_event, run_id):
        request = ClipboardRequest(run_id, text)
        self.events.put(("copy_text", request))
        deadline = time.monotonic() + 5.0
        while not request.done.is_set():
            if self.wait_or_stop(0.01, stop_event):
                return False
            if time.monotonic() >= deadline:
                raise RuntimeError("No se pudo copiar el texto al portapapeles a tiempo.")
        if stop_event.is_set():
            return False
        if request.error:
            raise RuntimeError(request.error)
        return True

    def _shortcut(self, letter, stop_event):
        held = []
        try:
            for key in (keyboard.Key.ctrl, letter):
                if self.wait_or_stop(0, stop_event):
                    return False
                held.append(key)
                self.key_controller.press(key)
                if self.wait_or_stop(0.03, stop_event):
                    return False
        finally:
            error = None
            for key in reversed(held):
                try:
                    self.key_controller.release(key)
                except Exception as exc:
                    error = exc
            if error is not None:
                raise RuntimeError(f"No se pudo soltar una tecla: {error}")
        return not self.wait_or_stop(0.04, stop_event)

    def _run_point(self, point, stop_event, run_id):
        if self.wait_or_stop(point.wait, stop_event):
            return False
        held = None
        try:
            for idx in range(point.clicks):
                if self.wait_or_stop(0, stop_event):
                    return False
                with self.mouse_lock:
                    if stop_event.is_set():
                        return False
                    self.controller.position = (point.x, point.y)
                    if stop_event.is_set():
                        return False
                    held = getattr(mouse.Button, point.button)
                    self.controller.press(held)
                    self.wait_or_stop(0.02, stop_event)
                    self.controller.release(held)
                    held = None
                if idx + 1 < point.clicks and self.wait_or_stop(0.08, stop_event):
                    return False
        finally:
            if held is not None:
                self.controller.release(held)

        if not point.text:
            return not stop_event.is_set()

        if self.wait_or_stop(0.15, stop_event):
            return False
        if not self._copy_text(point.text, stop_event, run_id):
            return False
        if point.replace_text:
            if not self._shortcut("a", stop_event):
                return False
            if self.wait_or_stop(0.05, stop_event):
                return False
        if not self._shortcut("v", stop_event):
            return False
        if self.wait_or_stop(self.paste_wait, stop_event):
            return False
        return not stop_event.is_set()

    def _worker(self, points, initial_delay, stop_event, run_id, name):
        error = None
        try:
            if self.wait_or_stop(initial_delay, stop_event):
                return
            for index, point in enumerate(points, 1):
                self.events.put(("progress", (run_id, index, len(points), name)))
                if not self._run_point(point, stop_event, run_id):
                    return
            with self.lock:
                if self.run_id == run_id and not stop_event.is_set():
                    self.stop_reason = f"Perfil '{name}' completado."
        except Exception as exc:
            error = f"Error en '{name}': {exc}"
            self.log.error("Excepción en _worker:\n%s", traceback.format_exc())
        finally:
            with self.lock:
                self.phase = "finished"
                self.generation += 1
            self.events.put(("finished", (run_id, error)))

    def close(self):
        self.cancel()
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=1)


# ---------------------------------------------------------------------------
# Escucha global de teclas
# ---------------------------------------------------------------------------
class MultiHotkeys:
    """Atajos globales:
      - Ctrl+Shift+P → activar/pausar
      - Ctrl+Shift+O → overlay on/off
      - Esc → detener todo
      - Teclas de perfil cuando está ACTIVO.
    """

    VK_ESC = 0x1B
    VK_CTRL = 0x11
    VK_SHIFT = 0x10
    VK_P = 0x50
    VK_O = 0x4F

    def __init__(self, events, get_key_map, is_suspended, is_engine_running,
                 is_armed, logger=None):
        self.events = events
        self.get_key_map = get_key_map
        self.is_suspended = is_suspended
        self.is_engine_running = is_engine_running
        self.is_armed = is_armed
        self.healthy = False
        self.poll_thread = None
        self.poll_stop = threading.Event()
        self.reader = None
        self.latched = set()
        self.released_at = {}
        self.prime_poll = True
        self.lock = threading.RLock()
        self.listener = None
        self.down = set()
        self.log = logger or logging.getLogger("acc")

    def dispatch_escape(self):
        self.events.put(("escape", None))

    def dispatch_trigger(self, key_name):
        self.events.put(("trigger_profile", key_name))

    def dispatch_toggle_armed(self):
        self.events.put(("toggle_armed", None))

    def dispatch_toggle_overlay(self):
        self.events.put(("toggle_overlay", None))

    def _mods_down(self):
        return (bool(self.reader(self.VK_CTRL) & 0x8000),
                bool(self.reader(self.VK_SHIFT) & 0x8000))

    def poll_once(self, now):
        with self.lock:
            key_map = self.get_key_map()
            vk_to_key = {KEY_NAME_TO_VK[k]: k for k in key_map if k in KEY_NAME_TO_VK}
            vk_to_key[self.VK_ESC] = "__esc__"
            vk_to_key[self.VK_P] = "__toggle_armed__"
            vk_to_key[self.VK_O] = "__toggle_overlay__"
            pressed = {vk for vk in vk_to_key if self.reader(vk) & 0x8000}
            if self.prime_poll:
                self.latched = pressed
                self.released_at.clear()
                self.prime_poll = False
                if self.VK_ESC in pressed and self.is_engine_running():
                    self.dispatch_escape()
                return
            for vk in self.latched - pressed:
                since = self.released_at.setdefault(vk, now)
                if now - since >= 0.035:
                    self.latched.discard(vk)
                    self.released_at.pop(vk, None)
            for vk in pressed:
                self.released_at.pop(vk, None)
            fresh = pressed - self.latched
            self.latched.update(pressed)
            if self.VK_ESC in pressed:
                if self.VK_ESC in fresh or self.is_engine_running():
                    self.dispatch_escape()
                return
            if self.is_suspended():
                return
            ctrl, shift = self._mods_down()
            if ctrl and shift and self.VK_P in fresh:
                self.dispatch_toggle_armed()
                return
            if ctrl and shift and self.VK_O in fresh:
                self.dispatch_toggle_overlay()
                return
            if not self.is_armed():
                return
            for vk in fresh:
                name = vk_to_key.get(vk)
                if name and name not in ("__esc__", "__toggle_armed__",
                                         "__toggle_overlay__"):
                    self.dispatch_trigger(name)

    def _poll_loop(self):
        try:
            while not self.poll_stop.is_set():
                self.poll_once(time.monotonic())
                self.poll_stop.wait(0.008)
        except Exception as exc:
            self.healthy = False
            self.log.error("Hotkeys poll falló: %s\n%s", exc, traceback.format_exc())
            self.events.put(("hotkey_error", str(exc)))

    def _on_press(self, key):
        with self.lock:
            if key in self.down:
                return
            self.down.add(key)
        if key == keyboard.Key.esc:
            if self.is_engine_running():
                self.dispatch_escape()
            return
        if self.is_suspended():
            return
        if not self.is_armed():
            return
        for name in self.get_key_map():
            if _pynput_key(name) == key:
                self.dispatch_trigger(name)
                return

    def _on_release(self, key):
        with self.lock:
            self.down.discard(key)

    def start(self):
        if os.name == "nt":
            self.reader = ctypes.windll.user32.GetAsyncKeyState
            self.reader.argtypes = [ctypes.c_int]
            self.reader.restype = ctypes.c_short
            self.poll_stop.clear()
            self.prime_poll = True
            self.poll_thread = threading.Thread(target=self._poll_loop, daemon=True)
            self.healthy = True
            self.poll_thread.start()
            self.log.info("Hotkeys (Windows) iniciados.")
        else:
            self.listener = keyboard.Listener(on_press=self._on_press, on_release=self._on_release)
            self.listener.start()
            self.healthy = True
            self.log.info("Hotkeys (pynput) iniciados.")

    def close(self):
        self.healthy = False
        self.poll_stop.set()
        if self.poll_thread:
            self.poll_thread.join(timeout=1)
        if self.listener:
            self.listener.stop()


# ---------------------------------------------------------------------------
# Esc de emergencia
# ---------------------------------------------------------------------------
class EmergencyHotkey:
    HOTKEY_ID = 0x4A23

    def __init__(self, events, logger=None):
        self.events = events
        self.thread = None
        self.stop_event = threading.Event()
        self.ready = threading.Event()
        self.available = False
        self.error = ""
        self.log = logger or logging.getLogger("acc")

    def _trigger(self):
        self.events.put(("escape", None))

    def arm(self):
        if os.name != "nt":
            self.available = True
            return True
        if self.thread and self.thread.is_alive():
            return self.available and not self.stop_event.is_set()
        self.error = ""
        self.available = False
        self.stop_event = threading.Event()
        self.ready = threading.Event()
        self.thread = threading.Thread(target=self._windows_loop, daemon=True)
        self.thread.start()
        if not self.ready.wait(1) or not self.available:
            self.disarm()
            if not self.error:
                self.error = "Windows no respondió al activar Esc."
            return False
        return True

    def _windows_loop(self):
        from ctypes import wintypes
        registered = False
        user32 = None
        try:
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
            user32.RegisterHotKey.restype = wintypes.BOOL
            user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
            user32.UnregisterHotKey.restype = wintypes.BOOL
            user32.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                                            wintypes.UINT, wintypes.UINT, wintypes.UINT]
            user32.PeekMessageW.restype = wintypes.BOOL
            message = wintypes.MSG()
            user32.PeekMessageW(ctypes.byref(message), None, 0, 0, 0)
            registered = bool(user32.RegisterHotKey(None, self.HOTKEY_ID, 0x4000, 0x1B))
            if not registered:
                raise OSError(f"No se pudo registrar Esc (código {ctypes.get_last_error()}).")
            self.available = True
            self.ready.set()
            self.log.info("Esc de emergencia activado.")
            while not self.stop_event.is_set():
                while user32.PeekMessageW(ctypes.byref(message), None, 0, 0, 1):
                    if message.message == 0x0312 and message.wParam == self.HOTKEY_ID:
                        self._trigger()
                self.stop_event.wait(0.005)
        except Exception as exc:
            self.error = str(exc)
            self.log.error("EmergencyHotkey: %s", exc)
            self.events.put(("hotkey_error", self.error))
        finally:
            self.available = False
            self.ready.set()
            if registered:
                user32.UnregisterHotKey(None, self.HOTKEY_ID)

    def disarm(self):
        self.stop_event.set()
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=1)
        self.available = False


# ---------------------------------------------------------------------------
# Marcador arrastrable en pantalla
# ---------------------------------------------------------------------------
class PointMarker(tk.Toplevel):
    SIZE = 30

    def __init__(self, app, point, index):
        super().__init__(app)
        self._destroyed = False
        self.withdraw()
        self.app = app
        self.point = point
        self.index = index
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self.attributes("-alpha", 0.90)
        if os.name == "nt":
            self.attributes("-toolwindow", True)
        self.geometry(f"{self.SIZE}x{self.SIZE}")
        self.canvas = tk.Canvas(self, width=self.SIZE, height=self.SIZE,
                                bg="#092934", highlightthickness=0, cursor="fleur")
        self.canvas.pack(fill="both", expand=True)
        self._draw()
        self.canvas.bind("<ButtonPress-1>", self.begin_drag)
        self.canvas.bind("<B1-Motion>", self.drag)
        self.canvas.bind("<ButtonRelease-1>", self.end_drag)
        self.origin = None
        self.press_position = None
        try:
            self.update_idletasks()
        except tk.TclError:
            pass

    def _draw(self):
        self.canvas.delete("all")
        is_paste = self.point.is_paste
        fill = "#7a3a0f" if is_paste else "#0e5362"
        outline = "#ffb347" if is_paste else "#48e7df"
        self.canvas.create_oval(1, 1, self.SIZE - 1, self.SIZE - 1,
                                fill=fill, outline=outline, width=2)
        label = f"P{self.index}" if is_paste else str(self.index)
        self.canvas.create_text(self.SIZE // 2, self.SIZE // 2, text=label,
                                fill="white", font=("Segoe UI", 8, "bold"))

    def set_index(self, index):
        if self._destroyed or self.index == index:
            return
        self.index = index
        self._draw()

    def update_appearance(self):
        if not self._destroyed:
            self._draw()

    def place(self):
        if self._destroyed:
            return
        x = int(self.point.x - self.SIZE // 2)
        y = int(self.point.y - self.SIZE // 2)
        if os.name == "nt":
            from ctypes import wintypes
            user32 = ctypes.windll.user32
            user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
            user32.GetAncestor.restype = wintypes.HWND
            user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND,
                ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
            user32.SetWindowPos.restype = wintypes.BOOL
            hwnd = user32.GetAncestor(self.winfo_id(), 2)
            if hwnd:
                user32.SetWindowPos(hwnd, wintypes.HWND(-1), x, y,
                                    self.SIZE, self.SIZE, 0x0010)
        else:
            self.geometry(f"{self.SIZE}x{self.SIZE}+{x}+{y}")

    def show(self):
        if self._destroyed:
            return
        self.deiconify()
        self.place()

    def destroy(self):
        if self._destroyed:
            return
        self._destroyed = True
        try:
            super().destroy()
        except tk.TclError:
            pass

    def begin_drag(self, _event):
        if self._destroyed or self.app.busy:
            return
        self.app.dragging = True
        self.origin = (self.point.x, self.point.y)
        self.press_position = self.app.engine.controller.position
        self.app.select_point_in_tree(self.point.id)

    def drag(self, _event):
        if self._destroyed or self.origin is None or self.app.busy:
            return
        cx, cy = self.app.engine.controller.position
        self.point.x = self.origin[0] + cx - self.press_position[0]
        self.point.y = self.origin[1] + cy - self.press_position[1]
        self.place()
        self.app.schedule_point_row_update(self.point)

    def end_drag(self, event):
        if self.origin is None:
            return
        self.drag(event)
        self.origin = None
        self.app.dragging = False
        self.app.scene_changed(light=True)


# ---------------------------------------------------------------------------
# Overlay flotante con estado ACTIVO/PAUSADO
# ---------------------------------------------------------------------------
class OverlayWindow(tk.Toplevel):
    """Ventana pequeña, siempre encima, arrastrable, con botones
    ACTIVO/PAUSADO y Esc. La X solo la oculta."""

    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.withdraw()
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self.attributes("-alpha", 0.95)
        if os.name == "nt":
            self.attributes("-toolwindow", True)
        self.configure(bg="#1e2a30")
        try:
            self.geometry("+40+40")
        except tk.TclError:
            pass

        outer = tk.Frame(self, bg="#1e2a30", bd=0)
        outer.pack(fill="both", expand=True)
        titlebar = tk.Frame(outer, bg="#1e2a30", height=22)
        titlebar.pack(fill="x")
        self.title_lbl = tk.Label(titlebar, text="ACC · overlay", bg="#1e2a30",
                                  fg="#cfe3e7", font=("Segoe UI", 9, "bold"))
        self.title_lbl.pack(side="left", padx=(8, 0))
        close_btn = tk.Label(titlebar, text="✕", bg="#1e2a30", fg="#cfe3e7",
                             font=("Segoe UI", 10, "bold"), cursor="hand2")
        close_btn.pack(side="right", padx=(0, 8))
        close_btn.bind("<Button-1>", lambda e: self.hide())
        for w in (titlebar, self.title_lbl):
            w.bind("<ButtonPress-1>", self._drag_begin)
            w.bind("<B1-Motion>", self._drag_move)

        body = tk.Frame(outer, bg="#1e2a30")
        body.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        self.toggle_btn = tk.Button(
            body, text="🟢 ACTIVO", command=self.app.toggle_armed,
            bg="#0a7a44", fg="white", activebackground="#0a9b55",
            activeforeground="white", relief="flat",
            font=("Segoe UI", 10, "bold"), padx=10, pady=6, cursor="hand2")
        self.toggle_btn.pack(side="left", padx=(0, 6))
        self.stop_btn = tk.Button(
            body, text="■ Esc", command=self.app.stop_all,
            bg="#7a2c2c", fg="white", activebackground="#a03c3c",
            activeforeground="white", relief="flat",
            font=("Segoe UI", 10, "bold"), padx=10, pady=6, cursor="hand2")
        self.stop_btn.pack(side="left")

        self._drag_offset = None

    def _drag_begin(self, event):
        self._drag_offset = (event.x_root - self.winfo_rootx(),
                             event.y_root - self.winfo_rooty())

    def _drag_move(self, event):
        if self._drag_offset is None:
            return
        x = event.x_root - self._drag_offset[0]
        y = event.y_root - self._drag_offset[1]
        self.geometry(f"+{x}+{y}")

    def set_armed(self, armed):
        if armed:
            self.toggle_btn.configure(text="🟢 ACTIVO", bg="#0a7a44",
                                      activebackground="#0a9b55")
        else:
            self.toggle_btn.configure(text="⚪ PAUSADO", bg="#7a2c2c",
                                      activebackground="#a03c3c")

    def show(self):
        try:
            self.deiconify()
            self.attributes("-topmost", True)
            self.lift()
        except tk.TclError:
            pass

    def hide(self):
        try:
            self.withdraw()
        except tk.TclError:
            pass
        self.app.overlay_visible = False
        self.app.overlay_var.set(False)
        self.app.save_settings()


# ---------------------------------------------------------------------------
# Icono de bandeja
# ---------------------------------------------------------------------------
class TrayIcon:
    def __init__(self, app, logger):
        self.app = app
        self.log = logger
        self.icon = None
        self.thread = None

    def _make_image(self):
        size = 64
        img = Image.new("RGB", (size, size), (10, 122, 68))
        draw = ImageDraw.Draw(img)
        try:
            draw.rectangle([(6, 6), (size - 7, size - 7)], outline=(255, 255, 255), width=3)
            draw.text((14, 22), "ACC", fill=(255, 255, 255))
        except Exception:
            pass
        return img

    def _menu(self):
        def make_show(_):
            self.app.after(0, self.app.show_main_window)

        def make_overlay(_):
            self.app.after(0, self.app.toggle_overlay)

        def make_toggle(_):
            self.app.after(0, self.app.toggle_armed)

        def make_quit(_):
            self.app.after(0, self.app.quit_from_tray)

        items = [
            pystray.MenuItem("Mostrar ventana", make_show, default=True),
            pystray.MenuItem("Mostrar/ocultar overlay", make_overlay),
            pystray.MenuItem("Activar / Pausar", make_toggle),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Salir", make_quit),
        ]
        return pystray.Menu(*items)

    def start(self):
        if not TRAY_AVAILABLE:
            self.log.warning("pystray no disponible: la X cerrará el programa.")
            return False
        try:
            image = self._make_image()
            self.icon = pystray.Icon(
                "ACC", image, f"{APP_NAME} — corriendo",
                menu=self._menu())
            self.thread = threading.Thread(target=self.icon.run, daemon=True)
            self.thread.start()
            self.log.info("Icono de bandeja iniciado.")
            return True
        except Exception as exc:
            self.log.error("No se pudo iniciar la bandeja: %s", exc)
            return False

    def stop(self):
        if self.icon is not None:
            try:
                self.icon.stop()
            except Exception:
                pass
            self.icon = None


# ---------------------------------------------------------------------------
# Aplicación
# ---------------------------------------------------------------------------
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"{APP_NAME} v{APP_VERSION}")
        self.geometry("1160x840")
        self.minsize(1040, 720)
        self.configure(bg="#edf3f4")
        self.events = queue.Queue()
        self.log = setup_logging(self.events)
        self.log.info("=== ACC v%s iniciando ===", APP_VERSION)
        self.log.info("Carpeta de datos: %s", BASE_DIR)
        self.mouse_lock = threading.RLock()
        self.engine = ClickEngine(self.events, mouse_lock=self.mouse_lock, logger=self.log)
        self.profiles: dict[str, Profile] = {}
        self.profiles_lock = threading.RLock()
        self.selected_profile_id: str | None = None
        self.busy = False
        self.dragging = False
        self.modal_depth = 0
        self.armed = True
        self._armed_warned = False
        self.overlay_visible = False
        self.overlay: OverlayWindow | None = None
        self.tray: TrayIcon | None = None
        self._quitting = False
        self.markers: dict[str, PointMarker] = {}
        self.palette_point: ClickPoint | None = None
        self.palette_with_text = False
        self.palette_origin: tuple[int, int] | None = None
        self.session_job = None
        self.point_row_job = None
        self.wait_cell_editor = None
        self.wait_cell_point_id = None
        self.wait_change_job = None
        self.paste_wait_var = tk.StringVar(value=str(DEFAULT_PASTE_WAIT))
        self.visible_var = tk.BooleanVar(value=False)
        self.armed_var = tk.BooleanVar(value=True)
        self.overlay_var = tk.BooleanVar(value=False)
        self.status_var = tk.StringVar(value="Cargá o creá un perfil para empezar.")
        self.profile_info_var = tk.StringVar(value="")
        self.profile_name_var = tk.StringVar()
        self.profile_key_var = tk.StringVar()
        self.initial_delay_var = tk.StringVar(value="0.0")
        self.button_var = tk.StringVar(value="Izquierdo")
        self.clicks_var = tk.StringVar(value="1")
        self.wait_var = tk.StringVar(value="0.3")
        self.point_text_var = tk.StringVar()
        self.replace_var = tk.BooleanVar(value=True)
        self.hint_var = tk.StringVar()

        self._build_ui()
        self.load_settings()
        self.load_session()
        self.apply_armed_state()

        # Overlay
        self.overlay = OverlayWindow(self)
        self.overlay.set_armed(self.armed)
        if self.overlay_var.get():
            self.overlay.show()
            self.overlay_visible = True

        self.hotkeys = MultiHotkeys(
            self.events,
            get_key_map=lambda: self._key_map_snapshot(),
            is_suspended=lambda: self.modal_depth > 0,
            is_engine_running=lambda: self.engine.running,
            is_armed=lambda: self.armed,
            logger=self.log,
        )
        self.emergency = EmergencyHotkey(self.events, logger=self.log)

        # Bandeja del sistema
        self.tray = TrayIcon(self, self.log)
        if not self.tray.start():
            self.tray = None

        self.bind("<Map>", self.on_editor_map, add="+")
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.wait_var.trace_add("write", self.on_wait_changed)
        self.after(60, self.process_events)
        try:
            self.hotkeys.start()
        except Exception as exc:
            self.log.error("No se pudieron activar hotkeys: %s", exc)
            messagebox.showerror(APP_NAME, f"No se pudieron activar los atajos globales:\n{exc}")

        self.after(300, self.refresh_markers)

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("TButton", padding=(8, 6))
        style.configure("Treeview", rowheight=26)
        style.configure("ArmOn.TButton", background="#0a7a44", foreground="white",
                        font=("Segoe UI", 11, "bold"), padding=(14, 8))
        style.map("ArmOn.TButton", background=[("active", "#0a9b55")])
        style.configure("ArmOff.TButton", background="#7a2c2c", foreground="white",
                        font=("Segoe UI", 11, "bold"), padding=(14, 8))
        style.map("ArmOff.TButton", background=[("active", "#a03c3c")])

        # ================================================================
        # HEADER (2 filas para asegurar que los checkboxes se vean
        # completos, sin importar el ancho de la ventana).
        # ================================================================
        header = ttk.Frame(self, padding=(10, 8, 10, 4))
        header.pack(fill="x")

        # --- Fila 1: título + switch + About/Help ---
        row1 = ttk.Frame(header)
        row1.pack(fill="x")
        ttk.Label(row1, text="ACC · AutoCallCenter",
                  font=("Segoe UI", 16, "bold")).pack(side="left")

        self.arm_button = ttk.Button(
            row1, text="🟢 ACTIVO (Ctrl+Shift+P)",
            style="ArmOn.TButton", command=self.toggle_armed)
        self.arm_button.pack(side="left", padx=(16, 4))
        ttk.Label(row1, text="(si está apagado, las teclas de perfil no hacen nada)",
                  foreground="#555").pack(side="left", padx=(4, 0))

        ttk.Button(row1, text="Acerca de",
                   command=self.show_about).pack(side="right", padx=(6, 0))
        ttk.Button(row1, text="Ayuda",
                   command=self.show_help).pack(side="right", padx=(6, 0))

        # --- Fila 2: checkboxes con espacio de sobra ---
        row2 = ttk.Frame(header)
        row2.pack(fill="x", pady=(6, 0))
        ttk.Label(row2, text="Opciones:", foreground="#555").pack(side="left", padx=(0, 8))

        self.markers_chk = ttk.Checkbutton(
            row2, text="Mostrar marcadores",
            variable=self.visible_var, command=self.on_toggle_markers)
        self.markers_chk.pack(side="left", padx=(0, 20))

        self.overlay_chk = ttk.Checkbutton(
            row2, text="Overlay (Ctrl+Shift+O)",
            variable=self.overlay_var, command=self.on_toggle_overlay_checkbox)
        self.overlay_chk.pack(side="left", padx=(0, 20))

        # ================================================================
        # CUERPO
        # ================================================================
        outer = ttk.PanedWindow(self, orient="vertical")
        outer.pack(fill="both", expand=True, padx=10, pady=(4, 6))

        body = ttk.Frame(outer)
        outer.add(body, weight=4)
        body.columnconfigure(0, weight=1, minsize=260)
        body.columnconfigure(1, weight=3)
        body.rowconfigure(0, weight=1)

        # Panel izquierdo
        left = ttk.LabelFrame(body, text="Perfiles cargados", padding=8)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        left.rowconfigure(1, weight=1)
        left.columnconfigure(0, weight=1)

        toolbar = ttk.Frame(left)
        toolbar.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        ttk.Button(toolbar, text="+ Nuevo", command=self.new_profile).pack(side="left")
        ttk.Button(toolbar, text="Cargar JSON", command=self.load_profile_dialog).pack(side="left", padx=4)
        ttk.Button(toolbar, text="Guardar", command=self.save_current_profile).pack(side="left")
        ttk.Button(toolbar, text="Guardar como…", command=lambda: self.save_current_profile(save_as=True)).pack(side="left", padx=4)
        ttk.Button(toolbar, text="Eliminar", command=self.delete_current_profile).pack(side="left")

        self.profile_tree = ttk.Treeview(left, columns=("name", "key", "points"),
                                         show="headings", selectmode="browse")
        self.profile_tree.heading("name", text="Nombre")
        self.profile_tree.heading("key", text="Tecla")
        self.profile_tree.heading("points", text="Pts")
        self.profile_tree.column("name", width=140, stretch=True)
        self.profile_tree.column("key", width=90, anchor="center")
        self.profile_tree.column("points", width=45, anchor="center")
        self.profile_tree.grid(row=1, column=0, sticky="nsew")
        pscroll = ttk.Scrollbar(left, orient="vertical", command=self.profile_tree.yview)
        pscroll.grid(row=1, column=1, sticky="ns")
        self.profile_tree.configure(yscrollcommand=pscroll.set)
        self.profile_tree.bind("<<TreeviewSelect>>", self.on_profile_selected)

        ttk.Label(left, textvariable=self.profile_info_var,
                  wraplength=250, foreground="#555").grid(row=2, column=0, columnspan=2,
                                                          sticky="w", pady=(6, 0))

        # Panel derecho
        right = ttk.Frame(body)
        right.grid(row=0, column=1, sticky="nsew")
        right.rowconfigure(1, weight=1)
        right.columnconfigure(0, weight=1)

        meta = ttk.LabelFrame(right, text="Perfil", padding=8)
        meta.grid(row=0, column=0, sticky="ew")
        meta.columnconfigure(1, weight=1)
        meta.columnconfigure(3, weight=1)
        ttk.Label(meta, text="Nombre:").grid(row=0, column=0, sticky="w")
        name_entry = ttk.Entry(meta, textvariable=self.profile_name_var)
        name_entry.grid(row=0, column=1, sticky="ew", padx=(5, 15))
        name_entry.bind("<FocusOut>", lambda _e: self.apply_profile_meta())
        name_entry.bind("<Return>", lambda _e: self.apply_profile_meta())
        ttk.Label(meta, text="Tecla:").grid(row=0, column=2, sticky="w")
        self.key_combo = ttk.Combobox(meta, textvariable=self.profile_key_var,
                                      values=[label for _n, label in KEY_OPTIONS],
                                      state="readonly", width=18)
        self.key_combo.grid(row=0, column=3, sticky="ew", padx=(5, 0))
        self.key_combo.bind("<<ComboboxSelected>>", lambda _e: self.apply_profile_meta())
        ttk.Label(meta, text="Demora inicial (s):").grid(row=1, column=0, sticky="w", pady=(6, 0))
        delay_entry = ttk.Entry(meta, textvariable=self.initial_delay_var, width=10)
        delay_entry.grid(row=1, column=1, sticky="w", padx=(5, 15), pady=(6, 0))
        delay_entry.bind("<FocusOut>", lambda _e: self.apply_profile_meta())
        delay_entry.bind("<Return>", lambda _e: self.apply_profile_meta())
        ttk.Label(meta, text="Espera tras pegar (s):").grid(row=1, column=2, sticky="w", pady=(6, 0))
        paste_entry = ttk.Entry(meta, textvariable=self.paste_wait_var, width=10)
        paste_entry.grid(row=1, column=3, sticky="w", padx=(5, 0), pady=(6, 0))
        paste_entry.bind("<FocusOut>", lambda _e: self.on_paste_wait_changed())
        paste_entry.bind("<Return>", lambda _e: self.on_paste_wait_changed())

        points_frame = ttk.LabelFrame(right, text="Puntos del perfil", padding=8)
        points_frame.grid(row=1, column=0, sticky="nsew", pady=(8, 0))
        points_frame.rowconfigure(2, weight=1)
        points_frame.columnconfigure(0, weight=1)

        palettes = ttk.Frame(points_frame)
        palettes.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        self.click_palette = tk.Label(
            palettes, text="⊕  Arrastrá CLIC", bg="#086f78", fg="white",
            font=("Segoe UI", 11, "bold"), padx=12, pady=8, cursor="fleur")
        self.click_palette.pack(side="left", padx=(0, 8))
        self.click_palette.bind("<ButtonPress-1>", lambda e: self.palette_begin(False, e))
        self.click_palette.bind("<B1-Motion>", self.palette_drag)
        self.click_palette.bind("<ButtonRelease-1>", self.palette_end)
        self.paste_palette = tk.Label(
            palettes, text="📝  Arrastrá P (pegar texto)", bg="#9b5c00", fg="white",
            font=("Segoe UI", 11, "bold"), padx=12, pady=8, cursor="fleur")
        self.paste_palette.pack(side="left")
        self.paste_palette.bind("<ButtonPress-1>", lambda e: self.palette_begin(True, e))
        self.paste_palette.bind("<B1-Motion>", self.palette_drag)
        self.paste_palette.bind("<ButtonRelease-1>", self.palette_end)
        ttk.Button(palettes, text="+ Agregar punto", command=self.add_point_at_center).pack(side="right")

        columns = ("number", "x", "y", "button", "clicks", "wait", "text")
        self.points_tree = ttk.Treeview(points_frame, columns=columns, show="headings",
                                        selectmode="browse", height=10)
        for col, label, width in zip(columns,
            ["#", "X", "Y", "Botón", "Clics", "Espera (s)", "Texto a pegar"],
            [40, 55, 55, 75, 50, 90, 260]):
            self.points_tree.heading(col, text=label)
            self.points_tree.column(col, width=width, anchor="center", stretch=(col == "text"))
        self.points_tree.grid(row=2, column=0, sticky="nsew")
        tscroll = ttk.Scrollbar(points_frame, orient="vertical", command=self.points_tree.yview)
        tscroll.grid(row=2, column=1, sticky="ns")
        self.points_tree.configure(yscrollcommand=tscroll.set)
        self.points_tree.bind("<<TreeviewSelect>>", self.on_point_selected)
        self.points_tree.bind("<Double-1>", self.edit_wait_cell)

        bottom = ttk.Frame(points_frame)
        bottom.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        bottom.columnconfigure(1, weight=1)

        actions = ttk.LabelFrame(bottom, text="Acciones", padding=6)
        actions.grid(row=0, column=0, sticky="nw")
        ttk.Button(actions, text="Subir", command=lambda: self.move_point(-1)).grid(row=0, column=0, sticky="ew", padx=2, pady=2)
        ttk.Button(actions, text="Bajar", command=lambda: self.move_point(1)).grid(row=0, column=1, sticky="ew", padx=2, pady=2)
        ttk.Button(actions, text="Eliminar", command=self.delete_point).grid(row=1, column=0, columnspan=2, sticky="ew", padx=2, pady=2)

        options = ttk.LabelFrame(bottom, text="Opciones del punto seleccionado", padding=8)
        options.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        options.columnconfigure(1, weight=1)
        options.columnconfigure(3, weight=1)
        ttk.Label(options, text="Botón:").grid(row=0, column=0, sticky="w")
        ttk.Combobox(options, values=tuple(BUTTONS), textvariable=self.button_var,
                     state="readonly", width=11).grid(row=0, column=1, sticky="w", padx=(5, 15))
        ttk.Label(options, text="Clics:").grid(row=0, column=2, sticky="w")
        ttk.Spinbox(options, from_=1, to=100, width=7,
                    textvariable=self.clicks_var).grid(row=0, column=3, sticky="w", padx=(5, 0))
        ttk.Label(options, text="Espera antes (s):").grid(row=1, column=0, sticky="w", pady=(6, 0))
        ttk.Spinbox(options, from_=0, to=86400, increment=0.1, width=9,
                    textvariable=self.wait_var).grid(row=1, column=1, sticky="w", padx=(5, 15), pady=(6, 0))
        ttk.Checkbutton(options, text="Reemplazar texto previo (Ctrl+A)",
                        variable=self.replace_var).grid(row=1, column=2, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Label(options, text="Texto a pegar (vacío = solo clic):").grid(
            row=2, column=0, columnspan=4, sticky="w", pady=(8, 2))
        self.text_entry = ttk.Entry(options, textvariable=self.point_text_var)
        self.text_entry.grid(row=3, column=0, columnspan=4, sticky="ew")
        self.text_entry.bind("<Return>", lambda _e: self.apply_point_options())
        buttons = ttk.Frame(options)
        buttons.grid(row=4, column=0, columnspan=4, sticky="e", pady=(6, 0))
        ttk.Button(buttons, text="Aplicar cambios al punto",
                   command=self.apply_point_options).pack(side="right")

        status = ttk.Frame(body)
        status.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        ttk.Label(status, textvariable=self.status_var, wraplength=1080).pack(anchor="w")
        ttk.Label(status, textvariable=self.hint_var,
                  foreground="#555").pack(anchor="w")
        self.hint_var.set(
            "Ctrl+Shift+P: activar/pausar · Ctrl+Shift+O: overlay · "
            "Cada tecla arranca su perfil · si ya corre, la misma tecla lo detiene · "
            "Esc detiene todo · esquina (0–3) = parada de emergencia · "
            "X = minimizar a bandeja")

        log_frame = ttk.LabelFrame(outer, text="Registro de actividad", padding=6)
        outer.add(log_frame, weight=1)
        log_frame.rowconfigure(1, weight=1)
        log_frame.columnconfigure(0, weight=1)
        log_bar = ttk.Frame(log_frame)
        log_bar.grid(row=0, column=0, sticky="ew", pady=(0, 4))
        ttk.Button(log_bar, text="Copiar Log", command=self.copy_log).pack(side="left")
        ttk.Button(log_bar, text="Limpiar", command=self.clear_log).pack(side="left", padx=4)
        ttk.Button(log_bar, text="Abrir archivo de log",
                   command=self.open_log_file).pack(side="left")
        ttk.Label(log_bar, text=f"Datos: {BASE_DIR}",
                  foreground="#555").pack(side="right")
        self.log_widget = ScrolledText(log_frame, height=8, wrap="word",
                                       state="disabled", font=("Consolas", 9))
        self.log_widget.grid(row=1, column=0, sticky="nsew")
        self.log_widget.tag_configure("INFO", foreground="#203040")
        self.log_widget.tag_configure("WARNING", foreground="#b35900")
        self.log_widget.tag_configure("ERROR", foreground="#b30000")
        self.log_widget.tag_configure("DEBUG", foreground="#666666")

    # ------------------------------------------------------ Arm switch
    def toggle_armed(self):
        self.set_armed(not self.armed, source="botón")

    def set_armed(self, armed, source="sistema"):
        if armed == self.armed:
            if self.overlay is not None:
                self.overlay.set_armed(self.armed)
            return
        self.armed = armed
        self._armed_warned = False
        self.armed_var.set(armed)
        if not armed:
            if self.engine.running:
                self.engine.cancel("Detenido porque se apagó el switch principal.")
        self.apply_armed_state()
        self.save_settings()
        if armed:
            self.log.info("Switch principal ACTIVADO (%s). Las teclas de perfil funcionan.", source)
            self.status_var.set("AutoCallCenter ACTIVO. Las teclas de perfil funcionan.")
        else:
            self.log.warning("Switch principal APAGADO (%s). Las teclas de perfil se ignoran.", source)
            self.status_var.set("AutoCallCenter PAUSADO. Las teclas de perfil no hacen nada. "
                                "Esc sigue deteniendo ejecuciones en curso.")

    def apply_armed_state(self):
        if self.armed:
            self.arm_button.configure(text="🟢 ACTIVO — Ctrl+Shift+P para pausar",
                                      style="ArmOn.TButton")
        else:
            self.arm_button.configure(text="⚪ PAUSADO — Ctrl+Shift+P para activar",
                                      style="ArmOff.TButton")
        if self.overlay is not None:
            self.overlay.set_armed(self.armed)

    # ------------------------------------------------------ Overlay
    def on_toggle_overlay_checkbox(self):
        self.set_overlay_visible(self.overlay_var.get(), source="checkbox")

    def toggle_overlay(self):
        self.set_overlay_visible(not self.overlay_visible, source="atajo/botón")

    def set_overlay_visible(self, visible, source="sistema"):
        self.overlay_visible = bool(visible)
        self.overlay_var.set(self.overlay_visible)
        if self.overlay is None:
            return
        if self.overlay_visible:
            self.overlay.show()
            self.log.info("Overlay mostrado (%s).", source)
        else:
            self.overlay.hide()
            self.log.info("Overlay oculto (%s).", source)
        self.save_settings()

    # ----------------------------------------------------------- utilidades
    @contextmanager
    def dialog(self):
        self.modal_depth += 1
        try:
            yield
        finally:
            self.modal_depth -= 1

    def _key_map_snapshot(self):
        with self.profiles_lock:
            return {p.key: p.id for p in self.profiles.values() if p.key}

    def current_profile(self) -> Profile | None:
        with self.profiles_lock:
            return self.profiles.get(self.selected_profile_id)

    def selected_point(self) -> ClickPoint | None:
        selection = self.points_tree.selection()
        profile = self.current_profile()
        if not selection or not profile:
            return None
        ident = selection[0]
        return next((p for p in profile.points if p.id == ident), None)

    def select_point_in_tree(self, point_id):
        if self.points_tree.exists(point_id):
            self.points_tree.selection_set(point_id)
            self.points_tree.see(point_id)

    # --------------------------------------------------------------- Logs
    def append_log(self, level, message):
        self.log_widget.configure(state="normal")
        tag = level if level in ("INFO", "WARNING", "ERROR", "DEBUG") else "INFO"
        self.log_widget.insert("end", message + "\n", tag)
        line_count = int(self.log_widget.index("end-1c").split(".")[0])
        if line_count > 5000:
            self.log_widget.delete("1.0", f"{line_count - 5000}.0")
        self.log_widget.see("end")
        self.log_widget.configure(state="disabled")

    def copy_log(self):
        try:
            text = self.log_widget.get("1.0", "end-1c")
            self.clipboard_clear()
            self.clipboard_append(text)
            self.log.info("Log copiado al portapapeles (%d caracteres).", len(text))
            self.status_var.set("Log copiado al portapapeles.")
        except Exception as exc:
            self.log.error("No se pudo copiar el log: %s", exc)
            self.status_var.set(f"No se pudo copiar el log: {exc}")

    def clear_log(self):
        self.log_widget.configure(state="normal")
        self.log_widget.delete("1.0", "end")
        self.log_widget.configure(state="disabled")
        self.log.info("Registro visual limpiado (el archivo de log sigue intacto).")

    def open_log_file(self):
        try:
            LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
            if not LOG_FILE.exists():
                LOG_FILE.touch()
            if os.name == "nt":
                os.startfile(str(LOG_FILE))
            else:
                self.log.info("Archivo de log: %s", LOG_FILE)
        except OSError as exc:
            self.log.error("No se pudo abrir el archivo de log: %s", exc)

    # --------------------------------------------------------------- About
    def show_about(self):
        with self.dialog():
            win = tk.Toplevel(self)
            win.title("Acerca de")
            win.transient(self)
            win.resizable(False, False)
            win.grab_set()
            frame = ttk.Frame(win, padding=22)
            frame.pack(fill="both", expand=True)
            ttk.Label(frame, text=APP_NAME, font=("Segoe UI", 16, "bold")).pack()
            ttk.Label(frame, text=f"Versión {APP_VERSION}",
                      font=("Segoe UI", 10)).pack(pady=(0, 14))
            ttk.Separator(frame, orient="horizontal").pack(fill="x", pady=(0, 14))
            ttk.Label(frame, text="Desarrollado por",
                      font=("Segoe UI", 10)).pack()
            ttk.Label(frame, text=AUTHOR,
                      font=("Segoe UI", 12, "bold")).pack(pady=(2, 10))
            link = tk.Label(frame, text=GITHUB_URL, fg="#0066cc",
                            cursor="hand2", font=("Segoe UI", 10, "underline"))
            link.pack()
            link.bind("<Button-1>", lambda e: webbrowser.open(GITHUB_URL))
            ttk.Label(frame, text=f"\nDatos guardados en:\n{BASE_DIR}",
                      font=("Segoe UI", 8), foreground="#555", justify="center").pack(pady=(12, 0))
            ttk.Button(frame, text="Cerrar", command=win.destroy).pack(pady=(14, 0))
            win.update_idletasks()
            x = self.winfo_rootx() + (self.winfo_width() - win.winfo_width()) // 2
            y = self.winfo_rooty() + (self.winfo_height() - win.winfo_height()) // 2
            win.geometry(f"+{max(0, x)}+{max(0, y)}")
            win.wait_window()

    def show_help(self):
        with self.dialog():
            messagebox.showinfo(APP_NAME,
                "1. Cargá o creá un perfil.\n"
                "2. Elegí una tecla en el combo (numpad, dígitos, letras, F1–F12…).\n"
                "3. Arrastrá ⊕ CLIC para puntos que solo hacen clic.\n"
                "4. Arrastrá 📝 P para puntos que hacen clic y pegan un texto.\n"
                "   Al soltar P se te pedirá el texto a pegar.\n"
                "5. Guardá el perfil (o la sesión se conserva automáticamente).\n\n"
                "ATAJOS GLOBALES (funcionan aunque la ventana no tenga foco):\n"
                "  Ctrl+Shift+P → ACTIVAR/PAUSAR\n"
                "  Ctrl+Shift+O → mostrar/ocultar overlay\n"
                "  Esc → detener cualquier ejecución en curso\n\n"
                "OVERLAY:\n"
                "  Ventana pequeña flotante con botones ACTIVO/PAUSADO y Esc.\n"
                "  Se puede arrastrar desde su barra de título.\n"
                "  La X del overlay solo lo oculta.\n\n"
                "BANDEJA:\n"
                "  La X de la ventana principal MINIMIZA a la bandeja del sistema\n"
                "  (iconitos ocultos). Para cerrar de verdad, usá 'Salir' desde\n"
                "  el menú del ícono en la bandeja.\n\n"
                f"Los datos (.json de perfiles, sesión, settings, log) se guardan en:\n{BASE_DIR}",
                parent=self)

    # ----------------------------------------------------------- sesión
    def save_settings(self):
        try:
            write_json(SETTINGS_FILE, {
                "version": APP_VERSION,
                "paste_wait": number(self.paste_wait_var.get(), "Espera al pegar", 0, 60),
                "visible_markers": self.visible_var.get(),
                "armed": bool(self.armed),
                "overlay_visible": bool(self.overlay_visible),
            })
        except (OSError, ValueError) as exc:
            self.log.warning("No se pudo guardar settings en %s: %s", SETTINGS_FILE, exc)

    def load_settings(self):
        if not SETTINGS_FILE.exists():
            return
        try:
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            self.paste_wait_var.set(str(number(data.get("paste_wait", DEFAULT_PASTE_WAIT),
                                           "Espera al pegar", 0, 60)))
            self.visible_var.set(bool(data.get("visible_markers", False)))
            self.armed = bool(data.get("armed", True))
            self.armed_var.set(self.armed)
            self.overlay_visible = bool(data.get("overlay_visible", False))
            self.overlay_var.set(self.overlay_visible)
        except (OSError, ValueError, AttributeError) as exc:
            self.log.warning("No se pudo leer settings: %s", exc)

    def save_session(self):
        if self.session_job is not None:
            self.after_cancel(self.session_job)
            self.session_job = None
        self.session_job = self.after(200, self._write_session)

    def _write_session(self):
        self.session_job = None
        try:
            with self.profiles_lock:
                profiles_data = []
                for p in self.profiles.values():
                    d = profile_to_dict(p)
                    d["path"] = str(p.path) if p.path else None
                    profiles_data.append(d)
            write_json(SESSION_FILE, {
                "version": APP_VERSION,
                "profiles": profiles_data,
                "selected_id": self.selected_profile_id,
            })
        except (OSError, ValueError) as exc:
            self.log.warning("No se pudo guardar la sesión: %s", exc)

    def load_session(self):
        if not SESSION_FILE.exists():
            return
        try:
            data = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            self.log.warning("No se pudo leer la sesión: %s", exc)
            return
        raw = data.get("profiles") or []
        loaded = 0
        for item in raw:
            try:
                path_str = item.get("path")
                file_path = Path(path_str) if isinstance(path_str, str) and path_str else None
                profile = profile_from_dict(item, path=file_path)
                with self.profiles_lock:
                    self.profiles[profile.id] = profile
                loaded += 1
            except (ValueError, TypeError) as exc:
                self.log.warning("Perfil inválido en sesión: %s", exc)
        if loaded:
            self.refresh_profiles_tree()
            sel = data.get("selected_id")
            if sel and sel in self.profiles:
                self.select_profile(sel)
            elif self.profiles:
                self.select_profile(next(iter(self.profiles)))
            self.status_var.set(f"Sesión recuperada: {loaded} perfil(es).")
            self.log.info("Sesión cargada con %d perfil(es).", loaded)

    # --------------------------------------------------- perfiles
    def refresh_profiles_tree(self):
        selection = self.selected_profile_id
        for ident in self.profile_tree.get_children():
            self.profile_tree.delete(ident)
        with self.profiles_lock:
            items = sorted(self.profiles.values(),
                           key=lambda p: (p.key or "zzz", p.name.lower()))
        for profile in items:
            self.profile_tree.insert("", "end", iid=profile.id,
                values=(profile.name, key_label(profile.key), len(profile.points)))
        if selection and selection in self.profiles:
            self.profile_tree.selection_set(selection)
        self.profile_info_var.set(
            f"{len(self.profiles)} perfil(es) cargados. "
            "Hacé clic en uno para editarlo.")

    def on_profile_selected(self, _event=None):
        sel = self.profile_tree.selection()
        if not sel:
            return
        self.select_profile(sel[0])

    def select_profile(self, profile_id):
        if self.engine.running:
            self.status_var.set("Detené la ejecución antes de cambiar de perfil.")
            self.refresh_profiles_tree()
            return
        with self.profiles_lock:
            profile = self.profiles.get(profile_id)
        if profile is None:
            return
        self.selected_profile_id = profile_id
        self.profile_name_var.set(profile.name)
        self.profile_key_var.set(key_label(profile.key))
        self.initial_delay_var.set(f"{profile.initial_delay:g}")
        self.refresh_points_tree()
        self.refresh_markers()

    def _unique_key(self, key_name, exclude_id):
        if not key_name:
            return True
        with self.profiles_lock:
            for p in self.profiles.values():
                if p.id != exclude_id and p.key == key_name:
                    return False
        return True

    def apply_profile_meta(self):
        profile = self.current_profile()
        if profile is None:
            return
        name = self.profile_name_var.get().strip() or "Sin nombre"
        key_display = self.profile_key_var.get()
        key_name = KEY_LABEL_TO_NAME.get(key_display, "")
        try:
            delay = number(self.initial_delay_var.get(), "Demora inicial", 0, 60)
        except ValueError as exc:
            self.status_var.set(str(exc))
            return
        if key_name and not self._unique_key(key_name, profile.id):
            self.status_var.set(f"La tecla '{key_display}' ya está asignada a otro perfil.")
            self.profile_key_var.set(key_label(profile.key))
            return
        old_key = profile.key
        profile.name = name
        profile.key = key_name
        profile.initial_delay = delay
        self.save_session()
        self.refresh_profiles_tree()
        self.profile_tree.selection_set(profile.id)
        self.log.info("Perfil '%s' actualizado (tecla %s → %s).",
                      name, key_label(old_key), key_label(key_name))
        self.status_var.set(f"Perfil '{profile.name}' actualizado.")

    def on_paste_wait_changed(self):
        try:
            value = number(self.paste_wait_var.get(), "Espera al pegar", 0, 60)
        except ValueError as exc:
            self.status_var.set(str(exc))
            return
        self.engine.paste_wait = value
        self.paste_wait_var.set(f"{value:g}")
        self.save_settings()

    def new_profile(self):
        if self.engine.running:
            self.status_var.set("Detené la ejecución antes de crear un perfil.")
            return
        profile = Profile.create(name="Nuevo perfil", key="")
        with self.profiles_lock:
            self.profiles[profile.id] = profile
        self.refresh_profiles_tree()
        self.select_profile(profile.id)
        self.save_session()
        self.log.info("Perfil creado: %s", profile.id)
        self.status_var.set("Perfil creado. Asignale una tecla y agregá puntos.")

    def load_profile_dialog(self):
        if self.engine.running:
            self.status_var.set("Detené la ejecución antes de cargar un perfil.")
            return
        with self.dialog():
            path = filedialog.askopenfilename(
                parent=self, title="Cargar perfil JSON",
                initialdir=str(PROFILE_DIR if PROFILE_DIR.exists() else PROGRAM_DIR),
                filetypes=[("Perfiles JSON", "*.json"), ("Todos", "*.*")])
        if not path:
            return
        self.load_profile_from_path(Path(path))

    def load_profile_from_path(self, path: Path):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            profile = profile_from_dict(data, path=path)
        except (OSError, ValueError, TypeError) as exc:
            self.log.error("No se pudo cargar %s: %s", path, exc)
            with self.dialog():
                messagebox.showerror(APP_NAME, f"No se pudo cargar:\n{exc}", parent=self)
            return
        with self.profiles_lock:
            for existing in list(self.profiles.values()):
                if existing.path and existing.path == path:
                    del self.profiles[existing.id]
            self.profiles[profile.id] = profile
        self.refresh_profiles_tree()
        self.select_profile(profile.id)
        self.save_session()
        self.log.info("Perfil cargado desde %s (%d puntos).", path, len(profile.points))
        self.status_var.set(f"Perfil '{profile.name}' cargado desde {path.name}.")

    def save_current_profile(self, save_as=False):
        profile = self.current_profile()
        if profile is None:
            self.status_var.set("Seleccioná un perfil para guardar.")
            return
        path = profile.path
        if path is None or save_as:
            with self.dialog():
                filename = re.sub(r"[^\w .-]", "_", profile.name).strip(" .") or "perfil"
                selected = filedialog.asksaveasfilename(
                    parent=self, title="Guardar perfil como",
                    defaultextension=".json",
                    initialdir=str(PROFILE_DIR if PROFILE_DIR.exists() else PROGRAM_DIR),
                    initialfile=filename + ".json",
                    filetypes=[("Perfil JSON", "*.json")])
            if not selected:
                return
            path = Path(selected).resolve()
        try:
            write_json(path, profile_to_dict(profile))
            profile.path = path
            self.save_session()
            self.log.info("Perfil guardado en %s", path)
            self.status_var.set(f"Perfil guardado en {path}.")
        except (OSError, ValueError) as exc:
            self.log.error("No se pudo guardar: %s", exc)
            with self.dialog():
                messagebox.showerror(APP_NAME, f"No se pudo guardar:\n{exc}", parent=self)

    def delete_current_profile(self):
        profile = self.current_profile()
        if profile is None:
            return
        if self.engine.running:
            self.status_var.set("Detené la ejecución antes de eliminar un perfil.")
            return
        with self.dialog():
            ok = messagebox.askyesno(APP_NAME,
                f"¿Eliminar el perfil '{profile.name}' del programa?\n"
                "(El archivo .json no se borra del disco.)", parent=self)
        if not ok:
            return
        with self.profiles_lock:
            self.profiles.pop(profile.id, None)
        self.selected_profile_id = None
        self.refresh_profiles_tree()
        self.clear_markers()
        self.points_tree.delete(*self.points_tree.get_children())
        self.save_session()
        self.log.info("Perfil eliminado: %s", profile.name)
        if self.profiles:
            self.select_profile(next(iter(self.profiles)))
        else:
            self.status_var.set("No quedan perfiles cargados.")

    # ------------------------------------------------ puntos
    def refresh_points_tree(self):
        self.cancel_wait_cell_edit()
        for ident in self.points_tree.get_children():
            self.points_tree.delete(ident)
        profile = self.current_profile()
        if profile is None:
            return
        for index, point in enumerate(profile.points, 1):
            self.points_tree.insert("", "end", iid=point.id)
            self.update_point_row(point)
        if profile.points:
            self.points_tree.selection_set(profile.points[0].id)

    def update_point_row(self, point):
        profile = self.current_profile()
        if profile is None:
            return
        try:
            index = profile.points.index(point) + 1
        except ValueError:
            return
        text_preview = point.text.replace("\n", " ⏎ ")
        if len(text_preview) > 80:
            text_preview = text_preview[:77] + "…"
        self.points_tree.item(point.id, values=(
            index, point.x, point.y, BUTTON_LABELS[point.button],
            point.clicks, f"{point.wait:g}", text_preview))

    def schedule_point_row_update(self, point):
        self._pending_row_point = point
        if self.point_row_job is not None:
            return
        self.point_row_job = self.after(50, self._flush_point_row_update)

    def _flush_point_row_update(self):
        self.point_row_job = None
        point = getattr(self, "_pending_row_point", None)
        if point is not None:
            self.update_point_row(point)

    def on_point_selected(self, _event=None):
        point = self.selected_point()
        if not point:
            return
        self.button_var.set(BUTTON_LABELS[point.button])
        self.clicks_var.set(str(point.clicks))
        self.wait_var.set(f"{point.wait:g}")
        self.point_text_var.set(point.text)
        self.replace_var.set(point.replace_text)

    def scene_changed(self, light=False):
        self.save_session()
        if not light:
            self.refresh_profiles_tree()
            self.refresh_markers()

    def apply_point_options(self):
        point = self.selected_point()
        if point is None:
            self.status_var.set("Seleccioná un punto para editar sus opciones.")
            return
        try:
            button = BUTTONS[self.button_var.get()]
            clicks = integer(self.clicks_var.get(), "Clics", 1, 100)
            wait = number(self.wait_var.get(), "Espera antes", 0, 86400)
        except (ValueError, KeyError) as exc:
            self.status_var.set(str(exc))
            return
        point.button = button
        point.clicks = clicks
        point.wait = wait
        point.text = self.point_text_var.get()
        point.replace_text = self.replace_var.get()
        self.update_point_row(point)
        marker = self.markers.get(point.id)
        if marker is not None:
            marker.update_appearance()
        self.scene_changed()
        self.log.info("Punto actualizado: %s", point.id)
        self.status_var.set(
            f"Punto actualizado ({'CLIC + pegar' if point.is_paste else 'solo clic'}).")

    def add_point_at_center(self):
        x = self.winfo_rootx() + self.winfo_width() // 2
        y = self.winfo_rooty() + self.winfo_height() // 2
        self.add_point(x, y, with_text=False)

    def add_point(self, x, y, with_text=False):
        profile = self.current_profile()
        if profile is None:
            self.status_var.set("Primero creá o seleccioná un perfil.")
            return None
        if len(profile.points) >= MAX_POINTS:
            self.status_var.set(f"Máximo {MAX_POINTS} puntos por perfil.")
            return None
        try:
            button = BUTTONS[self.button_var.get()]
            clicks = integer(self.clicks_var.get(), "Clics", 1, 100)
            wait = number(self.wait_var.get(), "Espera antes", 0, 86400)
        except (ValueError, KeyError) as exc:
            self.status_var.set(str(exc))
            return None
        point = ClickPoint.create(x, y, button=button, clicks=clicks, wait=wait,
                                  text="", replace_text=True)
        profile.points.append(point)
        self.visible_var.set(True)
        self.refresh_points_tree()
        self.refresh_markers()
        self.select_point_in_tree(point.id)
        self.save_session()
        self.log.info("Punto agregado en (%d,%d).", x, y)
        return point

    def delete_point(self):
        profile = self.current_profile()
        point = self.selected_point()
        if profile is None or point is None:
            return
        profile.points.remove(point)
        marker = self.markers.pop(point.id, None)
        if marker is not None:
            marker.destroy()
        self.refresh_points_tree()
        self.scene_changed()
        self.log.info("Punto eliminado: %s", point.id)

    def move_point(self, delta):
        profile = self.current_profile()
        point = self.selected_point()
        if profile is None or point is None:
            return
        idx = profile.points.index(point)
        new_idx = idx + delta
        if 0 <= new_idx < len(profile.points):
            profile.points[idx], profile.points[new_idx] = \
                profile.points[new_idx], profile.points[idx]
            self.refresh_points_tree()
            self.points_tree.selection_set(point.id)
            self.refresh_markers()
            self.scene_changed()

    # ---------------------------- edición en celda de "Espera"
    def edit_wait_cell(self, event):
        if self.engine.running:
            return "break"
        if self.points_tree.identify_region(event.x, event.y) != "cell":
            return
        if self.points_tree.identify_column(event.x) != "#6":
            return
        ident = self.points_tree.identify_row(event.y)
        profile = self.current_profile()
        if not ident or profile is None:
            return "break"
        point = next((p for p in profile.points if p.id == ident), None)
        if point is None:
            return "break"
        if self.wait_cell_editor is not None:
            if not self.commit_wait_cell_edit():
                return "break"
        rect = self.points_tree.bbox(ident, "#6")
        if not rect:
            return "break"
        x, y, w, h = rect
        self.points_tree.selection_set(ident)
        editor = ttk.Entry(self.points_tree, justify="center")
        editor.insert(0, f"{point.wait:g}")
        editor.place(x=x, y=y, width=w, height=h)
        self.wait_cell_editor = editor
        self.wait_cell_point_id = ident
        editor.bind("<Return>", self.commit_wait_cell_edit)
        editor.bind("<KP_Enter>", self.commit_wait_cell_edit)
        editor.bind("<Escape>", self.cancel_wait_cell_edit)
        editor.bind("<FocusOut>", lambda _e: self.commit_wait_cell_edit() or self.cancel_wait_cell_edit())
        editor.focus_set()
        editor.selection_range(0, tk.END)
        return "break"

    def cancel_wait_cell_edit(self, _event=None):
        if self.wait_cell_editor is not None:
            self.wait_cell_editor.destroy()
        self.wait_cell_editor = None
        self.wait_cell_point_id = None
        return "break"

    def commit_wait_cell_edit(self, _event=None):
        if self.wait_cell_editor is None:
            return True
        try:
            value = number(self.wait_cell_editor.get(), "Espera antes", 0, 86400)
        except ValueError as exc:
            self.status_var.set(str(exc))
            return False
        profile = self.current_profile()
        ident = self.wait_cell_point_id
        self.wait_cell_editor.destroy()
        self.wait_cell_editor = None
        self.wait_cell_point_id = None
        if profile is None:
            return True
        point = next((p for p in profile.points if p.id == ident), None)
        if point is not None:
            point.wait = value
            self.update_point_row(point)
            if self.selected_point() is point:
                self.wait_var.set(f"{value:g}")
            self.scene_changed()
        return True

    def on_wait_changed(self, *_args):
        if self.engine.running or self.wait_cell_editor is not None:
            return
        if self.wait_change_job is not None:
            self.after_cancel(self.wait_change_job)
        self.wait_change_job = self.after(250, self._apply_wait_change)

    def _apply_wait_change(self):
        self.wait_change_job = None
        try:
            value = number(self.wait_var.get(), "Espera antes", 0, 86400)
        except ValueError:
            return
        point = self.selected_point()
        if point is None:
            return
        if point.wait == value:
            return
        point.wait = value
        self.update_point_row(point)
        self.save_session()

    # -------------------------------------------------- paletas de arrastre
    def palette_begin(self, with_text, _event):
        if self.engine.running:
            return
        profile = self.current_profile()
        if profile is None:
            self.status_var.set("Primero creá o seleccioná un perfil.")
            return
        x, y = self.engine.controller.position
        point = self.add_point(x, y, with_text=False)
        if point is None:
            return
        self.palette_point = point
        self.palette_with_text = with_text
        self.palette_origin = (x, y)
        self.dragging = True
        self.visible_var.set(True)
        self.refresh_markers()
        self.log.info("Nuevo punto arrastrable (%s).", "P" if with_text else "clic")

    def palette_drag(self, _event):
        if self.palette_point is None or self.engine.running:
            return
        self.palette_point.x, self.palette_point.y = map(int, self.engine.controller.position)
        marker = self.markers.get(self.palette_point.id)
        if marker is not None:
            marker.place()
        self.schedule_point_row_update(self.palette_point)

    def palette_end(self, event):
        if self.palette_point is None:
            return
        self.palette_drag(event)
        point = self.palette_point
        with_text = self.palette_with_text
        self.palette_point = None
        self.palette_with_text = False
        self.palette_origin = None
        self.dragging = False
        if with_text:
            self.after_idle(lambda: self._ask_palette_text(point))
        else:
            self.log.info("Punto de clic colocado en (%d,%d).", point.x, point.y)
        self.scene_changed(light=True)

    def _ask_palette_text(self, point):
        with self.dialog():
            text = simpledialog.askstring(
                "Texto a pegar",
                "Texto que se pegará en el punto P después del clic:",
                parent=self)
        if text:
            point.text = text
            self.update_point_row(point)
            marker = self.markers.get(point.id)
            if marker is not None:
                marker.update_appearance()
            self.scene_changed()
            self.log.info("Texto asignado al punto P: «%s»", text[:80])
            self.status_var.set(f"Punto P listo (pegará «{text[:40]}»).")
        else:
            self.log.info("Punto P creado sin texto (solo clic).")
            self.status_var.set("Punto creado sin texto (solo clic). "
                                "Seleccionalo para asignarle texto.")

    # -------------------------------------------------------- marcadores
    def on_toggle_markers(self):
        self.refresh_markers()
        self.save_settings()

    def refresh_markers(self):
        if self.engine.running or not self.visible_var.get():
            self.clear_markers()
            return
        profile = self.current_profile()
        if profile is None:
            self.clear_markers()
            return
        valid = {p.id for p in profile.points}
        for ident in list(self.markers):
            if ident not in valid:
                self.markers.pop(ident).destroy()
        for index, point in enumerate(profile.points, 1):
            marker = self.markers.get(point.id)
            if marker is None:
                marker = self.markers[point.id] = PointMarker(self, point, index)
            else:
                marker.set_index(index)
                marker.update_appearance()
            marker.show()

    def clear_markers(self):
        markers = list(self.markers.values())
        self.markers.clear()
        for m in markers:
            m.destroy()

    def on_editor_map(self, event):
        if event.widget is self and not self.engine.running:
            self.refresh_markers()

    # --------------------------------------------- disparo por tecla
    def trigger_profile(self, key_name):
        if not self.armed:
            if not self._armed_warned:
                self.log.warning("Tecla %s ignorada: switch principal en PAUSADO.",
                                 key_label(key_name))
                self.status_var.set("PAUSADO: las teclas de perfil no se ejecutan. "
                                    "Activá con Ctrl+Shift+P o el botón.")
                self._armed_warned = True
            return

        with self.profiles_lock:
            profile = next((p for p in self.profiles.values() if p.key == key_name), None)
        if profile is None:
            self.log.warning("Sin perfil para tecla %s", key_label(key_name))
            self.status_var.set(f"No hay perfil asignado a '{key_label(key_name)}'.")
            return
        if self.modal_depth > 0:
            return

        if self.engine.running:
            if self.engine.current_profile_id == profile.id:
                self.log.info("Deteniendo '%s' con su propia tecla (%s).",
                              profile.name, key_label(key_name))
                self.engine.cancel(f"'{profile.name}': detenido con la misma tecla.")
                self.status_var.set(f"Deteniendo '{profile.name}'…")
            else:
                self.log.warning("Ignorado %s: ya se está ejecutando '%s'.",
                                 key_label(key_name),
                                 self.engine.current_profile_name or "otro perfil")
                self.status_var.set(
                    f"Ya se está ejecutando '{self.engine.current_profile_name or 'otro perfil'}'. "
                    "Apretá su tecla o Esc para detenerlo.")
            return

        if not profile.points:
            self.status_var.set(f"El perfil '{profile.name}' no tiene puntos.")
            return
        try:
            self.engine.paste_wait = number(self.paste_wait_var.get(),
                                            "Espera al pegar", 0, 60)
        except ValueError as exc:
            self.status_var.set(str(exc))
            return
        if not self.emergency.arm():
            self.log.error("No se pudo armar Esc: %s", self.emergency.error)
            self.status_var.set(f"No se inició '{profile.name}': {self.emergency.error}")
            return
        self.busy = True
        self.clear_markers()
        self.iconify()
        self.update_idletasks()
        started = self.engine.start(profile)
        if not started:
            self.busy = False
            self.emergency.disarm()
            self.log.error("No se pudo iniciar '%s'.", profile.name)
            self.status_var.set(f"No se pudo iniciar '{profile.name}'.")
            return
        self.log.info("Ejecutando '%s' (%d puntos). La tecla %s lo detiene.",
                      profile.name, len(profile.points), key_label(key_name))
        self.status_var.set(f"Ejecutando '{profile.name}' ({len(profile.points)} puntos).")

    def stop_all(self):
        self.log.info("Deteniendo ejecución por pedido del usuario (Esc / botón).")
        self.engine.cancel("Detenido por el usuario.")
        self.status_var.set("Deteniendo…")

    # ------------------------------------------------- procesamiento eventos
    def process_events(self):
        for _ in range(400):
            try:
                event, data = self.events.get_nowait()
            except queue.Empty:
                break
            try:
                if event == "log":
                    level, msg = data
                    self.append_log(level, msg)
                elif event == "copy_text":
                    self.handle_copy_text(data)
                elif event == "progress":
                    run_id, index, total, name = data
                    if run_id == self.engine.run_id:
                        self.status_var.set(f"'{name}': punto {index}/{total}")
                elif event == "finished":
                    self.handle_finished(*data)
                elif event == "trigger_profile":
                    self.trigger_profile(data)
                elif event == "toggle_armed":
                    self.set_armed(not self.armed, source="atajo Ctrl+Shift+P")
                elif event == "toggle_overlay":
                    self.toggle_overlay()
                elif event == "escape":
                    self.stop_all()
                elif event == "hotkey_error":
                    self.stop_all()
                    self.status_var.set(f"Atajos desconectados: {data}")
            except Exception:
                self.log.error("Excepción procesando evento %s:\n%s",
                               event, traceback.format_exc())
        self.after(60, self.process_events)

    def handle_copy_text(self, request: ClipboardRequest):
        try:
            if request.run_id != self.engine.run_id:
                request.error = "Copia cancelada."
                return
            self.clipboard_clear()
            self.clipboard_append(request.text)
        except (tk.TclError, OSError) as exc:
            request.error = f"No se pudo copiar al portapapeles: {exc}"
            self.log.error(request.error)
        finally:
            request.done.set()

    def handle_finished(self, run_id, error):
        if not self.engine.acknowledge_finished(run_id):
            self.after(15, lambda: self.handle_finished(run_id, error))
            return
        self.busy = False
        self.emergency.disarm()
        info = error or self.engine.stop_reason or "Secuencia terminada."
        self.status_var.set(info)
        if error:
            self.log.error("Ejecución con error: %s", error)
        else:
            self.log.info("Ejecución finalizada: %s", info)
        self.refresh_markers()
        if error:
            with self.dialog():
                messagebox.showerror(APP_NAME, error, parent=self)

    # ------------------------------------------------------- bandeja / cierre
    def show_main_window(self):
        try:
            self.deiconify()
            self.lift()
            self.focus_force()
        except tk.TclError:
            pass

    def on_close(self):
        if self.tray is not None and not self._quitting:
            self.log.info("Ventana oculta en la bandeja del sistema.")
            try:
                self.withdraw()
            except tk.TclError:
                pass
            return
        self.quit_from_tray()

    def quit_from_tray(self):
        if self._quitting:
            return
        self._quitting = True
        self.log.info("Cerrando aplicación (desde bandeja)…")
        try:
            self.engine.cancel("Cerrando programa.")
            self.engine.close()
            self.emergency.disarm()
            self.hotkeys.close()
            self.save_settings()
            if self.session_job is not None:
                self.after_cancel(self.session_job)
                self.session_job = None
            if self.wait_change_job is not None:
                self.after_cancel(self.wait_change_job)
                self.wait_change_job = None
            self._write_session()
            self.clear_markers()
            if self.overlay is not None:
                try:
                    self.overlay.destroy()
                except tk.TclError:
                    pass
                self.overlay = None
            if self.tray is not None:
                self.tray.stop()
                self.tray = None
        finally:
            self.destroy()


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main():
    enable_dpi_awareness()

    try:
        ensure_data_directory()
    except OSError as exc:
        ctypes.windll.user32.MessageBoxW(
            None,
            f"No se pudo crear la carpeta de datos:\n{PROGRAM_DIR / 'Secuencias'}\n\n"
            f"Detalle: {exc}\n\n"
            "Solución: mover el programa a una carpeta con permisos de escritura "
            "(por ejemplo: C:\\ACC\\ o Documentos\\), NO a 'C:\\Program Files\\'.",
            APP_NAME, 0x10)
        return

    guard = SingleInstance()
    if not guard.acquire():
        if os.name == "nt":
            ctypes.windll.user32.MessageBoxW(
                None,
                "Ya hay un ACC - AutoCallCenter abierto. Cerrá la versión anterior o usá la ventana existente.",
                APP_NAME, 0x40)
        else:
            root = tk.Tk()
            root.withdraw()
            messagebox.showinfo(APP_NAME, "Ya hay un ACC - AutoCallCenter abierto.")
            root.destroy()
        return
    try:
        app = App()
        app.mainloop()
    finally:
        guard.close()


if __name__ == "__main__":
    main()