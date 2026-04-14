# Modbus Temperature Monitor

Standalone PyQt6 application for monitoring temperature from Modbus RTU sensor modules.

![Python](https://img.shields.io/badge/python-3.10+-blue)
![Platform](https://img.shields.io/badge/platform-Linux%20%7C%20Windows-lightgrey)

## Features

- **Multiple modules** — add and remove Modbus RTU sensor modules at runtime
- **Live charting** — Qt6 Charts with auto-scaling axes, per-module color-coded series, and legend
- **LCD readouts** — current temperature and 5-minute gradient (Δ5min) for a selectable primary module
- **CSV logging** — per-module CSV files with timestamps, enable/disable at runtime
- **Cross-platform** — runs on Linux and Windows (serial ports: `/dev/ttyUSB0` or `COM3`)

## Installation

```bash
python -m venv .venv

# Linux
source .venv/bin/activate

# Windows
.venv\Scripts\activate

pip install -r requirements.txt
```

## Usage

```bash
python temp_monitor.py
```

1. Enter the serial port, baud rate, parity, slave ID, poll rate, and scale for your module
2. Click **Add Module**
3. Repeat for additional modules
4. Select a primary module from the dropdown to see LCD readouts
5. Click **Select log file...** to enable CSV logging

## Modbus Configuration

| Parameter | Default | Description |
|-----------|---------|-------------|
| Serial port | `/dev/ttyUSB0` | Serial device path (`COMx` on Windows) |
| Baud rate | 9600 | Serial baud rate |
| Parity | N | None / Even / Odd |
| Slave ID | 1 | Modbus slave address (1-247) |
| Poll Hz | 5 | Polling frequency |
| Scale | 0.1 | °C per LSB (raw register conversion) |

The application reads holding register `0x0000` and interprets the value as a signed 16-bit integer, then applies: `temp_°C = raw × scale + offset`.

## CSV Log Format

Each module gets its own file (`<base>_module_<id>.csv`):

```csv
unix_time,iso_time,module_id,temp_c
1713090000.123456,2026-04-14T12:00:00.123,1,25.123456
```
