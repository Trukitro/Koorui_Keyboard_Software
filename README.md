# Koorui Keyboard Software

App de bandeja para Windows que muestra la batería del teclado **KOORUI 75% inalámbrico (82 teclas, con rueda)** en modo 2.4 GHz y permite cambiar sus luces desde el PC.

El panel que se abre al hacer clic en el icono:

![Panel de la app](docs/screenshot.png)

El icono en la bandeja del sistema muestra el porcentaje y cambia de color según el nivel (gris si no hay lectura):

![Iconos de la bandeja](docs/tray.png)

KOORUI no publica software para este teclado y, en modo 2.4 GHz, no hay forma de saber cuánta batería le queda. Esta app lo resuelve hablando directamente con el receptor USB mediante un protocolo obtenido por ingeniería inversa.

## Qué hace

- Muestra el porcentaje de batería como número en el icono de la bandeja (verde, naranja o rojo) y avisa al bajar del 20 %.
- Detecta cuándo el teclado está cargando: el icono pasa a azul y el panel muestra un rayo.
- Al hacer clic en el icono abre un panel con:
  - un teclado en miniatura animado con el modo y el color actuales;
  - los 18 modos de luz, cada uno con su miniatura animada;
  - selector de color (nueve predefinidos, multicolor o cualquier otro);
  - deslizadores de brillo y velocidad.
- Detecta los cambios hechos con las teclas Fn del teclado (se actualiza cada 30 segundos).
- Opción de iniciar con Windows desde el menú del clic derecho.

## Instalación

### Opción 1: ejecutable (no necesita Python)

1. Descarga `KooruiKeyboard.exe` desde la [última versión publicada](https://github.com/Trukitro/Koorui_Keyboard_Software/releases/latest).
2. Guárdalo en una carpeta fija (por ejemplo `Documentos\Koorui`) y ábrelo con doble clic.

Windows puede mostrar el aviso de SmartScreen porque el ejecutable no está firmado: pulsa "Más información" y luego "Ejecutar de todas formas".

### Opción 2: desde el código

Requiere Python 3.10 o posterior.

```bash
git clone https://github.com/Trukitro/Koorui_Keyboard_Software.git
cd Koorui_Keyboard_Software
pip install -r requirements.txt
pythonw koorui_battery.pyw
```

En ambos casos hace falta Windows 10 u 11 y el teclado conectado con su receptor 2.4 GHz, que debe identificarse como `VID 320F / PID 50C3` ("2.4G Receiver", Telink/EVision).

## Uso

- **Clic izquierdo** en el icono: abre o cierra el panel.
- **Clic derecho** en el icono: actualizar ahora, iniciar con Windows, salir.
- **Clic** en un modo: lo activa en el teclado.
- **Clic derecho** en un modo: cambia su nombre y la animación con la que se representa.

Los nombres de los modos, la última lectura de batería y el registro de errores se guardan en `%APPDATA%\KooruiBattery`.

## Compilar el ejecutable

```bash
pip install pyinstaller
pyinstaller --onefile --noconsole --name KooruiKeyboard --icon assets/icon.ico koorui_battery.pyw
```

El resultado queda en `dist\KooruiKeyboard.exe`. Si tienes PySide6 instalado además de PyQt6, añade `--exclude-module PySide6`.

## Cómo funciona

El receptor expone una colección HID propietaria (usage page `0xFF1C`, usage `0x92`, report ID 4). Los paquetes tienen 64 bytes:

```
[04][suma_lo][suma_hi][cmd][len][dir_lo][dir_hi][estado][datos...]
```

La suma es la de los bytes 3 a 63. En las respuestas, un estado `0xFF` significa "comando no soportado".

| Comando | Función |
|---|---|
| `0x01` / `0x02` | Abrir / cerrar una transacción |
| `0x1A` | Batería: porcentaje en el byte 8; el byte 9 vale 1 mientras carga |
| `0x05` | Leer configuración (máximo 24 bytes por paquete) |
| `0x06` | Escribir configuración |
| `0x1D` | Identificación del modelo (`UK6133`) |

La configuración es memoria flash organizada en páginas de 64 bytes. Escribir en la dirección 0 borra la página entera, así que la app siempre lee la página, cambia los bytes necesarios y la reescribe completa en tres trozos. Las lecturas no cruzan el límite de una página.

Página 0:

| Byte | Contenido |
|---|---|
| 1 | Modo de luz (1 a 18) |
| 2 | Brillo (0 a 4) |
| 3 | Velocidad |
| 5 | `0x00` = color fijo, `0xFF` = multicolor |
| 6–8 | Color: rojo, verde, azul |

Las teclas Fn nunca llegan al PC (las interpreta el propio teclado), por eso las luces se controlan escribiendo esta página y no simulando pulsaciones.

`snapshot.py` es la herramienta de solo lectura usada para descubrir el mapa: guarda una foto de la configuración y compara dos fotos para ver qué bytes cambian al pulsar una combinación Fn.

## Limitaciones conocidas

- Probado en un solo teclado. Otros modelos de KOORUI, u otros teclados con un receptor distinto, probablemente no funcionen.
- No hay documentación oficial del comando `0x1A`. Se comprobó que el valor sube mientras el cable está conectado y que el indicador de carga cambia al conectar y desconectar, pero falta validarlo durante una descarga completa.
- Solo está confirmado que el modo 6 es color fijo. Los nombres de los otros 17 modos vienen de la tabla habitual de los teclados EVision y pueden no coincidir; se corrigen con clic derecho.
- Las animaciones del panel son aproximaciones, no una copia exacta de los efectos del teclado.
- La app escribe en la memoria de configuración del teclado. Un bloque inválido hace que el teclado vuelva a su iluminación de fábrica. Úsala bajo tu responsabilidad.
- Solo funciona en modo 2.4 GHz; no cubre Bluetooth ni cable.

## Estructura del proyecto

| Archivo | Contenido |
|---|---|
| `koorui_battery.pyw` | La app: protocolo HID, modelo, panel e icono de bandeja |
| `snapshot.py` | Herramienta de solo lectura para investigar la configuración |
| `requirements.txt` | Dependencias |
| `assets/icon.ico` | Icono del ejecutable |

## Plan a futuro

Lo que queda por confirmar y las ideas para próximas versiones están en [ROADMAP.md](ROADMAP.md).

## Licencia

[MIT](LICENSE). Proyecto no oficial, sin relación con KOORUI.
