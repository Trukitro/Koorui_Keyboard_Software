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
import csv
import ctypes
import hashlib
import html
import json
import math
import os
import re
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.request
import uuid
import winreg
from ctypes import wintypes

import hid
from PyQt6.QtCore import (QAbstractNativeEventFilter, QObject, QPointF, QRectF, QStandardPaths,
                          Qt, QTime, QTimer, QUrl, pyqtSignal)
from PyQt6.QtGui import (QAction, QColor, QCursor, QDesktopServices, QFont, QFontMetrics,
                         QIcon, QPainter, QPainterPath, QPen, QPixmap)
from PyQt6.QtWidgets import (QApplication, QCheckBox, QColorDialog, QComboBox, QDialog,
                             QDialogButtonBox, QFileDialog, QFormLayout, QGridLayout, QHBoxLayout,
                             QInputDialog, QLabel, QLineEdit, QMenu, QMessageBox, QPushButton,
                             QScrollArea,
                             QSlider,
                             QSpinBox, QStackedWidget, QSystemTrayIcon, QTimeEdit,
                             QVBoxLayout, QWidget)

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
USAGE_FILE = os.path.join(DATA_DIR, "usage.json")      # minutos de uso por dia y hora
KEYS_FILE = os.path.join(DATA_DIR, "keys.json")        # pulsaciones por dia y tecla
TYPING_FILE = os.path.join(DATA_DIR, "typing.json")    # rachas de escritura por dia (PPM)
LOG_FILE = os.path.join(DATA_DIR, "error.log")
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

HOUR, DAY = 3600, 86400
HISTORY_KEEP = 90 * DAY         # se descartan lecturas mas antiguas
HISTORY_EVERY = 600             # sin cambios, una lectura cada 10 minutos
HISTORY_GAP = 3 * HOUR          # un hueco mayor (teclado apagado) se dibuja punteado
ACTIVE_GAP = 15 * 60            # lecturas mas separadas: el teclado estuvo en reposo
IDLE_AFTER = 3 * 60             # sin pulsaciones durante este tiempo: teclado en reposo
IDLE_POLL = 5 * 60              # intervalo de consulta mientras esta en reposo
ACTIVITY_KEEP = 90             # dias de uso y pulsaciones que se conservan
HEAT_DAYS = 30                 # dias que suma el mapa de calor

APP_VERSION = "1.9.0"
FROZEN = getattr(sys, "frozen", False)  # ejecutable empaquetado (no desde el codigo)
SETUP_NAME, SUMS_NAME = "KooruiKeyboardSetup.exe", "SHA256SUMS.txt"
BREAK_GAP = 5 * 60              # una parada mas larga cuenta como pausa
BURST_GAP = 2.0                 # una pausa mayor entre teclas cierra la racha de escritura
BURST_MIN_CHARS, BURST_MIN_SECS = 20, 6     # rachas mas cortas no cuentan para las PPM
BEST_MIN_SECS = 20              # duracion minima para contar como mejor racha
WPM_LEVELS = ((0, "Inicial", "Beginner"), (20, "Básico", "Basic"), (40, "Medio", "Average"),
              (60, "Rápido", "Fast"), (80, "Muy rápido", "Very fast"), (100, "Experto", "Expert"))
REPO = "Trukitro/Koorui_Keyboard_Software"
UPDATE_EVERY = DAY

BG, CARD, BORDER, TEXT, MUTED, ACCENT = "#15171c", "#1e2128", "#2c303a", "#e8eaf0", "#8a90a0", "#4da3ff"
GREEN, BLUE = "#3fbf7f", "#2878d7"

# Ajustes del usuario (se cargan de settings.json al arrancar).
DEFAULTS = {"lang": "es", "poll": 30, "low": 20, "critical": 10,
            "full_notify": True, "full_level": 100,
            "lock_off": False, "sched_on": False, "sched_from": "23:00", "sched_to": "07:00",
            "heatmap": False, "check_updates": True, "auto_update": False,
            "forecast_notify": True, "break_minutes": 60, "wpm": False}
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
    "set_general": ("GENERAL", "GENERAL"),
    "tab_typing": ("Escritura", "Typing"),
    "wpm": ("Medir palabras por minuto (PPM)", "Measure words per minute (WPM)"),
    "wpm_unit": ("PPM", "WPM"),
    "wpm_off": ("La medición de velocidad está desactivada. Si la activas, la app solo mira si "
                "cada pulsación de este teclado es una letra, un borrado u otra tecla, y cuánto "
                "tiempo pasa entre ellas. Nunca guarda qué escribes.",
                "Speed measurement is off. When on, the app only looks at whether each press on "
                "this keyboard is a character, a backspace or another key, and the time between "
                "them. It never stores what you type."),
    "wpm_enable": ("Activar medición", "Turn it on"),
    "wpm_none": ("Aún no hay rachas medidas. Escribe de seguido unos segundos con este teclado.",
                 "No bursts measured yet. Type continuously for a few seconds on this keyboard."),
    "wpm_today": ("hoy", "today"),
    "wpm_week": ("media de 7 días", "7-day average"),
    "wpm_accuracy": ("precisión {}%", "accuracy {}%"),
    "wpm_best": ("mejor racha {} {}", "best burst {} {}"),
    "wpm_last": ("ÚLTIMA RACHA", "LAST BURST"),
    "wpm_last_text": ("{} {} durante {} · {} · precisión {}%",
                      "{} {} for {} · {} · accuracy {}%"),
    "wpm_days": ("PPM POR DÍA (14 DÍAS)", "WPM PER DAY (14 DAYS)"),
    "wpm_avg7": ("Media 7 días", "7-day average"),
    "wpm_record": ("Récord", "Record"),
    "wpm_acc7": ("Precisión 7 días", "7-day accuracy"),
    "wpm_trend": ("Frente a la semana anterior", "Vs previous week"),
    "wpm_presses": ("PULSACIONES", "KEY PRESSES"),
    "wpm_note": ("Una palabra son 5 pulsaciones de carácter, el estándar de los test de "
                 "mecanografía. Solo cuenta mientras escribes de seguido: una pausa de más de 2 s "
                 "cierra la racha. La precisión cuenta cada borrado como un error. Escribiendo de "
                 "forma natural el resultado suele quedar algo por debajo de un test.",
                 "One word is 5 character presses, the standard used by typing tests. It only "
                 "counts while you type continuously: a pause longer than 2 s ends the burst. "
                 "Accuracy counts each backspace as an error. Natural typing usually comes out "
                 "somewhat below a typing test."),
    "set_battery": ("BATERÍA", "BATTERY"),
    "set_lights": ("LUCES", "LIGHTS"),
    "set_use": ("USO", "USE"),
    "set_updates": ("ACTUALIZACIONES", "UPDATES"),
    "set_note": ("Los cambios se guardan al momento.", "Changes are saved instantly."),
    "heatmap_confirm": ("Al desactivar el mapa de calor se borran los contadores de pulsaciones. "
                        "¿Continuar?",
                        "Turning the heatmap off deletes the key press counters. Continue?"),
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
    "tab_panel": ("Panel", "Panel"),
    "heatmap": ("Contar pulsaciones por tecla (mapa de calor)",
                "Count key presses per key (heatmap)"),
    "check_updates": ("Avisar de versiones nuevas", "Notify about new versions"),
    "rep_usage": ("USO DEL TECLADO", "KEYBOARD USE"),
    "rep_usage_today": ("Hoy: {} de uso · media de los días con uso (7 d): {}",
                        "Today: {} of use · average on days used (7 d): {}"),
    "rep_usage_none": ("Aún no hay datos de uso. Se registran los minutos en que escribes con este "
                       "teclado.",
                       "No usage data yet. The minutes you type on this keyboard are logged."),
    "rep_usage_hours": ("Minutos de uso por hora del día (media de 7 días)",
                        "Minutes of use per hour of day (7-day average)"),
    "rep_heat": ("MAPA DE CALOR ({} DÍAS)", "HEATMAP ({} DAYS)"),
    "rep_heat_off": ("Desactivado. Actívalo en Ajustes para contar cuántas veces pulsas cada "
                     "tecla (solo el número, nunca lo que escribes).",
                     "Off. Turn it on in Settings to count how often you press each key "
                     "(only the count, never what you type)."),
    "rep_heat_empty": ("Aún no hay pulsaciones contadas.", "No key presses counted yet."),
    "rep_heat_top": ("Más usadas: {}", "Most used: {}"),
    "rep_heat_total": ("Pulsaciones: hoy {} · media diaria {}", "Key presses: today {} · daily average {}"),
    "rep_wear": ("DESGASTE DE LA BATERÍA", "BATTERY WEAR"),
    "rep_wear_rows": ("Autonomía por mes con brillo {}", "Battery life per month at brightness {}"),
    "rep_wear_wait": ("Hacen falta al menos dos meses con uso registrado a un mismo brillo para "
                      "comparar.",
                      "At least two months of logged use at the same brightness are needed "
                      "to compare."),
    "rep_export_csv": ("Exportar CSV", "Export CSV"),
    "rep_export_html": ("Exportar informe HTML", "Export HTML report"),
    "msg_exported": ("Guardado en {}", "Saved to {}"),
    "msg_update": ("Hay una versión nueva: {}. Haz clic para verla en el panel.",
                   "A new version is available: {}. Click to see it in the panel."),
    "download_update": ("Actualizar a la versión {}", "Update to version {}"),
    "check_now": ("Buscar actualizaciones", "Check for updates"),
    "checking": ("Buscando…", "Checking…"),
    "up_to_date": ("Estás al día", "You're up to date"),
    "update_to": ("Actualizar a {}", "Update to {}"),
    "downloading": ("Descargando {}…", "Downloading {}…"),
    "installing": ("Instalando {}…", "Installing {}…"),
    "update_failed": ("No se pudo actualizar: {}", "Update failed: {}"),
    "update_no_asset": ("la versión no incluye el instalador",
                        "the release does not include the installer"),
    "update_no_hash": ("la versión no publica su huella SHA-256",
                       "the release does not publish its SHA-256 checksum"),
    "update_bad_hash": ("la huella del archivo descargado no coincide",
                        "the downloaded file's checksum does not match"),
    "update_source": ("Desde el código: actualiza con git pull", "From source: update with git pull"),
    "check_failed": ("Sin conexión con GitHub", "Could not reach GitHub"),
    "auto_update": ("Instalar actualizaciones automáticamente", "Install updates automatically"),
    "forecast_notify": ("Avisar antes de quedarse sin batería", "Warn before the battery runs low"),
    "msg_forecast": ("Al ritmo actual llegarás al {}% {}. Conviene cargarlo antes.",
                     "At the current rate you will reach {}% {}. Charge it before then."),
    "at_today": ("hoy hacia las {}", "today around {}"),
    "at_tomorrow": ("mañana hacia las {}", "tomorrow around {}"),
    "break_minutes": ("Recordar una pausa tras (0 = no)",
                      "Remind me to take a break after (0 = off)"),
    "msg_break": ("Llevas {} escribiendo sin parar. Buen momento para una pausa.",
                  "You have been typing for {} without a break. Good time to rest."),
    "backup": ("Copia de seguridad", "Backup"),
    "backup_export": ("Exportar…", "Export…"),
    "backup_import": ("Importar…", "Import…"),
    "backup_confirm": ("Esto reemplaza perfiles, nombres de modos, ajustes e historial por los "
                       "de la copia. ¿Continuar?",
                       "This replaces profiles, mode names, settings and history with the "
                       "backup's. Continue?"),
    "backup_invalid": ("El archivo no es una copia de seguridad de esta app.",
                       "The file is not a backup from this app."),
    "msg_backup_saved": ("Copia guardada en {}", "Backup saved to {}"),
    "msg_backup_loaded": ("Copia de seguridad restaurada", "Backup restored"),
    "rep_typing": ("ESCRITURA", "TYPING"),
    "rep_typing_off": ("Activa el mapa de calor en Ajustes para ver las pulsaciones por día.",
                       "Turn on the heatmap in Settings to see key presses per day."),
    "rep_typing_avg": ("Media: {} pulsaciones al día · {} por minuto de uso",
                       "Average: {} key presses a day · {} per minute of use"),
    "rep_typing_trend": ("Últimos 7 días frente a los 7 anteriores: {}",
                         "Last 7 days vs the previous 7: {}"),
    "rep_typing_days": ("Pulsaciones por día (14 días)", "Key presses per day (14 days)"),
    "html_title": ("Informe del teclado KOORUI", "KOORUI keyboard report"),
    "html_generated": ("Generado el {}", "Generated on {}"),
    "html_history": ("Historial completo de lecturas: exporta el CSV desde la app.",
                     "Full reading history: export the CSV from the app."),
    "csv_header": ("fecha;porcentaje;cargando;modo;brillo", "date,percent,charging,mode,brightness"),
    "tab_report": ("Informe", "Report"),
    "rep_forecast": ("PREVISIÓN", "FORECAST"),
    "rep_left": ("Al ritmo actual te queda {}.", "At the current rate you have {} left."),
    "rep_next": ("Conviene cargarlo {} (al llegar al {}%).",
                 "Plan to charge it {} (when it reaches {}%)."),
    "rep_charging_now": ("Cargando ahora.", "Charging now."),
    "rep_no_forecast": ("Aún no hay suficiente descarga reciente para estimar.",
                        "Not enough recent discharge to estimate yet."),
    "today": ("hoy", "today"),
    "tomorrow": ("mañana", "tomorrow"),
    "rep_autonomy": ("AUTONOMÍA SEGÚN EL BRILLO", "BATTERY LIFE BY BRIGHTNESS"),
    "rep_bright": ("Brillo {}", "Brightness {}"),
    "rep_off": ("Luces apagadas", "Lights off"),
    "rep_life": ("~{} de uso · {} %/h", "~{} of use · {} %/h"),
    "rep_measuring": ("midiendo · {} registradas", "measuring · {} logged"),
    "rep_used": ("Autonomía con carga completa, contando solo el tiempo de uso. Medido: {}.",
                 "Battery life on a full charge, counting active use only. Measured: {}."),
    "rep_no_usage": ("Aún no hay uso registrado. Hacen falta al menos 2 h de uso con un "
                     "mismo brillo.",
                     "No usage logged yet. At least 2 h of use at the same brightness "
                     "is needed."),
    "rep_charges": ("CARGAS", "CHARGING"),
    "rep_last": ("Última carga", "Last charge"),
    "rep_per_week": ("Cargas por semana", "Charges per week"),
    "rep_between": ("Entre cargas", "Between charges"),
    "rep_full_time": ("Carga de 0 a 100%", "Charge 0 to 100%"),
    "rep_recent": ("ÚLTIMAS CARGAS", "RECENT CHARGES"),
    "rep_no_charges": ("Todavía no se ha registrado ninguna carga.", "No charges logged yet."),
    "rep_in_progress": ("en curso", "in progress"),
    "bt_only": ("no disponible por Bluetooth", "not available over Bluetooth"),
    "msg_bt_lights": ("Por Bluetooth solo se puede ver la bateria; las luces se cambian "
                      "en 2.4G o por cable.",
                      "Over Bluetooth only the battery is available; change the lights "
                      "in 2.4G or wired mode."),
    "lock_off": ("Apagar luces al bloquear el PC", "Turn lights off when the PC is locked"),
    "sched_on": ("Apagar luces por horario", "Turn lights off on a schedule"),
    "sched_range": ("Horario de apagado", "Off hours"),
}


WEEKDAYS = (("lun", "mar", "mié", "jue", "vie", "sáb", "dom"),
            ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"))
WEEKDAYS_LONG = (("el lunes", "el martes", "el miércoles", "el jueves", "el viernes",
                  "el sábado", "el domingo"),
                 ("on Monday", "on Tuesday", "on Wednesday", "on Thursday", "on Friday",
                  "on Saturday", "on Sunday"))


MONTHS = (("ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"),
          ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"))


def month_label(key):
    """'2026-08' -> 'ago 2026'."""
    year, month = key.split("-")
    return f"{MONTHS[lang_index()][int(month) - 1]} {year}"


def lang_index():
    return 1 if CFG["lang"] == "en" else 0


def tr(key, *args):
    text = STR[key][lang_index()]
    return text.format(*args) if args else text


# Codigos de escaneo (codigo, extendida) de cada posicion de LAYOUT, en orden. None: sin tecla
# (la rueda o la tecla Fn, que no llega al PC).
SCAN_LAYOUT = (
    [(0x01, 0)] + [(c, 0) for c in range(0x3B, 0x45)] + [(0x57, 0), (0x58, 0), (0x53, 1),
                                                        None, None],
    [(0x29, 0)] + [(c, 0) for c in range(0x02, 0x0F)] + [(0x47, 1)],
    [(0x0F, 0)] + [(c, 0) for c in range(0x10, 0x1C)] + [(0x2B, 0), (0x49, 1)],
    [(0x3A, 0)] + [(c, 0) for c in range(0x1E, 0x29)] + [(0x1C, 0), (0x51, 1)],
    [(0x2A, 0)] + [(c, 0) for c in range(0x2C, 0x36)] + [(0x36, 0), (0x48, 1), (0x4F, 1)],
    [(0x1D, 0), (0x5B, 1), (0x38, 0), (0x39, 0), (0x38, 1), None, (0x1D, 1), (0x4B, 1),
     (0x50, 1), (0x4D, 1)],
)
KEY_NAMES = (
    ["Esc"] + [f"F{n}" for n in range(1, 13)] + ["Del", "", ""],
    list("`1234567890-=") + ["Backspace", "Home"],
    ["Tab"] + list("QWERTYUIOP[]") + ["\\", "PgUp"],
    ["Caps"] + list("ASDFGHJKL;'") + ["Enter", "PgDn"],
    ["Shift"] + list("ZXCVBNM,./") + ["Shift", "↑", "End"],
    ["Ctrl", "Win", "Alt", "Space", "AltGr", "Fn", "Ctrl", "←", "↓", "→"],
)
KEY_COUNT = sum(len(row) for row in SCAN_LAYOUT)
# Teclas que producen un caracter (letras, numeros, signos y espacio) y la de borrar.
CHAR_CODES = {(c, 0) for c in [*range(0x02, 0x0E), *range(0x10, 0x1C), *range(0x1E, 0x2A),
                               *range(0x2B, 0x36), 0x39]}
BACKSPACE = (0x0E, 0)
SCAN_INDEX = {code: i for i, code in enumerate(c for row in SCAN_LAYOUT for c in row) if code}
KEY_LABELS = [name for row in KEY_NAMES for name in row]

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


def active_pairs(history):
    """Pares de lecturas seguidas descargando y con el teclado en uso (sin reposo en medio)."""
    for a, b in zip(history, history[1:]):
        if not a[2] and not b[2] and 0 < b[0] - a[0] <= ACTIVE_GAP:
            yield a, b


def autonomy_by_brightness(history):
    """{brillo: (horas de uso medidas, puntos de bateria gastados)}."""
    groups = {}
    for a, b in active_pairs(history):
        if len(a) < 5 or a[4] is None:
            continue
        hours, drop = groups.get(a[4], (0.0, 0))
        groups[a[4]] = (hours + (b[0] - a[0]) / HOUR, drop + a[1] - b[1])
    return groups


def charge_sessions(history):
    """[(inicio, fin, % al empezar, % al terminar, sigue cargando)] de cada carga."""
    sessions, current, prev = [], None, None
    for entry in history:
        ts, pct, charging = entry[0], entry[1], entry[2]
        if charging:
            if current is None:
                start = prev[1] if prev and ts - prev[0] <= HISTORY_GAP else pct
                current = [ts, ts, start, pct]
            else:
                current[1], current[3] = ts, max(current[3], pct)
        elif current is not None:
            sessions.append((*current, False))
            current = None
        prev = entry
    if current is not None:
        sessions.append((*current, True))
    return sessions


def charge_stats(history, now=None):
    """Resumen de cargas: sesiones, cargas por semana, dias entre cargas, tiempo 0-100 %."""
    now = time.time() if now is None else now
    sessions = charge_sessions(history)
    span_days = (now - history[0][0]) / DAY if history else 0
    starts = [s[0] for s in sessions]
    full = [(s[1] - s[0]) * 100 / (s[3] - s[2]) for s in sessions
            if not s[4] and s[3] >= 95 and s[3] - s[2] >= 20 and s[1] > s[0]]
    return {
        "sessions": sessions,
        "per_week": len(sessions) / span_days * 7 if span_days >= 2 else None,
        "between": (starts[-1] - starts[0]) / (len(starts) - 1) if len(starts) >= 2 else None,
        "full_time": statistics.median(full) if full else None,
    }


def day_key(ts):
    return time.strftime("%Y-%m-%d", time.localtime(ts))


def recent_days(data, days, now=None):
    """Valores de un diccionario {dia: ...} de los ultimos `days` dias."""
    now = time.time() if now is None else now
    wanted = {day_key(now - n * DAY) for n in range(days)}
    return [v for k, v in data.items() if k in wanted]


def usage_stats(usage, now=None):
    """Minutos de uso hoy, media de los dias con uso (7 dias) y media por hora del dia."""
    now = time.time() if now is None else now
    week = [d for d in recent_days(usage, 7, now) if sum(d)]
    today = sum(usage.get(day_key(now), [0] * 24))
    if not week:
        return {"today": today, "daily": 0, "hours": [0] * 24, "days": 0}
    hours = [sum(d[h] for d in week) / len(week) for h in range(24)]
    return {"today": today, "daily": sum(map(sum, week)) / len(week), "hours": hours,
            "days": len(week)}


def key_stats(keys, now=None):
    """Pulsaciones por tecla de los ultimos HEAT_DAYS dias, de hoy y media diaria."""
    now = time.time() if now is None else now
    days = recent_days(keys, HEAT_DAYS, now)
    totals = [sum(d[i] for d in days if i < len(d)) for i in range(KEY_COUNT)]
    used = [sum(d) for d in days if sum(d)]
    return {"totals": totals, "today": sum(keys.get(day_key(now), [])),
            "daily": sum(used) / len(used) if used else 0}


def top_keys(totals, count=5):
    ranked = sorted(range(len(totals)), key=lambda i: totals[i], reverse=True)
    return [(KEY_LABELS[i], totals[i]) for i in ranked[:count] if totals[i]]


def wear_by_month(history):
    """(brillo mas usado, [(mes, autonomia en horas)]) de los meses con datos suficientes."""
    groups = {}
    for a, b in active_pairs(history):
        if len(a) < 5 or a[4] is None:
            continue
        key = (time.strftime("%Y-%m", time.localtime(a[0])), a[4])
        hours, drop = groups.get(key, (0.0, 0))
        groups[key] = (hours + (b[0] - a[0]) / HOUR, drop + a[1] - b[1])
    if not groups:
        return None, []
    per_bright = {}
    for (_, bright), (hours, _) in groups.items():
        per_bright[bright] = per_bright.get(bright, 0) + hours
    bright = max(per_bright, key=per_bright.get)
    months = [(month, 100 * h / d) for (month, b), (h, d) in sorted(groups.items())
              if b == bright and h >= 2 and d >= 3]
    return bright, months


def typing_stats(keys, usage, now=None):
    """Pulsaciones por dia (14 dias), media, ritmo por minuto de uso y tendencia semanal."""
    now = time.time() if now is None else now
    days = [day_key(now - n * DAY) for n in range(13, -1, -1)]
    per_day = [sum(keys.get(d, [])) for d in days]
    previous, last = per_day[:7], per_day[7:]
    used = [v for v in last if v]
    minutes = sum(sum(usage.get(d, [])) for d, v in zip(days[7:], last) if v)
    return {"days": days, "per_day": per_day,
            "avg": sum(used) / len(used) if used else 0,
            "rate": sum(last) / minutes if minutes else 0,
            "trend": (sum(last) - sum(previous)) / sum(previous) * 100 if sum(previous) else None}


def burst_wpm(entry):
    """PPM de un dia o grupo de dias {chars, secs}: (caracteres / 5) / minutos."""
    return entry["chars"] / 5 / (entry["secs"] / 60) if entry and entry["secs"] >= 30 else 0


def merge_days(entries):
    total = {"chars": 0, "secs": 0, "backs": 0}
    for e in entries:
        for k in total:
            total[k] += e[k]
    return total


def accuracy(entry):
    return max(0.0, 1 - entry["backs"] / entry["chars"]) * 100 if entry["chars"] else 0


def wpm_level(wpm):
    names = [lvl for lvl in WPM_LEVELS if wpm >= lvl[0]][-1]
    return names[1 + lang_index()]


def wpm_stats(typing, now=None):
    """PPM de hoy, por dia (14 dias), media y precision de 7 dias, tendencia y record."""
    now = time.time() if now is None else now
    days = [day_key(now - n * DAY) for n in range(13, -1, -1)]
    last = merge_days(typing[d] for d in days[7:] if d in typing)
    previous = merge_days(typing[d] for d in days[:7] if d in typing)
    record = max(((e.get("best", 0), d) for d, e in typing.items()), default=(0, None))
    week, before = burst_wpm(last), burst_wpm(previous)
    return {"days": days, "per_day": [round(burst_wpm(typing.get(d))) for d in days],
            "today": typing.get(day_key(now)), "week": week, "week_acc": accuracy(last),
            "trend": (week - before) / before * 100 if before and week else None,
            "record": record}


def when_text(ts, now=None):
    """'hoy hacia las 15:00' o 'manana hacia las 10:00'."""
    now = time.time() if now is None else now
    clock = time.strftime("%H:%M", time.localtime(ts))
    return tr("at_today" if day_key(ts) == day_key(now) else "at_tomorrow", clock)


def short_count(n):
    return f"{n / 1000:.1f}k" if n >= 1000 else str(round(n))


def export_csv(path, history):
    """Historial de lecturas; separador ';' en espanol (Excel en espanol lo espera asi)."""
    sep = "," if CFG["lang"] == "en" else ";"
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        out = csv.writer(f, delimiter=sep)
        out.writerow(tr("csv_header").split(sep))
        for entry in history:
            mode, bright = (entry[3], entry[4]) if len(entry) >= 5 else (None, None)
            out.writerow([time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(entry[0])),
                          entry[1], entry[2], "" if mode is None else mode,
                          "" if bright is None else bright])


def hours_text(seconds):
    """Duracion de uso siempre en horas: '41 h' o '25 min'."""
    if seconds < HOUR:
        return f"{max(1, round(seconds / 60))} min"
    return f"{seconds / HOUR:.0f} h"


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
    update_found = pyqtSignal(str, str)     # version, enlace
    update_status = pyqtSignal(str)         # progreso de la busqueda o instalacion

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
        self.usage = load_json(USAGE_FILE)  # {dia: [minutos de uso por hora]}
        self.keys = load_json(KEYS_FILE)    # {dia: [pulsaciones por tecla]}
        self.typing = load_json(TYPING_FILE)    # {dia: {chars, secs, backs, best, bursts}}
        self.burst = None                   # racha de escritura en curso
        self.last_burst = None              # (ppm, segundos, inicio, precision)
        self.data_lock = threading.Lock()
        self.data_dirty = False
        self.raw_input = False              # ya llegaron pulsaciones de este teclado
        self.last_activity = 0.0
        self.last_minute = 0
        self.last_poll = 0.0
        self.pressed = set()
        self.update = None                  # (version, enlace, datos) si hay una version nueva
        self.session_start = 0.0            # inicio de la racha de escritura actual
        self.break_warned = False
        self.wake = threading.Event()
        threading.Thread(target=self._loop, daemon=True).start()
        threading.Thread(target=self._update_loop, daemon=True).start()

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
        if CFG["heatmap"] and not values.get("heatmap", True):   # al desactivarlo se borra
            with self.data_lock:
                self.keys = {}
            try:
                os.remove(KEYS_FILE)
            except OSError:
                pass
        CFG.update(values)
        save_json(SETTINGS_FILE, CFG)
        self.settings_changed.emit()
        self.changed.emit()
        self.wake.set()

    # --- historial --------------------------------------------------------
    def _record(self, pct, charging, now):
        """Guarda [hora, %, cargando, modo, brillo]; modo y brillo None si no se conocen."""
        lights = ([self.page[OFF_MODE], self.page[OFF_BRIGHT]] if self.page is not None
                  else [None, None])
        last = self.history[-1] if self.history else None
        if (last and last[1] == pct and bool(last[2]) == charging and last[3:5] == lights
                and now - last[0] < HISTORY_EVERY):
            return
        self.history.append([round(now), pct, int(charging)] + lights)
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
        rate = self.discharge_rate()
        return "~" + human(self.percent / rate) if rate else ""

    def discharge_rate(self):
        """Puntos de bateria por segundo en el tramo de descarga actual, o None."""
        if self.percent is None or self.charging:
            return None
        run = []                            # del mas reciente hacia atras
        for ts, pct, chg, *_ in reversed(list(self.history)):
            if chg or (run and pct < run[-1][1] - 3):   # carga, o subio sin que lo vieramos
                break
            run.append((ts, pct))
        if len(run) < 2:
            return None
        span = run[0][0] - run[-1][0]
        drop = run[-1][1] - run[0][1]
        if span < 30 * 60 or drop < 3:
            return None
        return drop / span

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
            self.warned -= {"low", "critical", "forecast"}
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
        rate = self.discharge_rate()
        if (CFG["forecast_notify"] and rate and pct > CFG["low"]
                and "forecast" not in self.warned):
            when = time.time() + (pct - CFG["low"]) / rate
            if when - time.time() < DAY:        # llegara al umbral en menos de un dia
                self.warned.add("forecast")
                self.message.emit(tr("msg_forecast", CFG["low"], when_text(when)))

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

    # --- actividad del teclado ------------------------------------------------
    def key_event(self, code, down):
        """Una pulsacion de este teclado. code: (codigo de escaneo, extendida)."""
        now = time.time()
        self.raw_input = True
        gap = now - self.last_activity
        was_idle = gap > IDLE_AFTER
        if gap > BREAK_GAP:                     # tras una pausa empieza otra racha
            self.session_start = now
            self.break_warned = False
        self.last_activity = now
        if (CFG["break_minutes"] and not self.break_warned
                and now - self.session_start >= CFG["break_minutes"] * 60):
            self.break_warned = True
            self.message.emit(tr("msg_break", hours_text(now - self.session_start)))
        with self.data_lock:
            minute = int(now // 60)
            if minute != self.last_minute:      # un minuto con actividad = un minuto de uso
                self.last_minute = minute
                hours = self.usage.setdefault(day_key(now), [0] * 24)
                hours[time.localtime(now).tm_hour] += 1
                self.data_dirty = True
            first = down and code not in self.pressed   # sin la repeticion automatica
            if down:
                self.pressed.add(code)
            else:
                self.pressed.discard(code)
            if CFG["heatmap"] and first:
                index = SCAN_INDEX.get(code)
                if index is not None:
                    counts = self.keys.setdefault(day_key(now), [0] * KEY_COUNT)
                    counts[index] += 1
                    self.data_dirty = True
            if CFG["wpm"] and first and (code in CHAR_CODES or code == BACKSPACE):
                self._typing_key(code == BACKSPACE, now)
        if was_idle and now - self.last_poll > 20:   # se desperto: leer ya
            self.wake.set()

    def _typing_key(self, backspace, now):
        """Suma una tecla a la racha de escritura (con data_lock tomado)."""
        if self.burst is None or now - self.burst["last"] > BURST_GAP:
            self._close_burst()
            self.burst = {"start": now, "last": now, "chars": 0, "backs": 0}
        self.burst["last"] = now
        self.burst["backs" if backspace else "chars"] += 1

    def _close_burst(self):
        """Guarda la racha en curso si es lo bastante larga (con data_lock tomado)."""
        burst, self.burst = self.burst, None
        if burst is None:
            return
        secs = burst["last"] - burst["start"]
        if burst["chars"] < BURST_MIN_CHARS or secs < BURST_MIN_SECS:
            return
        wpm = burst["chars"] / 5 / (secs / 60)
        if wpm > 250:                       # teclas mantenidas o pulsadas sin escribir
            return
        day = self.typing.setdefault(day_key(burst["start"]),
                                     {"chars": 0, "secs": 0, "backs": 0, "best": 0, "bursts": 0})
        day["chars"] += burst["chars"]
        day["secs"] = round(day["secs"] + secs, 1)
        day["backs"] += burst["backs"]
        day["bursts"] += 1
        if secs >= BEST_MIN_SECS:
            day["best"] = max(day["best"], round(wpm))
        self.last_burst = (round(wpm), secs, burst["start"], accuracy(burst))
        self.data_dirty = True

    def _save_activity(self):
        with self.data_lock:
            if self.burst and time.time() - self.burst["last"] > BURST_GAP:
                self._close_burst()
            if not self.data_dirty:
                return
            oldest = day_key(time.time() - ACTIVITY_KEEP * DAY)
            self.usage = {k: v for k, v in self.usage.items() if k >= oldest}
            self.keys = {k: v for k, v in self.keys.items() if k >= oldest}
            self.typing = {k: v for k, v in self.typing.items() if k >= oldest}
            usage, keys = json.dumps(self.usage), json.dumps(self.keys)
            typing = json.dumps(self.typing)
            self.data_dirty = False
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(USAGE_FILE, "w", encoding="utf-8") as f:
            f.write(usage)
        if CFG["heatmap"]:
            with open(KEYS_FILE, "w", encoding="utf-8") as f:
                f.write(keys)
        with open(TYPING_FILE, "w", encoding="utf-8") as f:
            f.write(typing)

    def poll_interval(self):
        """En reposo se consulta menos; una pulsacion despierta la consulta al momento."""
        idle = (self.raw_input and not self.charging
                and time.time() - self.last_activity > IDLE_AFTER)
        return max(CFG["poll"], IDLE_POLL) if idle else CFG["poll"]

    def _loop(self):
        while True:
            try:
                self.last_poll = time.time()
                self._poll()
                self._save_activity()
            except Exception:
                log_error()
            self.wake.wait(self.poll_interval())
            self.wake.clear()

    # --- versiones nuevas -------------------------------------------------------
    @staticmethod
    def _get(url, timeout=15):
        return urllib.request.urlopen(urllib.request.Request(
            url, headers={"User-Agent": APP_NAME}), timeout=timeout)

    def check_update(self, manual=False):
        """Busca una version mas nueva en GitHub. manual: informa tambien si no la hay."""
        try:
            with self._get(f"https://api.github.com/repos/{REPO}/releases/latest") as response:
                release = json.load(response)
        except Exception:
            if manual:
                self.update_status.emit(tr("check_failed"))
            return
        tag = release.get("tag_name", "")
        latest = tuple(int(n) for n in re.findall(r"\d+", tag)[:3])
        if latest > tuple(int(n) for n in APP_VERSION.split(".")):
            known = self.update is not None and self.update[0] == tag
            self.update = (tag, release.get("html_url", f"https://github.com/{REPO}"), release)
            if not known:
                self.update_found.emit(tag, self.update[1])
                if CFG["auto_update"] and FROZEN and not manual:
                    self.install_update()
        elif manual:
            self.update_status.emit(tr("up_to_date"))

    def install_update(self, launch=True):
        """Descarga el instalador, comprueba su huella SHA-256 y lo ejecuta en silencio.

        El instalador cierra la app, la reemplaza y la vuelve a abrir (/AUTOUPDATE).
        """
        if not FROZEN and launch:
            self.update_status.emit(tr("update_source"))
            return
        threading.Thread(target=self._install_update, args=(launch,), daemon=True).start()

    def _install_update(self, launch):
        tag, _, release = self.update
        try:
            assets = {a["name"]: a["browser_download_url"] for a in release.get("assets", [])}
            if SETUP_NAME not in assets:
                raise ValueError(tr("update_no_asset"))
            if SUMS_NAME not in assets:
                raise ValueError(tr("update_no_hash"))
            with self._get(assets[SUMS_NAME]) as response:
                sums = dict(reversed(line.split()) for line in
                            response.read().decode().splitlines() if len(line.split()) == 2)
            expected = sums.get(SETUP_NAME, "").lower()
            if not expected:
                raise ValueError(tr("update_no_hash"))
            self.update_status.emit(tr("downloading", tag))
            path = os.path.join(tempfile.gettempdir(), f"KooruiKeyboardSetup-{tag}.exe")
            digest = hashlib.sha256()
            with self._get(assets[SETUP_NAME], timeout=60) as response, open(path, "wb") as f:
                while chunk := response.read(1 << 16):
                    digest.update(chunk)
                    f.write(chunk)
            if digest.hexdigest() != expected:
                os.remove(path)
                raise ValueError(tr("update_bad_hash"))
            self.update_status.emit(tr("installing", tag))
            if launch:
                subprocess.Popen([path, "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
                                  "/AUTOUPDATE"], close_fds=True,
                                 creationflags=subprocess.DETACHED_PROCESS
                                 | subprocess.CREATE_NEW_PROCESS_GROUP)
        except Exception as error:
            self.update_status.emit(tr("update_failed", error))
            self.message.emit(tr("update_failed", error))

    def _update_loop(self):
        time.sleep(60)                      # sin prisa al arrancar Windows
        while True:
            if CFG["check_updates"]:
                self.check_update()
            time.sleep(UPDATE_EVERY)

    # --- copia de seguridad -----------------------------------------------------
    def export_backup(self, path):
        with self.data_lock:
            usage, keys, typing = dict(self.usage), dict(self.keys), dict(self.typing)
        data = {"app": APP_NAME, "version": APP_VERSION, "created": time.time(),
                "settings": dict(CFG), "profiles": self.profiles, "modes": self.modes,
                "history": list(self.history), "usage": usage, "keys": keys, "typing": typing}
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)

    def import_backup(self, path):
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or data.get("app") != APP_NAME:
            raise ValueError(tr("backup_invalid"))
        CFG.update({k: v for k, v in data.get("settings", {}).items() if k in DEFAULTS})
        save_json(SETTINGS_FILE, CFG)
        self.profiles = list(data.get("profiles", []))
        save_json(PROFILES_FILE, self.profiles)
        self.modes = dict(data.get("modes", {}))
        save_json(MODES_FILE, self.modes)
        self.history = list(data.get("history", []))
        save_json(HISTORY_FILE, self.history)
        with self.data_lock:
            self.usage = dict(data.get("usage", {}))
            self.keys = dict(data.get("keys", {}))
            self.typing = dict(data.get("typing", {}))
            self.data_dirty = True
        self._save_activity()
        self.settings_changed.emit()
        self.profiles_changed.emit()
        self.changed.emit()


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
        if m.page is None:              # sin luces (Bluetooth o sin datos): miniatura apagada
            effect = ""
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


def section_label(text):
    label = QLabel(text)
    label.setStyleSheet(f"color:{MUTED}; font:600 10px 'Segoe UI'; letter-spacing:1px;"
                        "margin-top:4px;")
    return label


def body_label(text, color=TEXT):
    label = QLabel(text)
    label.setWordWrap(True)
    label.setStyleSheet(f"color:{color}; font:12px 'Segoe UI';")
    return label


class AutonomyRow(QWidget):
    """Una fila del informe: brillo, barra de autonomia y valores."""

    def __init__(self, label, life, longest, detail):
        super().__init__()
        self.label, self.life, self.longest, self.detail = label, life, longest, detail
        self.setFixedHeight(26)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setFont(ui_font(12))
        p.setPen(QColor(TEXT))
        p.drawText(QRectF(0, 0, 100, self.height()), Qt.AlignmentFlag.AlignVCenter, self.label)
        bar = QRectF(104, 9, self.width() - 104 - 150, 8)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(BORDER))
        p.drawRoundedRect(bar, 4, 4)
        if self.life:
            fill = QRectF(bar)
            fill.setWidth(max(8.0, bar.width() * self.life / self.longest))
            p.setBrush(QColor(GREEN))
            p.drawRoundedRect(fill, 4, 4)
        p.setPen(QColor(TEXT if self.life else MUTED))
        p.setFont(ui_font(11))
        p.drawText(QRectF(self.width() - 142, 0, 142, self.height()),
                   Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, self.detail)


class UsageBars(QWidget):
    """Minutos de uso medios por hora del dia."""

    def __init__(self, hours):
        super().__init__()
        self.hours = hours
        self.setFixedHeight(64)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        top = max(self.hours) or 1
        width = self.width() / 24
        plot = self.height() - 16
        p.setPen(Qt.PenStyle.NoPen)
        for h, minutes in enumerate(self.hours):
            height = max(2.0, plot * minutes / top) if minutes else 2.0
            p.setBrush(QColor(ACCENT if minutes else BORDER))
            p.drawRoundedRect(QRectF(h * width + 1.5, plot - height, width - 3, height), 2, 2)
        p.setPen(QColor(MUTED))
        p.setFont(ui_font(10))
        for h in (0, 6, 12, 18):
            p.drawText(QRectF(h * width, plot + 2, 40, 14), Qt.AlignmentFlag.AlignLeft, f"{h}h")


def heat_color(t):
    """Azul (poco) a rojo (mucho)."""
    if t <= 0:
        return QColor(48, 52, 62)
    return QColor.fromHsvF(0.66 * (1 - t), 0.85, 0.45 + 0.55 * t)


class HeatmapView(QWidget):
    """Teclado en miniatura coloreado segun cuantas veces se pulsa cada tecla."""

    def __init__(self, totals):
        super().__init__()
        self.totals = totals
        self.setFixedHeight(118)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(QPen(QColor(BORDER), 1))
        p.setBrush(QColor("#0f1115"))
        p.drawRoundedRect(QRectF(0.5, 0.5, self.width() - 1, self.height() - 1), 10, 10)
        top = max(self.totals) or 1
        pad = 8
        unit = (self.width() - 2 * pad) / 16
        row_h = (self.height() - 2 * pad) / len(LAYOUT)
        p.setPen(Qt.PenStyle.NoPen)
        i = 0
        for r, widths in enumerate(LAYOUT):
            x = 0.0
            for w in widths:
                p.setBrush(heat_color((self.totals[i] / top) ** 0.5))
                p.drawRoundedRect(QRectF(pad + x * unit + 1.2, pad + r * row_h + 1.2,
                                         w * unit - 2.4, row_h - 2.4), 3, 3)
                x += w
                i += 1


class DayBars(QWidget):
    """Barras por dia con la inicial del dia de la semana debajo."""

    def __init__(self, values, days):
        super().__init__()
        self.values, self.days = values, days
        self.setFixedHeight(64)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        top = max(self.values) or 1
        width = self.width() / len(self.values)
        plot = self.height() - 16
        names = WEEKDAYS[lang_index()]
        for n, (value, day) in enumerate(zip(self.values, self.days)):
            height = max(2.0, plot * value / top)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(ACCENT if value else BORDER))
            p.drawRoundedRect(QRectF(n * width + 2, plot - height, width - 4, height), 2, 2)
            weekday = time.strptime(day, "%Y-%m-%d").tm_wday
            p.setPen(QColor(MUTED))
            p.setFont(ui_font(9))
            p.drawText(QRectF(n * width, plot + 2, width, 14), Qt.AlignmentFlag.AlignHCenter,
                       names[weekday][0].upper())


class StatTile(QLabel):
    def __init__(self, value, caption):
        super().__init__(f"<div style='font-size:17px;font-weight:600;color:{TEXT}'>{value}</div>"
                         f"<div style='font-size:11px;color:{MUTED}'>{caption}</div>")
        self.setStyleSheet(f"background:{CARD}; border:1px solid {BORDER}; border-radius:10px;"
                           "padding:8px 10px; font-family:'Segoe UI';")


class ReportView(QWidget):
    """Pestana de informe: prevision, autonomia segun el brillo y cargas."""

    def __init__(self, model):
        super().__init__()
        self.model = model
        self.layout_ = QVBoxLayout(self)
        self.layout_.setContentsMargins(0, 0, 0, 0)
        self.body = None

    def refresh(self):
        if self.body is not None:
            self.layout_.removeWidget(self.body)
            self.body.setParent(None)       # fuera de la vista ya, no al borrarse
            self.body.deleteLater()
        self.body = QWidget()
        box = QVBoxLayout(self.body)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(8)
        history = list(self.model.history)
        self.add_forecast(box)
        self.add_autonomy(box, history)
        self.add_wear(box, history)
        self.add_charges(box, history)
        self.add_usage(box)
        self.add_heatmap(box)
        self.add_export(box)
        box.addStretch()
        self.layout_.addWidget(self.body)

    def add_wear(self, box, history):
        box.addWidget(section_label(tr("rep_wear")))
        bright, months = wear_by_month(history)
        if len(months) < 2:
            box.addWidget(body_label(tr("rep_wear_wait"), MUTED))
            return
        box.addWidget(body_label(tr("rep_wear_rows", bright), MUTED))
        first = months[0][1]
        longest = max(life for _, life in months)
        for month, life in months[-6:]:
            change = (life - first) / first * 100
            detail = f"~{hours_text(life * HOUR)}" + (f" · {change:+.0f}%" if month != months[0][0]
                                                      else "")
            box.addWidget(AutonomyRow(month_label(month), life, longest, detail))

    def add_usage(self, box):
        box.addWidget(section_label(tr("rep_usage")))
        with self.model.data_lock:
            stats = usage_stats(self.model.usage)
        if not stats["days"]:
            box.addWidget(body_label(tr("rep_usage_none"), MUTED))
            return
        box.addWidget(body_label(tr("rep_usage_today", hours_text(stats["today"] * 60),
                                    hours_text(stats["daily"] * 60))))
        box.addWidget(body_label(tr("rep_usage_hours"), MUTED))
        box.addWidget(UsageBars(stats["hours"]))

    def add_heatmap(self, box):
        box.addWidget(section_label(tr("rep_heat", HEAT_DAYS)))
        if not CFG["heatmap"]:
            box.addWidget(body_label(tr("rep_heat_off"), MUTED))
            return
        with self.model.data_lock:
            stats = key_stats(self.model.keys)
        if not any(stats["totals"]):
            box.addWidget(body_label(tr("rep_heat_empty"), MUTED))
            return
        box.addWidget(HeatmapView(stats["totals"]))
        top = ", ".join(f"{name} {short_count(n)}" for name, n in top_keys(stats["totals"]))
        box.addWidget(body_label(tr("rep_heat_top", top)))
        box.addWidget(body_label(tr("rep_heat_total", short_count(stats["today"]),
                                    short_count(stats["daily"])), MUTED))

    def add_export(self, box):
        row = QHBoxLayout()
        for key, action in (("rep_export_csv", "export_csv"), ("rep_export_html", "export_html")):
            button = QPushButton(tr(key))
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(lambda _, a=action: getattr(self.window(), a)())
            row.addWidget(button)
        box.addSpacing(6)
        box.addLayout(row)

    def add_forecast(self, box):
        m = self.model
        box.addWidget(section_label(tr("rep_forecast")))
        rate = m.discharge_rate()
        if m.charging:
            box.addWidget(body_label(tr("rep_charging_now")))
        elif not rate:
            box.addWidget(body_label(tr("rep_no_forecast"), MUTED))
        else:
            box.addWidget(body_label(tr("rep_left", "~" + human(m.percent / rate))))
            low = CFG["low"]
            when = time.time() + max(0, m.percent - low) / rate
            days = (time.localtime(when).tm_yday - time.localtime().tm_yday) % 366
            if days == 0:
                day = tr("today")
            elif days == 1:
                day = tr("tomorrow")
            else:
                day = WEEKDAYS_LONG[lang_index()][time.localtime(when).tm_wday]
            box.addWidget(body_label(tr("rep_next", day, low), MUTED))

    def add_autonomy(self, box, history):
        box.addWidget(section_label(tr("rep_autonomy")))
        groups = autonomy_by_brightness(history)
        if not groups:
            box.addWidget(body_label(tr("rep_no_usage"), MUTED))
            return
        lives = {b: 100 * h / d for b, (h, d) in groups.items() if h >= 2 and d >= 3}
        longest = max(lives.values(), default=1)
        for bright in sorted(groups, reverse=True):
            hours, drop = groups[bright]
            label = tr("rep_bright", bright) if bright else tr("rep_off")
            if bright in lives:
                detail = tr("rep_life", hours_text(lives[bright] * HOUR),
                            f"{drop / hours:.1f}")
            else:
                detail = tr("rep_measuring", hours_text(hours * HOUR))
            box.addWidget(AutonomyRow(label, lives.get(bright), longest, detail))
        total = sum(h for h, _ in groups.values())
        box.addWidget(body_label(tr("rep_used", hours_text(total * HOUR)), MUTED))

    def add_charges(self, box, history):
        box.addWidget(section_label(tr("rep_charges")))
        stats = charge_stats(history)
        sessions = stats["sessions"]
        last = sessions[-1] if sessions else None
        if last is None:
            last_text = "-"
        elif last[4]:
            last_text = tr("rep_in_progress")
        else:
            last_text = ago(last[1])
        values = (
            (last_text, tr("rep_last")),
            (f"{stats['per_week']:.1f}" if stats["per_week"] is not None else "-",
             tr("rep_per_week")),
            (human(stats["between"]) if stats["between"] else "-", tr("rep_between")),
            ("~" + human(stats["full_time"]) if stats["full_time"] else "-",
             tr("rep_full_time")),
        )
        grid = QGridLayout()
        grid.setSpacing(6)
        for n, (value, caption) in enumerate(values):
            grid.addWidget(StatTile(value, caption), n // 2, n % 2)
        box.addLayout(grid)
        box.addWidget(section_label(tr("rep_recent")))
        if not sessions:
            box.addWidget(body_label(tr("rep_no_charges"), MUTED))
            return
        names = WEEKDAYS[lang_index()]
        for start, end, frm, to, ongoing in reversed(sessions[-5:]):
            day = time.localtime(start)
            text = (f"{names[day.tm_wday]} {day.tm_mday:02d}/{day.tm_mon:02d} "
                    f"{time.strftime('%H:%M', day)}  ·  {frm}% → {to}%  ·  ")
            text += tr("rep_in_progress") if ongoing else human(max(60, end - start))
            box.addWidget(body_label(text))


class TypingView(QWidget):
    """Pestana de escritura: palabras por minuto dia a dia y pulsaciones."""

    def __init__(self, model):
        super().__init__()
        self.model = model
        self.layout_ = QVBoxLayout(self)
        self.layout_.setContentsMargins(0, 0, 8, 0)
        self.body = None

    def refresh(self):
        if self.body is not None:
            self.layout_.removeWidget(self.body)
            self.body.setParent(None)
            self.body.deleteLater()
        self.body = QWidget()
        box = QVBoxLayout(self.body)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(8)
        if CFG["wpm"]:
            self.add_wpm(box)
        else:
            box.addWidget(body_label(tr("wpm_off"), MUTED))
            button = QPushButton(tr("wpm_enable"))
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(lambda: self.model.save_settings({"wpm": True}))
            box.addWidget(button)
        self.add_typing(box)
        box.addStretch()
        self.layout_.addWidget(self.body)

    def add_wpm(self, box):
        with self.model.data_lock:
            stats = wpm_stats(self.model.typing)
            last = self.model.last_burst
        unit = tr("wpm_unit")
        today = stats["today"]
        if not (today and burst_wpm(today)) and not stats["week"]:
            box.addWidget(body_label(tr("wpm_none"), MUTED))
            box.addWidget(body_label(tr("wpm_note"), MUTED))
            return
        if today and burst_wpm(today):
            value, when, acc, best = burst_wpm(today), tr("wpm_today"), accuracy(today), today["best"]
        else:
            value, when, acc, best = stats["week"], tr("wpm_week"), stats["week_acc"], 0
        details = [when, wpm_level(value), tr("wpm_accuracy", f"{acc:.0f}")]
        if best:
            details.append(tr("wpm_best", best, unit))
        hero = QLabel(f"<span style='font-size:44px;font-weight:600;color:{TEXT}'>{value:.0f}</span>"
                      f"<span style='font-size:16px;color:{MUTED}'> {unit}</span><br>"
                      f"<span style='font-size:12px;color:{MUTED}'>{' · '.join(details)}</span>")
        hero.setStyleSheet("font-family:'Segoe UI';")
        box.addWidget(hero)

        if last:
            wpm, secs, start, acc = last
            box.addWidget(section_label(tr("wpm_last")))
            box.addWidget(body_label(tr("wpm_last_text", wpm, unit, hours_text(secs)
                                        if secs >= 60 else f"{secs:.0f} s", ago(start),
                                        f"{acc:.0f}")))

        record, record_day = stats["record"]
        tiles = (
            (f"{stats['week']:.0f} {unit}" if stats["week"] else "-", tr("wpm_avg7")),
            (f"{record} {unit}" if record else "-", tr("wpm_record") + (
                f" · {record_day[8:10]}/{record_day[5:7]}" if record_day and record else "")),
            (f"{stats['week_acc']:.0f}%" if stats["week"] else "-", tr("wpm_acc7")),
            (f"{stats['trend']:+.0f}%" if stats["trend"] is not None else "-", tr("wpm_trend")),
        )
        grid = QGridLayout()
        grid.setSpacing(6)
        for n, (text, caption) in enumerate(tiles):
            grid.addWidget(StatTile(text, caption), n // 2, n % 2)
        box.addLayout(grid)
        box.addWidget(section_label(tr("wpm_days")))
        box.addWidget(DayBars(stats["per_day"], stats["days"]))
        box.addWidget(body_label(tr("wpm_note"), MUTED))

    def add_typing(self, box):
        box.addWidget(section_label(tr("wpm_presses")))
        if not CFG["heatmap"]:
            box.addWidget(body_label(tr("rep_typing_off"), MUTED))
            return
        with self.model.data_lock:
            stats = typing_stats(self.model.keys, self.model.usage)
        if not any(stats["per_day"]):
            box.addWidget(body_label(tr("rep_heat_empty"), MUTED))
            return
        box.addWidget(body_label(tr("rep_typing_avg", short_count(stats["avg"]),
                                    f"{stats['rate']:.0f}")))
        if stats["trend"] is not None:
            box.addWidget(body_label(tr("rep_typing_trend", f"{stats['trend']:+.0f}%"), MUTED))
        box.addWidget(body_label(tr("rep_typing_days"), MUTED))
        box.addWidget(DayBars(stats["per_day"], stats["days"]))



def export_html(path, model):
    """Informe autocontenido con los mismos datos que la pestana de informe."""
    esc = html.escape
    history = list(model.history)
    parts = [f"<h1>{esc(tr('html_title'))}</h1>",
             f"<p class=muted>{esc(tr('html_generated', time.strftime('%Y-%m-%d %H:%M')))}"
             f" · v{APP_VERSION}</p>",
             f"<p><b>{esc(tr('battery'))}:</b> {esc(model.battery_text())}</p>"]

    def table(rows, header=None):
        out = "<table>"
        if header:
            out += "<tr>" + "".join(f"<th>{esc(str(h))}</th>" for h in header) + "</tr>"
        for row in rows:
            out += "<tr>" + "".join(f"<td>{esc(str(c))}</td>" for c in row) + "</tr>"
        return out + "</table>"

    groups = autonomy_by_brightness(history)
    parts.append(f"<h2>{esc(tr('rep_autonomy'))}</h2>")
    if groups:
        rows = []
        for bright in sorted(groups, reverse=True):
            hours, drop = groups[bright]
            label = tr("rep_bright", bright) if bright else tr("rep_off")
            if hours >= 2 and drop >= 3:
                rows.append((label, tr("rep_life", hours_text(100 * hours / drop * HOUR),
                                       f"{drop / hours:.1f}")))
            else:
                rows.append((label, tr("rep_measuring", hours_text(hours * HOUR))))
        parts.append(table(rows))
    else:
        parts.append(f"<p class=muted>{esc(tr('rep_no_usage'))}</p>")

    bright, months = wear_by_month(history)
    parts.append(f"<h2>{esc(tr('rep_wear'))}</h2>")
    if len(months) >= 2:
        parts.append(f"<p class=muted>{esc(tr('rep_wear_rows', bright))}</p>")
        parts.append(table([(month_label(m), "~" + hours_text(life * HOUR)) for m, life in months]))
    else:
        parts.append(f"<p class=muted>{esc(tr('rep_wear_wait'))}</p>")

    stats = charge_stats(history)
    parts.append(f"<h2>{esc(tr('rep_charges'))}</h2>")
    parts.append(table([
        (tr("rep_per_week"), f"{stats['per_week']:.1f}" if stats["per_week"] is not None else "-"),
        (tr("rep_between"), human(stats["between"]) if stats["between"] else "-"),
        (tr("rep_full_time"), "~" + human(stats["full_time"]) if stats["full_time"] else "-"),
    ]))
    if stats["sessions"]:
        parts.append(table([(time.strftime("%Y-%m-%d %H:%M", time.localtime(a)), f"{f}%", f"{t}%",
                             tr("rep_in_progress") if o else human(max(60, b - a)))
                            for a, b, f, t, o in reversed(stats["sessions"])]))

    with model.data_lock:
        usage = usage_stats(model.usage)
        keys = key_stats(model.keys)
    parts.append(f"<h2>{esc(tr('rep_usage'))}</h2>")
    if usage["days"]:
        parts.append(f"<p>{esc(tr('rep_usage_today', hours_text(usage['today'] * 60), hours_text(usage['daily'] * 60)))}</p>")
        top = max(usage["hours"]) or 1
        bars = "".join(f"<div class=bar title='{h}h: {m:.0f} min'><span style='height:{m / top * 100:.0f}%'>"
                       f"</span><i>{h}</i></div>" for h, m in enumerate(usage["hours"]))
        parts.append(f"<p class=muted>{esc(tr('rep_usage_hours'))}</p><div class=bars>{bars}</div>")
    else:
        parts.append(f"<p class=muted>{esc(tr('rep_usage_none'))}</p>")

    parts.append(f"<h2>{esc(tr('rep_heat', HEAT_DAYS))}</h2>")
    if CFG["heatmap"] and any(keys["totals"]):
        parts.append(table([(name, n) for name, n in top_keys(keys["totals"], 15)]))
    else:
        parts.append(f"<p class=muted>{esc(tr('rep_heat_off' if not CFG['heatmap'] else 'rep_heat_empty'))}</p>")
    with model.data_lock:
        speed = wpm_stats(model.typing)
    parts.append(f"<h2>{esc(tr('tab_typing').upper())}</h2>")
    if CFG["wpm"] and speed["week"]:
        record, record_day = speed["record"]
        parts.append(table([
            (tr("wpm_avg7"), f"{speed['week']:.0f} {tr('wpm_unit')} · {wpm_level(speed['week'])}"),
            (tr("wpm_acc7"), f"{speed['week_acc']:.0f}%"),
            (tr("wpm_record"), f"{record} {tr('wpm_unit')} ({record_day})" if record else "-"),
        ]))
        parts.append(table([(d, f"{v} {tr('wpm_unit')}") for d, v in
                            zip(speed["days"], speed["per_day"]) if v]))
    else:
        parts.append(f"<p class=muted>{esc(tr('wpm_off' if not CFG['wpm'] else 'wpm_none'))}</p>")
    parts.append(f"<p class=muted>{esc(tr('html_history'))}</p>")

    style = ("body{font-family:'Segoe UI',sans-serif;max-width:760px;margin:32px auto;padding:0 16px;"
             "color:#1d2027;background:#fff}h1{font-size:24px}h2{font-size:15px;margin-top:28px;"
             "letter-spacing:.5px;color:#4a5060}table{border-collapse:collapse;width:100%}"
             "td,th{border-bottom:1px solid #e3e5ea;padding:6px 8px;text-align:left;font-size:14px}"
             ".muted{color:#6b7180;font-size:13px}.bars{display:flex;gap:3px;height:90px;"
             "align-items:flex-end}.bar{flex:1;height:100%;display:flex;flex-direction:column;"
             "justify-content:flex-end;align-items:center}.bar span{display:block;width:100%;"
             "background:#4da3ff;border-radius:2px;min-height:2px}.bar i{font-size:9px;"
             "color:#6b7180;font-style:normal}")
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"<!doctype html><html lang='{CFG['lang']}'><meta charset='utf-8'>"
                f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
                f"<title>{esc(tr('html_title'))}</title><style>{style}</style><body>"
                + "".join(parts) + "</body></html>")


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


def no_wheel(widget):
    """La rueda del raton solo cambia el valor si el control tiene el foco; si no, desplaza."""
    widget.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
    original = widget.wheelEvent

    def wheel(event):
        if widget.hasFocus():
            original(event)
        else:
            event.ignore()
    widget.wheelEvent = wheel
    return widget


def scroll_area(widget):
    area = QScrollArea()
    area.setWidget(widget)
    area.setWidgetResizable(True)
    area.setFrameShape(QScrollArea.Shape.NoFrame)
    area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    area.setStyleSheet(
        "QScrollArea,QScrollArea>QWidget>QWidget{background:transparent;}"
        "QScrollBar:vertical{width:6px;background:transparent;margin:0;}"
        f"QScrollBar::handle:vertical{{background:{BORDER};border-radius:3px;min-height:30px;}}"
        "QScrollBar::add-line,QScrollBar::sub-line{height:0;}")
    return area


class SettingsView(QWidget):
    """Pestana de ajustes; cada cambio se guarda al momento."""

    def __init__(self, panel):
        super().__init__()
        self.panel, self.model = panel, panel.model
        self.layout_ = QVBoxLayout(self)
        self.layout_.setContentsMargins(0, 0, 8, 0)
        self.body = None
        self.fields = {}
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(400)
        self.timer.timeout.connect(self.commit)
        self.build()

    # --- controles --------------------------------------------------------
    def spin(self, key, low, high, suffix):
        box = no_wheel(QSpinBox())
        box.setRange(low, high)
        box.setValue(CFG[key])
        box.setSuffix(suffix)
        box.valueChanged.connect(self.changed)
        self.fields[key] = box
        return box

    def clock(self, key):
        edit = no_wheel(QTimeEdit(QTime.fromString(CFG[key], "HH:mm")))
        edit.setDisplayFormat("HH:mm")
        edit.timeChanged.connect(self.changed)
        self.fields[key] = edit
        return edit

    def check(self, box, key):
        check = QCheckBox(tr(key))
        check.setChecked(CFG[key])
        check.toggled.connect(self.changed)
        self.fields[key] = check
        box.addWidget(check)

    @staticmethod
    def row(box, key, *widgets):
        line = QHBoxLayout()
        line.addWidget(body_label(tr(key)), 1)
        for widget in widgets:
            line.addWidget(widget)
        box.addLayout(line)

    def build(self):
        self.timer.stop()
        if self.body is not None:
            self.layout_.removeWidget(self.body)
            self.body.setParent(None)
            self.body.deleteLater()
        self.fields = {}
        self.body = QWidget()
        box = QVBoxLayout(self.body)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(8)

        box.addWidget(section_label(tr("set_general")))
        lang = no_wheel(QComboBox())
        lang.addItem("Español", "es")
        lang.addItem("English", "en")
        lang.setCurrentIndex(lang_index())
        lang.currentIndexChanged.connect(self.commit)
        self.fields["lang"] = lang
        self.row(box, "language", lang)
        self.row(box, "poll", self.spin("poll", 10, 600, " s"))

        box.addWidget(section_label(tr("set_battery")))
        self.row(box, "low", self.spin("low", 5, 50, " %"))
        self.row(box, "critical", self.spin("critical", 0, 50, " %"))
        self.check(box, "full_notify")
        self.row(box, "full_level", self.spin("full_level", 80, 100, " %"))
        self.check(box, "forecast_notify")

        box.addWidget(section_label(tr("set_lights")))
        self.check(box, "lock_off")
        self.check(box, "sched_on")
        self.row(box, "sched_range", self.clock("sched_from"), QLabel("-"),
                 self.clock("sched_to"))

        box.addWidget(section_label(tr("set_use")))
        self.row(box, "break_minutes", self.spin("break_minutes", 0, 240, " min"))
        self.check(box, "heatmap")
        self.check(box, "wpm")

        box.addWidget(section_label(tr("set_updates")))
        self.check(box, "check_updates")
        self.check(box, "auto_update")

        box.addWidget(section_label(tr("backup").upper()))
        line = QHBoxLayout()
        for key in ("backup_export", "backup_import"):
            button = QPushButton(tr(key))
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(lambda _, k=key: getattr(self.panel, k)())
            line.addWidget(button)
        box.addLayout(line)

        box.addSpacing(4)
        box.addWidget(body_label(tr("set_note"), MUTED))
        box.addStretch()
        self.layout_.addWidget(self.body)

    # --- guardar ----------------------------------------------------------
    def changed(self, *_):
        self.timer.start()                  # espera a que deje de tocar el control

    def values(self):
        f = self.fields
        low = f["low"].value()
        values = {"lang": f["lang"].currentData(), "low": low,
                  "critical": min(f["critical"].value(), low),
                  "sched_from": f["sched_from"].time().toString("HH:mm"),
                  "sched_to": f["sched_to"].time().toString("HH:mm")}
        for key in ("poll", "full_level", "break_minutes"):
            values[key] = f[key].value()
        for key in ("full_notify", "forecast_notify", "lock_off", "sched_on", "heatmap",
                    "wpm", "check_updates", "auto_update"):
            values[key] = f[key].isChecked()
        return values

    def commit(self):
        self.timer.stop()
        values = self.values()
        if CFG["heatmap"] and not values["heatmap"]:
            answer = self.panel.modal(lambda: QMessageBox.question(
                self, tr("settings"), tr("heatmap_confirm")))
            if answer != QMessageBox.StandardButton.Yes:
                heatmap = self.fields["heatmap"]
                heatmap.blockSignals(True)
                heatmap.setChecked(True)
                heatmap.blockSignals(False)
                values["heatmap"] = True
        if all(CFG[k] == v for k, v in values.items()):
            return
        relabel = values["lang"] != CFG["lang"]
        self.model.save_settings(values)
        if relabel:                         # textos en el otro idioma
            QTimer.singleShot(0, self.build)


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

        root.addWidget(BatteryView(model))

        tabs = QHBoxLayout()
        tabs.setSpacing(4)
        self.tab_buttons = []
        for index in range(4):
            button = QPushButton("⚙" if index == 3 else "")
            button.setCheckable(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setStyleSheet(
                f"QPushButton{{background:transparent;border:none;color:{MUTED};"
                "border-bottom:2px solid transparent;border-radius:0;padding:4px 10px;"
                f"font:600 {15 if index == 3 else 12}px 'Segoe UI';}}"
                f"QPushButton:checked{{color:{TEXT};border-bottom:2px solid {ACCENT};}}"
                f"QPushButton:hover{{color:{TEXT};}}")
            button.clicked.connect(lambda _, n=index: self.show_tab(n))
            self.tab_buttons.append(button)
            tabs.addWidget(button)
        tabs.addStretch()
        root.addLayout(tabs)

        main = QWidget()
        body = QVBoxLayout(main)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(8)
        body.addWidget(self.section("history"))
        self.history = HistoryView(model)
        body.addWidget(self.history)

        self.keyboard = KeyboardView(model)
        body.addWidget(self.keyboard)

        body.addWidget(self.section("profiles"))
        self.profile_grid = QGridLayout()
        self.profile_grid.setSpacing(6)
        body.addLayout(self.profile_grid)

        body.addWidget(self.section("light_mode"))
        grid = QGridLayout()
        grid.setSpacing(6)
        self.tiles = []
        for n in range(MODE_COUNT):
            tile = ModeTile(model, n + 1, self.edit_mode)
            self.tiles.append(tile)
            grid.addWidget(tile, n // 6, n % 6)
        body.addLayout(grid)

        body.addWidget(self.section("color"))
        row = QHBoxLayout()
        row.setSpacing(4)
        self.swatches = [Swatch(model, (es, en), rgb) for es, en, rgb in COLORS]
        self.swatches.append(Swatch(model, "multicolor", kind="multi"))
        self.swatches.append(Swatch(model, "other_color", kind="custom"))
        for s in self.swatches:
            row.addWidget(s)
        row.addStretch()
        body.addLayout(row)

        self.bright = self.slider_row(body, "brightness", OFF_BRIGHT)
        self.speed = self.slider_row(body, "speed", OFF_SPEED)

        self.report = ReportView(model)
        self.report_scroll = scroll_area(self.report)
        self.typing = TypingView(model)
        self.typing_scroll = scroll_area(self.typing)
        self.settings = SettingsView(self)
        self.stack = QStackedWidget()
        self.stack.addWidget(main)
        self.stack.addWidget(self.report_scroll)
        self.stack.addWidget(self.typing_scroll)
        self.stack.addWidget(scroll_area(self.settings))
        root.addWidget(self.stack)
        self.show_tab(0)

        footer = QHBoxLayout()
        version = QLabel(f"Koorui Keyboard v{APP_VERSION}")
        version.setStyleSheet(f"color:{MUTED}; font:11px 'Segoe UI';")
        self.update_button = QPushButton()
        self.update_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.update_button.setStyleSheet(
            f"QPushButton{{background:transparent;border:none;color:{ACCENT};padding:0;"
            "font:11px 'Segoe UI';} QPushButton:hover{text-decoration:underline;}"
            f"QPushButton:disabled{{color:{MUTED};}}")
        self.update_button.clicked.connect(self.update_clicked)
        footer.addWidget(version)
        footer.addStretch()
        footer.addWidget(self.update_button)
        root.addLayout(footer)
        model.update_status.connect(self.show_update_status)
        model.update_found.connect(lambda *_: self.sync_update())

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
        for button, key in zip(self.tab_buttons, ("tab_panel", "tab_report", "tab_typing")):
            button.setText(tr(key))
        self.tab_buttons[3].setToolTip(tr("settings"))
        self.sync_update()
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
        pages = {1: (self.report, self.report_scroll), 2: (self.typing, self.typing_scroll)}
        if self.stack.currentIndex() in pages:
            page, area = pages[self.stack.currentIndex()]
            bar = area.verticalScrollBar()
            position = bar.value()
            page.refresh()
            QTimer.singleShot(0, lambda: bar.setValue(position))
        self.update()

    def show_tab(self, index):
        self.stack.setCurrentIndex(index)
        for n, button in enumerate(self.tab_buttons):
            button.setChecked(n == index)
        if index == 1:
            self.report.refresh()
        elif index == 2:
            self.typing.refresh()
        elif index == 3:
            self.settings.build()           # valores al dia (por ejemplo tras importar)

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

    def sync_update(self):
        update = self.model.update
        self.update_button.setEnabled(True)
        if update:
            self.update_button.setText(tr("update_to", update[0]))
            self.update_button.setStyleSheet(self.update_button.styleSheet().replace(
                "font:11px", "font:600 11px"))
        else:
            self.update_button.setText(tr("check_now"))

    def update_clicked(self):
        self.update_button.setEnabled(False)
        if self.model.update:
            self.model.install_update()
        else:
            self.update_button.setText(tr("checking"))
            threading.Thread(target=self.model.check_update, args=(True,), daemon=True).start()

    def show_update_status(self, text):
        self.update_button.setText(text)
        if not text.endswith("…"):          # resultado final: volver al estado normal luego
            QTimer.singleShot(5000, self.sync_update)

    def backup_export(self):
        folder = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DocumentsLocation)
        path, _ = self.modal(lambda: QFileDialog.getSaveFileName(
            self, tr("backup"), os.path.join(folder, f"koorui-backup-{day_key(time.time())}.json"),
            "JSON (*.json)"))
        if path:
            self.model.export_backup(path)
            self.model.message.emit(tr("msg_backup_saved", path))

    def backup_import(self):
        folder = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DocumentsLocation)
        path, _ = self.modal(lambda: QFileDialog.getOpenFileName(
            self, tr("backup"), folder, "JSON (*.json)"))
        if not path:
            return
        answer = self.modal(lambda: QMessageBox.question(self, tr("backup"), tr("backup_confirm")))
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self.model.import_backup(path)
        except Exception as error:
            self.modal(lambda: QMessageBox.warning(self, tr("backup"), str(error)))
            return
        self.model.message.emit(tr("msg_backup_loaded"))
        QTimer.singleShot(0, self.settings.build)   # mostrar los ajustes importados

    def save_path(self, name, pattern):
        folder = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DocumentsLocation)
        path, _ = self.modal(lambda: QFileDialog.getSaveFileName(
            self, tr("tab_report"), os.path.join(folder, name), pattern))
        return path

    def export_csv(self):
        path = self.save_path(f"koorui-{day_key(time.time())}.csv", "CSV (*.csv)")
        if path:
            export_csv(path, list(self.model.history))
            self.model.message.emit(tr("msg_exported", path))

    def export_html(self):
        path = self.save_path(f"koorui-{day_key(time.time())}.html", "HTML (*.html)")
        if path:
            export_html(path, self.model)
            self.model.message.emit(tr("msg_exported", path))
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def open_settings(self):
        if not self.isVisible():
            self.popup()
        self.show_tab(3)

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
        model.update_found.connect(self.on_update)
        self.messageClicked.connect(lambda: self.model.update and self.panel.popup())
        self.build_menu()
        self.sync()

    def build_menu(self):
        menu = self.menu
        menu.clear()
        if self.model.update:
            menu.addAction(tr("download_update", self.model.update[0]), self.open_update)
            menu.addSeparator()
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

    def on_update(self, version, _):
        self.build_menu()
        self.showMessage(tr("kb_title"), tr("msg_update", version))

    def open_update(self):
        if not self.model.update:
            return
        if FROZEN:
            self.panel.popup()
            self.panel.update_clicked()
        else:
            QDesktopServices.openUrl(QUrl(self.model.update[1]))

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


class RAWINPUTDEVICE(ctypes.Structure):
    _fields_ = [("usUsagePage", wintypes.USHORT), ("usUsage", wintypes.USHORT),
                ("dwFlags", wintypes.DWORD), ("hwndTarget", wintypes.HWND)]


class RAWINPUTHEADER(ctypes.Structure):
    _fields_ = [("dwType", wintypes.DWORD), ("dwSize", wintypes.DWORD),
                ("hDevice", wintypes.HANDLE), ("wParam", wintypes.WPARAM)]


class RAWKEYBOARD(ctypes.Structure):
    _fields_ = [("MakeCode", wintypes.USHORT), ("Flags", wintypes.USHORT),
                ("Reserved", wintypes.USHORT), ("VKey", wintypes.USHORT),
                ("Message", wintypes.UINT), ("ExtraInformation", wintypes.ULONG)]


class RAWINPUTKEYBOARD(ctypes.Structure):
    _fields_ = [("header", RAWINPUTHEADER), ("keyboard", RAWKEYBOARD)]


user32 = ctypes.windll.user32
user32.GetRawInputData.argtypes = [wintypes.HANDLE, wintypes.UINT, ctypes.c_void_p,
                                   ctypes.POINTER(wintypes.UINT), wintypes.UINT]
user32.GetRawInputData.restype = wintypes.UINT
user32.GetRawInputDeviceInfoW.argtypes = [wintypes.HANDLE, wintypes.UINT, ctypes.c_void_p,
                                          ctypes.POINTER(wintypes.UINT)]
user32.GetRawInputDeviceInfoW.restype = wintypes.UINT


def is_koorui_device(name):
    name = name.upper()
    return f"VID_{VID:04X}" in name or f"VID&02{BT_VID:04X}_PID&{BT_PID:04X}" in name


class NativeWatcher(QAbstractNativeEventFilter):
    """Mensajes de Windows: bloqueo de sesion y pulsaciones de este teclado (Raw Input).

    De cada pulsacion solo se usa que teclado la envio y, si el mapa de calor esta activo,
    que tecla fue, para sumar uno a su contador. No se guarda lo que se escribe.
    """

    WM_WTSSESSION_CHANGE, WTS_SESSION_LOCK, WTS_SESSION_UNLOCK = 0x02B1, 7, 8
    WM_INPUT, RID_INPUT, RIDI_DEVICENAME, RIM_TYPEKEYBOARD = 0x00FF, 0x10000003, 0x20000007, 1
    RIDEV_INPUTSINK, RI_KEY_BREAK, RI_KEY_E0 = 0x100, 1, 2

    def __init__(self, model, hwnd):
        super().__init__()
        self.model = model
        self.devices = {}               # handle -> es este teclado
        ctypes.windll.wtsapi32.WTSRegisterSessionNotification(wintypes.HWND(hwnd), 0)
        keyboards = RAWINPUTDEVICE(0x01, 0x06, self.RIDEV_INPUTSINK, hwnd)
        user32.RegisterRawInputDevices(ctypes.byref(keyboards), 1,
                                       ctypes.sizeof(RAWINPUTDEVICE))

    def is_ours(self, handle):
        if handle not in self.devices:
            size = wintypes.UINT(0)
            user32.GetRawInputDeviceInfoW(handle, self.RIDI_DEVICENAME, None, ctypes.byref(size))
            name = ctypes.create_unicode_buffer(size.value + 1)
            user32.GetRawInputDeviceInfoW(handle, self.RIDI_DEVICENAME, name, ctypes.byref(size))
            self.devices[handle] = is_koorui_device(name.value)
        return self.devices[handle]

    def raw_input(self, lparam):
        size = wintypes.UINT(0)
        header = ctypes.sizeof(RAWINPUTHEADER)
        handle = wintypes.HANDLE(lparam)
        user32.GetRawInputData(handle, self.RID_INPUT, None, ctypes.byref(size), header)
        if size.value < ctypes.sizeof(RAWINPUTKEYBOARD):
            return
        buffer = ctypes.create_string_buffer(size.value)
        if user32.GetRawInputData(handle, self.RID_INPUT, buffer, ctypes.byref(size),
                                  header) != size.value:
            return
        data = RAWINPUTKEYBOARD.from_buffer_copy(buffer)
        if (data.header.dwType != self.RIM_TYPEKEYBOARD or not data.header.hDevice
                or not self.is_ours(data.header.hDevice)):
            return
        flags = data.keyboard.Flags
        self.model.key_event((data.keyboard.MakeCode, int(bool(flags & self.RI_KEY_E0))),
                             not flags & self.RI_KEY_BREAK)

    def nativeEventFilter(self, event_type, message):
        if bytes(event_type) == b"windows_generic_MSG":
            msg = wintypes.MSG.from_address(int(message))
            if msg.message == self.WM_INPUT:
                try:
                    self.raw_input(msg.lParam)
                except Exception:
                    log_error()
            elif msg.message == self.WM_WTSSESSION_CHANGE:
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
        f"QToolTip{{background:{CARD};color:{TEXT};border:1px solid {BORDER};}}"
        f"QPushButton:hover{{border-color:{ACCENT};}}")
    model = Model()
    panel = Panel(model)
    tray = Tray(model, panel)
    tray.show()
    watcher = NativeWatcher(model, int(panel.winId()))
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
