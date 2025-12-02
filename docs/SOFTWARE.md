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
├── version.py           # Version information
├── serial/
│   ├── __init__.py
│   └── reader.py        # Serial communication layer (921600 baud)
└── ui/
    ├── __init__.py
    ├── main_window.py   # Main GUI window
    ├── theme.py         # Theme definitions
    ├── settings.py      # Settings management
    ├── report.py        # Export functionality (CSV/PDF with graphs)
    └── widgets/         # Reusable UI components
        ├── __init__.py
        ├── plot_buffers.py   # Data buffering for plots
        ├── plot_widget.py    # Three-panel graph widget
        ├── stat_card.py      # Statistics display card
        └── port_discovery.py # Serial port detection
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
    baud: int = 921600  # High-speed serial for fast data transfer
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

**`PlotWidget`** (`app/ui/widgets/plot_widget.py`)
- Custom pyqtgraph widget with three synchronized panels
- Y-axis locked during X-axis zoom (auto-scales to visible data)
- Middle-click to reset X-axis auto-scroll
- Theme switching support
- DateTimeAxis for timestamps
- Region selector for data export

**`PlotBuffers`** (`app/ui/widgets/plot_buffers.py`)
- Manages data for real-time plotting
- Millisecond-precision timestamps
- Rejects out-of-order samples to prevent artifacts

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

Export and import functionality for measurements.

#### `Statistics` (dataclass)

```python
@dataclass
class Statistics:
    voltage_min: float
    voltage_max: float
    voltage_avg: float
    voltage_std: float
    current_min: float
    current_max: float
    current_avg: float
    current_std: float
    power_min: float
    power_max: float
    power_avg: float
    power_std: float
    energy_wh: float
    charge_ah: float
    duration_seconds: float
    count: int
```

#### `CSVImporter`

**Import from CSV**
- Auto-detection of separator (comma, semicolon, tab, space)
- Multiple timestamp format support
- Load previously exported data for re-analysis

```python
records = CSVImporter.import_csv(Path("measurement.csv"))
```

#### `ReportGenerator`

**CSV Export**
- Full measurement history
- Timestamp, Voltage, Current, Power columns
- Configurable separator

**PDF Export**
- Professional report with statistics tables
- **Voltage, Current, and Power graphs** (matplotlib)
- Derived metrics (sampling rate, ripple, impedance)
- Automatic pagination

---

## Serial Communication

### Protocol

The firmware sends CSV data over USB serial at **921600 baud**:

```
Timestamp,Voltage[V],Current[A],Power[W]
2025-11-30 12:34:56.123,5.0123,0.2500,1.2531
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

### Millisecond-Precision Timestamps

The firmware uses the DS3231 SQW (1Hz square wave) output synchronized with `millis()` to provide millisecond-accurate timestamps:

```
12:34:56.000 → V=5.001, I=250.1, P=1250.3
12:34:56.100 → V=4.999, I=249.9, P=1249.7
12:34:56.200 → V=5.002, I=250.2, P=1250.5
```

The `PlotBuffers` class stores each sample directly and rejects any out-of-order timestamps to prevent graph artifacts.

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
|---------|---------|----------|
| PySide6 | ≥6.0 | Qt GUI framework |
| pyqtgraph | ≥0.13 | Real-time plotting |
| pyserial | ≥3.5 | Serial communication |
| reportlab | ≥4.0 | PDF generation |
| matplotlib | ≥3.5 | Graphs in PDF reports |
| numpy | ≥1.20 | Numerical operations |

Install all:
```bash
pip install PySide6 pyqtgraph pyserial reportlab matplotlib numpy
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
