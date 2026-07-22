"""PortDiscovery resiliency tests.

Regression coverage for a bug where pyserial's Linux port enumeration
(list_ports.comports()) raises TypeError while constructing a single
port's info - e.g. `int(None)` on a sysfs attribute unreadable under
snap strict confinement without raw-usb/hardware-observe granted -
which aborts the whole batch and silently hides every port, including
ones that would have enumerated fine. Also covers ListPortInfo fields
(vid/pid/serial_number/description/hwid) coming back None, which
pyserial does legitimately for non-USB or partially-readable devices.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.ui.widgets.port_discovery import PortDiscovery, _safe_attr


def _fake_port(device, **fields):
    defaults = dict(
        device=device, description=None, hwid=None,
        vid=None, pid=None, serial_number=None,
    )
    defaults.update(fields)
    return SimpleNamespace(**defaults)


def test_safe_attr_returns_default_when_field_is_none():
    port = _fake_port('/dev/ttyUSB0', vid=None)
    assert _safe_attr(port, 'vid') is None
    assert _safe_attr(port, 'vid', 0) == 0


def test_safe_attr_returns_value_when_populated():
    port = _fake_port('/dev/ttyUSB0', vid=0x10C4)
    assert _safe_attr(port, 'vid') == 0x10C4


def test_get_ports_handles_all_fields_none(monkeypatch):
    # A device that "enumerated" but with every USB-specific field left
    # None by pyserial (e.g. a bare TTY with no sysfs USB info).
    port = _fake_port('/dev/ttyUSB0', description=None, hwid=None)
    monkeypatch.setattr(PortDiscovery, '_comports', classmethod(lambda cls: [port]))

    result = PortDiscovery.get_ports(show_all=True)

    assert result == [('/dev/ttyUSB0', '/dev/ttyUSB0 — Unknown')]


def test_get_ports_normal_populated_entry(monkeypatch):
    port = _fake_port(
        '/dev/ttyUSB0', description='CP2102 USB to UART Bridge',
        hwid='USB VID:PID=10C4:EA60', vid=0x10C4, pid=0xEA60,
        serial_number='ABC123',
    )
    monkeypatch.setattr(PortDiscovery, '_comports', classmethod(lambda cls: [port]))

    result = PortDiscovery.get_ports(show_all=False)

    assert result == [('/dev/ttyUSB0', '/dev/ttyUSB0 — CP2102 USB to UART Bridge')]


def test_find_esp32_ports_handles_none_description(monkeypatch):
    ports = [
        _fake_port('/dev/ttyUSB0', description=None),
        _fake_port('/dev/ttyUSB1', description='CP2104 USB to UART Bridge'),
    ]
    monkeypatch.setattr(PortDiscovery, '_comports', classmethod(lambda cls: ports))

    result = PortDiscovery.find_esp32_ports()

    assert [p.device for p in result] == ['/dev/ttyUSB1']


def test_comports_falls_back_when_batch_enumeration_raises(monkeypatch):
    """The exact reported bug: comports() raises TypeError for the whole
    batch because one device's sysfs attribute (bNumInterfaces) can't be
    read under partial snap confinement. A single bad device must not
    hide every other port.
    """
    import app.ui.widgets.port_discovery as pd_module

    def _raising_comports():
        raise TypeError("int() argument must be a string, a bytes-like "
                         "object or a real number, not 'NoneType'")

    monkeypatch.setattr(pd_module.list_ports, 'comports', _raising_comports)
    monkeypatch.setattr(pd_module.sys, 'platform', 'linux')
    monkeypatch.setattr(
        PortDiscovery, '_scan_devices_individually',
        classmethod(lambda cls: [_fake_port('/dev/ttyUSB0')]),
    )

    result = PortDiscovery._comports()

    assert [p.device for p in result] == ['/dev/ttyUSB0']


def test_comports_fallback_skips_on_non_linux(monkeypatch):
    import app.ui.widgets.port_discovery as pd_module

    def _raising_comports():
        raise TypeError("boom")

    monkeypatch.setattr(pd_module.list_ports, 'comports', _raising_comports)
    monkeypatch.setattr(pd_module.sys, 'platform', 'win32')

    assert PortDiscovery._comports() == []


def test_scan_devices_individually_isolates_per_device_failure(monkeypatch, tmp_path):
    import app.ui.widgets.port_discovery as pd_module

    monkeypatch.setattr(
        pd_module.glob, 'glob',
        lambda pattern: ['/dev/ttyUSB0'] if pattern == '/dev/ttyUSB*' else [],
    )

    def _raising_sysfs(device):
        raise TypeError("int() argument must be a string, a bytes-like "
                         "object or a real number, not 'NoneType'")

    monkeypatch.setattr(
        'serial.tools.list_ports_linux.SysFS', _raising_sysfs, raising=False,
    )

    result = PortDiscovery._scan_devices_individually()

    # Falls back to a bare ListPortInfo carrying just the device path.
    assert len(result) == 1
    assert result[0].device == '/dev/ttyUSB0'
