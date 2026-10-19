# Koorui Keyboard Software

**English** | [Español](README.md)

Windows tray app that shows the battery level of the **KOORUI 75% wireless keyboard (82 keys, with knob)** and lets you change its lighting from the PC. Built for 2.4 GHz mode; it also works over the cable and, for battery only, over Bluetooth.

The panel that opens when you click the tray icon (the interface is available in English and Spanish; screenshots show Spanish):

![App panel](docs/screenshot.png)

### Battery display

The tray icon shows the percentage and changes color with the state:

![Tray icons](docs/tray.png)

| Color | Meaning |
|---|---|
| Green | Above 50% |
| Orange | 21% to 50% |
| Red | 20% or less (a notification is also shown) |
| Blue | Charging |
| Gray | No reading (keyboard off or receiver unplugged) |

With the cable plugged in, the battery in the panel turns blue with a bolt and the text says "charging":

![Panel while charging](docs/charging.png)

KOORUI does not publish software for this keyboard and, in 2.4 GHz mode, there is no way to know how much battery is left. This app solves that by talking directly to the USB receiver using a reverse-engineered protocol.

## Features

- **Battery in the tray**: the percentage as a number on the icon, colored by level.
- **Charging detection**: blue icon and a bolt on the battery while the cable is plugged in.
- **History and estimate**: chart of the last days (green while discharging, blue while charging) and approximate time left at the current rate.
- **Notifications**: low battery, critical battery and fully charged, with configurable thresholds.
- **Lighting**: all 18 modes with an animated thumbnail, color (nine presets, multicolor or any other), brightness and speed.
- **Lighting profiles**: save combinations of mode, color, brightness and speed under a name and apply them with one click, from the panel or from the tray menu.
- **Automatic lights off**: when the PC is locked and/or on a schedule (for example 23:00 to 07:00). On unlock or when the schedule ends, your previous brightness comes back. Saves battery.
- **Report**: forecast of when to charge, battery life by brightness, monthly battery wear, charging statistics, keyboard use by hour and a key heatmap (optional). It can be exported to CSV or to an HTML report.
- **Smart polling**: while you type it checks every 30 s; when idle it drops to every 5 min and reads again as soon as you press a key.
- **New version notice**: once a day it checks GitHub for a newer published version and shows a tray notification (can be turned off).
- **Settings**: language (English or Spanish), polling interval, notification thresholds and automatic lights off.
- Picks up changes made with the keyboard's Fn keys and can start with Windows.

Settings window (gear button or right-click on the tray icon):

![Settings](docs/settings.png)

Report tab:

![Report](docs/report.png)

![Report, lower part](docs/report2.png)

What the report shows:

- **Forecast**: how long the battery lasts at the current rate and which day to charge it (when it reaches the low-battery threshold).
- **Battery life by brightness**: hours of use a full charge gives at each brightness level, and battery points used per hour. Only time while the keyboard is awake counts.
- **Battery wear**: battery life per month at the brightness you use most; if it drops over time, the battery is wearing out. Needs at least two months of data.
- **Charging**: last charge, charges per week, average time between charges and how long a 0 to 100% charge takes, plus the last five charges.
- **Keyboard use**: minutes of use today, the average on days you use it and a chart of use by hour of day.
- **Heatmap** (optional, off by default): the keyboard colored by how often you press each key over the last 30 days, and the most used keys.
- **Export**: the reading history as CSV (opens in Excel) or an HTML report with all of the above.

### Privacy

To know when you use the keyboard, the app receives key press notifications from Windows (Raw Input) and keeps only those from this keyboard:

- Without the heatmap, it only uses the minute each press happened in, to add up minutes of use.
- With the heatmap on, it also adds one to that key's daily counter. It never stores what you type or in what order.
- Turning the heatmap off deletes the counters.
- Everything stays on your PC. The only internet connection is the daily version check against GitHub, which can be turned off in the settings.

## Installation

Neither of the first two options needs Python. The files are in the [latest release](https://github.com/Trukitro/Koorui_Keyboard_Software/releases/latest).

### Option 1: installer (recommended, since v1.3.0)

1. Download `KooruiKeyboardSetup.exe` and run it.
2. Follow the wizard. You can tick "Start with Windows".

It installs for your user only (no administrator rights needed), adds a Start menu shortcut and can be removed from Windows "Installed apps".

### Option 2: portable executable

1. Download `KooruiKeyboard.exe`.
2. Save it in a permanent folder (for example `Documents\Koorui`) and double-click it.

In both cases Windows may show a SmartScreen warning because the files are not signed: click "More info" and then "Run anyway".

### Option 3: from source

Requires Python 3.10 or later.

```bash
git clone https://github.com/Trukitro/Koorui_Keyboard_Software.git
cd Koorui_Keyboard_Software
pip install -r requirements.txt
pythonw koorui_battery.pyw
```

In all cases you need Windows 10 or 11 and the keyboard connected in one of the modes in the table below.

## Usage

- **Left-click** the icon: open or close the panel.
- **Right-click** the icon: profiles, refresh now, settings, start with Windows, quit.
- **Click** a mode: activates it on the keyboard.
- **Right-click** a mode: change its name and the animation used to represent it.
- **"+ Save current"**: saves the current lighting as a profile.
- **Click** a profile: applies it. **Right-click**: rename, update with the current lights, or delete.
- **Gear**: opens the settings.

Profiles, mode names, battery history (90 days), settings and the error log are stored in `%APPDATA%\KooruiBattery`.

## Publishing a version

When a version is published on GitHub, the [`Compilar`](.github/workflows/release.yml) action builds the executable and the installer on Windows and attaches them to that version. Bump `APP_VERSION` in `koorui_battery.pyw` to the same number as the tag first, so the new version notice works:

```bash
gh release create v1.6.0 --title "v1.6.0" --notes "What's new..."
```

It can also be run by hand from the Actions tab; then it leaves the files as a downloadable artifact.

## Building

```bash
pip install pyinstaller
pyinstaller --onefile --noconsole --name KooruiKeyboard --icon assets/icon.ico koorui_battery.pyw
```

The result is `dist\KooruiKeyboard.exe`. If you have PySide6 installed alongside PyQt6, add `--exclude-module PySide6`.

Building the installer requires [Inno Setup 6](https://jrsoftware.org/isinfo.php):

```bash
iscc installer\koorui.iss
```

The result is `dist\KooruiKeyboardSetup.exe`.

## Connection modes

2.4 GHz is the main mode. Wired and Bluetooth work as options; the app detects which mode the keyboard is in and shows it next to the name.

| Mode | Identifies as | Battery | Charging | Lights and profiles |
|---|---|---|---|---|
| **2.4 GHz** (main) | `320F:50C3` "2.4G Receiver" | Yes | Yes | Yes |
| USB cable | `320F:50C2` "USB Gaming Keyboard" | Yes | Yes | Yes |
| Bluetooth | `245A:8276` "MK082-5.0" | Yes | No | No |

Over the cable the keyboard speaks the same protocol as the receiver. Over Bluetooth that channel does not exist: the battery level comes from the standard Bluetooth LE battery service and the app reads it from Windows, so in that mode the lights cannot be changed and charging cannot be detected.

## How it works

The receiver exposes a vendor-specific HID collection (usage page `0xFF1C`, usage `0x92`, report ID 4). Packets are 64 bytes:

```
[04][sum_lo][sum_hi][cmd][len][addr_lo][addr_hi][status][data...]
```

The checksum is the sum of bytes 3 to 63. In responses, status `0xFF` means "command not supported".

| Command | Purpose |
|---|---|
| `0x01` / `0x02` | Begin / end a transaction |
| `0x1A` | Battery: percentage in byte 8; byte 9 is 1 while charging |
| `0x05` | Read configuration (24 bytes per packet at most) |
| `0x06` | Write configuration |
| `0x1D` | Model identification (`UK6133`) |

The configuration is flash memory organized in 64-byte pages. Writing at address 0 erases the whole page, so the app always reads the page, changes the bytes it needs and rewrites it completely in three chunks. Reads do not cross a page boundary.

Page 0:

| Byte | Content |
|---|---|
| 1 | Lighting mode (1 to 18) |
| 2 | Brightness (0 to 4) |
| 3 | Speed |
| 5 | `0x00` = fixed color, `0xFF` = multicolor |
| 6–8 | Color: red, green, blue |

Fn keys never reach the PC (the keyboard handles them internally), which is why lighting is controlled by writing this page rather than by simulating key presses.

`snapshot.py` is the read-only tool used to discover this map: it saves a snapshot of the configuration and compares two snapshots to show which bytes change when an Fn combination is pressed.

## Known limitations

- Tested on a single keyboard. Other KOORUI models, or keyboards with a different receiver, will probably not work.
- There is no official documentation for the protocol. The battery reading (discharging and charging), the 18 modes, brightness and speed have been validated on the author's keyboard.
- Mode names come from the usual EVision keyboard table; if one does not describe the effect well, change it with a right-click.
- The panel animations are approximations, not an exact copy of the keyboard's effects.
- Use by hour and the heatmap start counting from v1.6.0, and only while the app is running.
- The report needs data: battery life for a brightness level appears after 2 h of use at that level with a 3-point drop, and charging statistics after a few days. Readings stored before v1.5.0 do not include brightness, so they do not count towards battery life.
- Time left is an estimate based on the current discharge run; it needs at least half an hour of readings and a 3-point drop, and it varies with usage and brightness.
- The fully-charged notification fires at the configured level (100% by default, which is where the author's keyboard finishes charging).
- Automatic lights off sets brightness to 0 and stores the previous value to restore it. If you raise the brightness by hand while it is active, the app respects that until the next lock or schedule.
- The app writes to the keyboard's configuration memory. An invalid block makes the keyboard fall back to its factory lighting. Use it at your own risk.
- Over Bluetooth only the battery is available (no lights, no charging detection), and the value is whatever Windows has stored, which can take a while to update.
- After a few idle minutes the keyboard goes to sleep and stops answering; the app keeps the last reading and shows how old it is.

## Project structure

| File | Content |
|---|---|
| `koorui_battery.pyw` | The app: HID protocol, model, panel and tray icon |
| `snapshot.py` | Read-only tool for investigating the configuration |
| `requirements.txt` | Dependencies |
| `assets/icon.ico` | Executable icon |
| `installer/koorui.iss` | Inno Setup script for the installer |
| `.github/workflows/release.yml` | Automatic build when a version is published |

## Roadmap

What is still unconfirmed and the ideas for future versions are in [ROADMAP.md](ROADMAP.md) (Spanish).

## License

[MIT](LICENSE). Unofficial project, not affiliated with KOORUI.
