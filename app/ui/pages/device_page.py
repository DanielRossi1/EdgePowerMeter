"""Device page: connection details, INA226 configuration, clock, calibration
and the device log."""

from __future__ import annotations

import time
from datetime import datetime

import numpy as np
from PySide6 import QtGui, QtWidgets

from ...export.units import format_si
from ...i18n import N_, tr
from ...serial.protocol import AVG_CHOICES, CT_CHOICES_US, nominal_rate_hz
from ..controller import (STATE_CONNECTING, STATE_IDLE, STATE_RECONNECTING, STATE_STALE,
                          STATE_STREAMING, AcquisitionController)
from ..widgets.common import Card, FormGrid, KeyValueList, button, combo, label, scroll_page, spin

# (name, avg, bus conversion µs, shunt conversion µs)
PRESETS = (
    (N_("Fast"), 4, 140, 140),
    (N_("Balanced"), 16, 140, 140),
    (N_("Low noise"), 64, 332, 332),
    (N_("Very low noise"), 256, 1100, 1100),
)


class DevicePage(QtWidgets.QWidget):
    def __init__(self, ctrl: AcquisitionController, theme, parent=None):
        super().__init__(parent)
        self.ctrl = ctrl
        self.theme = theme
        self._dirty = False
        s = ctrl.settings

        body = QtWidgets.QWidget()
        col = QtWidgets.QVBoxLayout(body)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(14)

        # --------------------------------------------------------- status
        top = QtWidgets.QHBoxLayout()
        top.setSpacing(14)
        status = Card(tr("Connection"))
        self.conn = KeyValueList([
            ("state", tr("State")),
            ("port", tr("Port")),
            ("firmware", tr("Firmware")),
            ("protocol", tr("Data format")),
            ("rate", tr("Sample rate")),
            ("lost", tr("Lost samples")),
        ])
        status.body.addWidget(self.conn)
        top.addWidget(status, 1)

        clock = Card(tr("Clock"), tr("Timestamps come from the DS3231 real-time clock on the device."))
        self.clock = KeyValueList([
            ("source", tr("Time reference")),
            ("offset", tr("Device - PC offset")),
            ("synced", tr("Last synchronization")),
        ])
        clock.body.addWidget(self.clock)
        row = QtWidgets.QHBoxLayout()
        self.sync_btn = button(tr("Sync clock with PC"), icon_name="clock", icon_color=theme.text_secondary)
        self.sync_btn.setToolTip(tr("Set the device clock to this computer's time, aligned to the second."))
        self.sync_btn.clicked.connect(self._sync_clock)
        row.addWidget(self.sync_btn)
        row.addStretch()
        clock.body.addLayout(row)
        self.sync_auto = QtWidgets.QCheckBox(tr("Synchronize automatically on connect"))
        self.sync_auto.setChecked(s.sync_clock_on_connect)
        self.sync_auto.toggled.connect(lambda on: self._set("sync_clock_on_connect", on))
        clock.body.addWidget(self.sync_auto)
        top.addWidget(clock, 1)
        col.addLayout(top)

        self.legacy_note = label("", "Hint", wrap=True)
        self.legacy_note.setText(tr(
            "This device runs firmware 1.x. Flash firmware 2.0 (folder 'firmware') to enable "
            "device timestamps, lost-sample detection, sensor configuration and clock sync."))
        self.legacy_note.setStyleSheet(f"color: {theme.warning};")
        col.addWidget(self.legacy_note)

        # --------------------------------------------------------- sensor
        sensor = Card(tr("Sensor (INA226)"),
                      tr("More averaging and longer conversion times lower the noise but also "
                         "the sample rate. The device applies the settings immediately."))
        presets = QtWidgets.QHBoxLayout()
        presets.addWidget(label(tr("Presets"), "Muted"))
        self.preset_buttons = []
        for name, avg, vct, ict in PRESETS:
            b = button(f"{tr(name)}  ·  {nominal_rate_hz(avg, vct, ict):.0f} Hz", "flat")
            b.clicked.connect(lambda _=False, a=avg, v=vct, i=ict: self._apply_preset(a, v, i))
            presets.addWidget(b)
            self.preset_buttons.append(b)
        presets.addStretch()
        sensor.body.addLayout(presets)

        form = FormGrid()
        self.avg_combo = combo([(str(a), a) for a in AVG_CHOICES], 4)
        form.add(tr("Averaging"), self.avg_combo, tr("Number of conversions averaged per sample."))
        self.vct_combo = combo([(f"{c} µs", c) for c in CT_CHOICES_US], 140)
        form.add(tr("Voltage conversion time"), self.vct_combo)
        self.ict_combo = combo([(f"{c} µs", c) for c in CT_CHOICES_US], 140)
        form.add(tr("Current conversion time"), self.ict_combo)
        self.rate_preview = label("", "KeyValue")
        form.add(tr("Resulting sample rate"), self.rate_preview)
        for c in (self.avg_combo, self.vct_combo, self.ict_combo):
            c.currentIndexChanged.connect(self._update_rate_preview)
            c.currentIndexChanged.connect(self._mark_dirty)
        sensor.body.addLayout(form)

        self.oled_check = QtWidgets.QCheckBox(tr("Device display (OLED) enabled"))
        self.oled_check.toggled.connect(self._mark_dirty)
        self.oled_check.setToolTip(tr("The display is refreshed only in spare time between samples."))
        sensor.body.addWidget(self.oled_check)

        btns = QtWidgets.QHBoxLayout()
        self.apply_btn = button(tr("Apply to device"), "primary")
        self.apply_btn.clicked.connect(self._apply_sensor)
        btns.addWidget(self.apply_btn)
        self.save_btn = button(tr("Save as device default"))
        self.save_btn.setToolTip(tr("Store the current settings in the device flash memory, "
                                    "so they are used after power-up."))
        self.save_btn.clicked.connect(lambda: self.ctrl.send_command("SAVE"))
        btns.addWidget(self.save_btn)
        btns.addStretch()
        self.reply_label = label("", "Hint")
        btns.addWidget(self.reply_label)
        sensor.body.addLayout(btns)
        col.addWidget(sensor)

        # ---------------------------------------------------- calibration
        cal = Card(tr("Calibration"),
                   tr("Applied by this application to new samples (data already recorded is "
                      "not changed). Compare against a reference meter to fine-tune."))
        cform = FormGrid()
        self.shunt = spin(0.00001, 100.0, s.shunt_ohm, 5, 0.001, " Ω")
        cform.add(tr("Shunt resistance"), self.shunt, tr("Nominal value of the current-sense resistor (board default 0.010 Ω)."))
        offs_row = QtWidgets.QHBoxLayout()
        self.i_offset = spin(-1000.0, 1000.0, s.current_offset_a * 1000, 3, 0.01, " mA")
        offs_row.addWidget(self.i_offset)
        self.zero_btn = button(tr("Zero now"), icon_name="zero", icon_color=theme.text_secondary)
        self.zero_btn.setToolTip(tr("Disconnect the load first: the average current of the last "
                                    "second becomes the new zero."))
        self.zero_btn.clicked.connect(self._zero_current)
        offs_row.addWidget(self.zero_btn)
        offs_row.addStretch()
        w = QtWidgets.QWidget()
        w.setLayout(offs_row)
        offs_row.setContentsMargins(0, 0, 0, 0)
        cform.add(tr("Current offset"), w)
        self.i_gain = spin(0.5, 2.0, s.current_gain, 5, 0.0001)
        cform.add(tr("Current gain"), self.i_gain)
        self.v_offset = spin(-1000.0, 1000.0, s.voltage_offset_v * 1000, 2, 1.0, " mV")
        cform.add(tr("Voltage offset"), self.v_offset)
        self.v_gain = spin(0.5, 2.0, s.voltage_gain, 5, 0.0001)
        cform.add(tr("Voltage gain"), self.v_gain)
        cal.body.addLayout(cform)
        for sp in (self.shunt, self.i_offset, self.i_gain, self.v_offset, self.v_gain):
            sp.valueChanged.connect(self._on_calibration_changed)
        r = QtWidgets.QHBoxLayout()
        reset = button(tr("Reset calibration"), "flat")
        reset.clicked.connect(self._reset_calibration)
        r.addWidget(reset)
        r.addStretch()
        cal.body.addLayout(r)
        col.addWidget(cal)

        # ------------------------------------------------------------- log
        logc = Card(tr("Device log"), tr("Messages printed by the firmware and replies to commands."))
        self.log = QtWidgets.QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(500)
        self.log.setMinimumHeight(140)
        f = QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.FixedFont)
        self.log.setFont(f)
        logc.body.addWidget(self.log)
        cmd_row = QtWidgets.QHBoxLayout()
        self.cmd_edit = QtWidgets.QLineEdit()
        self.cmd_edit.setPlaceholderText(tr("Send a command (e.g. GET, HELLO)"))
        self.cmd_edit.returnPressed.connect(self._send_raw)
        cmd_row.addWidget(self.cmd_edit, 1)
        send = button(tr("Send"))
        send.clicked.connect(self._send_raw)
        cmd_row.addWidget(send)
        logc.body.addLayout(cmd_row)
        col.addWidget(logc)
        col.addStretch()

        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(scroll_page(body))

        ctrl.device_log.connect(self._log)
        ctrl.command_reply.connect(self._on_reply)
        ctrl.clock_synced.connect(self._on_clock_synced)
        self._update_rate_preview()
        self.update_status()

    def detach(self) -> None:
        """Disconnect from the controller before this page is deleted."""
        for sig, slot in ((self.ctrl.device_log, self._log),
                          (self.ctrl.command_reply, self._on_reply),
                          (self.ctrl.clock_synced, self._on_clock_synced)):
            try:
                sig.disconnect(slot)
            except (RuntimeError, TypeError):
                pass

    def _on_clock_synced(self, when: str) -> None:
        self._log(tr("Clock synchronized to {time}", time=when))

    # ------------------------------------------------------------ helpers

    def _set(self, name: str, value) -> None:
        setattr(self.ctrl.settings, name, value)
        self.ctrl.settings.save()

    def _log(self, text: str) -> None:
        self.log.appendPlainText(f"{datetime.now():%H:%M:%S}  {text}")

    def _mark_dirty(self, *_):
        # User edits must not be overwritten by the periodic refresh from the
        # device configuration until they are applied.
        self._dirty = True

    def _update_rate_preview(self) -> None:
        avg, vct, ict = self.avg_combo.currentData(), self.vct_combo.currentData(), self.ict_combo.currentData()
        rate = nominal_rate_hz(avg, vct, ict)
        period_ms = 1000.0 / rate
        text = f"{rate:,.1f} Hz  ({period_ms:.2f} ms)"
        too_fast = period_ms < 0.5
        if too_fast:
            text += "  -  " + tr("too fast: some conversions will be skipped")
        self.rate_preview.setText(text)
        self.rate_preview.setStyleSheet(f"color: {self.theme.warning};" if too_fast else "")

    # ----------------------------------------------------------- status

    def update_status(self) -> None:
        c = self.ctrl
        t = self.theme
        states = {
            STATE_IDLE: (tr("Disconnected"), t.text_muted),
            STATE_CONNECTING: (tr("Connecting..."), t.warning),
            STATE_STREAMING: (tr("Receiving data"), t.success),
            STATE_STALE: (tr("No data"), t.warning),
            STATE_RECONNECTING: (tr("Reconnecting..."), t.warning),
        }
        text, color = states.get(c.state, (c.state, ""))
        self.conn.set("state", text, color)
        self.conn.set("port", c.port or "-")
        info = c.device_info
        self.conn.set("firmware", f"{info.name} {info.firmware}" if info else
                      (tr("1.x (legacy)") if c.protocol == "legacy" else "-"))
        self.conn.set("protocol", {"v2": tr("RAW with device time (protocol 2)"),
                                   "legacy": tr("CSV (firmware 1.x)")}.get(c.protocol, "-"))
        cfg = c.device_config
        measured = c.recent_rate() if c.is_running else 0.0
        if cfg is not None:
            self.conn.set("rate", f"{measured:.1f} Hz " + tr("(nominal {rate})",
                          rate=f"{cfg.sample_rate_hz:.1f} Hz"))
        else:
            self.conn.set("rate", f"{measured:.1f} Hz" if measured else "-")
        self.conn.set("lost", f"{c.lost_samples:,}" if c.is_v2 else "-",
                      t.warning if c.lost_samples else "")

        v2 = c.is_v2 and c.is_running
        for w in (self.avg_combo, self.vct_combo, self.ict_combo, self.apply_btn, self.save_btn,
                  self.oled_check, self.sync_btn, *self.preset_buttons):
            w.setEnabled(v2)
        if not v2:
            self._dirty = False     # nothing pending without a firmware 2.x device
        self.legacy_note.setVisible(c.protocol == "legacy")
        self.zero_btn.setEnabled(c.is_running and len(c.data()) > 10)

        if cfg is not None and not self._dirty:
            for combo_w, val in ((self.avg_combo, cfg.avg), (self.vct_combo, cfg.vct_us),
                                 (self.ict_combo, cfg.ict_us)):
                idx = combo_w.findData(val)
                if idx >= 0 and not combo_w.hasFocus():
                    combo_w.blockSignals(True)
                    combo_w.setCurrentIndex(idx)
                    combo_w.blockSignals(False)
            self.oled_check.blockSignals(True)
            self.oled_check.setChecked(cfg.oled)
            self.oled_check.blockSignals(False)
            self._update_rate_preview()

        # clock
        if cfg is not None:
            self.clock.set("source", tr("RTC + SQW pulse (µs accurate)") if cfg.sqw
                           else tr("RTC polling (ms accurate)"))
        elif c.protocol == "legacy":
            self.clock.set("source", tr("RTC (ms resolution)"))
        else:
            self.clock.set("source", "-")
        data = c.data()
        if c.is_running and len(data) and c.state == STATE_STREAMING:
            offset = float(data.wall[-1]) - time.time()
            self.clock.set("offset", f"{offset:+.3f} s", t.warning if abs(offset) > 2 else "")
        else:
            self.clock.set("offset", "-")
        self.clock.set("synced", c.last_clock_sync or "-")

    # ---------------------------------------------------------- actions

    def _apply_preset(self, avg: int, vct: int, ict: int) -> None:
        for combo_w, val in ((self.avg_combo, avg), (self.vct_combo, vct), (self.ict_combo, ict)):
            combo_w.setCurrentIndex(combo_w.findData(val))
        if self.apply_btn.isEnabled():
            self._apply_sensor()

    def _apply_sensor(self) -> None:
        c = self.ctrl
        cmds = [f"SET AVG {self.avg_combo.currentData()}",
                f"SET VCT {self.vct_combo.currentData()}",
                f"SET ICT {self.ict_combo.currentData()}",
                f"SET OLED {1 if self.oled_check.isChecked() else 0}"]
        for cmd in cmds:
            c.send_command(cmd)
            self._log("> " + cmd)
        self._dirty = False
        self.reply_label.setText(tr("Sent, waiting for the device..."))

    def _on_reply(self, ok: bool, cmd: str, msg: str) -> None:
        self._log(("OK " if ok else "ERROR ") + cmd + (f" ({msg})" if msg else ""))
        if not ok:
            self.reply_label.setText(tr("The device rejected {cmd}: {reason}", cmd=cmd, reason=msg))
            self.reply_label.setStyleSheet(f"color: {self.theme.danger};")
        else:
            self._dirty = False
            self.reply_label.setText(tr("Applied"))
            self.reply_label.setStyleSheet(f"color: {self.theme.success};")

    def _sync_clock(self) -> None:
        if self.ctrl.request_clock_sync():
            self._log(tr("Synchronizing clock at the next second boundary..."))

    def _send_raw(self) -> None:
        text = self.cmd_edit.text().strip()
        if not text:
            return
        if self.ctrl.send_command(text):
            self._log("> " + text)
            self.cmd_edit.clear()
        else:
            self._log(tr("Not connected."))

    def _on_calibration_changed(self) -> None:
        s = self.ctrl.settings
        s.shunt_ohm = self.shunt.value()
        s.current_offset_a = self.i_offset.value() / 1000.0
        s.current_gain = self.i_gain.value()
        s.voltage_offset_v = self.v_offset.value() / 1000.0
        s.voltage_gain = self.v_gain.value()
        s.save()
        self.ctrl.apply_calibration()

    def _zero_current(self) -> None:
        data = self.ctrl.data()
        if len(data) < 10:
            return
        # Only samples taken after the last calibration change: the previous
        # zero may still be in the recent data if "Zero now" is pressed twice.
        start = max(data.t[-1] - 1.0, self.ctrl.calibration_changed_t)
        k = int(np.searchsorted(data.t, start, side="right"))
        if len(data) - k < 5:
            self._log(tr("Wait a moment: not enough new samples since the last change."))
            return
        mean_i = float(np.mean(data.i[k:], dtype=np.float64))
        new_offset = self.ctrl.settings.current_offset_a - mean_i
        self.i_offset.setValue(new_offset * 1000.0)
        self._log(tr("Current zero set: offset {value}", value=format_si(new_offset, "A")))

    def _reset_calibration(self) -> None:
        for sp, val in ((self.shunt, 0.010), (self.i_offset, 0.0), (self.i_gain, 1.0),
                        (self.v_offset, 0.0), (self.v_gain, 1.0)):
            sp.blockSignals(True)
            sp.setValue(val)
            sp.blockSignals(False)
        self._on_calibration_changed()
