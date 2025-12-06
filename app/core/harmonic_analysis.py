"""Harmonic analysis for power measurements.

Provides advanced signal analysis including:
- Total Harmonic Distortion (THD)
- Individual harmonic components
- Power Factor calculation
- Harmonic spectrum visualization
"""

from __future__ import annotations
from dataclasses import dataclass
from typing import List, Optional, TYPE_CHECKING
import numpy as np
from numpy.fft import rfft, rfftfreq

if TYPE_CHECKING:
    from .measurement import MeasurementRecord


@dataclass
class HarmonicComponent:
    """Single harmonic component information."""
    order: int  # Harmonic order (1=fundamental, 2=2nd harmonic, etc.)
    frequency: float  # Frequency in Hz
    amplitude: float  # Amplitude in original units
    percentage: float  # Percentage relative to fundamental (%)
    phase: float  # Phase in degrees


@dataclass
class HarmonicAnalysis:
    """Complete harmonic analysis results."""
    # Fundamental frequency
    fundamental_freq: float  # Hz
    fundamental_amplitude: float
    
    # THD metrics
    thd_percent: float  # Total Harmonic Distortion (%)
    thd_db: float  # THD in decibels
    
    # Harmonics (up to specified order)
    harmonics: List[HarmonicComponent]
    
    # Power quality metrics (optional, requires V-I sync)
    power_factor: Optional[float] = None
    displacement_factor: Optional[float] = None
    distortion_factor: Optional[float] = None
    
    # Frequency spectrum for plotting
    frequencies: np.ndarray = None  # Hz
    magnitudes: np.ndarray = None  # Amplitude
    
    def __post_init__(self):
        """Convert lists to numpy arrays if needed."""
        if self.frequencies is not None and not isinstance(self.frequencies, np.ndarray):
            self.frequencies = np.array(self.frequencies)
        if self.magnitudes is not None and not isinstance(self.magnitudes, np.ndarray):
            self.magnitudes = np.array(self.magnitudes)


class HarmonicAnalyzer:
    """Analyzer for frequency spectrum and power quality.
    
    Performs FFT analysis on DC systems with dynamic load variations.
    No assumptions about AC fundamentals - works for any signal variation.
    """
    
    def __init__(self, max_harmonics: int = 10):
        """Initialize spectrum analyzer.
        
        Args:
            max_harmonics: Maximum number of harmonics to analyze (for AC signals)
        """
        self.max_harmonics = max_harmonics
    
    def analyze_signal(self, records: List['MeasurementRecord'], 
                      signal_type: str = 'current',
                      max_display_freq: float = 25.0) -> Optional[HarmonicAnalysis]:
        """Perform frequency spectrum analysis on any signal (DC or AC).
        
        This method works for DC systems with dynamic load variations.
        It shows where the energy is concentrated in the frequency spectrum.
        
        Args:
            records: Measurement records
            signal_type: 'voltage', 'current', or 'power'
            max_display_freq: Maximum frequency to show in results (Hz)
            
        Returns:
            HarmonicAnalysis object with spectrum data, or None if analysis fails
        """
        if len(records) < 100:  # Need sufficient samples
            return None
        
        # Extract signal data
        if signal_type == 'voltage':
            signal = np.array([r.voltage for r in records])
        elif signal_type == 'current':
            signal = np.array([r.current for r in records])
        elif signal_type == 'power':
            signal = np.array([r.power for r in records])
        else:
            return None
        
        # Calculate sampling rate
        times = np.array([r.relative_time for r in records])
        dt = np.mean(np.diff(times))
        if dt <= 0:
            return None
        sample_rate = 1.0 / dt
        
        # Remove DC component (we analyze variations, not DC level)
        signal_mean = np.mean(signal)
        signal_ac = signal - signal_mean
        
        # Check if signal has sufficient variation
        signal_std = np.std(signal_ac)
        if signal_std < 0.0001:  # Signal too flat
            return None
        
        # Apply Hanning window to reduce spectral leakage
        window = np.hanning(len(signal_ac))
        signal_windowed = signal_ac * window
        
        # Compute FFT
        n = len(signal_windowed)
        fft_values = rfft(signal_windowed)
        fft_freq = rfftfreq(n, dt)
        fft_magnitude = np.abs(fft_values) * 2.0 / n  # Scale to actual amplitude
        
        # Limit display to max_display_freq
        freq_mask = fft_freq <= max_display_freq
        display_freq = fft_freq[freq_mask]
        display_mag = fft_magnitude[freq_mask]
        
        # Find dominant frequency (peak, excluding DC at index 0)
        if len(display_mag) > 1:
            peak_idx = np.argmax(display_mag[1:]) + 1
            dominant_freq = display_freq[peak_idx]
            dominant_amp = display_mag[peak_idx]
        else:
            dominant_freq = 0.0
            dominant_amp = 0.0
        
        # Calculate total AC power (RMS of all frequency components)
        ac_power = np.sqrt(np.sum(display_mag[1:] ** 2))  # Exclude DC
        
        # For DC systems, we can show "modulation depth" instead of THD
        # This indicates how much the signal varies relative to its mean
        if signal_mean > 0.0001:
            modulation_depth = (signal_std / signal_mean) * 100.0
        else:
            modulation_depth = 0.0
        
        return HarmonicAnalysis(
            fundamental_freq=dominant_freq,
            fundamental_amplitude=dominant_amp,
            thd_percent=modulation_depth,  # Repurposed as modulation depth for DC
            thd_db=20 * np.log10(modulation_depth / 100.0) if modulation_depth > 0 else -np.inf,
            harmonics=[],  # No harmonic extraction for DC systems
            frequencies=display_freq,
            magnitudes=display_mag
        )
    
    def analyze_spectrum(self, records: List['MeasurementRecord'], 
                        signal_type: str = 'current',
                        max_freq: Optional[float] = None) -> Optional[HarmonicAnalysis]:
        """Perform general frequency spectrum analysis (not limited to harmonics).
        
        This is useful for non-periodic signals or when you just want to see
        the frequency content without assuming a fundamental frequency.
        
        Args:
            records: Measurement records
            signal_type: 'voltage', 'current', or 'power'
            max_freq: Maximum frequency to include in results (Hz), None for Nyquist/2
            
        Returns:
            HarmonicAnalysis object with spectrum data, or None if analysis fails
        """
        if len(records) < 100:  # Need sufficient samples
            return None
        
        # Extract signal data
        if signal_type == 'voltage':
            signal = np.array([r.voltage for r in records])
        elif signal_type == 'current':
            signal = np.array([r.current for r in records])
        elif signal_type == 'power':
            signal = np.array([r.power for r in records])
        else:
            return None
        
        # Calculate sampling rate
        times = np.array([r.relative_time for r in records])
        dt = np.mean(np.diff(times))
        if dt <= 0:
            return None
        sample_rate = 1.0 / dt
        
        # Remove DC component
        signal_mean = np.mean(signal)
        signal = signal - signal_mean
        
        # Check if signal has sufficient variation
        signal_std = np.std(signal)
        if signal_std < 0.001:  # Too flat
            return None
        
        # Apply Hanning window
        window = np.hanning(len(signal))
        signal_windowed = signal * window
        
        # Compute FFT
        n = len(signal_windowed)
        fft_values = rfft(signal_windowed)
        fft_freq = rfftfreq(n, dt)
        fft_magnitude = np.abs(fft_values) * 2.0 / n
        
        # Limit to max_freq if specified
        if max_freq is not None:
            freq_mask = fft_freq <= max_freq
            fft_freq = fft_freq[freq_mask]
            fft_magnitude = fft_magnitude[freq_mask]
        
        # Find peak frequency (excluding DC at index 0)
        if len(fft_magnitude) > 1:
            peak_idx = np.argmax(fft_magnitude[1:]) + 1
            peak_freq = fft_freq[peak_idx]
            peak_amp = fft_magnitude[peak_idx]
        else:
            peak_freq = 0.0
            peak_amp = 0.0
        
        # Return analysis with spectrum data (no harmonics for general spectrum)
        return HarmonicAnalysis(
            fundamental_freq=peak_freq,
            fundamental_amplitude=peak_amp,
            thd_percent=0.0,
            thd_db=-np.inf,
            harmonics=[],
            frequencies=fft_freq,
            magnitudes=fft_magnitude
        )
    
    def analyze_power_factor(self, voltage_records: List['MeasurementRecord'],
                            current_records: List['MeasurementRecord']) -> Optional[float]:
        """Calculate power factor from synchronized voltage and current.
        
        This requires voltage and current measurements to be time-aligned.
        For single INA226 setup, this is limited as we only measure one side.
        
        Args:
            voltage_records: Voltage measurements
            current_records: Current measurements (must be time-aligned)
            
        Returns:
            Power factor (0-1) or None if cannot calculate
        """
        if len(voltage_records) != len(current_records):
            return None
        
        if len(voltage_records) < 100:
            return None
        
        # Extract signals
        voltage = np.array([r.voltage for r in voltage_records])
        current = np.array([r.current for r in current_records])
        power = np.array([r.power for r in voltage_records])
        
        # Calculate apparent power (S = Vrms * Irms)
        v_rms = np.sqrt(np.mean(voltage ** 2))
        i_rms = np.sqrt(np.mean(current ** 2))
        apparent_power = v_rms * i_rms
        
        if apparent_power < 1e-6:
            return None
        
        # Calculate real power (P = average of instantaneous power)
        real_power = np.mean(power)
        
        # Power factor = P / S
        power_factor = real_power / apparent_power
        
        # Clamp to valid range [0, 1]
        return max(0.0, min(1.0, abs(power_factor)))
    
    @staticmethod
    def get_harmonic_limits_iec() -> dict:
        """Get IEC 61000-3-2 harmonic current limits.
        
        Returns dictionary of {harmonic_order: limit_percentage}
        These are typical limits for Class A equipment.
        """
        return {
            3: 86.0,   # 3rd harmonic: 86% of fundamental
            5: 61.0,   # 5th harmonic: 61%
            7: 43.0,   # 7th harmonic: 43%
            9: 28.0,   # 9th harmonic: 28%
            11: 20.0,  # 11th harmonic: 20%
            13: 15.0,  # 13th harmonic: 15%
        }
    
    def check_compliance(self, analysis: HarmonicAnalysis) -> dict:
        """Check if harmonics comply with IEC limits.
        
        Args:
            analysis: Harmonic analysis results
            
        Returns:
            Dictionary with compliance status for each harmonic
        """
        limits = self.get_harmonic_limits_iec()
        compliance = {}
        
        for harmonic in analysis.harmonics:
            if harmonic.order in limits:
                limit = limits[harmonic.order]
                is_compliant = harmonic.percentage <= limit
                compliance[harmonic.order] = {
                    'measured': harmonic.percentage,
                    'limit': limit,
                    'compliant': is_compliant,
                    'margin': limit - harmonic.percentage
                }
        
        return compliance
