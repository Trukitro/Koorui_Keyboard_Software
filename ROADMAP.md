# Plan a futuro

Estado actual: **v1.4.0**. Batería con detección de carga, historial y estimación, avisos configurables, perfiles de luces, apagado automático, ajustes con idioma, instalador para Windows, y modos cable y Bluetooth como opciones.

El plan está ordenado por prioridad. Primero va confirmar lo que hoy son suposiciones, porque varias funciones futuras dependen de ello.

## 1. Confirmar lo que aún es suposición

Ya validado en el teclado real: batería (descarga y carga), los 18 modos, brillo y velocidad.

| Pendiente | Cómo se comprueba |
|---|---|
| Qué devuelve el receptor con el teclado apagado o dormido | Leer la batería con el interruptor en OFF y tras varios minutos sin uso |
| Significado de los bytes 5 y 9 de la página 0 | Fotos con `snapshot.py` antes y después de cambiar de color con Fn |
| Qué hacen los comandos `0x0D` y `0x0E` (no contestaron al probarlos) | Investigar solo con el teclado en un estado fácil de recuperar |

## 2. Mejoras de batería

- ~~Detectar si está cargando.~~ Hecho: el byte 9 de la respuesta de `0x1A` vale 1 con el cable conectado.
- ~~Aviso de carga completa.~~ Hecho en v1.2.0.
- ~~Historial y estimación.~~ Hecho en v1.2.0: gráfica de hasta 7 días y tiempo restante aproximado.
- **Actualizar al usar el teclado** en lugar de cada 30 segundos fijos, y dejar de consultar cuando lleva rato inactivo.
- ~~Umbral de aviso configurable y aviso crítico.~~ Hecho en v1.2.0.

## 3. Mejoras de luces

- ~~Perfiles.~~ Hecho en v1.2.0.
- **Atajos globales** de Windows para cambiar de perfil sin abrir el panel.
- **Perfil por aplicación**: cambiar las luces según el programa en primer plano.
- ~~Apagado automático por horario o al bloquear el PC.~~ Hecho en v1.3.0.
- **Animaciones más fieles** a los efectos reales, una vez confirmados los 18 modos.

## 4. Aplicación

- ~~Ventana de ajustes e interfaz en inglés.~~ Hecho en v1.2.0.
- ~~README en inglés.~~ Hecho en v1.3.0.
- ~~Instalador con acceso directo y desinstalación.~~ Hecho en v1.3.0.
- **Firmar el ejecutable y el instalador**, para que Windows SmartScreen no avise.
- **Ejecutable más pequeño y rápido de abrir** (hoy 36 MB), quitando módulos de Qt que no se usan.
- **Compilación automática** del `.exe` y del instalador con GitHub Actions al publicar una versión.
- **Aviso de versión nueva** dentro de la app.
- **Pruebas automáticas** con un receptor simulado, para poder cambiar el protocolo sin riesgo.

## 5. Investigación a largo plazo

Requieren más ingeniería inversa y tienen riesgo, porque implican escribir en zonas de memoria aún no entendidas.

- **Color por tecla.** Los comandos `0x10`, `0x11` y `0x14` parecen relacionados, pero no están explorados.
- **Reasignación de teclas y macros.** El comando `0x1B` devuelve lo que parece una tabla de teclas.
- ~~Modo cable USB y Bluetooth.~~ Hecho en v1.4.0 como opciones: cable completo, Bluetooth solo batería.
- **Luces por Bluetooth.** El teclado expone por BLE un informe propietario (página `0xFF01`, ID 5, 4 bytes) y un servicio Telink sin explorar.
- **Otros teclados** KOORUI o con receptor Telink/EVision parecido.

## Reglas para seguir investigando

Aprendidas durante la v1.0:

1. Primero leer, después escribir. `snapshot.py` permite ver qué cambia sin tocar nada.
2. Antes de cualquier escritura, guardar una copia de la página que se va a modificar.
3. Escribir siempre la página completa: la dirección 0 borra los 64 bytes.
4. Un cambio por prueba, y con alguien mirando el teclado.
