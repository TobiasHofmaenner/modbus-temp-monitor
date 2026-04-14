#!/usr/bin/env python3
"""
Standalone Modbus RTU Temperature Monitor.

- Connects to one or more Modbus RTU sensor modules via serial ports.
- Live-plots all sensor data using Qt6 Charts.
- Optional CSV logging (per-sensor files).
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
    QScrollArea, QFrame,
)
from PyQt6.QtCore import Qt, QTimer, QPointF
from PyQt6.QtGui import QColor, QPen, QFont
from PyQt6.QtCharts import (
    QChart, QChartView, QLineSeries, QValueAxis,
)

from pymodbus.client import AsyncModbusSerialClient
from pymodbus.exceptions import ModbusIOException

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REG_TEMP = 0x0000
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
        poll_hz: float = 5.0,
    ):
        self.module_id = module_id
        self.port = port
        self.baudrate = baudrate
        self.parity = parity
        self.stopbits = stopbits
        self.slave_id = slave_id
        self.scale = scale
        self.offset = offset
        self.poll_hz = poll_hz

        self.client: Optional[AsyncModbusSerialClient] = None
        self.connected = False
        self.last_temp: Optional[float] = None
        self.last_raw: Optional[int] = None
        self.history: collections.deque = collections.deque(maxlen=100_000)

        # CSV logging
        self.log_fh = None
        self.log_writer = None
        self.log_path: Optional[str] = None

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
        if not self.client or not self.connected:
            return None
        try:
            rr = await self.client.read_holding_registers(
                address=REG_TEMP, count=1, slave=self.slave_id,
            )
            if rr.isError():
                print(f"[Module {self.module_id}] Modbus error response: {rr}")
                return None
            raw_u16 = rr.registers[0] & 0xFFFF
            raw = raw_u16 - 0x10000 if raw_u16 & 0x8000 else raw_u16
            temp_c = raw * self.scale + self.offset
            self.last_raw = raw
            self.last_temp = temp_c
            now = time.time()
            self.history.append((now, temp_c))
            self._log_row(now, temp_c)
            return temp_c
        except Exception as e:
            print(f"[Module {self.module_id}] Poll exception: {e}")
            return None

    # -- logging helpers --
    def open_log(self, path: str):
        self.close_log()
        self.log_path = path
        is_new = not os.path.exists(path) or os.path.getsize(path) == 0
        self.log_fh = open(path, "a", newline="", buffering=1)
        self.log_writer = csv.writer(self.log_fh)
        if is_new:
            self.log_writer.writerow(["unix_time", "iso_time", "module_id", "temp_c"])

    def close_log(self):
        if self.log_fh:
            try:
                self.log_fh.flush()
                self.log_fh.close()
            except Exception:
                pass
        self.log_fh = None
        self.log_writer = None
        self.log_path = None

    def _log_row(self, ts: float, temp_c: float):
        if self.log_writer is None:
            return
        iso = datetime.fromtimestamp(ts).isoformat(timespec="milliseconds")
        try:
            self.log_writer.writerow([f"{ts:.6f}", iso, self.module_id, f"{temp_c:.6f}"])
        except Exception:
            self.close_log()


# ---------------------------------------------------------------------------
# Async poller running in a background thread
# ---------------------------------------------------------------------------

class Poller:
    """Runs an asyncio event loop in a daemon thread, polls all modules."""

    def __init__(self):
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._thread.start()
        self._lock = threading.Lock()
        self._modules: dict[int, SensorModule] = {}
        self._poll_tasks: dict[int, asyncio.Task] = {}

    def add_module(self, mod: SensorModule):
        with self._lock:
            self._modules[mod.module_id] = mod

        async def _start():
            ok = await mod.connect()
            if ok:
                task = self._loop.create_task(self._poll_loop(mod))
                self._poll_tasks[mod.module_id] = task
                print(f"[Poller] Module {mod.module_id} polling started at {mod.poll_hz} Hz.")
            else:
                print(f"[Poller] Module {mod.module_id} not connected — polling not started.")

        asyncio.run_coroutine_threadsafe(_start(), self._loop)

    def remove_module(self, module_id: int):
        with self._lock:
            mod = self._modules.pop(module_id, None)
        if mod is None:
            return
        task = self._poll_tasks.pop(module_id, None)
        if task:
            task.cancel()

        async def _stop():
            await mod.disconnect()
            mod.close_log()

        asyncio.run_coroutine_threadsafe(_stop(), self._loop)

    async def _poll_loop(self, mod: SensorModule):
        interval = 1.0 / max(mod.poll_hz, 0.1)
        while True:
            await mod.poll_once()
            await asyncio.sleep(interval)

    def get_modules(self) -> dict[int, SensorModule]:
        with self._lock:
            return dict(self._modules)

    def shutdown(self):
        for mid in list(self._modules):
            self.remove_module(mid)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=2)


# ---------------------------------------------------------------------------
# Module card widget (one per added module)
# ---------------------------------------------------------------------------

class ModuleCard(QGroupBox):
    """Small card showing status for one module."""

    def __init__(self, mod: SensorModule, on_remove):
        super().__init__(f"Module {mod.module_id}  —  {mod.port}")
        self.mod = mod
        self._on_remove = on_remove
        self._build()

    def _build(self):
        lay = QHBoxLayout(self)
        self.lbl_status = QLabel("connecting...")
        self.lbl_temp = QLabel("--- °C")
        font = QFont()
        font.setPointSize(14)
        font.setBold(True)
        self.lbl_temp.setFont(font)

        btn_rm = QPushButton("Remove")
        btn_rm.setFixedWidth(80)
        btn_rm.clicked.connect(lambda: self._on_remove(self.mod.module_id))

        lay.addWidget(self.lbl_status)
        lay.addStretch()
        lay.addWidget(self.lbl_temp)
        lay.addWidget(btn_rm)

    def refresh(self):
        if self.mod.connected:
            self.lbl_status.setText("connected")
            self.lbl_status.setStyleSheet("color: green;")
        else:
            self.lbl_status.setText("disconnected")
            self.lbl_status.setStyleSheet("color: red;")

        if self.mod.last_temp is not None:
            self.lbl_temp.setText(f"{self.mod.last_temp:.1f} °C")
        else:
            self.lbl_temp.setText("--- °C")


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class MainWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Modbus Temperature Monitor")
        self.setMinimumSize(1100, 750)

        self._poller = Poller()
        self._next_id = 1
        self._cards: dict[int, ModuleCard] = {}
        self._series: dict[int, QLineSeries] = {}
        self._log_base: Optional[str] = None

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
        self.inp_port = QLineEdit("/dev/ttyUSB0")
        form.addWidget(self.inp_port, 0, 1)

        form.addWidget(QLabel("Baud:"), 0, 2)
        self.inp_baud = QSpinBox()
        self.inp_baud.setRange(1200, 115200)
        self.inp_baud.setValue(9600)
        form.addWidget(self.inp_baud, 0, 3)

        form.addWidget(QLabel("Parity:"), 0, 4)
        self.inp_parity = QComboBox()
        self.inp_parity.addItems(["N", "E", "O"])
        form.addWidget(self.inp_parity, 0, 5)

        form.addWidget(QLabel("Slave ID:"), 1, 0)
        self.inp_slave = QSpinBox()
        self.inp_slave.setRange(1, 247)
        self.inp_slave.setValue(1)
        form.addWidget(self.inp_slave, 1, 1)

        form.addWidget(QLabel("Poll Hz:"), 1, 2)
        self.inp_poll = QSpinBox()
        self.inp_poll.setRange(1, 50)
        self.inp_poll.setValue(5)
        form.addWidget(self.inp_poll, 1, 3)

        form.addWidget(QLabel("Scale (°C/LSB):"), 1, 4)
        self.inp_scale = QLineEdit("0.1")
        self.inp_scale.setFixedWidth(60)
        form.addWidget(self.inp_scale, 1, 5)

        self.btn_add = QPushButton("Add Module")
        self.btn_add.clicked.connect(self._on_add_module)
        form.addWidget(self.btn_add, 0, 6, 2, 1)

        root.addWidget(form_group)

        # ---- Module cards (scrollable) ----
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setMaximumHeight(160)
        self._cards_container = QWidget()
        self._cards_layout = QVBoxLayout(self._cards_container)
        self._cards_layout.setContentsMargins(0, 0, 0, 0)
        self._cards_layout.addStretch()
        scroll.setWidget(self._cards_container)
        root.addWidget(scroll)

        # ---- Primary sensor + LCDs ----
        lcd_row = QHBoxLayout()

        lcd_row.addWidget(QLabel("Primary:"))
        self.combo_primary = QComboBox()
        self.combo_primary.setMinimumWidth(120)
        lcd_row.addWidget(self.combo_primary)

        self.lcd_temp = QLCDNumber()
        self.lcd_temp.setDigitCount(7)
        self.lcd_temp.display("----")
        self.lcd_temp.setSegmentStyle(QLCDNumber.SegmentStyle.Flat)
        self.lcd_temp.setMinimumHeight(50)
        lcd_row.addWidget(self.lcd_temp, 2)
        lcd_row.addWidget(QLabel("°C"))

        self.lcd_grad = QLCDNumber()
        self.lcd_grad.setDigitCount(7)
        self.lcd_grad.display("----")
        self.lcd_grad.setSegmentStyle(QLCDNumber.SegmentStyle.Flat)
        self.lcd_grad.setMinimumHeight(50)
        lcd_row.addWidget(self.lcd_grad, 2)
        lcd_row.addWidget(QLabel("Δ5min °C"))

        root.addLayout(lcd_row)

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

    # ------------------------------------------------------------ actions

    def _on_add_module(self):
        port = self.inp_port.text().strip()
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
            poll_hz=float(self.inp_poll.value()),
        )

        # If logging is active, open a log for this module immediately
        if self._log_base:
            root_path, ext = os.path.splitext(self._log_base)
            ext = ext or ".csv"
            mod.open_log(f"{root_path}_module_{mid}{ext}")

        self._poller.add_module(mod)

        # series
        series = QLineSeries()
        series.setName(f"Module {mid} ({port})")
        color = PALETTE[(mid - 1) % len(PALETTE)]
        pen = QPen(color, 2)
        series.setPen(pen)
        self._chart.addSeries(series)
        series.attachAxis(self._axis_x)
        series.attachAxis(self._axis_y)
        self._series[mid] = series

        # card
        card = ModuleCard(mod, self._on_remove_module)
        self._cards[mid] = card
        # insert before the stretch
        self._cards_layout.insertWidget(self._cards_layout.count() - 1, card)

        # primary combo
        self.combo_primary.addItem(f"Module {mid}", mid)
        if self.combo_primary.count() == 1:
            self.combo_primary.setCurrentIndex(0)

    def _on_remove_module(self, module_id: int):
        self._poller.remove_module(module_id)

        card = self._cards.pop(module_id, None)
        if card:
            self._cards_layout.removeWidget(card)
            card.deleteLater()

        series = self._series.pop(module_id, None)
        if series:
            self._chart.removeSeries(series)

        # remove from combo
        for i in range(self.combo_primary.count()):
            if self.combo_primary.itemData(i) == module_id:
                self.combo_primary.removeItem(i)
                break

    def _on_select_log(self):
        default = datetime.now().strftime("temperature_%Y%m%d_%H%M%S.csv")
        path, _ = QFileDialog.getSaveFileName(
            self, "Select log base file", os.path.expanduser(f"~/{default}"),
            "CSV (*.csv);;All (*)",
        )
        if not path:
            return
        if not os.path.splitext(path)[1]:
            path += ".csv"

        self._log_base = path
        root_path, ext = os.path.splitext(path)
        ext = ext or ".csv"

        for mid, mod in self._poller.get_modules().items():
            mod.open_log(f"{root_path}_module_{mid}{ext}")

        self.lbl_log.setText(f"Log: {os.path.basename(path)}")

    def _on_disable_log(self):
        self._log_base = None
        for mod in self._poller.get_modules().values():
            mod.close_log()
        self.lbl_log.setText("Log: (disabled)")

    def _on_reset(self):
        for mod in self._poller.get_modules().values():
            mod.history.clear()
            mod.last_temp = None
        for s in self._series.values():
            s.clear()
        self.lcd_temp.display("----")
        self.lcd_grad.display("----")
        self._axis_x.setRange(0, 60)

    # ------------------------------------------------------------ refresh

    def _refresh(self):
        modules = self._poller.get_modules()

        y_min = float("inf")
        y_max = float("-inf")
        x_max = 0.0

        for mid, mod in modules.items():
            # card
            card = self._cards.get(mid)
            if card:
                card.refresh()

            # series
            series = self._series.get(mid)
            if series is None or not mod.history:
                continue

            t0 = mod.history[0][0]
            points = []
            for t, v in mod.history:
                x = t - t0
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

        # primary LCD
        primary_mid = self.combo_primary.currentData()
        if primary_mid is not None and primary_mid in modules:
            mod = modules[primary_mid]
            if mod.last_temp is not None:
                self.lcd_temp.display(f"{mod.last_temp:.1f}")
            else:
                self.lcd_temp.display("----")

            # 5-min gradient
            hist = mod.history
            if len(hist) >= 2:
                last_t, last_v = hist[-1]
                target = last_t - 300.0
                if hist[0][0] <= target:
                    val_5m = None
                    for t, v in hist:
                        if t >= target:
                            val_5m = v
                            break
                    if val_5m is not None:
                        self.lcd_grad.display(f"{last_v - val_5m:+.1f}")
                    else:
                        self.lcd_grad.display("----")
                else:
                    self.lcd_grad.display("----")
            else:
                self.lcd_grad.display("----")
        else:
            self.lcd_temp.display("----")
            self.lcd_grad.display("----")

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
