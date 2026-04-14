#!/usr/bin/env python3
"""
Standalone Modbus RTU Temperature Monitor.

- Connects to one or more Modbus RTU sensor modules via serial ports.
- All modules are sampled simultaneously at each tick.
- Live-plots all sensor data using Qt6 Charts.
- Optional CSV logging (single shared file, one row per sample).
- No ROS 2 dependency.

Usage:
    pip install pymodbus pyserial PyQt6 PyQt6-Charts
    python temp_monitor.py
"""

import sys
import asyncio
import threading
import collections
import csv
import os
import time
from datetime import datetime
from typing import Optional

from PyQt6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QPushButton, QComboBox, QLCDNumber, QFileDialog,
    QGroupBox, QLineEdit, QSpinBox, QMessageBox, QSizePolicy,
    QScrollArea,
)
from PyQt6.QtCore import Qt, QTimer, QPointF
from PyQt6.QtGui import QColor, QPen, QFont, QPalette, QIcon
from PyQt6.QtCharts import (
    QChart, QChartView, QLineSeries, QValueAxis,
)

from pymodbus.client import AsyncModbusSerialClient
from pymodbus.exceptions import ModbusIOException
import serial.tools.list_ports

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REG_TEMP = 0x0000

# pymodbus renamed the slave-id kwarg across versions:
#   <3.4 = 'unit', 3.4-3.12 = 'slave', 3.11+ = 'device_id'
import inspect as _inspect
_params = set(_inspect.signature(AsyncModbusSerialClient.read_holding_registers).parameters)
_SLAVE_KW = next(k for k in ("device_id", "slave", "unit") if k in _params)
PALETTE = [
    QColor(255, 80, 80),
    QColor(80, 200, 120),
    QColor(80, 160, 255),
    QColor(255, 200, 80),
    QColor(200, 120, 255),
    QColor(80, 230, 230),
    QColor(255, 120, 200),
    QColor(180, 180, 180),
]


# ---------------------------------------------------------------------------
# Sensor module abstraction
# ---------------------------------------------------------------------------

class SensorModule:
    """One physical Modbus RTU temperature module."""

    def __init__(
        self,
        module_id: int,
        port: str,
        baudrate: int = 9600,
        parity: str = "N",
        stopbits: int = 1,
        slave_id: int = 1,
        scale: float = 0.1,
        offset: float = 0.0,
    ):
        self.module_id = module_id
        self.port = port
        self.baudrate = baudrate
        self.parity = parity
        self.stopbits = stopbits
        self.slave_id = slave_id
        self.scale = scale
        self.offset = offset

        self.client: Optional[AsyncModbusSerialClient] = None
        self.connected = False
        self.last_temp: Optional[float] = None
        self.last_raw: Optional[int] = None
        self.history: collections.deque = collections.deque(maxlen=100_000)

    async def connect(self):
        print(f"[Module {self.module_id}] Connecting to {self.port} "
              f"(baud={self.baudrate}, parity={self.parity}, stop={self.stopbits}, slave={self.slave_id})...")
        try:
            self.client = AsyncModbusSerialClient(
                port=self.port,
                baudrate=self.baudrate,
                parity=self.parity,
                stopbits=self.stopbits,
                timeout=0.6,
            )
            ok = await self.client.connect()
            self.connected = bool(ok)
            if self.connected:
                print(f"[Module {self.module_id}] Connected successfully.")
            else:
                print(f"[Module {self.module_id}] connect() returned False — check port/permissions.")
            return self.connected
        except Exception as e:
            print(f"[Module {self.module_id}] Connect exception: {e}")
            self.connected = False
            return False

    async def disconnect(self):
        if self.client:
            try:
                await self.client.close()
            except Exception:
                pass
        self.connected = False
        self.client = None

    async def poll_once(self) -> Optional[float]:
        """Read one sample. Returns temp_c or None on failure.
        Does NOT update history — the Poller does that with a shared timestamp."""
        if not self.client or not self.connected:
            return None
        try:
            rr = await self.client.read_holding_registers(
                address=REG_TEMP, count=1, **{_SLAVE_KW: self.slave_id},
            )
            if rr.isError():
                print(f"[Module {self.module_id}] Modbus error response: {rr}")
                return None
            raw_u16 = rr.registers[0] & 0xFFFF
            raw = raw_u16 - 0x10000 if raw_u16 & 0x8000 else raw_u16
            temp_c = raw * self.scale + self.offset
            self.last_raw = raw
            self.last_temp = temp_c
            return temp_c
        except Exception as e:
            print(f"[Module {self.module_id}] Poll exception: {e}")
            return None


# ---------------------------------------------------------------------------
# Async poller — single synchronized loop for all modules
# ---------------------------------------------------------------------------

class Poller:
    """Runs an asyncio event loop in a daemon thread.
    All modules are sampled concurrently at the same tick."""

    def __init__(self, poll_hz: float = 5.0):
        self._poll_hz = poll_hz
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._thread.start()
        self._lock = threading.Lock()
        self._modules: dict[int, SensorModule] = {}
        self._poll_task: Optional[asyncio.Task] = None

        # shared CSV log
        self._log_fh = None
        self._log_writer = None
        self._log_path: Optional[str] = None
        self._log_header_ids: list[int] = []  # module IDs in the current header

    def set_poll_hz(self, hz: float):
        self._poll_hz = hz

    def add_module(self, mod: SensorModule):
        async def _connect():
            await mod.connect()
            with self._lock:
                self._modules[mod.module_id] = mod
                self._rewrite_log_header_locked()
            if not self._poll_task or self._poll_task.done():
                self._poll_task = self._loop.create_task(self._poll_loop())
            print(f"[Poller] Module {mod.module_id} added. "
                  f"Synchronized polling at {self._poll_hz} Hz.")

        asyncio.run_coroutine_threadsafe(_connect(), self._loop)

    def remove_module(self, module_id: int):
        with self._lock:
            mod = self._modules.pop(module_id, None)
            self._rewrite_log_header_locked()
        if mod is None:
            return

        async def _stop():
            await mod.disconnect()

        asyncio.run_coroutine_threadsafe(_stop(), self._loop)

    async def _poll_loop(self):
        interval = 1.0 / max(self._poll_hz, 0.1)
        while True:
            with self._lock:
                mods = dict(self._modules)
            if not mods:
                await asyncio.sleep(interval)
                continue

            # Sample all modules concurrently — same timestamp
            results = await asyncio.gather(
                *(mod.poll_once() for mod in mods.values()),
                return_exceptions=True,
            )
            now = time.time()

            with self._lock:
                for mod, result in zip(mods.values(), results):
                    if isinstance(result, Exception) or result is None:
                        continue
                    temp_c = result
                    mod.history.append((now, temp_c))

                # log one row with all values
                self._log_row_locked(now, mods, results)

            await asyncio.sleep(interval)

    # ---- shared CSV logging ----

    def open_log(self, path: str):
        with self._lock:
            self._close_log_locked()
            self._log_path = path
            is_new = not os.path.exists(path) or os.path.getsize(path) == 0
            self._log_fh = open(path, "a", newline="", buffering=1)
            self._log_writer = csv.writer(self._log_fh)
            if is_new:
                self._write_log_header_locked()
            print(f"[Log] Logging to {path}")

    def close_log(self):
        with self._lock:
            self._close_log_locked()

    def _close_log_locked(self):
        if self._log_fh:
            try:
                self._log_fh.flush()
                self._log_fh.close()
            except Exception:
                pass
        self._log_fh = None
        self._log_writer = None
        self._log_path = None
        self._log_header_ids = []

    def _write_log_header_locked(self):
        if self._log_writer is None:
            return
        ids = sorted(self._modules.keys())
        self._log_header_ids = ids
        header = ["unix_time", "iso_time"]
        for mid in ids:
            header.append(f"module_{mid}_temp_c")
        self._log_writer.writerow(header)

    def _rewrite_log_header_locked(self):
        """When modules change, check if we need to update the header.
        We close and reopen the file with a new header row."""
        if self._log_path is None:
            return
        current_ids = sorted(self._modules.keys())
        if current_ids == self._log_header_ids:
            return
        # reopen — append a new header block for the updated column set
        self._write_log_header_locked()

    def _log_row_locked(self, now: float, mods: dict, results: list):
        if self._log_writer is None:
            return
        iso = datetime.fromtimestamp(now).isoformat(timespec="milliseconds")
        row = [f"{now:.6f}", iso]
        for mid in self._log_header_ids:
            mod = mods.get(mid)
            if mod and mod.last_temp is not None:
                row.append(f"{mod.last_temp:.6f}")
            else:
                row.append("")
        try:
            self._log_writer.writerow(row)
        except Exception as e:
            print(f"[Log] Write error: {e}")
            self._close_log_locked()

    def get_log_path(self) -> Optional[str]:
        with self._lock:
            return self._log_path

    def get_modules(self) -> dict[int, SensorModule]:
        with self._lock:
            return dict(self._modules)

    def shutdown(self):
        if self._poll_task:
            self._poll_task.cancel()
        with self._lock:
            self._close_log_locked()
        for mid in list(self._modules):
            self.remove_module(mid)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=2)


# ---------------------------------------------------------------------------
# Module card widget (one per added module)
# ---------------------------------------------------------------------------

class ModuleCard(QGroupBox):
    """Small card showing status + color-matched 7-segment temp for one module."""

    def __init__(self, mod: SensorModule, color: QColor, on_remove):
        super().__init__(f"Module {mod.module_id}  —  {mod.port}")
        self.mod = mod
        self._color = color
        self._on_remove = on_remove
        self._build()

    def _build(self):
        lay = QHBoxLayout(self)

        self.lbl_status = QLabel("connecting...")
        lay.addWidget(self.lbl_status)

        lay.addStretch()

        # 7-segment LCD colored to match the plot line
        self.lcd_temp = QLCDNumber()
        self.lcd_temp.setDigitCount(7)
        self.lcd_temp.display("----")
        self.lcd_temp.setSegmentStyle(QLCDNumber.SegmentStyle.Flat)
        self.lcd_temp.setMinimumHeight(45)
        self.lcd_temp.setMinimumWidth(140)
        pal = self.lcd_temp.palette()
        pal.setColor(QPalette.ColorRole.WindowText, self._color)
        self.lcd_temp.setPalette(pal)
        lay.addWidget(self.lcd_temp)

        lbl_unit = QLabel("°C")
        font = QFont()
        font.setPointSize(12)
        lbl_unit.setFont(font)
        lay.addWidget(lbl_unit)

        btn_rm = QPushButton("Remove")
        btn_rm.setFixedWidth(80)
        btn_rm.clicked.connect(lambda: self._on_remove(self.mod.module_id))
        lay.addWidget(btn_rm)

    def refresh(self):
        if self.mod.connected:
            self.lbl_status.setText("connected")
            self.lbl_status.setStyleSheet("color: green;")
        else:
            self.lbl_status.setText("disconnected")
            self.lbl_status.setStyleSheet("color: red;")

        if self.mod.last_temp is not None:
            self.lcd_temp.display(f"{self.mod.last_temp:.1f}")
        else:
            self.lcd_temp.display("----")


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class MainWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Modbus Temperature Monitor")
        self.setMinimumSize(1100, 700)

        # window icon — handle both normal and PyInstaller bundled paths
        base = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
        icon_path = os.path.join(base, "icon.png")
        if os.path.exists(icon_path):
            self.setWindowIcon(QIcon(icon_path))

        self._poller = Poller(poll_hz=5.0)
        self._next_id = 1
        self._cards: dict[int, ModuleCard] = {}
        self._series: dict[int, QLineSeries] = {}
        self._t0: Optional[float] = None

        self._build_ui()

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh)
        self._timer.start(200)

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        root = QVBoxLayout(self)

        # ---- Add-module form ----
        form_group = QGroupBox("Add Modbus Module")
        form = QGridLayout(form_group)

        form.addWidget(QLabel("Serial port:"), 0, 0)
        self.inp_port = QComboBox()
        self.inp_port.setEditable(True)
        self.inp_port.setMinimumWidth(180)
        form.addWidget(self.inp_port, 0, 1)

        self.btn_refresh_ports = QPushButton("Refresh")
        self.btn_refresh_ports.setFixedWidth(70)
        self.btn_refresh_ports.clicked.connect(self._refresh_ports)
        form.addWidget(self.btn_refresh_ports, 0, 2)

        form.addWidget(QLabel("Baud:"), 0, 3)
        self.inp_baud = QSpinBox()
        self.inp_baud.setRange(1200, 115200)
        self.inp_baud.setValue(9600)
        form.addWidget(self.inp_baud, 0, 4)

        form.addWidget(QLabel("Parity:"), 0, 5)
        self.inp_parity = QComboBox()
        self.inp_parity.addItems(["N", "E", "O"])
        form.addWidget(self.inp_parity, 0, 6)

        form.addWidget(QLabel("Slave ID:"), 1, 0)
        self.inp_slave = QSpinBox()
        self.inp_slave.setRange(1, 247)
        self.inp_slave.setValue(1)
        form.addWidget(self.inp_slave, 1, 1)

        form.addWidget(QLabel("Poll Hz:"), 1, 3)
        self.inp_poll = QSpinBox()
        self.inp_poll.setRange(1, 50)
        self.inp_poll.setValue(5)
        form.addWidget(self.inp_poll, 1, 4)

        form.addWidget(QLabel("Scale (°C/LSB):"), 1, 5)
        self.inp_scale = QLineEdit("0.1")
        self.inp_scale.setFixedWidth(60)
        form.addWidget(self.inp_scale, 1, 6)

        self.btn_add = QPushButton("Add Module")
        self.btn_add.clicked.connect(self._on_add_module)
        form.addWidget(self.btn_add, 0, 7, 2, 1)

        root.addWidget(form_group)

        # populate ports on startup
        self._refresh_ports()

        # ---- Module cards (scrollable after 4) ----
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._cards_container = QWidget()
        self._cards_layout = QVBoxLayout(self._cards_container)
        self._cards_layout.setContentsMargins(0, 0, 0, 0)
        self._cards_layout.addStretch()
        self._scroll.setWidget(self._cards_container)
        self._scroll.setVisible(False)
        self._max_cards_before_scroll = 4
        root.addWidget(self._scroll)

        # ---- Logging controls ----
        log_row = QHBoxLayout()
        self.btn_log = QPushButton("Select log file...")
        self.btn_log.clicked.connect(self._on_select_log)
        log_row.addWidget(self.btn_log)

        self.btn_log_off = QPushButton("Disable logging")
        self.btn_log_off.clicked.connect(self._on_disable_log)
        log_row.addWidget(self.btn_log_off)

        self.btn_reset = QPushButton("Reset data")
        self.btn_reset.clicked.connect(self._on_reset)
        log_row.addWidget(self.btn_reset)

        log_row.addStretch()
        self.lbl_log = QLabel("Log: (disabled)")
        log_row.addWidget(self.lbl_log)
        root.addLayout(log_row)

        # ---- Chart ----
        self._chart = QChart()
        self._chart.setTitle("Temperature over time")
        self._chart.setAnimationOptions(QChart.AnimationOption.NoAnimation)
        self._chart.legend().setVisible(True)

        self._axis_x = QValueAxis()
        self._axis_x.setTitleText("Time (s)")
        self._axis_x.setRange(0, 60)
        self._chart.addAxis(self._axis_x, Qt.AlignmentFlag.AlignBottom)

        self._axis_y = QValueAxis()
        self._axis_y.setTitleText("Temperature (°C)")
        self._axis_y.setRange(0, 50)
        self._chart.addAxis(self._axis_y, Qt.AlignmentFlag.AlignLeft)

        chart_view = QChartView(self._chart)
        chart_view.setRenderHints(chart_view.renderHints())
        chart_view.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        root.addWidget(chart_view, 1)

    # ------------------------------------------------------------ ports

    def _refresh_ports(self):
        current = self.inp_port.currentText()
        used = {card.mod.port for card in self._cards.values()}
        self.inp_port.clear()
        ports = sorted(serial.tools.list_ports.comports(), key=lambda p: p.device)
        for p in ports:
            if p.device in used:
                continue
            label = f"{p.device}" if not p.description or p.description == "n/a" else f"{p.device}  ({p.description})"
            self.inp_port.addItem(label, p.device)
        if self.inp_port.count() > 0:
            self.inp_port.setCurrentIndex(0)

    # ------------------------------------------------------------ cards sizing

    def _update_cards_scroll(self):
        n = len(self._cards)
        self._scroll.setVisible(n > 0)
        if n <= self._max_cards_before_scroll:
            self._scroll.setMaximumHeight(16777215)
        else:
            card_h = 65
            self._scroll.setMaximumHeight(card_h * self._max_cards_before_scroll + 10)

    # ------------------------------------------------------------ actions

    def _on_add_module(self):
        port = self.inp_port.currentData()
        if not port:
            port = self.inp_port.currentText().split("(")[0].strip()
        if not port:
            QMessageBox.warning(self, "Error", "Serial port is required.")
            return

        try:
            scale = float(self.inp_scale.text())
        except ValueError:
            scale = 0.1

        mid = self._next_id
        self._next_id += 1

        mod = SensorModule(
            module_id=mid,
            port=port,
            baudrate=self.inp_baud.value(),
            parity=self.inp_parity.currentText(),
            slave_id=self.inp_slave.value(),
            scale=scale,
        )

        # update poll rate from form
        self._poller.set_poll_hz(float(self.inp_poll.value()))
        self._poller.add_module(mod)

        # series
        color = PALETTE[(mid - 1) % len(PALETTE)]
        series = QLineSeries()
        series.setName(f"Module {mid} ({port})")
        pen = QPen(color, 2)
        series.setPen(pen)
        self._chart.addSeries(series)
        series.attachAxis(self._axis_x)
        series.attachAxis(self._axis_y)
        self._series[mid] = series

        # card
        card = ModuleCard(mod, color, self._on_remove_module)
        self._cards[mid] = card
        self._cards_layout.insertWidget(self._cards_layout.count() - 1, card)
        self._update_cards_scroll()
        self._refresh_ports()

    def _on_remove_module(self, module_id: int):
        self._poller.remove_module(module_id)

        card = self._cards.pop(module_id, None)
        if card:
            self._cards_layout.removeWidget(card)
            card.deleteLater()

        series = self._series.pop(module_id, None)
        if series:
            self._chart.removeSeries(series)

        self._update_cards_scroll()
        self._refresh_ports()

    def _on_select_log(self):
        default = datetime.now().strftime("temperature_%Y%m%d_%H%M%S.csv")
        path, _ = QFileDialog.getSaveFileName(
            self, "Select log file", os.path.expanduser(f"~/{default}"),
            "CSV (*.csv);;All (*)",
        )
        if not path:
            return
        if not os.path.splitext(path)[1]:
            path += ".csv"

        self._poller.open_log(path)
        self.lbl_log.setText(f"Log: {os.path.basename(path)}")

    def _on_disable_log(self):
        self._poller.close_log()
        self.lbl_log.setText("Log: (disabled)")

    def _on_reset(self):
        for mod in self._poller.get_modules().values():
            mod.history.clear()
            mod.last_temp = None
        for s in self._series.values():
            s.clear()
        for card in self._cards.values():
            card.lcd_temp.display("----")
        self._t0 = None
        self._axis_x.setRange(0, 60)

    # ------------------------------------------------------------ refresh

    def _refresh(self):
        modules = self._poller.get_modules()

        y_min = float("inf")
        y_max = float("-inf")
        x_max = 0.0

        # establish global t0 from the earliest data point
        if self._t0 is None:
            for mod in modules.values():
                if mod.history:
                    self._t0 = mod.history[0][0]
                    break

        for mid, mod in modules.items():
            card = self._cards.get(mid)
            if card:
                card.refresh()

            series = self._series.get(mid)
            if series is None or not mod.history or self._t0 is None:
                continue

            points = []
            for t, v in mod.history:
                x = t - self._t0
                points.append(QPointF(x, v))
                if v < y_min:
                    y_min = v
                if v > y_max:
                    y_max = v
                if x > x_max:
                    x_max = x

            series.replace(points)

        # auto-range axes
        if x_max > 0:
            self._axis_x.setRange(0, x_max * 1.05 + 1)
        if y_min < float("inf"):
            margin = max((y_max - y_min) * 0.1, 1.0)
            self._axis_y.setRange(y_min - margin, y_max + margin)

        # update log label
        lp = self._poller.get_log_path()
        if lp:
            self.lbl_log.setText(f"Log: {os.path.basename(lp)}")
        else:
            self.lbl_log.setText("Log: (disabled)")

    # ------------------------------------------------------------ cleanup

    def closeEvent(self, event):
        self._timer.stop()
        self._poller.shutdown()
        super().closeEvent(event)


# ---------------------------------------------------------------------------

def main():
    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
