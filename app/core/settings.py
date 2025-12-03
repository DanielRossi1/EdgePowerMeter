"""Application settings with persistence."""

from __future__ import annotations
from dataclasses import dataclass, fields
from PySide6.QtCore import QSettings


@dataclass
class AppSettings:
    """Application settings."""
    # Theme
    dark_mode: bool = True
    
    # Units
    voltage_unit: str = "V"
    current_unit: str = "A"
    power_unit: str = "W"
    
    # Display
    plot_points: int = 5000  # Max points to render in view
    show_grid: bool = True
    grid_alpha: float = 0.2  # Grid opacity 0-1
    show_crosshair: bool = True  # Show cursor values on hover
    
    # Average Power
    use_moving_average: bool = True
    moving_average_window: int = 100
    
    # Serial
    baud_rate: int = 921600  # High-speed for ESP32-C3
    auto_reconnect: bool = True
    reconnect_interval: int = 2  # Seconds between reconnect attempts
    
    # Export
    csv_separator: str = ","
    timestamp_format: str = "%Y-%m-%d %H:%M:%S.%f"
    
    # Analysis
    include_fft: bool = False  # Include FFT analysis in PDF report
    
    def save(self) -> None:
        """Save settings to persistent storage.
        
        Uses QSettings which automatically handles:
        - Linux: ~/.config/EdgePowerMeter/EdgePowerMeter.conf
        - Windows: Registry HKEY_CURRENT_USER\\Software\\EdgePowerMeter
        - macOS: ~/Library/Preferences/com.EdgePowerMeter.plist
        
        Directories/keys are created automatically if they don't exist.
        """
        try:
            settings = QSettings("EdgePowerMeter", "EdgePowerMeter")
            for f in fields(self):
                value = getattr(self, f.name)
                settings.setValue(f.name, value)
            settings.sync()
        except Exception:
            # Silently fail - settings will use defaults next time
            pass
    
    @classmethod
    def load(cls) -> 'AppSettings':
        """Load settings from persistent storage.
        
        Returns default settings if file doesn't exist or can't be read.
        """
        instance = cls()  # Start with defaults
        
        try:
            settings = QSettings("EdgePowerMeter", "EdgePowerMeter")
            
            for f in fields(instance):
                if settings.contains(f.name):
                    stored = settings.value(f.name)
                    default_val = getattr(instance, f.name)
                    
                    # Type conversion based on default value type
                    if isinstance(default_val, bool):
                        # QSettings stores bools as strings on some platforms
                        if isinstance(stored, bool):
                            value = stored
                        elif isinstance(stored, str):
                            value = stored.lower() in ('true', '1', 'yes')
                        else:
                            value = bool(stored)
                    elif isinstance(default_val, int):
                        value = int(stored)
                    elif isinstance(default_val, float):
                        value = float(stored)
                    else:
                        value = str(stored)
                    setattr(instance, f.name, value)
        except Exception:
            # Return defaults if anything goes wrong
            pass
        
        return instance
