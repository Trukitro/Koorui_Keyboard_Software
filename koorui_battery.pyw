"""Bateria y luces del teclado KOORUI 75% en la bandeja del sistema.

Modo principal: 2.4G (receptor 320F:50C3). Tambien funciona por cable (320F:50C2,
mismo protocolo) y por Bluetooth (245A:8276), donde solo hay bateria: la da el
servicio de bateria BLE estandar y se lee de Windows.

El receptor y el teclado por cable (Telink/EVision) responden por su coleccion HID
propietaria (usage page 0xFF1C, report ID 4) con paquetes de 64 bytes:

    [04][suma_lo][suma_hi][cmd][len][dir_lo][dir_hi][estado][datos...]

donde suma = suma de los bytes 3..63. Comandos usados:

    0x01 / 0x02  abrir / cerrar una transaccion
    0x1A         bateria: porcentaje en el byte 8, byte 9 = 1 si esta cargando
    0x05         leer configuracion (maximo 0x18 bytes por paquete)
    0x06         escribir configuracion

La configuracion es flash en paginas de 0x40 bytes: escribir en la direccion 0
borra la pagina entera, asi que siempre se reescribe completa. Pagina 0:

    [1] modo (1..18)   [2] brillo (0..4)   [3] velocidad
    [5] 0x00 = color fijo, 0xFF = multicolor   [6..8] R, G, B

Requiere: pip install -r requirements.txt
"""
import colorsys
import ctypes
import json
import math
import os
import re
import statistics
import sys
import threading
import time
import traceback
import uuid
import winreg
from ctypes import wintypes

import hid
from PyQt6.QtCore import (QAbstractNativeEventFilter, QObject, QPointF, QRectF, Qt, QTime,
                          QTimer, pyqtSignal)
from PyQt6.QtGui import (QAction, QColor, QCursor, QFont, QFontMetrics, QIcon, QPainter,
                         QPainterPath, QPen, QPixmap)
from PyQt6.QtWidgets import (QApplication, QCheckBox, QColorDialog, QComboBox, QDialog,
                             QDialogButtonBox, QFormLayout, QGridLayout, QHBoxLayout,
                             QInputDialog, QLabel, QLineEdit, QMenu, QPushButton, QSlider,
                             QSpinBox, QSystemTrayIcon, QTimeEdit, QVBoxLayout, QWidget)

VID = 0x320F
PID_WIRED, PID_DONGLE = 0x50C2, 0x50C3      # teclado por cable / receptor 2.4G
BT_VID, BT_PID = 0x245A, 0x8276             # teclado por Bluetooth LE ("MK082-5.0")
LINK_USB, LINK_24G, LINK_BT = "USB", "2.4G", "Bluetooth"
USAGE_PAGE, USAGE = 0xFF1C, 0x92
REPORT_ID = 0x04
CMD_BEGIN, CMD_END, CMD_READ, CMD_WRITE, CMD_BATTERY = 0x01, 0x02, 0x05, 0x06, 0x1A
PAGE_CHUNKS = ((0x00, 0x18), (0x18, 0x18), (0x30, 0x10))
OFF_MODE, OFF_BRIGHT, OFF_SPEED, OFF_MULTI, OFF_RGB = 1, 2, 3, 5, 6

MODE_COUNT = 18
MAX_LEVEL = 4
# (nombre es, nombre en, rgb)
COLORS = (("Naranja", "Orange", (0xFF, 0x60, 0x00)), ("Rojo", "Red", (0xFF, 0x00, 0x00)),
          ("Amarillo", "Yellow", (0xFF, 0xFF, 0x00)), ("Verde", "Green", (0x00, 0xFF, 0x00)),
          ("Cian", "Cyan", (0x00, 0xFF, 0xFF)), ("Azul", "Blue", (0x00, 0x00, 0xFF)),
          ("Morado", "Purple", (0x80, 0x00, 0xFF)), ("Rosa", "Pink", (0xFF, 0x00, 0x80)),
          ("Blanco", "White", (0xFF, 0xFF, 0xFF)))

APP_NAME = "KooruiBattery"
DATA_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), APP_NAME)
STATE_FILE = os.path.join(DATA_DIR, "state.json")
MODES_FILE = os.path.join(DATA_DIR, "modes.json")
PROFILES_FILE = os.path.join(DATA_DIR, "profiles.json")
HISTORY_FILE = os.path.join(DATA_DIR, "history.json")
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")
AUTO_FILE = os.path.join(DATA_DIR, "auto.json")
LOG_FILE = os.path.join(DATA_DIR, "error.log")
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

HOUR, DAY = 3600, 86400
HISTORY_KEEP = 30 * DAY         # se descartan lecturas mas antiguas
HISTORY_EVERY = 600             # sin cambios, una lectura cada 10 minutos
HISTORY_GAP = 3 * HOUR          # un hueco mayor (teclado apagado) se dibuja punteado

BG, CARD, BORDER, TEXT, MUTED, ACCENT = "#15171c", "#1e2128", "#2c303a", "#e8eaf0", "#8a90a0", "#4da3ff"
GREEN, BLUE = "#3fbf7f", "#2878d7"

# Ajustes del usuario (se cargan de settings.json al arrancar).
DEFAULTS = {"lang": "es", "poll": 30, "low": 20, "critical": 10,
            "full_notify": True, "full_level": 100,
            "lock_off": False, "sched_on": False, "sched_from": "23:00", "sched_to": "07:00"}
AUTO_SETTLE = 10                # segundos de margen tras una escritura automatica
CFG = dict(DEFAULTS)

# Textos de la interfaz: clave -> (espanol, ingles).
STR = {
    "battery": ("Batería", "Battery"),
    "no_data": ("sin datos", "no data"),
    "charging": ("cargando", "charging"),
    "left": ("quedan {}", "{} left"),
    "now": ("ahora", "just now"),
    "ago_min": ("hace {} min", "{} min ago"),
    "ago_h": ("hace {} h", "{} h ago"),
    "ago_d": ("hace {} d", "{} d ago"),
    "history": ("HISTORIAL", "HISTORY"),
    "last": ("últimas {}", "last {}"),
    "no_history": ("Aún no hay suficientes lecturas", "Not enough readings yet"),
    "profiles": ("PERFILES", "PROFILES"),
    "save_profile": ("+ Guardar actual", "+ Save current"),
    "profile_name": ("Nombre del perfil:", "Profile name:"),
    "new_profile": ("Nuevo perfil", "New profile"),
    "rename": ("Renombrar", "Rename"),
    "overwrite": ("Actualizar con las luces actuales", "Update with current lights"),
    "delete": ("Eliminar", "Delete"),
    "profile_tip": ("Clic: aplicar · Clic derecho: más opciones",
                    "Click: apply · Right-click: more options"),
    "light_mode": ("MODO DE LUZ", "LIGHT MODE"),
    "mode": ("Modo {}", "Mode {}"),
    "mode_tip": ("Clic: activar · Clic derecho: nombre y animación",
                 "Click: activate · Right-click: name and animation"),
    "define_mode": ("Clic derecho en un modo para definir su animación",
                    "Right-click a mode to set its animation"),
    "color": ("COLOR", "COLOR"),
    "multicolor": ("Multicolor", "Multicolor"),
    "other_color": ("Otro color...", "Other color..."),
    "pick_color": ("Color del teclado", "Keyboard color"),
    "brightness": ("BRILLO", "BRIGHTNESS"),
    "speed": ("VELOCIDAD", "SPEED"),
    "name": ("Nombre", "Name"),
    "animation": ("Animación", "Animation"),
    "name_hint": ("Por ejemplo: Ola, Fijo...", "For example: Wave, Static..."),
    "open_panel": ("Abrir panel", "Open panel"),
    "refresh": ("Actualizar ahora", "Refresh now"),
    "settings": ("Ajustes", "Settings"),
    "autostart": ("Iniciar con Windows", "Start with Windows"),
    "quit": ("Salir", "Quit"),
    "lights": ("Luces", "Lights"),
    "tray_profiles": ("Perfiles", "Profiles"),
    "no_profiles": ("(ninguno guardado)", "(none saved)"),
    "language": ("Idioma", "Language"),
    "poll": ("Consultar cada", "Check every"),
    "low": ("Aviso de batería baja", "Low battery warning"),
    "critical": ("Aviso crítico (0 = desactivado)", "Critical warning (0 = off)"),
    "full_notify": ("Avisar cuando termine de cargar", "Notify when charging is done"),
    "full_level": ("Considerar cargado al", "Treat as charged at"),
    "msg_no_reply": ("El teclado no respondió", "The keyboard did not respond"),
    "msg_rejected": ("El teclado rechazó el cambio", "The keyboard rejected the change"),
    "msg_low": ("Batería baja: {}%", "Low battery: {}%"),
    "msg_critical": ("Batería crítica: {}%. Conecta el cable.",
                     "Critical battery: {}%. Plug in the cable."),
    "msg_full": ("Carga completa ({}%). Ya puedes desconectar el cable.",
                 "Fully charged ({}%). You can unplug the cable."),
    "kb_title": ("Teclado KOORUI", "KOORUI keyboard"),
    "bt_only": ("no disponible por Bluetooth", "not available over Bluetooth"),
    "msg_bt_lights": ("Por Bluetooth solo se puede ver la bateria; las luces se cambian "
                      "en 2.4G o por cable.",
                      "Over Bluetooth only the battery is available; change the lights "
                      "in 2.4G or wired mode."),
    "lock_off": ("Apagar luces al bloquear el PC", "Turn lights off when the PC is locked"),
    "sched_on": ("Apagar luces por horario", "Turn lights off on a schedule"),
    "sched_range": ("Horario de apagado", "Off hours"),
}


def lang_index():
    return 1 if CFG["lang"] == "en" else 0


def tr(key, *args):
    text = STR[key][lang_index()]
    return text.format(*args) if args else text


# Teclado 75%: anchos de tecla por fila, en unidades (cada fila suma 16).
LAYOUT = (
    [1] * 16,
    [1] * 13 + [2, 1],
    [1.5] + [1] * 12 + [1.5, 1],
    [1.75] + [1] * 11 + [2.25, 1],
    [2.25] + [1] * 10 + [1.75, 1, 1],
    [1.25, 1.25, 1.25, 6.25, 1, 1, 1, 1, 1, 1],
)


# --- animaciones de vista previa ------------------------------------------------
def _hue(h):
    return colorsys.hsv_to_rgb(h % 1.0, 1.0, 1.0)


def _scale(rgb, v):
    v = max(0.0, min(1.0, v))
    return tuple(c * v for c in rgb)


def _phase(i):
    return ((i * 2654435761) % 1000) / 1000.0


def _fx_fijo(x, y, i, t, base):
    return base


def _fx_respiracion(x, y, i, t, base):
    return _scale(base, 0.5 + 0.5 * math.sin(t * 2.2))


def _fx_ola(x, y, i, t, base):
    return _hue(x - t * 0.4)


def _fx_ola_vertical(x, y, i, t, base):
    return _hue(y * 0.6 - t * 0.4)


def _fx_ciclo(x, y, i, t, base):
    return _hue(t * 0.15)


def _fx_rotacion(x, y, i, t, base):
    return _hue(math.atan2(y - 0.5, (x - 0.5) * 2.5) / math.tau + t * 0.3)


def _fx_ondas(x, y, i, t, base):
    return _hue(math.hypot((x - 0.5) * 2.5, y - 0.5) * 0.6 - t * 0.4)


def _fx_reactivo(x, y, i, t, base):
    return _scale(base, 1 - ((t * 0.8 + _phase(i) * 3) % 3) * 2)


def _fx_estrellas(x, y, i, t, base):
    return _scale(base, max(0.0, math.sin(t * 2 + _phase(i) * math.tau)) ** 4)


def _fx_serpiente(x, y, i, t, base):
    pos = (t * 0.5 + y * 0.35) % 1.3 - 0.15
    return _scale(base, 1 - abs(x - pos) * 6)


def _fx_lluvia(x, y, i, t, base):
    pos = (t * 0.8 + _phase(int(x * 16)) * 1.4) % 1.4 - 0.2
    return _scale(base, 1 - abs(y - pos) * 3)


def _fx_apagado(x, y, i, t, base):
    return (0.0, 0.0, 0.0)


# clave -> (nombre es, nombre en, funcion)
EFFECTS = {
    "": ("Sin definir", "Not set", None),
    "fijo": ("Color fijo", "Static color", _fx_fijo),
    "respiracion": ("Respiración", "Breathing", _fx_respiracion),
    "ola": ("Ola arcoiris horizontal", "Horizontal rainbow wave", _fx_ola),
    "ola_vertical": ("Ola arcoiris vertical", "Vertical rainbow wave", _fx_ola_vertical),
    "ciclo": ("Ciclo de colores (todo a la vez)", "Color cycle (all keys)", _fx_ciclo),
    "rotacion": ("Colores girando", "Spinning colors", _fx_rotacion),
    "ondas": ("Ondas desde el centro", "Ripples from the center", _fx_ondas),
    "reactivo": ("Reactivo al pulsar", "Reactive to key presses", _fx_reactivo),
    "estrellas": ("Estrellas / destellos", "Stars / sparkles", _fx_estrellas),
    "serpiente": ("Barrido / serpiente", "Sweep / snake", _fx_serpiente),
    "lluvia": ("Lluvia", "Rain", _fx_lluvia),
    "apagado": ("Apagado", "Off", _fx_apagado),
}

# Modos de fabrica segun la tabla habitual de los teclados EVision, validados en
# este teclado. Se pueden renombrar con clic derecho en cada modo.
# numero -> (nombre es, nombre en, animacion)
DEFAULT_MODES = {
    1: ("Ola corta", "Short wave", "ola"), 2: ("Ola larga", "Long wave", "ola"),
    3: ("Rueda", "Wheel", "rotacion"), 4: ("Ciclo", "Cycle", "ciclo"),
    5: ("Respirar", "Breathe", "respiracion"), 6: ("Fijo", "Static", "fijo"),
    7: ("Reactivo", "Reactive", "reactivo"), 8: ("Onda", "Ripple", "ondas"),
    9: ("Línea", "Line", "serpiente"), 10: ("Destellos", "Sparkle", "estrellas"),
    11: ("Flores", "Bloom", "ondas"), 12: ("Ola vert.", "Vert. wave", "ola_vertical"),
    13: ("Huracán", "Hurricane", "rotacion"), 14: ("Acumular", "Accumulate", "serpiente"),
    15: ("Estrellas", "Stars", "estrellas"), 16: ("Visor", "Visor", "serpiente"),
    17: ("Lluvia", "Rain", "lluvia"), 18: ("Ola circ.", "Circle wave", "ondas"),
}


def effect_color(effect, x, y, i, t, rgb, multi, level=1.0):
    """Color (QColor) de una tecla en la vista previa."""
    fx = EFFECTS.get(effect, EFFECTS[""])[2]
    if fx is None:
        return QColor(48, 52, 62)
    base = _hue(x * 0.8) if multi else tuple(c / 255 for c in rgb)
    r, g, b = _scale(fx(x, y, i, t, base), level)
    # Mezcla con el gris de la tecla apagada para que nunca desaparezca.
    return QColor(int(34 + r * 221), int(37 + g * 218), int(44 + b * 211))


# --- acceso al teclado ----------------------------------------------------------
class DEVPROPKEY(ctypes.Structure):
    _fields_ = [("fmtid", ctypes.c_ubyte * 16), ("pid", ctypes.c_ulong)]


# Propiedad donde Windows guarda el porcentaje del servicio de bateria BLE.
BT_BATTERY_KEY = DEVPROPKEY(
    (ctypes.c_ubyte * 16)(*uuid.UUID("104EA319-6EE2-4701-BD47-8DDBF425BBE5").bytes_le), 2)


def bluetooth_battery(hid_path):
    """Porcentaje de bateria que Windows conoce del teclado por Bluetooth, o None."""
    mac = re.search(rb"_([0-9a-f]{12})[&#]", hid_path.lower())
    if not mac:
        return None
    wanted = "DEV_" + mac.group(1).decode().upper()
    cfg = ctypes.windll.cfgmgr32
    size = ctypes.c_ulong()
    if cfg.CM_Get_Device_ID_List_SizeW(ctypes.byref(size), "BTHLE", 1):
        return None
    ids = ctypes.create_unicode_buffer(size.value)
    if cfg.CM_Get_Device_ID_ListW("BTHLE", ids, size, 1):
        return None
    for dev_id in ids[:size.value].split("\0"):
        if wanted not in dev_id.upper():
            continue
        node = ctypes.c_ulong()
        if cfg.CM_Locate_DevNodeW(ctypes.byref(node), dev_id, 1):
            continue
        kind, data, length = ctypes.c_ulong(), (ctypes.c_ubyte * 8)(), ctypes.c_ulong(8)
        if (cfg.CM_Get_DevNode_PropertyW(node, ctypes.byref(BT_BATTERY_KEY), ctypes.byref(kind),
                                         data, ctypes.byref(length), 0) == 0
                and 1 <= data[0] <= 100):
            return data[0]
    return None


def find_link():
    """(enlace, ruta HID) segun como este conectado el teclado; (None, None) si no esta."""
    def vendor_path(pid):
        return next((d["path"] for d in hid.enumerate(VID, pid)
                     if (d["usage_page"], d["usage"]) == (USAGE_PAGE, USAGE)), None)

    # El teclado solo aparece por cable o por Bluetooth cuando esta en ese modo; el
    # receptor 2.4G sigue enchufado siempre, asi que se mira el ultimo.
    path = vendor_path(PID_WIRED)
    if path:
        return LINK_USB, path
    bluetooth = hid.enumerate(BT_VID, BT_PID)
    if bluetooth:
        return LINK_BT, bluetooth[0]["path"]
    path = vendor_path(PID_DONGLE)
    return (LINK_24G, path) if path else (None, None)


class Keyboard:
    """Acceso al teclado; un solo hilo a la vez."""

    def __init__(self):
        self.lock = threading.Lock()
        self.dev = None
        self.link = None        # LINK_USB, LINK_24G, LINK_BT o None

    def _open(self):
        self.link, path = find_link()
        if path is None:
            raise OSError("teclado no conectado")
        if self.link == LINK_BT:
            raise OSError("por Bluetooth no hay canal de configuracion")
        self.dev = hid.device()
        self.dev.open_path(path)
        self.dev.set_nonblocking(1)

    def _close(self):
        if self.dev:
            self.dev.close()
            self.dev = None

    def _tx(self, cmd, length=0, addr=0, data=b""):
        p = bytearray(64)
        p[0], p[3], p[4], p[5], p[6] = REPORT_ID, cmd, length, addr & 0xFF, addr >> 8
        p[8:8 + len(data)] = data
        s = sum(p[3:])
        p[1], p[2] = s & 0xFF, s >> 8
        self.dev.write(bytes(p))
        deadline = time.time() + 0.4
        while time.time() < deadline:
            r = self.dev.read(64)
            if r and len(r) >= 9 and r[0] == REPORT_ID and r[3] == cmd:
                return bytes(r)
            time.sleep(0.003)
        return None

    def _read_page(self):
        self._tx(CMD_BEGIN)
        page = bytearray()
        for off, ln in PAGE_CHUNKS:
            r = self._tx(CMD_READ, ln, off)
            if r is None or r[7] != 0:
                page = None
                break
            page += r[8:8 + ln]
        self._tx(CMD_END)
        return page

    def battery(self):
        """(porcentaje, cargando) o None si no hay datos.

        El porcentaje es la mediana de varias lecturas (el valor baila 1-3 puntos).
        """
        with self.lock:
            try:
                link, path = find_link()
                if link == LINK_BT:
                    self.link = link
                    percent = bluetooth_battery(path)
                    return None if percent is None else (percent, False)
                self._open()
                samples, charging = [], False
                for _ in range(5):
                    r = self._tx(CMD_BATTERY)
                    if r and r[7] == 0 and 1 <= r[8] <= 100:
                        samples.append(r[8])
                        charging = r[9] == 1
                    time.sleep(0.05)
                if not samples:
                    return None
                return round(statistics.median(samples)), charging
            except OSError:
                return None
            finally:
                self._close()

    def read_page(self):
        with self.lock:
            try:
                self._open()
                return self._read_page()
            except OSError:
                return None
            finally:
                self._close()

    def update_page(self, changes):
        """Aplica {offset: valor} sobre la pagina 0. Devuelve la pagina releida o None."""
        with self.lock:
            try:
                self._open()
                page = self._read_page()
                if page is None:
                    return None
                for off, value in changes.items():
                    page[off] = value
                self._tx(CMD_BEGIN)
                for off, ln in PAGE_CHUNKS:
                    self._tx(CMD_WRITE, ln, off, page[off:off + ln])
                    time.sleep(0.05)
                self._tx(CMD_END)
                time.sleep(0.4)
                return self._read_page()
            except OSError:
                return None
            finally:
                self._close()


# --- utilidades -----------------------------------------------------------------
def load_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = None
    if default is None:
        default = {}
    return data if isinstance(data, type(default)) else default


def save_json(path, data):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)


def log_error():
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(time.strftime("%Y-%m-%d %H:%M:%S\n") + traceback.format_exc() + "\n")


def ago(ts):
    mins = int((time.time() - ts) / 60)
    if mins < 1:
        return tr("now")
    if mins < 60:
        return tr("ago_min", mins)
    if mins < 60 * 24:
        return tr("ago_h", mins // 60)
    return tr("ago_d", mins // (60 * 24))


def human(seconds):
    """Duracion aproximada: '3 d', '5 h' o '20 min'."""
    if seconds >= 2 * DAY:
        return f"{round(seconds / DAY)} d"
    if seconds >= HOUR:
        return f"{round(seconds / HOUR)} h"
    return f"{max(1, round(seconds / 60))} min"


def in_schedule(now=None):
    """True dentro del horario de apagado de luces (puede cruzar la medianoche)."""
    if not CFG["sched_on"]:
        return False
    clock = time.strftime("%H:%M", time.localtime(now))
    start, end = CFG["sched_from"], CFG["sched_to"]
    if start <= end:
        return start <= clock < end
    return clock >= start or clock < end


def battery_color(percent, stale=False, charging=False):
    if percent is None:
        return QColor(110, 110, 110)
    if charging:
        c = (40, 120, 215)
    elif percent <= CFG["low"]:
        c = (200, 40, 40)
    elif percent <= 50:
        c = (215, 140, 0)
    else:
        c = (30, 150, 70)
    if stale:
        c = tuple((v + 110) // 2 for v in c)
    return QColor(*c)


def make_icon(percent, stale, charging=False):
    pm = QPixmap(64, 64)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(battery_color(percent, stale, charging))
    p.drawRoundedRect(QRectF(0, 0, 64, 64), 12, 12)
    text = "?" if percent is None else str(percent)
    font = QFont("Arial", 1, QFont.Weight.Bold)
    font.setPixelSize(36 if len(text) >= 3 else 50)
    p.setFont(font)
    p.setPen(QColor("white"))
    p.drawText(QRectF(0, 0, 64, 64), Qt.AlignmentFlag.AlignCenter, text)
    p.end()
    return QIcon(pm)


def ui_font(pixels, weight=QFont.Weight.Normal):
    font = QFont("Segoe UI", 1, weight)
    font.setPixelSize(pixels)
    return font


def autostart_command():
    if getattr(sys, "frozen", False):       # empaquetado como .exe
        return f'"{sys.executable}"'
    return f'"{sys.executable}" "{os.path.abspath(__file__)}"'


def autostart_enabled():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            return winreg.QueryValueEx(k, APP_NAME)[0] == autostart_command()
    except OSError:
        return False


def set_autostart(enabled):
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if enabled:
            winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, autostart_command())
        elif autostart_enabled():
            winreg.DeleteValue(k, APP_NAME)


# --- modelo compartido ----------------------------------------------------------
class Model(QObject):
    """Estado del teclado; sondea en segundo plano y avisa a la interfaz."""

    changed = pyqtSignal()
    message = pyqtSignal(str)
    profiles_changed = pyqtSignal()
    settings_changed = pyqtSignal()

    def __init__(self):
        super().__init__()
        CFG.update({k: v for k, v in load_json(SETTINGS_FILE).items() if k in DEFAULTS})
        self.kb = Keyboard()
        self.state = load_json(STATE_FILE)
        self.modes = load_json(MODES_FILE)
        self.profiles = load_json(PROFILES_FILE, [])
        self.history = load_json(HISTORY_FILE, [])
        self.page = None
        self.started = time.time()
        self.warned = set()
        self.locked = False                 # sesion de Windows bloqueada
        self.auto = load_json(AUTO_FILE)    # apagado automatico: activo y brillo a restaurar
        self.overridden = False             # el usuario encendio las luces durante el apagado
        self.auto_write = 0.0
        self.wake = threading.Event()
        threading.Thread(target=self._loop, daemon=True).start()

    # --- lectura comoda ---------------------------------------------------
    def value(self, off, default=0):
        return self.page[off] if self.page is not None else default

    @property
    def rgb(self):
        return tuple(self.page[OFF_RGB:OFF_RGB + 3]) if self.page is not None else (255, 255, 255)

    @property
    def multi(self):
        return self.value(OFF_MULTI) != 0

    @property
    def percent(self):
        return self.state.get("percent")

    @property
    def link(self):
        return self.kb.link

    @property
    def charging(self):
        return bool(self.state.get("charging")) and not self.stale

    @property
    def stale(self):
        return time.time() - self.state.get("time", 0) > max(90, CFG["poll"] * 3)

    def battery_text(self):
        if self.percent is None:
            return tr("no_data")
        parts = [f"{self.percent}%", ago(self.state["time"])]
        if self.charging:
            parts.append(tr("charging"))
        else:
            left = self.estimate()
            if left:
                parts.append(tr("left", left))
        return " · ".join(parts)

    # --- modos ------------------------------------------------------------
    def mode_info(self, mode):
        info = self.modes.get(str(mode))
        if info is None:
            es, en, effect = DEFAULT_MODES.get(mode, ("", "", ""))
            return (es, en)[lang_index()], effect
        return info.get("name", ""), info.get("effect", "")

    def mode_label(self, mode):
        return self.mode_info(mode)[0] or tr("mode", mode)

    def set_mode_info(self, mode, name, effect):
        self.modes[str(mode)] = {"name": name, "effect": effect}
        save_json(MODES_FILE, self.modes)
        self.changed.emit()

    # --- perfiles de luces ------------------------------------------------
    def current_lights(self):
        if self.page is None:
            return None
        return {"mode": self.page[OFF_MODE], "bright": self.page[OFF_BRIGHT],
                "speed": self.page[OFF_SPEED], "multi": self.multi, "rgb": list(self.rgb)}

    def profile_active(self, profile):
        lights = self.current_lights()
        return lights is not None and all(profile.get(k) == v for k, v in lights.items())

    def _store_profiles(self):
        save_json(PROFILES_FILE, self.profiles)
        self.profiles_changed.emit()

    def add_profile(self, name):
        lights = self.current_lights()
        if lights is not None:
            self.profiles.append({"name": name, **lights})
            self._store_profiles()

    def rename_profile(self, index, name):
        self.profiles[index]["name"] = name
        self._store_profiles()

    def overwrite_profile(self, index):
        lights = self.current_lights()
        if lights is not None:
            self.profiles[index].update(lights)
            self._store_profiles()

    def delete_profile(self, index):
        del self.profiles[index]
        self._store_profiles()

    def apply_profile(self, index):
        p = self.profiles[index]
        r, g, b = p["rgb"]
        self.apply({OFF_MODE: p["mode"], OFF_BRIGHT: p["bright"], OFF_SPEED: p["speed"],
                    OFF_MULTI: 0xFF if p["multi"] else 0,
                    OFF_RGB: r, OFF_RGB + 1: g, OFF_RGB + 2: b})

    # --- ajustes ----------------------------------------------------------
    def save_settings(self, values):
        CFG.update(values)
        save_json(SETTINGS_FILE, CFG)
        self.settings_changed.emit()
        self.changed.emit()
        self.wake.set()

    # --- historial --------------------------------------------------------
    def _record(self, pct, charging, now):
        last = self.history[-1] if self.history else None
        if (last and last[1] == pct and bool(last[2]) == charging
                and now - last[0] < HISTORY_EVERY):
            return
        self.history.append([round(now), pct, int(charging)])
        self.history = [h for h in self.history if h[0] >= now - HISTORY_KEEP]
        save_json(HISTORY_FILE, self.history)

    def history_window(self):
        """Lecturas a dibujar y ancho de la ventana en segundos."""
        now = time.time()
        history = list(self.history)
        if not history:
            return [], 6 * HOUR
        window = min(7 * DAY, max(6 * HOUR, now - history[0][0]))
        return [h for h in history if h[0] >= now - window], window

    def estimate(self):
        """Tiempo restante aproximado al ritmo del tramo de descarga actual, o ''."""
        if self.percent is None or self.charging:
            return ""
        run = []                            # del mas reciente hacia atras
        for ts, pct, chg in reversed(list(self.history)):
            if chg or (run and pct < run[-1][1] - 3):   # carga, o subio sin que lo vieramos
                break
            run.append((ts, pct))
        if len(run) < 2:
            return ""
        span = run[0][0] - run[-1][0]
        drop = run[-1][1] - run[0][1]
        if span < 30 * 60 or drop < 3:
            return ""
        return "~" + human(self.percent / (drop / span))

    # --- escritura y sondeo -----------------------------------------------
    def refresh(self):
        self.wake.set()

    def apply(self, changes, quiet=False):
        """Escribe cambios en el teclado. quiet: sin vista previa inmediata ni avisos."""
        if self.link == LINK_BT:
            if not quiet:
                self.message.emit(tr("msg_bt_lights"))
            return
        if self.page is not None and not quiet:     # respuesta inmediata en pantalla
            for off, value in changes.items():
                self.page[off] = value
            self.changed.emit()

        def work():
            page = self.kb.update_page(changes)
            if page is None:
                if not quiet:
                    self.message.emit(tr("msg_no_reply"))
            else:
                self.page = page
                if not quiet and any(page[off] != value for off, value in changes.items()):
                    self.message.emit(tr("msg_rejected"))
            self.changed.emit()
        threading.Thread(target=work, daemon=True).start()

    # --- apagado automatico de luces --------------------------------------
    def _set_auto(self, active, saved=None):
        self.auto = {"active": active, "saved": self.auto.get("saved", MAX_LEVEL)
                     if saved is None else saved}
        save_json(AUTO_FILE, self.auto)

    def _auto_write(self, bright):
        self.auto_write = time.time()
        self.apply({OFF_BRIGHT: bright}, quiet=True)

    def auto_tick(self):
        """Pone el brillo a 0 al bloquear el PC o en horario y lo restaura despues."""
        if self.page is None:
            return
        want_off = (CFG["lock_off"] and self.locked) or in_schedule()
        bright = self.page[OFF_BRIGHT]
        settled = time.time() - self.auto_write > AUTO_SETTLE
        active = self.auto.get("active", False)
        if not want_off:
            self.overridden = False
            if active and bright > 0:
                self._set_auto(False)
            elif active and settled:        # reintenta hasta que el teclado responda
                self._auto_write(self.auto.get("saved", MAX_LEVEL))
        elif active:
            if bright > 0 and settled:      # las encendieron a mano: respetarlo
                self.overridden = True
                self._set_auto(False)
        elif bright > 0 and not self.overridden:
            self._set_auto(True, bright)
            self._auto_write(0)

    def _notify(self, pct, charging):
        if charging:
            self.warned -= {"low", "critical"}
            if CFG["full_notify"] and pct >= CFG["full_level"] and "full" not in self.warned:
                self.warned.add("full")
                self.message.emit(tr("msg_full", pct))
            return
        self.warned.discard("full")
        if pct > CFG["low"]:
            self.warned -= {"low", "critical"}
        if CFG["critical"] and pct <= CFG["critical"] and "critical" not in self.warned:
            self.warned |= {"low", "critical"}
            self.message.emit(tr("msg_critical", pct))
        elif pct <= CFG["low"] and "low" not in self.warned:
            self.warned.add("low")
            self.message.emit(tr("msg_low", pct))

    def _poll(self):
        reading = self.kb.battery()
        if reading is not None:
            pct, charging = reading
            now = time.time()
            self.state = {"percent": pct, "time": now, "charging": charging}
            save_json(STATE_FILE, self.state)
            self._record(pct, charging, now)
            self._notify(pct, charging)
        if self.link == LINK_BT:
            self.page = None                # por Bluetooth no se pueden leer las luces
        else:
            page = self.kb.read_page()
            if page is not None:
                self.page = page
        self.changed.emit()

    def _loop(self):
        while True:
            try:
                self._poll()
            except Exception:
                log_error()
            self.wake.wait(CFG["poll"])
            self.wake.clear()


# --- piezas del panel -----------------------------------------------------------
class BatteryView(QWidget):
    def __init__(self, model):
        super().__init__()
        self.model = model
        self.setFixedHeight(54)

    def paintEvent(self, _):
        m = self.model
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        pct = m.percent
        color = battery_color(pct, m.stale, m.charging)
        # pila
        body = QRectF(self.width() - 96, 12, 84, 30)
        p.setPen(QPen(QColor(MUTED), 2))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(body, 7, 7)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(MUTED))
        p.drawRoundedRect(QRectF(body.right() + 2, 21, 5, 12), 2, 2)
        if pct:
            fill = body.adjusted(4, 4, -4, -4)
            fill.setWidth(max(6.0, fill.width() * pct / 100))
            p.setBrush(color)
            p.drawRoundedRect(fill, 4, 4)
        if m.charging:                      # rayo sobre la pila
            cx, cy = body.center().x(), body.center().y()
            bolt = QPainterPath()
            for k, (dx, dy) in enumerate(((2, -11), (-7, 2), (-1, 2), (-3, 11), (7, -3), (1, -3))):
                (bolt.moveTo if k == 0 else bolt.lineTo)(cx + dx, cy + dy)
            bolt.closeSubpath()
            p.setBrush(QColor("white"))
            p.drawPath(bolt)
        # textos
        p.setPen(QColor(TEXT))
        p.setFont(ui_font(17, QFont.Weight.DemiBold))
        p.drawText(QRectF(0, 4, 220, 24), Qt.AlignmentFlag.AlignVCenter,
                   f"KOORUI 75% · {m.link}" if m.link else "KOORUI 75%")
        p.setFont(ui_font(12))
        p.setPen(QColor(MUTED))
        p.drawText(QRectF(0, 28, self.width() - 104, 20), Qt.AlignmentFlag.AlignVCenter,
                   f"{tr('battery')} {m.battery_text()}")


class HistoryView(QWidget):
    """Grafica de la bateria: verde al descargar, azul al cargar."""

    def __init__(self, model):
        super().__init__()
        self.model = model
        self.setFixedHeight(62)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(QPen(QColor(BORDER), 1))
        p.setBrush(QColor("#0f1115"))
        p.drawRoundedRect(QRectF(0.5, 0.5, self.width() - 1, self.height() - 1), 10, 10)
        plot = QRectF(10, 8, self.width() - 20, self.height() - 16)
        p.setPen(QPen(QColor(BORDER), 1, Qt.PenStyle.DotLine))
        for frac in (0.0, 0.5, 1.0):
            y = plot.bottom() - frac * plot.height()
            p.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
        points, window = self.model.history_window()
        if len(points) < 2:
            p.setPen(QColor(MUTED))
            p.setFont(ui_font(11))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, tr("no_history"))
            return
        start = time.time() - window

        def pos(sample):
            return QPointF(plot.left() + (sample[0] - start) / window * plot.width(),
                           plot.bottom() - sample[1] / 100 * plot.height())

        for a, b in zip(points, points[1:]):
            color = QColor(BLUE if b[2] else GREEN)
            if b[0] - a[0] > HISTORY_GAP:
                p.setPen(QPen(color, 1, Qt.PenStyle.DotLine))
            else:
                p.setPen(QPen(color, 2))
            p.drawLine(pos(a), pos(b))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(BLUE if points[-1][2] else GREEN))
        p.drawEllipse(pos(points[-1]), 3, 3)


class KeyboardView(QWidget):
    """Teclado en miniatura con la animacion del modo activo."""

    def __init__(self, model):
        super().__init__()
        self.model = model
        self.setFixedHeight(134)

    def paintEvent(self, _):
        m = self.model
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(QPen(QColor(BORDER), 1))
        p.setBrush(QColor("#0f1115"))
        p.drawRoundedRect(QRectF(0.5, 0.5, self.width() - 1, self.height() - 1), 12, 12)
        mode = m.value(OFF_MODE)
        effect = m.mode_info(mode)[1]
        level = 0.25 + 0.75 * min(m.value(OFF_BRIGHT, MAX_LEVEL), MAX_LEVEL) / MAX_LEVEL
        t = time.time() - m.started
        pad = 10
        unit = (self.width() - 2 * pad) / 16
        rows = len(LAYOUT)
        row_h = (self.height() - 2 * pad) / rows
        p.setPen(Qt.PenStyle.NoPen)
        i = 0
        for r, widths in enumerate(LAYOUT):
            x = 0.0
            for w in widths:
                cx, cy = (x + w / 2) / 16, (r + 0.5) / rows
                p.setBrush(effect_color(effect, cx, cy, i, t, m.rgb, m.multi, level))
                p.drawRoundedRect(QRectF(pad + x * unit + 1.5, pad + r * row_h + 1.5,
                                         w * unit - 3, row_h - 3), 4, 4)
                x += w
                i += 1
        if not effect and m.page is not None:
            p.setPen(QColor(TEXT))
            p.setFont(ui_font(12))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, tr("define_mode"))


class ModeTile(QWidget):
    COLS, ROWS = 8, 3

    def __init__(self, model, mode, on_edit):
        super().__init__()
        self.model, self.mode, self.on_edit = model, mode, on_edit
        self.hover = False
        self.setFixedHeight(60)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def enterEvent(self, _):
        self.hover = True
        self.update()

    def leaveEvent(self, _):
        self.hover = False
        self.update()

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self.model.apply({OFF_MODE: self.mode})
        elif e.button() == Qt.MouseButton.RightButton:
            self.on_edit(self.mode)

    def paintEvent(self, _):
        m = self.model
        selected = m.value(OFF_MODE) == self.mode
        name, effect = m.mode_info(self.mode)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(QPen(QColor(ACCENT if selected else BORDER), 2 if selected else 1))
        p.setBrush(QColor("#272b34" if self.hover else CARD))
        p.drawRoundedRect(QRectF(1, 1, self.width() - 2, self.height() - 2), 9, 9)
        # miniatura
        t = time.time() - m.started
        area = QRectF(7, 7, self.width() - 14, 28)
        cw, ch = area.width() / self.COLS, area.height() / self.ROWS
        p.setPen(Qt.PenStyle.NoPen)
        for r in range(self.ROWS):
            for c in range(self.COLS):
                i = r * self.COLS + c
                p.setBrush(effect_color(effect, (c + 0.5) / self.COLS, (r + 0.5) / self.ROWS,
                                        i, t, m.rgb, m.multi))
                p.drawRoundedRect(QRectF(area.x() + c * cw + 0.8, area.y() + r * ch + 0.8,
                                         cw - 1.6, ch - 1.6), 2, 2)
        # etiqueta
        font = ui_font(11)
        p.setFont(font)
        p.setPen(QColor(TEXT if selected or name else MUTED))
        label = f"{self.mode} · {name}" if name else tr("mode", self.mode)
        label = QFontMetrics(font).elidedText(label, Qt.TextElideMode.ElideRight, self.width() - 10)
        p.drawText(QRectF(0, 37, self.width(), 18), Qt.AlignmentFlag.AlignCenter, label)


def paint_rainbow(p, circle):
    """Disco multicolor recortado al circulo dado."""
    p.save()
    path = QPainterPath()
    path.addEllipse(circle)
    p.setClipPath(path)
    p.setPen(Qt.PenStyle.NoPen)
    for k in range(36):
        p.setBrush(QColor.fromHsvF(k / 36, 1, 1))
        p.drawPie(circle.adjusted(-2, -2, 2, 2), k * 160, 170)
    p.restore()


class Swatch(QWidget):
    def __init__(self, model, names, rgb=None, kind="color"):
        super().__init__()
        self.model, self.names, self.rgb, self.kind = model, names, rgb, kind
        self.setFixedSize(30, 30)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def retranslate(self):
        self.setToolTip(tr(self.names) if isinstance(self.names, str)
                        else self.names[lang_index()])

    def is_selected(self):
        m = self.model
        if m.page is None:
            return False
        if self.kind == "multi":
            return m.multi
        if self.kind == "custom":
            return not m.multi and m.rgb not in [c for *_, c in COLORS]
        return not m.multi and m.rgb == self.rgb

    def mousePressEvent(self, e):
        if e.button() != Qt.MouseButton.LeftButton:
            return
        if self.kind == "multi":
            self.model.apply({OFF_MULTI: 0xFF})
        elif self.kind == "custom":
            self.window().pick_color()
        else:
            self.model.apply({OFF_MULTI: 0, OFF_RGB: self.rgb[0], OFF_RGB + 1: self.rgb[1],
                              OFF_RGB + 2: self.rgb[2]})

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        circle = QRectF(5, 5, 20, 20)
        if self.is_selected():
            p.setPen(QPen(QColor(TEXT), 2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(QRectF(1.5, 1.5, 27, 27))
        p.setPen(Qt.PenStyle.NoPen)
        if self.kind == "multi":
            paint_rainbow(p, circle)
        elif self.kind == "custom":
            p.setBrush(QColor(*self.model.rgb) if self.is_selected() else QColor(CARD))
            p.setPen(QPen(QColor(MUTED), 1))
            p.drawEllipse(circle)
            p.setPen(QPen(QColor(TEXT), 1.6))
            p.drawLine(QPointF(15, 10.5), QPointF(15, 19.5))
            p.drawLine(QPointF(10.5, 15), QPointF(19.5, 15))
        else:
            p.setBrush(QColor(*self.rgb))
            p.drawEllipse(circle)


class ProfileChip(QWidget):
    """Un perfil guardado; con index None es el boton de guardar las luces actuales."""

    def __init__(self, model, panel, index=None):
        super().__init__()
        self.model, self.panel, self.index = model, panel, index
        self.hover = False
        self.setFixedHeight(30)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        if index is not None:
            self.setToolTip(tr("profile_tip"))

    def enterEvent(self, _):
        self.hover = True
        self.update()

    def leaveEvent(self, _):
        self.hover = False
        self.update()

    def mousePressEvent(self, e):
        if self.index is None:
            if e.button() == Qt.MouseButton.LeftButton:
                self.panel.save_profile()
        elif e.button() == Qt.MouseButton.LeftButton:
            self.model.apply_profile(self.index)
        elif e.button() == Qt.MouseButton.RightButton:
            self.panel.profile_menu(self.index, e.globalPosition().toPoint())

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        box = QRectF(1, 1, self.width() - 2, self.height() - 2)
        font = ui_font(11)
        p.setFont(font)
        p.setBrush(QColor("#272b34" if self.hover else CARD))
        if self.index is None:
            p.setPen(QPen(QColor(MUTED), 1, Qt.PenStyle.DashLine))
            p.drawRoundedRect(box, 8, 8)
            p.setPen(QColor(MUTED))
            p.drawText(box, Qt.AlignmentFlag.AlignCenter, tr("save_profile"))
            return
        profile = self.model.profiles[self.index]
        active = self.model.profile_active(profile)
        p.setPen(QPen(QColor(ACCENT if active else BORDER), 2 if active else 1))
        p.drawRoundedRect(box, 8, 8)
        dot = QRectF(9, 9, 12, 12)
        if profile["multi"]:
            paint_rainbow(p, dot)
        else:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(*profile["rgb"]))
            p.drawEllipse(dot)
        p.setPen(QColor(TEXT))
        name = QFontMetrics(font).elidedText(profile["name"], Qt.TextElideMode.ElideRight,
                                             self.width() - 34)
        p.drawText(QRectF(27, 0, self.width() - 32, self.height()),
                   Qt.AlignmentFlag.AlignVCenter, name)


class ModeDialog(QDialog):
    def __init__(self, parent, mode, name, effect):
        super().__init__(parent)
        self.setWindowTitle(tr("mode", mode))
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint)
        self.name = QLineEdit(name)
        self.name.setPlaceholderText(tr("name_hint"))
        self.effect = QComboBox()
        for key, labels in EFFECTS.items():
            self.effect.addItem(labels[lang_index()], key)
        self.effect.setCurrentIndex(max(0, self.effect.findData(effect)))
        form = QFormLayout(self)
        form.addRow(tr("name"), self.name)
        form.addRow(tr("animation"), self.effect)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                   | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)
        self.setMinimumWidth(320)


class SettingsDialog(QDialog):
    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle(tr("settings"))
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint)
        self.lang = QComboBox()
        self.lang.addItem("Español", "es")
        self.lang.addItem("English", "en")
        self.lang.setCurrentIndex(lang_index())
        self.poll = self.spin(10, 600, CFG["poll"], " s")
        self.low = self.spin(5, 50, CFG["low"], " %")
        self.critical = self.spin(0, 50, CFG["critical"], " %")
        self.full_notify = QCheckBox()
        self.full_notify.setChecked(CFG["full_notify"])
        self.full_level = self.spin(80, 100, CFG["full_level"], " %")
        self.lock_off = QCheckBox()
        self.lock_off.setChecked(CFG["lock_off"])
        self.sched_on = QCheckBox()
        self.sched_on.setChecked(CFG["sched_on"])
        self.sched_from = self.clock(CFG["sched_from"])
        self.sched_to = self.clock(CFG["sched_to"])
        hours = QHBoxLayout()
        hours.addWidget(self.sched_from)
        hours.addWidget(QLabel("-"))
        hours.addWidget(self.sched_to)
        hours.addStretch()
        form = QFormLayout(self)
        form.setVerticalSpacing(10)
        form.addRow(tr("language"), self.lang)
        form.addRow(tr("poll"), self.poll)
        form.addRow(tr("low"), self.low)
        form.addRow(tr("critical"), self.critical)
        form.addRow(tr("full_notify"), self.full_notify)
        form.addRow(tr("full_level"), self.full_level)
        form.addRow(tr("lock_off"), self.lock_off)
        form.addRow(tr("sched_on"), self.sched_on)
        form.addRow(tr("sched_range"), hours)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                   | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)
        self.setMinimumWidth(360)

    @staticmethod
    def spin(low, high, value, suffix):
        box = QSpinBox()
        box.setRange(low, high)
        box.setValue(value)
        box.setSuffix(suffix)
        return box

    @staticmethod
    def clock(text):
        edit = QTimeEdit(QTime.fromString(text, "HH:mm"))
        edit.setDisplayFormat("HH:mm")
        return edit

    def values(self):
        low = self.low.value()
        return {"lang": self.lang.currentData(), "poll": self.poll.value(), "low": low,
                "critical": min(self.critical.value(), low),
                "full_notify": self.full_notify.isChecked(),
                "full_level": self.full_level.value(),
                "lock_off": self.lock_off.isChecked(), "sched_on": self.sched_on.isChecked(),
                "sched_from": self.sched_from.time().toString("HH:mm"),
                "sched_to": self.sched_to.time().toString("HH:mm")}


class Panel(QWidget):
    WIDTH = 440
    PROFILE_COLS = 3

    def __init__(self, model):
        super().__init__()
        self.model = model
        self.busy = False           # hay un dialogo abierto: no ocultar al perder el foco
        self.hidden_at = 0.0
        self.labels = {}
        self.setWindowFlags(Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
                            | Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedWidth(self.WIDTH)

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 14, 18, 14)
        root.setSpacing(8)

        header = QHBoxLayout()
        header.addWidget(BatteryView(model))
        self.gear = QPushButton("⚙")
        self.gear.setFixedSize(30, 30)
        self.gear.setCursor(Qt.CursorShape.PointingHandCursor)
        self.gear.setStyleSheet(
            f"QPushButton{{background:transparent;border:none;color:{MUTED};font-size:17px;"
            f"padding:0;}} QPushButton:hover{{color:{TEXT};}}")
        self.gear.clicked.connect(self.open_settings)
        header.addWidget(self.gear)
        root.addLayout(header)

        root.addWidget(self.section("history"))
        self.history = HistoryView(model)
        root.addWidget(self.history)

        self.keyboard = KeyboardView(model)
        root.addWidget(self.keyboard)

        root.addWidget(self.section("profiles"))
        self.profile_grid = QGridLayout()
        self.profile_grid.setSpacing(6)
        root.addLayout(self.profile_grid)

        root.addWidget(self.section("light_mode"))
        grid = QGridLayout()
        grid.setSpacing(6)
        self.tiles = []
        for n in range(MODE_COUNT):
            tile = ModeTile(model, n + 1, self.edit_mode)
            self.tiles.append(tile)
            grid.addWidget(tile, n // 6, n % 6)
        root.addLayout(grid)

        root.addWidget(self.section("color"))
        row = QHBoxLayout()
        row.setSpacing(4)
        self.swatches = [Swatch(model, (es, en), rgb) for es, en, rgb in COLORS]
        self.swatches.append(Swatch(model, "multicolor", kind="multi"))
        self.swatches.append(Swatch(model, "other_color", kind="custom"))
        for s in self.swatches:
            row.addWidget(s)
        row.addStretch()
        root.addLayout(row)

        self.bright = self.slider_row(root, "brightness", OFF_BRIGHT)
        self.speed = self.slider_row(root, "speed", OFF_SPEED)

        self.timer = QTimer(self)
        self.timer.setInterval(40)
        self.timer.timeout.connect(self.animate)
        model.changed.connect(self.sync)
        model.profiles_changed.connect(self.rebuild_profiles)
        model.settings_changed.connect(self.retranslate)
        self.retranslate()

    # --- construccion -----------------------------------------------------
    def section(self, key):
        label = QLabel()
        label.setStyleSheet(f"color:{MUTED}; font:600 10px 'Segoe UI'; letter-spacing:1px;"
                            "margin-top:2px;")
        self.labels[key] = label
        return label

    def slider_row(self, root, key, off):
        row = QHBoxLayout()
        label = self.section(key)
        label.setFixedWidth(92)
        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setRange(0, MAX_LEVEL)
        slider.setPageStep(1)
        slider.setStyleSheet(
            f"QSlider::groove:horizontal{{height:6px;background:{BORDER};border-radius:3px;}}"
            f"QSlider::sub-page:horizontal{{background:{ACCENT};border-radius:3px;}}"
            f"QSlider::handle:horizontal{{width:16px;margin:-6px 0;border-radius:8px;"
            f"background:{TEXT};}}")
        value = QLabel("0")
        value.setFixedWidth(16)
        value.setStyleSheet(f"color:{TEXT}; font:12px 'Segoe UI';")
        slider.valueChanged.connect(lambda v: value.setText(str(v)))
        slider.sliderReleased.connect(lambda: self.model.apply({off: slider.value()}))
        slider.actionTriggered.connect(
            lambda _: None if slider.isSliderDown() else QTimer.singleShot(
                0, lambda: self.model.apply({off: slider.value()})))
        row.addWidget(label)
        row.addWidget(slider)
        row.addWidget(value)
        root.addLayout(row)
        return slider

    def rebuild_profiles(self):
        while self.profile_grid.count():
            self.profile_grid.takeAt(0).widget().deleteLater()
        chips = [ProfileChip(self.model, self, i) for i in range(len(self.model.profiles))]
        chips.append(ProfileChip(self.model, self))
        for n, chip in enumerate(chips):
            self.profile_grid.addWidget(chip, n // self.PROFILE_COLS, n % self.PROFILE_COLS)
        if self.isVisible():
            QTimer.singleShot(0, self.place)

    def retranslate(self):
        for key, label in self.labels.items():
            label.setText(tr(key))
        for tile in self.tiles:
            tile.setToolTip(tr("mode_tip"))
        for swatch in self.swatches:
            swatch.retranslate()
        self.gear.setToolTip(tr("settings"))
        self.rebuild_profiles()
        self.sync()

    # --- estado -----------------------------------------------------------
    def sync(self):
        m = self.model
        for slider, off in ((self.bright, OFF_BRIGHT), (self.speed, OFF_SPEED)):
            if not slider.isSliderDown():
                slider.blockSignals(True)
                slider.setValue(min(m.value(off), MAX_LEVEL))
                slider.blockSignals(False)
                slider.valueChanged.emit(slider.value())
        if m.page is not None:
            current = m.mode_label(m.value(OFF_MODE))
        else:
            current = tr("bt_only" if m.link == LINK_BT else "no_data")
        self.labels["light_mode"].setText(f"{tr('light_mode')}  ·  {current}")
        window = m.history_window()[1]
        self.labels["history"].setText(f"{tr('history')}  ·  {tr('last', human(window))}")
        self.update()

    def animate(self):
        self.keyboard.update()
        for tile in self.tiles:
            tile.update()

    # --- dialogos ---------------------------------------------------------
    def modal(self, action):
        """Ejecuta un dialogo sin que el panel se oculte al perder el foco."""
        self.busy = True
        try:
            return action()
        finally:
            self.busy = False
            if self.isVisible():
                self.activateWindow()

    def edit_mode(self, mode):
        name, effect = self.model.mode_info(mode)
        dlg = ModeDialog(self, mode, name, effect)
        if self.modal(dlg.exec):
            self.model.set_mode_info(mode, dlg.name.text().strip(), dlg.effect.currentData())

    def pick_color(self):
        color = self.modal(lambda: QColorDialog.getColor(QColor(*self.model.rgb), self,
                                                         tr("pick_color")))
        if color.isValid():
            self.model.apply({OFF_MULTI: 0, OFF_RGB: color.red(), OFF_RGB + 1: color.green(),
                              OFF_RGB + 2: color.blue()})

    def ask_name(self, title, initial=""):
        name, ok = self.modal(lambda: QInputDialog.getText(self, title, tr("profile_name"),
                                                           text=initial))
        return name.strip() if ok else ""

    def save_profile(self):
        name = self.ask_name(tr("new_profile"))
        if name:
            self.model.add_profile(name)

    def profile_menu(self, index, pos):
        menu = QMenu(self)
        rename = menu.addAction(tr("rename"))
        overwrite = menu.addAction(tr("overwrite"))
        delete = menu.addAction(tr("delete"))
        chosen = self.modal(lambda: menu.exec(pos))
        if chosen == rename:
            name = self.ask_name(tr("rename"), self.model.profiles[index]["name"])
            if name:
                self.model.rename_profile(index, name)
        elif chosen == overwrite:
            self.model.overwrite_profile(index)
        elif chosen == delete:
            self.model.delete_profile(index)

    def open_settings(self):
        dlg = SettingsDialog(self)
        if self.modal(dlg.exec):
            self.model.save_settings(dlg.values())

    # --- ventana ----------------------------------------------------------
    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(QPen(QColor(BORDER), 1))
        p.setBrush(QColor(BG))
        p.drawRoundedRect(QRectF(0.5, 0.5, self.width() - 1, self.height() - 1), 16, 16)

    def place(self):
        screen = QApplication.screenAt(QCursor.pos()) or QApplication.primaryScreen()
        area = screen.availableGeometry()
        self.keyboard.setVisible(True)
        self.adjustSize()
        if self.sizeHint().height() > area.height() - 24:   # pantalla baja: sin miniatura
            self.keyboard.setVisible(False)
            self.adjustSize()
        self.move(area.right() - self.width() - 12, area.bottom() - self.height() - 12)

    def popup(self):
        self.place()
        self.show()
        self.raise_()
        self.activateWindow()
        self.timer.start()
        self.model.refresh()

    def hideEvent(self, _):
        self.timer.stop()
        self.hidden_at = time.time()

    def changeEvent(self, e):
        if (e.type() == e.Type.ActivationChange and not self.isActiveWindow()
                and not self.busy and self.isVisible()):
            self.hide()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key.Key_Escape:
            self.hide()


class Tray(QSystemTrayIcon):
    def __init__(self, model, panel):
        super().__init__(make_icon(model.percent, True))
        self.model, self.panel = model, panel
        self.menu = QMenu()
        self.setContextMenu(self.menu)
        self.activated.connect(self.on_click)
        model.changed.connect(self.sync)
        model.settings_changed.connect(self.build_menu)
        model.message.connect(lambda text: self.showMessage(tr("kb_title"), text))
        self.build_menu()
        self.sync()

    def build_menu(self):
        menu = self.menu
        menu.clear()
        menu.addAction(tr("open_panel"), self.panel.popup)
        self.profiles = menu.addMenu(tr("tray_profiles"))
        self.profiles.aboutToShow.connect(self.fill_profiles)
        menu.addAction(tr("refresh"), self.model.refresh)
        menu.addAction(tr("settings") + "...", self.panel.open_settings)
        auto = QAction(tr("autostart"), menu, checkable=True)
        auto.setChecked(autostart_enabled())
        auto.toggled.connect(set_autostart)
        menu.addAction(auto)
        menu.addSeparator()
        menu.addAction(tr("quit"), QApplication.quit)

    def fill_profiles(self):
        self.profiles.clear()
        if not self.model.profiles:
            self.profiles.addAction(tr("no_profiles")).setEnabled(False)
        for index, profile in enumerate(self.model.profiles):
            action = self.profiles.addAction(profile["name"])
            action.setCheckable(True)
            action.setChecked(self.model.profile_active(profile))
            action.triggered.connect(lambda _, i=index: self.model.apply_profile(i))

    def on_click(self, reason):
        if reason != QSystemTrayIcon.ActivationReason.Trigger:
            return
        # El clic en el icono ya oculto el panel al quitarle el foco: no reabrirlo.
        if self.panel.isVisible() or time.time() - self.panel.hidden_at < 0.3:
            self.panel.hide()
        else:
            self.panel.popup()

    def sync(self):
        m = self.model
        self.setIcon(make_icon(m.percent, m.stale, m.charging))
        lights = m.mode_label(m.value(OFF_MODE)) if m.page is not None else tr("no_data")
        self.setToolTip(f"KOORUI · {tr('battery')} {m.battery_text()}\n{tr('lights')}: {lights}")


class LockWatcher(QAbstractNativeEventFilter):
    """Avisa al modelo cuando la sesion de Windows se bloquea o desbloquea."""

    WM_WTSSESSION_CHANGE, WTS_SESSION_LOCK, WTS_SESSION_UNLOCK = 0x02B1, 7, 8

    def __init__(self, model, hwnd):
        super().__init__()
        self.model = model
        ctypes.windll.wtsapi32.WTSRegisterSessionNotification(wintypes.HWND(hwnd), 0)

    def nativeEventFilter(self, event_type, message):
        if bytes(event_type) == b"windows_generic_MSG":
            msg = wintypes.MSG.from_address(int(message))
            if msg.message == self.WM_WTSSESSION_CHANGE:
                if msg.wParam == self.WTS_SESSION_LOCK:
                    self.model.locked = True
                elif msg.wParam == self.WTS_SESSION_UNLOCK:
                    self.model.locked = False
        return False, 0


def main():
    # Una sola instancia.
    mutex = ctypes.windll.kernel32.CreateMutexW(None, False, APP_NAME + "Mutex")
    if ctypes.windll.kernel32.GetLastError() == 183:
        return
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    field = (f"background:{CARD};color:{TEXT};border:1px solid {BORDER};"
             "border-radius:6px;padding:5px;")
    app.setStyleSheet(
        f"QDialog{{background:{BG};}} QLabel,QCheckBox{{color:{TEXT};}}"
        f"QLineEdit,QComboBox,QSpinBox,QTimeEdit{{{field}}}"
        f"QComboBox QAbstractItemView{{background:{CARD};color:{TEXT};"
        f"selection-background-color:{ACCENT};}}"
        f"QPushButton{{background:{CARD};color:{TEXT};border:1px solid {BORDER};"
        "border-radius:6px;padding:5px 14px;}"
        f"QMenu{{background:{CARD};color:{TEXT};border:1px solid {BORDER};padding:4px;}}"
        f"QMenu::item{{padding:5px 22px;}} QMenu::item:selected{{background:{ACCENT};}}"
        f"QMenu::item:disabled{{color:{MUTED};}}"
        f"QToolTip{{background:{CARD};color:{TEXT};border:1px solid {BORDER};}}")
    model = Model()
    panel = Panel(model)
    tray = Tray(model, panel)
    tray.show()
    watcher = LockWatcher(model, int(panel.winId()))
    app.installNativeEventFilter(watcher)
    auto = QTimer()
    auto.timeout.connect(model.auto_tick)
    auto.start(2000)
    if "--shot" in sys.argv:        # captura del panel para revisar el diseno
        path = sys.argv[sys.argv.index("--shot") + 1]
        panel.busy = True
        panel.popup()
        QTimer.singleShot(2500, lambda: (panel.grab().save(path), app.quit()))
    code = app.exec()
    ctypes.windll.kernel32.CloseHandle(mutex)
    sys.exit(code)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        log_error()
        raise
