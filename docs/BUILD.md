# Building EdgePowerMeter

This guide explains how to build EdgePowerMeter as a standalone executable for distribution.

## Table of Contents

- [Prerequisites](#prerequisites)
- [Building on Linux](#building-on-linux)
- [Building on Windows](#building-on-windows)
- [Building on macOS](#building-on-macos)
- [Build Script Reference](#build-script-reference)
- [Troubleshooting](#troubleshooting)

---

## Prerequisites

### Python Dependencies

```bash
# Required packages
pip install PySide6 pyqtgraph pyserial reportlab numpy

# Build tools
pip install pyinstaller
```

### System Dependencies

#### Linux (Debian/Ubuntu)
```bash
# For building .deb packages
sudo apt install dpkg-dev

# Optional: for smaller builds
sudo apt install upx-ucl
```

#### Windows
- No additional dependencies required

#### macOS
```bash
# Optional: for creating .app bundles
brew install create-dmg
```

---

## Building on Linux

### Quick Build

```bash
# Build everything (executable + .deb package)
python build.py all

# Or step by step:
python build.py clean   # Clean previous builds
python build.py exe     # Build executable
python build.py deb     # Create .deb package
```

### Output Files

After building, you'll find:

| File | Location | Description |
|------|----------|-------------|
| `EdgePowerMeter` | `dist/` | Standalone executable |
| `edgepowermeter_*.deb` | `dist/` | Debian package |

### Installing the .deb Package

```bash
# Install
sudo dpkg -i dist/edgepowermeter_1.0.0_amd64.deb

# If there are dependency issues
sudo apt --fix-broken install

# Uninstall
sudo dpkg -r edgepowermeter
```

### Running the Executable

```bash
# Direct execution
./dist/EdgePowerMeter

# Or after installing .deb
edgepowermeter
```

---

## Building on Windows

### Prerequisites

1. Install [Python 3.8+](https://www.python.org/downloads/)
2. Install dependencies:
   ```cmd
   pip install PySide6 pyqtgraph pyserial reportlab numpy pyinstaller
   ```

### Building

```cmd
# Clean and build
python build.py clean
python build.py exe
```

### Output

The executable will be created at:
```
dist\EdgePowerMeter.exe
```

### Creating an Installer (Optional)

For creating a Windows installer, you can use [Inno Setup](https://jrsoftware.org/isinfo.php) or [NSIS](https://nsis.sourceforge.io/).

Example Inno Setup script (`installer.iss`):

```iss
[Setup]
AppName=EdgePowerMeter
AppVersion=1.0.0
DefaultDirName={autopf}\EdgePowerMeter
DefaultGroupName=EdgePowerMeter
OutputDir=dist
OutputBaseFilename=EdgePowerMeter_Setup

[Files]
Source: "dist\EdgePowerMeter.exe"; DestDir: "{app}"

[Icons]
Name: "{group}\EdgePowerMeter"; Filename: "{app}\EdgePowerMeter.exe"
Name: "{commondesktop}\EdgePowerMeter"; Filename: "{app}\EdgePowerMeter.exe"
```

---

## Building on macOS

### Prerequisites

```bash
pip install PySide6 pyqtgraph pyserial reportlab numpy pyinstaller
```

### Building

```bash
python build.py clean
python build.py exe
```

### Output

The app will be created at:
```
dist/EdgePowerMeter.app
```

### Creating a DMG (Optional)

```bash
# Install create-dmg
brew install create-dmg

# Create DMG
create-dmg \
    --volname "EdgePowerMeter" \
    --window-size 600 400 \
    --icon-size 100 \
    --app-drop-link 450 200 \
    dist/EdgePowerMeter.dmg \
    dist/EdgePowerMeter.app
```

---

## Build Script Reference

### Commands

| Command | Description |
|---------|-------------|
| `python build.py clean` | Remove all build artifacts |
| `python build.py exe` | Build standalone executable |
| `python build.py deb` | Create .deb package (Linux only) |
| `python build.py all` | Clean + exe + deb |

### Configuration

Edit `build.py` to modify:

```python
APP_NAME = "EdgePowerMeter"    # Application name
VERSION = "1.0.0"               # Version number
DESCRIPTION = "..."             # Package description
AUTHOR = "Daniel Rossi"         # Author name
```

### Excluding Modules

To reduce executable size, modules can be excluded in `build.py`:

```python
"--exclude-module", "torch",
"--exclude-module", "tensorflow",
# Add more as needed
```

---

## Troubleshooting

### Large Executable Size

If the executable is too large (>100MB), you may have heavy packages like PyTorch installed. Add them to the exclude list in `build.py`:

```python
"--exclude-module", "torch",
"--exclude-module", "torchvision",
"--exclude-module", "tensorflow",
"--exclude-module", "cv2",
```

### Missing Modules at Runtime

If the app crashes with `ModuleNotFoundError`, add the module as a hidden import:

```python
"--hidden-import", "missing_module",
```

### Serial Port Access (Linux)

If the app can't access serial ports:

```bash
# Add user to dialout group
sudo usermod -a -G dialout $USER

# Log out and back in, or:
newgrp dialout
```

### Qt Platform Plugin Error (Linux)

If you see `qt.qpa.plugin: Could not load the Qt platform plugin`:

```bash
# Install Qt dependencies
sudo apt install libxcb-xinerama0 libxcb-cursor0
```

### Windows Defender False Positive

PyInstaller executables are sometimes flagged by antivirus. You can:
1. Add an exception for the executable
2. Sign the executable with a code signing certificate

### macOS Gatekeeper

If macOS blocks the app:

```bash
# Remove quarantine attribute
xattr -d com.apple.quarantine dist/EdgePowerMeter.app

# Or allow in System Preferences > Security & Privacy
```

---

## Build Artifacts

After a successful build, your `dist/` folder should contain:

```
dist/
├── EdgePowerMeter              # Linux executable
├── EdgePowerMeter.exe          # Windows executable
├── EdgePowerMeter.app/         # macOS application
└── edgepowermeter_1.0.0_amd64.deb  # Debian package
```

The `build/` folder contains intermediate files and can be safely deleted.

---

## CI/CD Integration

Example GitHub Actions workflow (`.github/workflows/build.yml`):

```yaml
name: Build

on:
  push:
    tags:
      - 'v*'

jobs:
  build-linux:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.10'
      - run: pip install PySide6 pyqtgraph pyserial reportlab numpy pyinstaller
      - run: python build.py all
      - uses: actions/upload-artifact@v4
        with:
          name: linux-build
          path: dist/

  build-windows:
    runs-on: windows-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.10'
      - run: pip install PySide6 pyqtgraph pyserial reportlab numpy pyinstaller
      - run: python build.py exe
      - uses: actions/upload-artifact@v4
        with:
          name: windows-build
          path: dist/

  build-macos:
    runs-on: macos-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.10'
      - run: pip install PySide6 pyqtgraph pyserial reportlab numpy pyinstaller
      - run: python build.py exe
      - uses: actions/upload-artifact@v4
        with:
          name: macos-build
          path: dist/
```

---

## Support

If you encounter build issues, please:

1. Check the [Troubleshooting](#troubleshooting) section
2. Search existing [GitHub Issues](https://github.com/DanielRossi1/EdgePowerMeter/issues)
3. Open a new issue with:
   - Your operating system and version
   - Python version (`python --version`)
   - Full error message/traceback
   - Steps to reproduce
