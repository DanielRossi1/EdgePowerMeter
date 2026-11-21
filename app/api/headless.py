"""Headless mode (`edgepowermeter --headless`): the automation API and the
acquisition without a window, for servers, SSH sessions and scripted runs.

Runs on a QCoreApplication (no widgets, no GL). Logs go to stderr; the API token
is never printed (it is in the settings and the discovery file).
"""

from __future__ import annotations

import signal
import sys
from typing import Any, Optional

from PySide6 import QtCore

from ..core.settings import AppSettings
from ..ui.controller import AcquisitionController
from ..ui.widgets.port_discovery import PortDiscovery
from .schema import api_discovery_path, generate_token
from .server import ApiServer


def _log(text: str) -> None:
    print(text, file=sys.stderr, flush=True)


def _suggested_port() -> Optional[str]:
    """Same choice as the API's list_ports: an ESP32-like USB device first."""
    esp = PortDiscovery.find_esp32_ports()
    if esp:
        return esp[0].device
    ports = PortDiscovery.get_ports(False)
    return ports[0][0] if ports else None


def run(args: Any) -> int:
    """Run until SIGINT/SIGTERM. Returns the process exit code."""
    app = QtCore.QCoreApplication.instance() or QtCore.QCoreApplication([sys.argv[0]])
    app.setApplicationName("EdgePowerMeter")
    app.setOrganizationName("EdgePowerMeter")

    settings = AppSettings.load()
    if not settings.api_token:
        settings.api_token = generate_token()
        settings.save()
    # 0 is a valid explicit port (any free port), so test for None, not truthiness.
    port = args.api_port if args.api_port is not None else settings.api_port

    ctrl = AcquisitionController(settings, app)
    server = ApiServer(ctrl, settings, port=port, token=settings.api_token, parent=app)
    ctrl.message.connect(lambda level, text: _log(f"[{level}] {text}"))
    ctrl.state_changed.connect(lambda state: _log(f"state: {state}"))

    try:
        server.start()
    except OSError as exc:
        _log(f"error: {exc}")
        ctrl.shutdown()
        return 2

    _log(f"EdgePowerMeter headless: automation API at {server.url}")
    _log(f"discovery file: {api_discovery_path()}")

    if args.start:
        device = args.port or _suggested_port()
        if device is None:
            _log("error: no serial port found: connect the meter or pass --port DEV")
        else:
            _log(f"starting acquisition on {device}")
            ctrl.start(device)

    _log("Ctrl-C to quit")

    def _quit(*_: Any) -> None:
        app.quit()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _quit)
        except (ValueError, OSError):  # not supported on this platform
            pass
    # Python runs signal handlers only between bytecodes; a periodic no-op
    # timer keeps the Qt event loop returning to Python so Ctrl-C is seen.
    ticker = QtCore.QTimer(app)
    ticker.timeout.connect(lambda: None)
    ticker.start(200)

    code = app.exec()

    _log("stopping")
    server.stop()
    ctrl.shutdown()
    settings.save()
    return code
