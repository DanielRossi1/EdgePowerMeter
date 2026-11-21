import argparse
import os
import sys

# Qt and the application modules are imported inside main(): `--mcp` must start
# without a display or Qt, and the GPU probe subprocess must stay light.

# Same value as app.core.gpu_preflight.PROBE_ENV_VAR (kept literal: importing that
# module loads Qt through app.core).
PROBE_ENV_VAR = "EPM_GPU_PROBE"


def ensure_gpu_ready():
    """Lazy wrapper around app.core.gpu_preflight.ensure_gpu_ready (see module note)."""
    from app.core.gpu_preflight import ensure_gpu_ready as _ensure
    return _ensure()


def _parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="edgepowermeter",
        description="EdgePowerMeter. Without options the desktop application starts.",
        epilog="Options not listed here are passed on to Qt (e.g. -platform offscreen).")
    parser.add_argument("--mcp", action="store_true",
                        help="run the MCP bridge (stdio) for AI agents; no window")
    parser.add_argument("--api", action="store_true",
                        help="enable the automation API for this session (not saved)")
    parser.add_argument("--api-port", type=int, metavar="N",
                        help="automation API port for this session (0-65535); "
                             "also enables the API for this session, like --api")
    parser.add_argument("--headless", action="store_true",
                        help="run without a window: automation API and acquisition only")
    parser.add_argument("--port", metavar="DEV",
                        help="serial port to use with --headless --start, e.g. /dev/ttyACM0")
    parser.add_argument("--start", action="store_true",
                        help="with --headless: start the acquisition at startup")
    args, rest = parser.parse_known_args(argv)
    if args.api_port is not None and not 0 <= args.api_port <= 65535:
        parser.error("--api-port must be between 0 and 65535")
    if args.start and not args.headless:
        parser.error("--start needs --headless")
    if args.port and not args.headless:
        parser.error("--port needs --headless")
    return args, rest


def main():
    if os.environ.get(PROBE_ENV_VAR) == "1":
        # Started as the GPU probe subprocess (frozen builds re-run this same
        # executable): only test GL and exit, never start the application.
        from app.core.gpu_probe import probe
        sys.exit(0 if probe() else 1)

    args, rest = _parse_args(sys.argv[1:])

    if args.mcp:
        from app.api.mcp_bridge import main as bridge_main
        sys.exit(bridge_main())

    if args.headless:
        # No window, so no GL and no GPU preflight.
        from app.api.headless import run as run_headless
        sys.exit(run_headless(args))

    from PySide6 import QtGui, QtWidgets

    from app.core.gpu_preflight import MODE_SOFTWARE

    # Must run before QApplication is constructed so a broken default GL
    # path can be routed around instead of leaving a blank/crashed window.
    # May re-exec this process with adjusted env vars (sys.argv is kept),
    # in which case this call never returns.
    gpu_mode = ensure_gpu_ready()

    # Proper WM_CLASS on Linux for dock icon matching
    QtWidgets.QApplication.setDesktopFileName("edgepowermeter")
    app = QtWidgets.QApplication([sys.argv[0]] + rest)
    app.setApplicationName("EdgePowerMeter")
    app.setOrganizationName("EdgePowerMeter")
    app.setStyle("Fusion")  # consistent base for the stylesheet on every platform
    QtGui.QIcon.setThemeName("")

    from app.ui.main_window import MainWindow  # heavy imports after the preflight
    # --api-port alone implies --api: naming a port means the API is wanted.
    api_on = args.api or args.api_port is not None
    win = MainWindow(gpu_mode, api_override=api_on, api_port_override=args.api_port)
    if gpu_mode == MODE_SOFTWARE:
        win.show_software_rendering_banner()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
