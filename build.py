#!/usr/bin/env python3
"""Build script for EdgePowerMeter - creates executables for different platforms."""

import subprocess
import sys
import shutil
from pathlib import Path

# Import version from app
sys.path.insert(0, str(Path(__file__).parent))
from app.version import __version__, APP_NAME, AUTHOR, DESCRIPTION

VERSION = __version__
ENTRY_POINT = "run.py"

ROOT = Path(__file__).parent
DIST_DIR = ROOT / "dist"
BUILD_DIR = ROOT / "build"


def clean():
    """Clean build artifacts."""
    print("[CLEAN] Cleaning build artifacts...")
    for d in [DIST_DIR, BUILD_DIR, ROOT / f"{APP_NAME}.spec"]:
        if isinstance(d, Path) and d.exists():
            if d.is_dir():
                shutil.rmtree(d)
            else:
                d.unlink()
    print("   Done!")


def build_pyinstaller():
    """Build executable using PyInstaller."""
    print(f"[BUILD] Building {APP_NAME} with PyInstaller...")
    
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--name", APP_NAME,
        "--onefile",           # Single executable
        "--windowed",          # No console window (GUI app)
        "--clean",
        # Hidden imports for PySide6 and pyqtgraph
        "--hidden-import", "PySide6.QtCore",
        "--hidden-import", "PySide6.QtGui", 
        "--hidden-import", "PySide6.QtWidgets",
        "--hidden-import", "pyqtgraph",
        "--hidden-import", "numpy",
        "--hidden-import", "serial",
        "--hidden-import", "reportlab",
        "--hidden-import", "matplotlib",
        "--hidden-import", "matplotlib.pyplot",
        "--hidden-import", "matplotlib.dates",
        "--hidden-import", "matplotlib.backends.backend_agg",
        "--hidden-import", "PIL",
        "--hidden-import", "PIL.Image",
        "--hidden-import", "OpenGL",
        "--hidden-import", "OpenGL.GL",
        "--hidden-import", "OpenGL.platform.glx",
        "--hidden-import", "OpenGL.platform.egl",
        "--hidden-import", "scipy",
        "--hidden-import", "scipy.fft",
        # Collect all matplotlib and PIL data files
        "--collect-all", "matplotlib",
        "--collect-all", "PIL",
        # Exclude unnecessary modules to reduce size
        "--exclude-module", "tkinter",
        "--exclude-module", "torch",
        "--exclude-module", "torchvision",
        "--exclude-module", "ultralytics",
        "--exclude-module", "cv2",
        "--exclude-module", "opencv",
        "--exclude-module", "tensorflow",
        "--exclude-module", "keras",
        "--exclude-module", "IPython",
        "--exclude-module", "jupyter",
        "--exclude-module", "notebook",
        "--exclude-module", "pandas",
        "--exclude-module", "triton",
        "--exclude-module", "onnx",
        "--exclude-module", "onnxruntime",
        ENTRY_POINT,
    ]
    
    subprocess.run(cmd, check=True)
    
    exe_path = DIST_DIR / APP_NAME
    if sys.platform == "win32":
        exe_path = exe_path.with_suffix(".exe")
    
    if exe_path.exists():
        size_mb = exe_path.stat().st_size / (1024 * 1024)
        print(f"[OK] Built: {exe_path} ({size_mb:.1f} MB)")
    else:
        print("[ERROR] Build failed!")
        sys.exit(1)


def get_architecture() -> str:
    """Detect current CPU architecture."""
    import platform
    machine = platform.machine().lower()
    if machine in ('x86_64', 'amd64'):
        return 'amd64'
    elif machine in ('aarch64', 'arm64'):
        return 'arm64'
    elif machine.startswith('arm'):
        return 'armhf'
    else:
        return machine


def create_deb_structure():
    """Create .deb package structure."""
    print(f"[BUILD] Creating .deb package for {APP_NAME}...")
    
    # Check if executable exists
    exe_path = DIST_DIR / APP_NAME
    # Also check for ARM64 renamed executable
    if not exe_path.exists():
        exe_path = DIST_DIR / f"{APP_NAME}-arm64"
    if not exe_path.exists():
        print("[ERROR] Executable not found. Run build first!")
        sys.exit(1)
    
    arch = get_architecture()
    deb_name = f"{APP_NAME.lower()}_{VERSION}_{arch}"
    deb_dir = DIST_DIR / deb_name
    
    # Create directory structure
    (deb_dir / "DEBIAN").mkdir(parents=True, exist_ok=True)
    (deb_dir / "usr" / "bin").mkdir(parents=True, exist_ok=True)
    (deb_dir / "usr" / "share" / "applications").mkdir(parents=True, exist_ok=True)
    (deb_dir / "usr" / "share" / "doc" / APP_NAME.lower()).mkdir(parents=True, exist_ok=True)
    
    # Copy executable
    shutil.copy(exe_path, deb_dir / "usr" / "bin" / APP_NAME.lower())
    (deb_dir / "usr" / "bin" / APP_NAME.lower()).chmod(0o755)
    
    # Create control file
    control_content = f"""Package: {APP_NAME.lower()}
Version: {VERSION}
Section: electronics
Priority: optional
Architecture: {arch}
Maintainer: {AUTHOR}
Description: {DESCRIPTION}
 EdgePowerMeter is a real-time power monitoring application
 that reads data from an ESP32-based power meter via serial
 port and displays voltage, current, and power graphs.
"""
    (deb_dir / "DEBIAN" / "control").write_text(control_content)
    
    # Create .desktop file
    desktop_content = f"""[Desktop Entry]
Name={APP_NAME}
Comment={DESCRIPTION}
Exec=/usr/bin/{APP_NAME.lower()}
Terminal=false
Type=Application
Categories=Utility;Electronics;
Keywords=power;meter;monitoring;serial;
"""
    (deb_dir / "usr" / "share" / "applications" / f"{APP_NAME.lower()}.desktop").write_text(desktop_content)
    
    # Create copyright file
    copyright_content = f"""Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/
Upstream-Name: {APP_NAME}
Upstream-Contact: {AUTHOR}

Files: *
Copyright: 2025 {AUTHOR}
License: MIT
"""
    (deb_dir / "usr" / "share" / "doc" / APP_NAME.lower() / "copyright").write_text(copyright_content)
    
    # Build .deb package
    deb_file = DIST_DIR / f"{deb_name}.deb"
    subprocess.run(["dpkg-deb", "--build", str(deb_dir), str(deb_file)], check=True)
    
    # Cleanup
    shutil.rmtree(deb_dir)
    
    if deb_file.exists():
        size_mb = deb_file.stat().st_size / (1024 * 1024)
        print(f"[OK] Built: {deb_file} ({size_mb:.1f} MB)")
        print(f"    Install with: sudo dpkg -i {deb_file}")
    else:
        print("[ERROR] .deb build failed!")
        sys.exit(1)


def main():
    import argparse
    
    parser = argparse.ArgumentParser(description="Build EdgePowerMeter")
    parser.add_argument("command", choices=["clean", "exe", "deb", "all"],
                       help="Build command")
    args = parser.parse_args()
    
    if args.command == "clean":
        clean()
    elif args.command == "exe":
        build_pyinstaller()
    elif args.command == "deb":
        create_deb_structure()
    elif args.command == "all":
        clean()
        build_pyinstaller()
        if sys.platform == "linux":
            create_deb_structure()
        print("\n[DONE] Build complete!")


if __name__ == "__main__":
    main()
