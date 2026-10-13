"""Bateria y luces del teclado KOORUI 75% (modo 2.4G) en la bandeja del sistema.

El receptor (320F:50C3, Telink/EVision) responde por su coleccion HID
propietaria (usage page 0xFF1C, report ID 4) con paquetes de 64 bytes:

    [04][suma_lo][suma_hi][cmd][len][dir_lo][dir_hi][estado][datos...]

donde suma = suma de los bytes 3..63. Comandos usados:

    0x01 / 0x02  abrir / cerrar una transaccion
    0x1A         bateria: porcentaje en el byte 8
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
import statistics
import sys
import threading
import time
import traceback
import winreg

import hid
from PyQt6.QtCore import QObject, QPointF, QRectF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import (QAction, QColor, QCursor, QFont, QFontMetrics, QIcon, QPainter,
                         QPainterPath, QPen, QPixmap)
from PyQt6.QtWidgets import (QApplication, QColorDialog, QComboBox, QDialog,
                             QDialogButtonBox, QFormLayout, QGridLayout, QHBoxLayout, QLabel,
                             QLineEdit, QMenu, QPushButton, QSlider, QSystemTrayIcon,
                             QVBoxLayout, QWidget)

VID, PID = 0x320F, 0x50C3
USAGE_PAGE, USAGE = 0xFF1C, 0x92
REPORT_ID = 0x04
CMD_BEGIN, CMD_END, CMD_READ, CMD_WRITE, CMD_BATTERY = 0x01, 0x02, 0x05, 0x06, 0x1A
PAGE_CHUNKS = ((0x00, 0x18), (0x18, 0x18), (0x30, 0x10))
OFF_MODE, OFF_BRIGHT, OFF_SPEED, OFF_MULTI, OFF_RGB = 1, 2, 3, 5, 6

MODE_COUNT = 18
MAX_LEVEL = 4
COLORS = (("Naranja", (0xFF, 0x60, 0x00)), ("Rojo", (0xFF, 0x00, 0x00)),
          ("Amarillo", (0xFF, 0xFF, 0x00)), ("Verde", (0x00, 0xFF, 0x00)),
          ("Cian", (0x00, 0xFF, 0xFF)), ("Azul", (0x00, 0x00, 0xFF)),
          ("Morado", (0x80, 0x00, 0xFF)), ("Rosa", (0xFF, 0x00, 0x80)),
          ("Blanco", (0xFF, 0xFF, 0xFF)))

POLL_SECONDS = 30
LOW_BATTERY = 20
APP_NAME = "KooruiBattery"
DATA_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), APP_NAME)
STATE_FILE = os.path.join(DATA_DIR, "state.json")
MODES_FILE = os.path.join(DATA_DIR, "modes.json")
LOG_FILE = os.path.join(DATA_DIR, "error.log")
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

BG, CARD, BORDER, TEXT, MUTED, ACCENT = "#15171c", "#1e2128", "#2c303a", "#e8eaf0", "#8a90a0", "#4da3ff"

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


EFFECTS = {
    "": ("Sin definir", None),
    "fijo": ("Color fijo", _fx_fijo),
    "respiracion": ("Respiracion", _fx_respiracion),
    "ola": ("Ola arcoiris horizontal", _fx_ola),
    "ola_vertical": ("Ola arcoiris vertical", _fx_ola_vertical),
    "ciclo": ("Ciclo de colores (todo a la vez)", _fx_ciclo),
    "rotacion": ("Colores girando", _fx_rotacion),
    "ondas": ("Ondas desde el centro", _fx_ondas),
    "reactivo": ("Reactivo al pulsar", _fx_reactivo),
    "estrellas": ("Estrellas / destellos", _fx_estrellas),
    "serpiente": ("Barrido / serpiente", _fx_serpiente),
    "lluvia": ("Lluvia", _fx_lluvia),
    "apagado": ("Apagado", _fx_apagado),
}


# Modos de fabrica segun la tabla habitual de los teclados EVision (el 6 = fijo esta
# confirmado en este teclado). Se pueden corregir con clic derecho en cada modo.
DEFAULT_MODES = {
    1: ("Ola corta", "ola"), 2: ("Ola larga", "ola"), 3: ("Rueda", "rotacion"),
    4: ("Ciclo", "ciclo"), 5: ("Respirar", "respiracion"), 6: ("Fijo", "fijo"),
    7: ("Reactivo", "reactivo"), 8: ("Onda", "ondas"), 9: ("Linea", "serpiente"),
    10: ("Destellos", "estrellas"), 11: ("Flores", "ondas"), 12: ("Ola vert.", "ola_vertical"),
    13: ("Huracan", "rotacion"), 14: ("Acumular", "serpiente"), 15: ("Estrellas", "estrellas"),
    16: ("Visor", "serpiente"), 17: ("Lluvia", "lluvia"), 18: ("Ola circ.", "ondas"),
}


def effect_color(effect, x, y, i, t, rgb, multi, level=1.0):
    """Color (QColor) de una tecla en la vista previa."""
    fx = EFFECTS.get(effect, EFFECTS[""])[1]
    if fx is None:
        return QColor(48, 52, 62)
    base = _hue(x * 0.8) if multi else tuple(c / 255 for c in rgb)
    r, g, b = _scale(fx(x, y, i, t, base), level)
    # Mezcla con el gris de la tecla apagada para que nunca desaparezca.
    return QColor(int(34 + r * 221), int(37 + g * 218), int(44 + b * 211))


# --- acceso al teclado ----------------------------------------------------------
class Keyboard:
    """Acceso al receptor; un solo hilo a la vez."""

    def __init__(self):
        self.lock = threading.Lock()
        self.dev = None

    def _open(self):
        path = next((d["path"] for d in hid.enumerate(VID, PID)
                     if (d["usage_page"], d["usage"]) == (USAGE_PAGE, USAGE)), None)
        if path is None:
            raise OSError("receptor no conectado")
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
        """Mediana de varias lecturas (el valor baila 1-3 puntos). None si no hay datos."""
        with self.lock:
            try:
                self._open()
                samples = []
                for _ in range(5):
                    r = self._tx(CMD_BATTERY)
                    if r and r[7] == 0 and 1 <= r[8] <= 100:
                        samples.append(r[8])
                    time.sleep(0.05)
                return round(statistics.median(samples)) if samples else None
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
def load_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


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
        return "ahora"
    if mins < 60:
        return f"hace {mins} min"
    if mins < 60 * 24:
        return f"hace {mins // 60} h"
    return f"hace {mins // (60 * 24)} d"


def battery_color(percent, stale=False):
    if percent is None:
        return QColor(110, 110, 110)
    if percent <= LOW_BATTERY:
        c = (200, 40, 40)
    elif percent <= 50:
        c = (215, 140, 0)
    else:
        c = (30, 150, 70)
    if stale:
        c = tuple((v + 110) // 2 for v in c)
    return QColor(*c)


def make_icon(percent, stale):
    pm = QPixmap(64, 64)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(battery_color(percent, stale))
    p.drawRoundedRect(QRectF(0, 0, 64, 64), 12, 12)
    text = "?" if percent is None else str(percent)
    font = QFont("Arial", 1, QFont.Weight.Bold)
    font.setPixelSize(36 if len(text) >= 3 else 50)
    p.setFont(font)
    p.setPen(QColor("white"))
    p.drawText(QRectF(0, 0, 64, 64), Qt.AlignmentFlag.AlignCenter, text)
    p.end()
    return QIcon(pm)


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

    def __init__(self):
        super().__init__()
        self.kb = Keyboard()
        self.state = load_json(STATE_FILE)
        self.modes = load_json(MODES_FILE)
        self.page = None
        self.started = time.time()
        self.warned = False
        self.wake = threading.Event()
        threading.Thread(target=self._loop, daemon=True).start()

    # lectura comoda de la pagina
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
    def stale(self):
        return time.time() - self.state.get("time", 0) > POLL_SECONDS * 3

    def mode_info(self, mode):
        info = self.modes.get(str(mode))
        if info is None:
            return DEFAULT_MODES.get(mode, ("", ""))
        return info.get("name", ""), info.get("effect", "")

    def mode_label(self, mode):
        return self.mode_info(mode)[0] or f"Modo {mode}"

    def set_mode_info(self, mode, name, effect):
        self.modes[str(mode)] = {"name": name, "effect": effect}
        save_json(MODES_FILE, self.modes)
        self.changed.emit()

    def battery_text(self):
        if self.percent is None:
            return "sin datos"
        return f"{self.percent}% · {ago(self.state['time'])}"

    def refresh(self):
        self.wake.set()

    def apply(self, changes):
        if self.page is not None:           # respuesta inmediata en pantalla
            for off, value in changes.items():
                self.page[off] = value
            self.changed.emit()

        def work():
            page = self.kb.update_page(changes)
            if page is None:
                self.message.emit("El teclado no respondio")
            else:
                self.page = page
                if any(page[off] != value for off, value in changes.items()):
                    self.message.emit("El teclado rechazo el cambio")
            self.changed.emit()
        threading.Thread(target=work, daemon=True).start()

    def _poll(self):
        pct = self.kb.battery()
        if pct is not None:
            self.state = {"percent": pct, "time": time.time()}
            save_json(STATE_FILE, self.state)
            if pct > LOW_BATTERY:
                self.warned = False
            elif not self.warned:
                self.warned = True
                self.message.emit(f"Bateria baja: {pct}%")
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
            self.wake.wait(POLL_SECONDS)
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
        color = battery_color(pct, m.stale)
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
        # textos
        p.setPen(QColor(TEXT))
        font = QFont("Segoe UI", 1, QFont.Weight.DemiBold)
        font.setPixelSize(17)
        p.setFont(font)
        p.drawText(QRectF(0, 4, 220, 24), Qt.AlignmentFlag.AlignVCenter, "KOORUI 75% · 2.4G")
        font.setPixelSize(12)
        font.setWeight(QFont.Weight.Normal)
        p.setFont(font)
        p.setPen(QColor(MUTED))
        p.drawText(QRectF(0, 28, 240, 20), Qt.AlignmentFlag.AlignVCenter,
                   "Bateria " + m.battery_text())


class KeyboardView(QWidget):
    """Teclado en miniatura con la animacion del modo activo."""

    def __init__(self, model):
        super().__init__()
        self.model = model
        self.setFixedHeight(158)

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
        pad = 12
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
            font = QFont("Segoe UI")
            font.setPixelSize(12)
            p.setFont(font)
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter,
                       "Clic derecho en un modo para definir su animacion")


class ModeTile(QWidget):
    COLS, ROWS = 8, 3

    def __init__(self, model, mode, on_edit):
        super().__init__()
        self.model, self.mode, self.on_edit = model, mode, on_edit
        self.hover = False
        self.setFixedHeight(60)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("Clic: activar · Clic derecho: nombre y animacion")

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
        font = QFont("Segoe UI")
        font.setPixelSize(11)
        p.setFont(font)
        p.setPen(QColor(TEXT if selected or name else MUTED))
        label = f"{self.mode} · {name}" if name else f"Modo {self.mode}"
        label = QFontMetrics(font).elidedText(label, Qt.TextElideMode.ElideRight, self.width() - 10)
        p.drawText(QRectF(0, 37, self.width(), 18), Qt.AlignmentFlag.AlignCenter, label)


class Swatch(QWidget):
    def __init__(self, model, name, rgb=None, kind="color"):
        super().__init__()
        self.model, self.rgb, self.kind = model, rgb, kind
        self.setFixedSize(30, 30)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(name)

    def is_selected(self):
        m = self.model
        if m.page is None:
            return False
        if self.kind == "multi":
            return m.multi
        if self.kind == "custom":
            return not m.multi and m.rgb not in [c for _, c in COLORS]
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
            path = QPainterPath()
            path.addEllipse(circle)
            p.setClipPath(path)
            for k in range(36):
                p.setBrush(QColor.fromHsvF(k / 36, 1, 1))
                p.drawPie(circle.adjusted(-2, -2, 2, 2), k * 160, 170)
        elif self.kind == "custom":
            m = self.model
            p.setBrush(QColor(*m.rgb) if self.is_selected() else QColor(CARD))
            p.setPen(QPen(QColor(MUTED), 1))
            p.drawEllipse(circle)
            p.setPen(QPen(QColor(TEXT), 1.6))
            p.drawLine(QPointF(15, 10.5), QPointF(15, 19.5))
            p.drawLine(QPointF(10.5, 15), QPointF(19.5, 15))
        else:
            p.setBrush(QColor(*self.rgb))
            p.drawEllipse(circle)


class ModeDialog(QDialog):
    def __init__(self, parent, mode, name, effect):
        super().__init__(parent)
        self.setWindowTitle(f"Modo {mode}")
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint)
        self.name = QLineEdit(name)
        self.name.setPlaceholderText("Por ejemplo: Ola, Fijo, Respiracion...")
        self.effect = QComboBox()
        for key, (label, _) in EFFECTS.items():
            self.effect.addItem(label, key)
        self.effect.setCurrentIndex(max(0, self.effect.findData(effect)))
        form = QFormLayout(self)
        form.addRow("Nombre", self.name)
        form.addRow("Animacion", self.effect)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                   | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)
        self.setMinimumWidth(320)


class Panel(QWidget):
    WIDTH = 440

    def __init__(self, model):
        super().__init__()
        self.model = model
        self.busy = False           # hay un dialogo abierto: no ocultar al perder el foco
        self.hidden_at = 0.0
        self.setWindowFlags(Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
                            | Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedWidth(self.WIDTH)

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(10)
        root.addWidget(BatteryView(model))
        self.keyboard = KeyboardView(model)
        root.addWidget(self.keyboard)

        self.mode_title = self.section("MODO DE LUZ")
        root.addWidget(self.mode_title)
        grid = QGridLayout()
        grid.setSpacing(6)
        self.tiles = []
        for n in range(MODE_COUNT):
            tile = ModeTile(model, n + 1, self.edit_mode)
            self.tiles.append(tile)
            grid.addWidget(tile, n // 6, n % 6)
        root.addLayout(grid)

        root.addWidget(self.section("COLOR"))
        row = QHBoxLayout()
        row.setSpacing(4)
        self.swatches = [Swatch(model, name, rgb) for name, rgb in COLORS]
        self.swatches.append(Swatch(model, "Multicolor", kind="multi"))
        self.swatches.append(Swatch(model, "Otro color...", kind="custom"))
        for s in self.swatches:
            row.addWidget(s)
        row.addStretch()
        root.addLayout(row)

        self.bright = self.slider_row(root, "BRILLO", OFF_BRIGHT)
        self.speed = self.slider_row(root, "VELOCIDAD", OFF_SPEED)

        self.timer = QTimer(self)
        self.timer.setInterval(40)
        self.timer.timeout.connect(self.animate)
        model.changed.connect(self.sync)
        self.sync()

    def section(self, text):
        label = QLabel(text)
        label.setStyleSheet(f"color:{MUTED}; font:600 10px 'Segoe UI'; letter-spacing:1px;"
                            "margin-top:4px;")
        return label

    def slider_row(self, root, text, off):
        row = QHBoxLayout()
        label = self.section(text)
        label.setFixedWidth(84)
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

    def sync(self):
        m = self.model
        for slider, off in ((self.bright, OFF_BRIGHT), (self.speed, OFF_SPEED)):
            if not slider.isSliderDown():
                slider.blockSignals(True)
                slider.setValue(min(m.value(off), MAX_LEVEL))
                slider.blockSignals(False)
                slider.valueChanged.emit(slider.value())
        self.mode_title.setText("MODO DE LUZ" + (
            f"  ·  {m.mode_label(m.value(OFF_MODE))}" if m.page is not None else "  ·  sin datos"))
        self.update()

    def animate(self):
        self.keyboard.update()
        for tile in self.tiles:
            tile.update()

    def edit_mode(self, mode):
        name, effect = self.model.mode_info(mode)
        self.busy = True
        dlg = ModeDialog(self, mode, name, effect)
        if dlg.exec():
            self.model.set_mode_info(mode, dlg.name.text().strip(), dlg.effect.currentData())
        self.busy = False
        self.activateWindow()

    def pick_color(self):
        self.busy = True
        color = QColorDialog.getColor(QColor(*self.model.rgb), self, "Color del teclado")
        self.busy = False
        self.activateWindow()
        if color.isValid():
            self.model.apply({OFF_MULTI: 0, OFF_RGB: color.red(), OFF_RGB + 1: color.green(),
                              OFF_RGB + 2: color.blue()})

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(QPen(QColor(BORDER), 1))
        p.setBrush(QColor(BG))
        p.drawRoundedRect(QRectF(0.5, 0.5, self.width() - 1, self.height() - 1), 16, 16)

    def popup(self):
        self.adjustSize()
        screen = QApplication.screenAt(QCursor.pos()) or QApplication.primaryScreen()
        area = screen.availableGeometry()
        self.move(area.right() - self.width() - 12, area.bottom() - self.height() - 12)
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
        menu = QMenu()
        menu.addAction("Abrir panel", panel.popup)
        menu.addAction("Actualizar ahora", model.refresh)
        self.auto = QAction("Iniciar con Windows", menu, checkable=True)
        self.auto.setChecked(autostart_enabled())
        self.auto.toggled.connect(set_autostart)
        menu.addAction(self.auto)
        menu.addSeparator()
        menu.addAction("Salir", QApplication.quit)
        self.menu = menu
        self.setContextMenu(menu)
        self.activated.connect(self.on_click)
        model.changed.connect(self.sync)
        model.message.connect(lambda text: self.showMessage("Teclado KOORUI", text))
        self.sync()

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
        self.setIcon(make_icon(m.percent, m.stale))
        lights = m.mode_label(m.value(OFF_MODE)) if m.page is not None else "sin datos"
        self.setToolTip(f"KOORUI · Bateria {m.battery_text()}\nLuces: {lights}")


def main():
    # Una sola instancia.
    mutex = ctypes.windll.kernel32.CreateMutexW(None, False, APP_NAME + "Mutex")
    if ctypes.windll.kernel32.GetLastError() == 183:
        return
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setStyleSheet(
        f"QDialog{{background:{BG};}} QLabel{{color:{TEXT};}}"
        f"QLineEdit,QComboBox{{background:{CARD};color:{TEXT};border:1px solid {BORDER};"
        "border-radius:6px;padding:5px;}"
        f"QComboBox QAbstractItemView{{background:{CARD};color:{TEXT};"
        f"selection-background-color:{ACCENT};}}"
        f"QPushButton{{background:{CARD};color:{TEXT};border:1px solid {BORDER};"
        "border-radius:6px;padding:5px 14px;}"
        f"QMenu{{background:{CARD};color:{TEXT};border:1px solid {BORDER};padding:4px;}}"
        f"QMenu::item{{padding:5px 22px;}} QMenu::item:selected{{background:{ACCENT};}}"
        f"QToolTip{{background:{CARD};color:{TEXT};border:1px solid {BORDER};}}")
    model = Model()
    panel = Panel(model)
    tray = Tray(model, panel)
    tray.show()
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
