# Koorui Keyboard Software

[English](README.en.md) | **Español**

App de bandeja para Windows que muestra la batería del teclado **KOORUI 75% inalámbrico (82 teclas, con rueda)** en modo 2.4 GHz y permite cambiar sus luces desde el PC.

El panel que se abre al hacer clic en el icono:

![Panel de la app](docs/screenshot.png)

### Representación de la batería

El icono en la bandeja del sistema muestra el porcentaje y cambia de color según el estado:

![Iconos de la bandeja](docs/tray.png)

| Color | Significado |
|---|---|
| Verde | Más del 50 % |
| Naranja | Entre 21 % y 50 % |
| Rojo | 20 % o menos (además salta un aviso) |
| Azul | Cargando |
| Gris | Sin lectura (teclado apagado o receptor desconectado) |

Con el cable conectado, la pila del panel pasa a azul con un rayo y el texto indica "cargando":

![Panel con el teclado cargando](docs/charging.png)

KOORUI no publica software para este teclado y, en modo 2.4 GHz, no hay forma de saber cuánta batería le queda. Esta app lo resuelve hablando directamente con el receptor USB mediante un protocolo obtenido por ingeniería inversa.

## Qué hace

- **Batería en la bandeja**: el porcentaje como número en el icono, con color según el nivel.
- **Detección de carga**: icono azul y un rayo sobre la pila mientras el cable está conectado.
- **Historial y estimación**: gráfica de los últimos días (verde al descargar, azul al cargar) y tiempo restante aproximado al ritmo actual.
- **Avisos**: batería baja, batería crítica y carga completa, con umbrales configurables.
- **Luces**: los 18 modos con miniatura animada, color (nueve predefinidos, multicolor o cualquier otro), brillo y velocidad.
- **Perfiles de luces**: guarda combinaciones de modo, color, brillo y velocidad con nombre y aplícalas con un clic, desde el panel o desde el menú del icono.
- **Apagado automático de luces**: al bloquear el PC y/o en un horario (por ejemplo de 23:00 a 07:00). Al desbloquear o terminar el horario vuelve el brillo que tenías. Ahorra batería.
- **Ajustes**: idioma (español o inglés), intervalo de consulta, umbrales de aviso y apagado automático.
- Detecta los cambios hechos con las teclas Fn del teclado y puede iniciarse con Windows.

Ventana de ajustes (botón del engranaje o clic derecho en el icono):

![Ajustes](docs/settings.png)

## Instalación

Ninguna de las dos primeras opciones necesita Python. Los archivos están en la [última versión publicada](https://github.com/Trukitro/Koorui_Keyboard_Software/releases/latest).

### Opción 1: instalador (recomendada, desde la v1.3.0)

1. Descarga `KooruiKeyboardSetup.exe` y ábrelo.
2. Sigue el asistente. Puedes marcar "Iniciar con Windows".

Se instala solo para tu usuario (no pide permisos de administrador), crea un acceso en el menú Inicio y se desinstala desde "Aplicaciones instaladas" de Windows.

### Opción 2: ejecutable portable

1. Descarga `KooruiKeyboard.exe`.
2. Guárdalo en una carpeta fija (por ejemplo `Documentos\Koorui`) y ábrelo con doble clic.

En ambos casos Windows puede mostrar el aviso de SmartScreen porque los archivos no están firmados: pulsa "Más información" y luego "Ejecutar de todas formas".

### Opción 3: desde el código

Requiere Python 3.10 o posterior.

```bash
git clone https://github.com/Trukitro/Koorui_Keyboard_Software.git
cd Koorui_Keyboard_Software
pip install -r requirements.txt
pythonw koorui_battery.pyw
```

En todos los casos hace falta Windows 10 u 11 y el teclado conectado con su receptor 2.4 GHz, que debe identificarse como `VID 320F / PID 50C3` ("2.4G Receiver", Telink/EVision).

## Uso

- **Clic izquierdo** en el icono: abre o cierra el panel.
- **Clic derecho** en el icono: perfiles, actualizar ahora, ajustes, iniciar con Windows, salir.
- **Clic** en un modo: lo activa en el teclado.
- **Clic derecho** en un modo: cambia su nombre y la animación con la que se representa.
- **"+ Guardar actual"**: guarda las luces de ese momento como perfil.
- **Clic** en un perfil: lo aplica. **Clic derecho**: renombrar, actualizar con las luces actuales o eliminar.
- **Engranaje**: abre los ajustes.

Los perfiles, los nombres de los modos, el historial de batería (30 días), los ajustes y el registro de errores se guardan en `%APPDATA%\KooruiBattery`.

## Compilar el ejecutable

```bash
pip install pyinstaller
pyinstaller --onefile --noconsole --name KooruiKeyboard --icon assets/icon.ico koorui_battery.pyw
```

El resultado queda en `dist\KooruiKeyboard.exe`. Si tienes PySide6 instalado además de PyQt6, añade `--exclude-module PySide6`.

Para generar el instalador hace falta [Inno Setup 6](https://jrsoftware.org/isinfo.php):

```bash
iscc installer\koorui.iss
```

El resultado queda en `dist\KooruiKeyboardSetup.exe`.

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
- No hay documentación oficial del protocolo. La lectura de batería (descarga y carga), los 18 modos, el brillo y la velocidad están validados en el teclado del autor.
- Los nombres de los modos vienen de la tabla habitual de los teclados EVision; si alguno no describe bien el efecto, se cambia con clic derecho.
- Las animaciones del panel son aproximaciones, no una copia exacta de los efectos del teclado.
- El tiempo restante es una estimación a partir del tramo de descarga actual; necesita al menos media hora de lecturas y una caída de 3 puntos, y cambia según el uso y el brillo.
- El aviso de carga completa salta al llegar al nivel configurado (100 % por defecto). Si tu teclado se queda en 98 o 99 % al terminar, baja ese nivel en los ajustes.
- El apagado automático pone el brillo a 0 y guarda el valor anterior para restaurarlo. Si subes el brillo a mano mientras está activo, la app lo respeta hasta el siguiente bloqueo u horario.
- La app escribe en la memoria de configuración del teclado. Un bloque inválido hace que el teclado vuelva a su iluminación de fábrica. Úsala bajo tu responsabilidad.
- Solo funciona en modo 2.4 GHz; no cubre Bluetooth ni cable.

## Estructura del proyecto

| Archivo | Contenido |
|---|---|
| `koorui_battery.pyw` | La app: protocolo HID, modelo, panel e icono de bandeja |
| `snapshot.py` | Herramienta de solo lectura para investigar la configuración |
| `requirements.txt` | Dependencias |
| `assets/icon.ico` | Icono del ejecutable |
| `installer/koorui.iss` | Guion de Inno Setup para el instalador |

## Plan a futuro

Lo que queda por confirmar y las ideas para próximas versiones están en [ROADMAP.md](ROADMAP.md).

## Licencia

[MIT](LICENSE). Proyecto no oficial, sin relación con KOORUI.
