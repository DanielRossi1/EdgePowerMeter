"""Power supply quality analysis for DC systems.

Provides metrics to evaluate DC power supply performance:
- Voltage regulation and ripple
- Load regulation
- Stability and noise analysis
"""

from __future__ import annotations
from dataclasses import dataclass
from typing import List, Optional, TYPE_CHECKING
import numpy as np

if TYPE_CHECKING:
    from .measurement import MeasurementRecord


@dataclass
class PowerSupplyQuality:
    """Power supply quality metrics for DC systems."""
    # Voltage regulation
    nominal_voltage: float  # Expected/average voltage (V)
    min_voltage: float  # Minimum voltage observed (V)
    max_voltage: float  # Maximum voltage observed (V)
    voltage_ripple_percent: float  # Peak-to-peak variation as % of nominal
    voltage_ripple_mv: float  # Peak-to-peak variation in mV
    
    # Load regulation (if load changes detected)
    load_regulation_percent: Optional[float] = None  # Voltage change per load change
    settling_time_ms: Optional[float] = None  # Time to stabilize after load change
    
    # Stability metrics
    rms_noise: float = 0.0  # RMS noise/variation (V)
    std_deviation: float = 0.0  # Standard deviation (V)
    stability_rating: str = "Unknown"  # Excellent/Good/Fair/Poor
    
    # Compliance flags
    meets_1percent_spec: bool = False  # <1% ripple (switching PSU typical)
    meets_01percent_spec: bool = False  # <0.1% ripple (linear PSU typical)
    meets_005percent_spec: bool = False  # <0.05% ripple (precision PSU)


class PowerSupplyAnalyzer:
    """Analyzer for DC power supply quality."""
    
    # Quality rating thresholds (% of nominal voltage)
    EXCELLENT_THRESHOLD = 0.05  # <0.05% ripple
    GOOD_THRESHOLD = 0.1        # <0.1% ripple
    FAIR_THRESHOLD = 1.0        # <1% ripple
    # >1% is Poor
    
    def analyze_voltage_quality(self, records: List['MeasurementRecord'],
                               nominal_voltage: Optional[float] = None) -> Optional[PowerSupplyQuality]:
        """Analyze DC voltage quality from measurement records.
        
        Args:
            records: Measurement records
            nominal_voltage: Expected nominal voltage (V). If None, uses mean.
            
        Returns:
            PowerSupplyQuality object or None if insufficient data
        """
        if len(records) < 10:
            return None
        
        # Extract voltage data
        voltages = np.array([r.voltage for r in records])
        
        # Calculate basic statistics
        v_min = np.min(voltages)
        v_max = np.max(voltages)
        v_mean = np.mean(voltages)
        v_std = np.std(voltages)
        
        # Use mean as nominal if not specified
        if nominal_voltage is None:
            nominal_voltage = v_mean
        
        # Calculate ripple (peak-to-peak variation)
        voltage_ripple_v = v_max - v_min
        voltage_ripple_mv = voltage_ripple_v * 1000.0
        voltage_ripple_percent = (voltage_ripple_v / nominal_voltage) * 100.0
        
        # Calculate RMS noise (variation from mean)
        rms_noise = np.sqrt(np.mean((voltages - v_mean) ** 2))
        
        # Determine stability rating
        ripple_pct = voltage_ripple_percent
        if ripple_pct < self.EXCELLENT_THRESHOLD:
            stability_rating = "Excellent"
        elif ripple_pct < self.GOOD_THRESHOLD:
            stability_rating = "Good"
        elif ripple_pct < self.FAIR_THRESHOLD:
            stability_rating = "Fair"
        else:
            stability_rating = "Poor"
        
        # Check compliance with common specs
        meets_1percent = voltage_ripple_percent < 1.0
        meets_01percent = voltage_ripple_percent < 0.1
        meets_005percent = voltage_ripple_percent < 0.05
        
        # Analyze load regulation (if we detect load changes)
        load_regulation = None
        settling_time = None
        
        currents = np.array([r.current for r in records])
        if len(currents) > 10:
            load_regulation, settling_time = self._analyze_load_regulation(
                records, voltages, currents, nominal_voltage
            )
        
        return PowerSupplyQuality(
            nominal_voltage=nominal_voltage,
            min_voltage=v_min,
            max_voltage=v_max,
            voltage_ripple_percent=voltage_ripple_percent,
            voltage_ripple_mv=voltage_ripple_mv,
            load_regulation_percent=load_regulation,
            settling_time_ms=settling_time,
            rms_noise=rms_noise,
            std_deviation=v_std,
            stability_rating=stability_rating,
            meets_1percent_spec=meets_1percent,
            meets_01percent_spec=meets_01percent,
            meets_005percent_spec=meets_005percent
        )
    
    def _analyze_load_regulation(self, records: List['MeasurementRecord'],
                                voltages: np.ndarray, currents: np.ndarray,
                                nominal_voltage: float) -> tuple[Optional[float], Optional[float]]:
        """Analyze load regulation by detecting load steps.
        
        Returns:
            (load_regulation_percent, settling_time_ms) tuple
        """
        # Detect significant current changes (load steps)
        current_diff = np.abs(np.diff(currents))
        threshold = np.std(currents) * 2.0  # 2 sigma threshold
        
        # Find load step indices
        step_indices = np.where(current_diff > threshold)[0]
        
        if len(step_indices) == 0:
            return None, None
        
        # Analyze first significant load step
        step_idx = step_indices[0]
        
        # Get voltage before and after step
        if step_idx < 5 or step_idx >= len(voltages) - 20:
            return None, None
        
        v_before = np.mean(voltages[max(0, step_idx-5):step_idx])
        
        # Find settling point (where voltage stabilizes)
        window_size = 10
        settling_idx = None
        for i in range(step_idx + 1, min(step_idx + 100, len(voltages) - window_size)):
            window = voltages[i:i+window_size]
            if np.std(window) < 0.001:  # Voltage has stabilized
                settling_idx = i
                break
        
        if settling_idx is None:
            # Use fixed window if no clear settling point
            settling_idx = min(step_idx + 20, len(voltages) - 1)
        
        v_after = np.mean(voltages[settling_idx:settling_idx+5])
        
        # Calculate load regulation
        voltage_change = abs(v_after - v_before)
        load_regulation_percent = (voltage_change / nominal_voltage) * 100.0
        
        # Calculate settling time
        times = np.array([r.relative_time for r in records])
        settling_time_ms = (times[settling_idx] - times[step_idx]) * 1000.0
        
        return load_regulation_percent, settling_time_ms
    
    @staticmethod
    def get_quality_recommendations(quality: PowerSupplyQuality) -> List[str]:
        """Get recommendations based on power supply quality analysis.
        
        Args:
            quality: PowerSupplyQuality analysis results
            
        Returns:
            List of recommendation strings
        """
        recommendations = []
        
        if quality.stability_rating == "Excellent":
            recommendations.append("✓ Excellent voltage stability - suitable for precision applications")
        elif quality.stability_rating == "Good":
            recommendations.append("✓ Good voltage stability - suitable for most applications")
        elif quality.stability_rating == "Fair":
            recommendations.append("⚠ Fair voltage stability - consider upgrading for sensitive loads")
            recommendations.append("• Add output filtering capacitors to reduce ripple")
        else:  # Poor
            recommendations.append("✗ Poor voltage stability - not recommended for sensitive electronics")
            recommendations.append("• Consider replacing power supply")
            recommendations.append("• Add LC filter to output")
            recommendations.append("• Check for loose connections or damaged components")
        
        # Load regulation feedback
        if quality.load_regulation_percent is not None:
            if quality.load_regulation_percent < 0.5:
                recommendations.append("✓ Excellent load regulation")
            elif quality.load_regulation_percent < 1.0:
                recommendations.append("✓ Good load regulation")
            elif quality.load_regulation_percent < 3.0:
                recommendations.append("⚠ Fair load regulation - voltage drops under load")
            else:
                recommendations.append("✗ Poor load regulation - significant voltage drop under load")
        
        # Settling time feedback
        if quality.settling_time_ms is not None:
            if quality.settling_time_ms < 10:
                recommendations.append("✓ Fast transient response (<10ms)")
            elif quality.settling_time_ms < 100:
                recommendations.append("✓ Good transient response (<100ms)")
            else:
                recommendations.append("⚠ Slow transient response - may affect dynamic loads")
        
        return recommendations
