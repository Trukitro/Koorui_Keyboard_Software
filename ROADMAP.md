# Plan a futuro

Estado actual: **v1.1.0**. Batería en la bandeja con detección de carga, panel con los 18 modos, color, brillo y velocidad, ejecutable para Windows.

El plan está ordenado por prioridad. Primero va confirmar lo que hoy son suposiciones, porque varias funciones futuras dependen de ello.

## 1. Confirmar lo que aún es suposición

| Pendiente | Cómo se comprueba |
|---|---|
| Que el porcentaje de `0x1A` es fiable en todo el rango (ya se vio subir al cargar) | Anotar el valor durante un ciclo completo de descarga y carga |
| Nombre y animación de los 17 modos no confirmados | Activar cada modo y comparar con el teclado real |
| Cuál de los bytes 2 y 3 es brillo y cuál velocidad, y el rango real de la velocidad | Mover cada deslizador y observar el teclado |
| Qué devuelve el receptor con el teclado apagado o dormido | Leer la batería con el interruptor en OFF y tras varios minutos sin uso |
| Significado de los bytes 5 y 9 de la página 0 | Fotos con `snapshot.py` antes y después de cambiar de color con Fn |
| Qué hacen los comandos `0x0D` y `0x0E` (no contestaron al probarlos) | Investigar solo con el teclado en un estado fácil de recuperar |

## 2. Mejoras de batería

- ~~Detectar si está cargando.~~ Hecho: el byte 9 de la respuesta de `0x1A` vale 1 con el cable conectado.
- **Aviso de carga completa**, para saber cuándo desconectar el cable.
- **Historial y estimación.** Guardar las lecturas y mostrar una gráfica y los días que quedan al ritmo actual.
- **Actualizar al usar el teclado** en lugar de cada 30 segundos fijos, y dejar de consultar cuando lleva rato inactivo.
- **Umbral de aviso configurable** (hoy fijo en 20 %) y un segundo aviso crítico.

## 3. Mejoras de luces

- **Perfiles**: guardar combinaciones de modo, color, brillo y velocidad con nombre y aplicarlas con un clic.
- **Atajos globales** de Windows para cambiar de perfil sin abrir el panel.
- **Perfil por aplicación**: cambiar las luces según el programa en primer plano.
- **Apagado automático** por horario o al bloquear el PC.
- **Animaciones más fieles** a los efectos reales, una vez confirmados los 18 modos.

## 4. Aplicación

- **Ventana de ajustes**: intervalo de consulta, umbrales, idioma.
- **Interfaz y README en inglés**, ya que el repositorio es público.
- **Instalador** con acceso directo y desinstalación, en lugar de un `.exe` suelto.
- **Ejecutable más pequeño y rápido de abrir** (hoy 36 MB), quitando módulos de Qt que no se usan.
- **Compilación automática** del `.exe` con GitHub Actions al publicar una versión.
- **Aviso de versión nueva** dentro de la app.
- **Pruebas automáticas** con un receptor simulado, para poder cambiar el protocolo sin riesgo.

## 5. Investigación a largo plazo

Requieren más ingeniería inversa y tienen riesgo, porque implican escribir en zonas de memoria aún no entendidas.

- **Color por tecla.** Los comandos `0x10`, `0x11` y `0x14` parecen relacionados, pero no están explorados.
- **Reasignación de teclas y macros.** El comando `0x1B` devuelve lo que parece una tabla de teclas.
- **Modo cable USB y Bluetooth**, que hoy no están cubiertos.
- **Otros teclados** KOORUI o con receptor Telink/EVision parecido.

## Reglas para seguir investigando

Aprendidas durante la v1.0:

1. Primero leer, después escribir. `snapshot.py` permite ver qué cambia sin tocar nada.
2. Antes de cualquier escritura, guardar una copia de la página que se va a modificar.
3. Escribir siempre la página completa: la dirección 0 borra los 64 bytes.
4. Un cambio por prueba, y con alguien mirando el teclado.
