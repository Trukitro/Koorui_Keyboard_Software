# Koorui Keyboard Software

App de bandeja para Windows que muestra la batería del teclado **KOORUI 75% inalámbrico (82 teclas, con rueda)** en modo 2.4 GHz y permite cambiar sus luces desde el PC.

![Panel de la app](docs/screenshot.png)

KOORUI no publica software para este teclado y, en modo 2.4 GHz, no hay forma de saber cuánta batería le queda. Esta app lo resuelve hablando directamente con el receptor USB mediante un protocolo obtenido por ingeniería inversa.

## Qué hace

- Muestra el porcentaje de batería como número en el icono de la bandeja (verde, naranja o rojo) y avisa al bajar del 20 %.
- Al hacer clic en el icono abre un panel con:
  - un teclado en miniatura animado con el modo y el color actuales;
  - los 18 modos de luz, cada uno con su miniatura animada;
  - selector de color (nueve predefinidos, multicolor o cualquier otro);
  - deslizadores de brillo y velocidad.
- Detecta los cambios hechos con las teclas Fn del teclado (se actualiza cada 30 segundos).
- Opción de iniciar con Windows desde el menú del clic derecho.

## Requisitos

- Windows 10 u 11.
- Python 3.10 o posterior.
- El teclado conectado con su receptor 2.4 GHz. El receptor debe identificarse como `VID 320F / PID 50C3` ("2.4G Receiver", Telink/EVision).

## Instalación

```bash
git clone https://github.com/Trukitro/Koorui_Keyboard_Software.git
cd Koorui_Keyboard_Software
pip install -r requirements.txt
```

## Uso

Haz doble clic en `koorui_battery.pyw`, o ejecútalo sin ventana de consola:

```bash
pythonw koorui_battery.pyw
```

- **Clic izquierdo** en el icono: abre o cierra el panel.
- **Clic derecho** en el icono: actualizar ahora, iniciar con Windows, salir.
- **Clic** en un modo: lo activa en el teclado.
- **Clic derecho** en un modo: cambia su nombre y la animación con la que se representa.

Los nombres de los modos, la última lectura de batería y el registro de errores se guardan en `%APPDATA%\KooruiBattery`.

## Cómo funciona

El receptor expone una colección HID propietaria (usage page `0xFF1C`, usage `0x92`, report ID 4). Los paquetes tienen 64 bytes:

```
[04][suma_lo][suma_hi][cmd][len][dir_lo][dir_hi][estado][datos...]
```

La suma es la de los bytes 3 a 63. En las respuestas, un estado `0xFF` significa "comando no soportado".

| Comando | Función |
|---|---|
| `0x01` / `0x02` | Abrir / cerrar una transacción |
| `0x1A` | Batería: porcentaje en el byte 8 |
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
- Que el comando `0x1A` devuelve la batería es una deducción: es el único valor que se comporta como un porcentaje. No hay documentación oficial que lo confirme.
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

## Licencia

[MIT](LICENSE). Proyecto no oficial, sin relación con KOORUI.
