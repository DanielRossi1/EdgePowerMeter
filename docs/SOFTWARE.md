# EdgePowerMeter Software Documentation

This document provides detailed information about the EdgePowerMeter desktop application.

## Table of Contents

1. [Architecture](#architecture)
2. [Modules](#modules)
3. [Serial Communication](#serial-communication)
4. [User Interface](#user-interface)
5. [Data Processing](#data-processing)
6. [Export Formats](#export-formats)
7. [Themes](#themes)
8. [Settings](#settings)

---

## Architecture

The application follows a modular architecture with clear separation of concerns:

```
app/
├── main.py              # Application entry point
├── serial/
│   ├── __init__.py
│   └── reader.py        # Serial communication layer
└── ui/
    ├── __init__.py
    ├── main_window.py   # Main GUI window
    ├── theme.py         # Theme definitions
    ├── components.py    # Reusable UI components
    ├── settings.py      # Settings management
    └── report.py        # Export functionality
```

### Design Principles

- **Clean Code**: Modular design with single responsibility
- **Type Hints**: Full type annotations for better IDE support
- **Dataclasses**: Used for configuration and data structures
- **Separation**: UI, serial communication, and business logic are separated

---

## Modules

### `app/serial/reader.py`

Handles all serial port communication with the EdgePowerMeter hardware.

#### Classes

**`Measurement`** (dataclass)
```python
@dataclass
class Measurement:
    timestamp: datetime
    voltage: float    # Volts
    current: float    # Milliamps
    power: float      # Milliwatts
```

**`SerialConfig`** (dataclass)
```python
@dataclass
class SerialConfig:
    port: str
    baud: int = 115200
    timeout: float = 0.1
```

**`SerialReader`**
- Manages serial connection lifecycle
- Parses CSV data from firmware
- Emits measurements via callback

#### Usage Example

```python
from app.serial.reader import SerialReader, SerialConfig

def on_measurement(m: Measurement):
    print(f"V={m.voltage:.3f}V, I={m.current:.2f}mA, P={m.power:.2f}mW")

config = SerialConfig(port="/dev/ttyUSB0")
reader = SerialReader(config, callback=on_measurement)
reader.start()
```

---

### `app/ui/main_window.py`

The main application window containing:
- Three real-time graphs (Voltage, Current, Power)
- Statistics cards
- Control panel (Connect, Start, Export)
- Settings access

#### Key Classes

**`PlotWidget`**
- Custom pyqtgraph widget
- Supports theme switching
- DateTimeAxis for timestamps

**`PlotBuffers`**
- Manages data for plotting
- Handles timestamp aggregation (averages multiple samples per second)
- Separate full data storage for export

**`MainWindow`**
- Main application window
- Manages all UI components
- Handles serial connection state

---

### `app/ui/theme.py`

Defines the application's visual themes.

#### Theme Colors

**Dark Theme**
```python
DARK_THEME = ThemeColors(
    bg_primary="#0d1117",
    bg_secondary="#161b22",
    text_primary="#f0f6fc",
    accent="#58a6ff",
    chart_voltage="#58a6ff",
    chart_current="#d29922",
    chart_power="#3fb950",
    ...
)
```

**Light Theme**
```python
LIGHT_THEME = ThemeColors(
    bg_primary="#ffffff",
    bg_secondary="#f6f8fa",
    text_primary="#1f2328",
    accent="#0969da",
    ...
)
```

#### Stylesheet Generation

```python
def generate_stylesheet(theme: ThemeColors) -> str:
    """Generate complete Qt stylesheet from theme colors."""
```

---

### `app/ui/components.py`

Reusable UI components used across the application.

#### Components

- **`StatCard`**: Displays a single statistic with label and value
- **`IconButton`**: Button with icon support
- Custom styled widgets

---

### `app/ui/settings.py`

Application settings management.

#### `AppSettings` (dataclass)

```python
@dataclass
class AppSettings:
    # Appearance
    theme: str = "dark"
    
    # Units
    voltage_unit: str = "V"
    current_unit: str = "mA"
    power_unit: str = "mW"
    
    # Display
    graph_points: int = 1000
    update_interval: int = 50
    
    # Serial
    default_baud: int = 115200
    auto_reconnect: bool = True
    
    # Export
    csv_separator: str = ","
    pdf_include_graphs: bool = True
```

#### `SettingsDialog`

Tabbed dialog for configuring:
- **Appearance**: Theme selection
- **Units**: Measurement units
- **Display**: Graph settings
- **Serial**: Connection settings
- **Export**: Export preferences

---

### `app/ui/report.py`

Export functionality for measurements.

#### `Statistics` (dataclass)

```python
@dataclass
class Statistics:
    voltage_min: float
    voltage_max: float
    voltage_avg: float
    current_min: float
    current_max: float
    current_avg: float
    power_min: float
    power_max: float
    power_avg: float
    energy_wh: float
    duration_s: float
    sample_count: int
```

#### `ReportGenerator`

**CSV Export**
- Full measurement history
- Timestamp, Voltage, Current, Power columns
- Configurable separator

**PDF Export**
- Professional dark-themed report
- Statistics summary
- Derived metrics (sampling rate, ripple, impedance)
- Automatic pagination

---

## Serial Communication

### Protocol

The firmware sends CSV data over USB serial:

```
Timestamp,Voltage[V],Current[A],Power[W]
2025-11-30 12:34:56,5.0123,0.2500,1.2531
```

### Parsing

The `MeasurementParser` class handles:
1. Line splitting by comma
2. Timestamp parsing (multiple formats supported)
3. Value conversion (A→mA, W→mW)
4. Invalid data handling

### Connection Methods

Two methods are attempted:
1. **Direct file descriptor** (Linux) - lowest latency
2. **PySerial** - cross-platform fallback

---

## Data Processing

### Timestamp Aggregation

Since the DS3231 RTC has 1-second resolution, multiple samples may share the same timestamp. The `PlotBuffers` class averages these:

```python
# Multiple readings at same second
12:34:56 → V=5.001, I=250.1, P=1250.3
12:34:56 → V=4.999, I=249.9, P=1249.7

# Displayed as single averaged point
12:34:56 → V=5.000, I=250.0, P=1250.0
```

### Statistics Calculation

Statistics are computed in real-time:
- Min/Max tracking
- Running average
- Energy integration (Wh = P × dt)

---

## Export Formats

### CSV Format

```csv
Timestamp,Voltage (V),Current (mA),Power (mW)
2025-11-30 12:34:56.123456,5.0123,250.1234,1253.0876
```

### PDF Format

Professional report including:
- Header with generation timestamp
- Measurement duration and sample count
- Voltage/Current/Power statistics
- Derived metrics
- Footer with page numbers

---

## Themes

### Switching Themes

```python
# In code
window.toggle_theme()

# Or via settings dialog
```

### Custom Themes

Create a new `ThemeColors` instance:

```python
MY_THEME = ThemeColors(
    bg_primary="#1a1a2e",
    bg_secondary="#16213e",
    text_primary="#eaeaea",
    accent="#e94560",
    ...
)
```

---

## Settings

Settings are stored in memory and can be accessed:

```python
from app.ui.settings import AppSettings, SettingsDialog

# Load defaults
settings = AppSettings()

# Show dialog
dialog = SettingsDialog(settings, parent=window)
if dialog.exec() == QDialog.Accepted:
    settings = dialog.get_settings()
```

### Persistence

Settings can be saved to JSON:

```python
import json
from dataclasses import asdict

# Save
with open("settings.json", "w") as f:
    json.dump(asdict(settings), f)

# Load
with open("settings.json") as f:
    settings = AppSettings(**json.load(f))
```

---

## Dependencies

| Package | Version | Purpose |
|---------|---------|---------|
| PySide6 | ≥6.0 | Qt GUI framework |
| pyqtgraph | ≥0.13 | Real-time plotting |
| pyserial | ≥3.5 | Serial communication |
| reportlab | ≥4.0 | PDF generation |

Install all:
```bash
pip install PySide6 pyqtgraph pyserial reportlab
```

---

## Troubleshooting

### Serial Connection Issues

**Permission denied on Linux:**
```bash
sudo usermod -a -G dialout $USER
# Log out and back in
```

**Port not found:**
```bash
# List available ports
ls /dev/ttyUSB* /dev/ttyACM*
```

### Graph Performance

If graphs are slow:
1. Reduce `graph_points` in settings
2. Increase `update_interval`
3. Close other applications

### PDF Export Issues

If PDF is cut off:
- Update to latest reportlab version
- Check available disk space

---

## API Reference

For complete API documentation, see the source code docstrings or generate with:

```bash
pip install pdoc
pdoc --html app/
```
