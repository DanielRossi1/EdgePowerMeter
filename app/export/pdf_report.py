"""PDF report generation for EdgePowerMeter."""

from __future__ import annotations
from pathlib import Path
from datetime import datetime
from typing import List
from io import BytesIO
import csv

from ..version import __version__, APP_NAME
from ..core import Statistics, MeasurementRecord


class ReportGenerator:
    """Generate PDF and CSV reports from measurement data."""
    
    def __init__(self):
        self.include_fft = False  # Set by caller based on settings
    
    def export_csv(self, filepath: Path, records: List[MeasurementRecord], 
                   separator: str = ',') -> None:
        """Export measurements to CSV file.
        
        Includes both absolute timestamp and relative time (seconds from start).
        """
        with open(filepath, 'w', newline='') as f:
            writer = csv.writer(f, delimiter=separator)
            writer.writerow(['Timestamp', 'RelativeTime[s]', 'Voltage[V]', 'Current[A]', 'Power[W]'])
            for r in records:
                writer.writerow([
                    r.timestamp.strftime('%Y-%m-%d %H:%M:%S.%f')[:-3],
                    f"{r.relative_time:.6f}",
                    f"{r.voltage:.6f}",
                    f"{r.current:.6f}",
                    f"{r.power:.6f}",
                ])
    
    def export_pdf(self, filepath: Path, stats: Statistics, 
                   records: List[MeasurementRecord]) -> None:
        """Export report to PDF file with graphs."""
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.units import mm
        from reportlab.lib.enums import TA_CENTER
        from reportlab.platypus import (
            SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, 
            KeepTogether, Image, PageBreak
        )
        
        doc = SimpleDocTemplate(
            str(filepath),
            pagesize=A4,
            rightMargin=20*mm,
            leftMargin=20*mm,
            topMargin=20*mm,
            bottomMargin=20*mm,
        )
        
        styles = getSampleStyleSheet()
        
        # Custom styles
        title_style = ParagraphStyle(
            name='ReportTitle',
            parent=styles['Heading1'],
            fontSize=24,
            spaceAfter=20,
            textColor=colors.HexColor('#1a1a2e'),
            alignment=TA_CENTER,
        )
        
        section_style = ParagraphStyle(
            name='SectionTitle',
            parent=styles['Heading2'],
            fontSize=14,
            spaceBefore=20,
            spaceAfter=10,
            textColor=colors.HexColor('#58a6ff'),
        )
        
        story = []
        
        # Title
        story.append(Paragraph("EdgePowerMeter Report", title_style))
        story.append(Spacer(1, 10))
        
        # Metadata
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        start = records[0].timestamp.strftime("%Y-%m-%d %H:%M:%S") if records else "N/A"
        end = records[-1].timestamp.strftime("%Y-%m-%d %H:%M:%S") if records else "N/A"
        
        meta_data = [
            ["Report Generated:", now],
            ["Recording Start:", start],
            ["Recording End:", end],
            ["Total Samples:", f"{stats.count:,}"],
            ["Duration:", self._format_duration(stats.duration_seconds)],
        ]
        
        meta_table = Table(meta_data, colWidths=[100, 200])
        meta_table.setStyle(TableStyle([
            ('FONTNAME', (0, 0), (-1, -1), 'Helvetica'),
            ('FONTSIZE', (0, 0), (-1, -1), 10),
            ('TEXTCOLOR', (0, 0), (0, -1), colors.HexColor('#8b949e')),
            ('ALIGN', (0, 0), (0, -1), 'RIGHT'),
            ('ALIGN', (1, 0), (1, -1), 'LEFT'),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ]))
        story.append(meta_table)
        story.append(Spacer(1, 20))
        
        # Summary Statistics - keep title and table together
        summary_data = [
            ["Metric", "Min", "Max", "Average", "Std Dev"],
            ["Voltage (V)", 
             f"{stats.voltage_min:.4f}", f"{stats.voltage_max:.4f}", 
             f"{stats.voltage_avg:.4f}", f"{stats.voltage_std:.4f}"],
            ["Current (A)", 
             f"{stats.current_min:.4f}", f"{stats.current_max:.4f}", 
             f"{stats.current_avg:.4f}", f"{stats.current_std:.4f}"],
            ["Power (W)", 
             f"{stats.power_min:.4f}", f"{stats.power_max:.4f}", 
             f"{stats.power_avg:.4f}", f"{stats.power_std:.4f}"],
        ]
        
        summary_table = Table(summary_data, colWidths=[80, 70, 70, 70, 70])
        summary_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#58a6ff')),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTNAME', (0, 1), (-1, -1), 'Helvetica'),
            ('FONTSIZE', (0, 0), (-1, -1), 10),
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
            ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#30363d')),
            ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#f6f8fa')]),
            ('TOPPADDING', (0, 0), (-1, -1), 8),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
        ]))
        story.append(KeepTogether([
            Paragraph("Summary Statistics", section_style),
            summary_table,
            Spacer(1, 20),
        ]))
        
        # Energy Analysis - keep title and table together
        energy_data = [
            ["Metric", "Value", "Unit"],
            ["Total Energy", f"{stats.energy_wh:.6f}", "Wh"],
            ["Total Energy", f"{stats.energy_wh * 1000:.4f}", "mWh"],
            ["Total Charge", f"{stats.charge_ah:.6f}", "Ah"],
            ["Total Charge", f"{stats.charge_ah * 1000:.4f}", "mAh"],
            ["Average Power", f"{stats.power_avg:.4f}", "W"],
            ["Peak Power", f"{stats.power_max:.4f}", "W"],
        ]
        
        energy_table = Table(energy_data, colWidths=[120, 100, 60])
        energy_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#3fb950')),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTNAME', (0, 1), (-1, -1), 'Helvetica'),
            ('FONTSIZE', (0, 0), (-1, -1), 10),
            ('ALIGN', (1, 0), (-1, -1), 'CENTER'),
            ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#30363d')),
            ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#f6f8fa')]),
            ('TOPPADDING', (0, 0), (-1, -1), 8),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
        ]))
        story.append(KeepTogether([
            Paragraph("Energy Analysis", section_style),
            energy_table,
            Spacer(1, 20),
        ]))
        
        # Derived Metrics - keep title and table together
        sampling_rate = stats.count / stats.duration_seconds if stats.duration_seconds > 0 else 0
        power_factor = stats.power_avg / (stats.voltage_avg * stats.current_avg) if (stats.voltage_avg * stats.current_avg) > 0 else 0
        impedance = stats.voltage_avg / stats.current_avg if stats.current_avg > 0 else 0
        
        derived_data = [
            ["Metric", "Value", "Description"],
            ["Sampling Rate", f"{sampling_rate:.1f} Hz", "Samples per second"],
            ["Voltage Ripple", f"{stats.voltage_max - stats.voltage_min:.4f} V", "Peak-to-peak"],
            ["Current Ripple", f"{stats.current_max - stats.current_min:.4f} A", "Peak-to-peak"],
            ["Power Factor Est.", f"{power_factor:.3f}", "P / (V × I)"],
            ["Impedance Est.", f"{impedance:.2f} Ω", "V / I average"],
        ]
        
        derived_table = Table(derived_data, colWidths=[100, 80, 180])
        derived_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#a371f7')),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTNAME', (0, 1), (-1, -1), 'Helvetica'),
            ('FONTSIZE', (0, 0), (-1, -1), 9),
            ('ALIGN', (1, 0), (1, -1), 'CENTER'),
            ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#30363d')),
            ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#f6f8fa')]),
            ('TOPPADDING', (0, 0), (-1, -1), 6),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ]))
        story.append(KeepTogether([
            Paragraph("Derived Metrics", section_style),
            derived_table,
        ]))
        
        # Generate graphs
        story.append(PageBreak())
        story.append(Paragraph("Measurement Graphs", section_style))
        story.append(Spacer(1, 10))
        
        # Create graphs using matplotlib
        graph_images = self._generate_graphs(records)
        for img in graph_images:
            story.append(img)
            story.append(Spacer(1, 10))
        
        # FFT Analysis (if enabled)
        if self.include_fft:
            story.append(PageBreak())
            story.append(Paragraph("Frequency Spectrum Analysis", section_style))
            story.append(Spacer(1, 5))
            story.append(Paragraph(
                "FFT analysis of current signal to identify switching noise, ripple, and periodic patterns.",
                ParagraphStyle(
                    name='FFTInfo',
                    parent=styles['Normal'],
                    fontSize=10,
                    textColor=colors.HexColor('#8b949e'),
                )
            ))
            story.append(Spacer(1, 10))
            
            fft_image = self._generate_fft_graph(records)
            if fft_image:
                story.append(fft_image)
        
        # Footer
        story.append(Spacer(1, 30))
        story.append(Paragraph(
            f"Generated by {APP_NAME} v{__version__}",
            ParagraphStyle(
                name='Footer',
                parent=styles['Normal'],
                fontSize=8,
                textColor=colors.HexColor('#8b949e'),
                alignment=TA_CENTER,
            )
        ))
        
        doc.build(story)
    
    @staticmethod
    def _format_duration(seconds: float) -> str:
        """Format duration in human-readable format."""
        if seconds < 60:
            return f"{seconds:.1f} seconds"
        elif seconds < 3600:
            return f"{seconds / 60:.1f} minutes"
        else:
            return f"{seconds / 3600:.2f} hours"
    
    def _generate_graphs(self, records: List[MeasurementRecord]) -> list:
        """Generate graphs for voltage, current, and power.
        
        Args:
            records: List of measurement records
            
        Returns:
            List of reportlab Image objects
        """
        import matplotlib
        matplotlib.use('Agg')  # Non-interactive backend
        import matplotlib.pyplot as plt
        from reportlab.platypus import Image
        from reportlab.lib.units import mm
        
        # Downsample if too many points (for performance)
        MAX_GRAPH_POINTS = 2000
        if len(records) > MAX_GRAPH_POINTS:
            step = len(records) // MAX_GRAPH_POINTS
            records = records[::step]
        
        # Extract data - use relative_time for X axis (starts from 0)
        times = [r.relative_time for r in records]
        voltages = [r.voltage for r in records]
        currents = [r.current for r in records]
        powers = [r.power for r in records]
        
        # Graph settings
        fig_width = 170 * mm / 25.4  # Convert mm to inches
        fig_height = 70 * mm / 25.4  # Slightly taller for better readability
        
        images = []
        
        # Define graph configurations - darker, more visible colors
        graphs = [
            ('Voltage [V]', voltages, '#1f77b4', '#0d3d6e'),  # Blue
            ('Current [A]', currents, '#d62728', '#8b1a1a'),  # Red
            ('Power [W]', powers, '#2ca02c', '#1a5c1a'),      # Green
        ]
        
        for title, data, line_color, fill_color in graphs:
            fig, ax = plt.subplots(figsize=(fig_width, fig_height))
            
            # Set white background
            ax.set_facecolor('white')
            fig.patch.set_facecolor('white')
            
            # Thicker line for better visibility
            ax.plot(times, data, color=line_color, linewidth=1.5)
            ax.fill_between(times, data, alpha=0.3, color=fill_color)
            
            ax.set_ylabel(title, fontsize=11, fontweight='bold')
            ax.set_xlabel('Time [s]', fontsize=10)
            ax.grid(True, alpha=0.4, linestyle='-', linewidth=0.5)
            
            # Format x-axis based on duration (in seconds from 0)
            duration = times[-1] - times[0] if times else 0
            if duration > 3600:
                # Show in minutes for long recordings
                ax.set_xlabel('Time [min]', fontsize=10)
                ax.set_xticks([t for t in range(0, int(duration) + 1, int(duration / 10) or 1)])
                ax.set_xticklabels([f'{t/60:.1f}' for t in ax.get_xticks()])
            
            plt.xticks(fontsize=9)
            plt.yticks(fontsize=9)
            
            # Add min/max/avg annotations
            min_val = min(data)
            max_val = max(data)
            avg_val = sum(data) / len(data)
            
            # Average line - more visible
            ax.axhline(y=avg_val, color=line_color, linestyle='--', alpha=0.7, linewidth=1.2)
            
            # Stats box with better formatting
            stats_text = f'Min: {min_val:.4f}  |  Max: {max_val:.4f}  |  Avg: {avg_val:.4f}'
            ax.text(
                0.02, 0.95, 
                stats_text,
                transform=ax.transAxes,
                fontsize=9,
                fontweight='bold',
                verticalalignment='top',
                bbox=dict(boxstyle='round,pad=0.4', facecolor='white', 
                         edgecolor=line_color, alpha=0.9, linewidth=1.5)
            )
            
            # Add some padding to y-axis
            y_range = max_val - min_val
            if y_range > 0:
                ax.set_ylim(min_val - y_range * 0.1, max_val + y_range * 0.15)
            
            plt.tight_layout()
            
            # Save to buffer - higher DPI for better quality
            buf = BytesIO()
            fig.savefig(buf, format='png', dpi=200, bbox_inches='tight', 
                       facecolor='white', edgecolor='none')
            buf.seek(0)
            plt.close(fig)
            
            # Create reportlab Image
            img = Image(buf, width=170*mm, height=70*mm)
            images.append(img)
        
        return images
    
    def _generate_fft_graph(self, records: List[MeasurementRecord]):
        """Generate FFT spectrum analysis of current signal.
        
        Args:
            records: List of measurement records
            
        Returns:
            reportlab Image object or None if insufficient data
        """
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        import numpy as np
        from reportlab.platypus import Image
        from reportlab.lib.units import mm
        
        if len(records) < 64:
            return None  # Need enough samples for meaningful FFT
        
        # Extract current data
        currents = np.array([r.current for r in records])
        times = np.array([r.relative_time for r in records])
        
        # Calculate sampling rate
        dt = np.mean(np.diff(times))
        if dt <= 0:
            return None
        fs = 1.0 / dt  # Sampling frequency
        
        # Remove DC component (mean)
        currents_ac = currents - np.mean(currents)
        
        # Apply window to reduce spectral leakage
        window = np.hanning(len(currents_ac))
        currents_windowed = currents_ac * window
        
        # Compute FFT
        n = len(currents_windowed)
        fft_result = np.fft.rfft(currents_windowed)
        freqs = np.fft.rfftfreq(n, dt)
        
        # Compute magnitude spectrum (in dB relative to max)
        magnitude = np.abs(fft_result) * 2 / n  # Scale for single-sided spectrum
        
        # Avoid log of zero
        magnitude[magnitude < 1e-10] = 1e-10
        magnitude_db = 20 * np.log10(magnitude / np.max(magnitude))
        
        # Create figure
        fig_width = 170 * mm / 25.4
        fig_height = 90 * mm / 25.4
        
        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(fig_width, fig_height))
        fig.patch.set_facecolor('white')
        
        # Plot 1: Linear frequency spectrum
        ax1.set_facecolor('white')
        ax1.plot(freqs, magnitude * 1000, color='#d62728', linewidth=1.2)  # mA
        ax1.fill_between(freqs, magnitude * 1000, alpha=0.3, color='#d62728')
        ax1.set_xlabel('Frequency [Hz]', fontsize=10)
        ax1.set_ylabel('Amplitude [mA]', fontsize=10)
        ax1.set_title('Current Spectrum (Linear)', fontsize=11, fontweight='bold')
        ax1.grid(True, alpha=0.4)
        ax1.set_xlim(0, min(fs/2, 1000))  # Limit to 1kHz or Nyquist
        
        # Plot 2: Log frequency spectrum (dB)
        ax2.set_facecolor('white')
        ax2.plot(freqs, magnitude_db, color='#1f77b4', linewidth=1.2)
        ax2.fill_between(freqs, magnitude_db, -100, alpha=0.3, color='#1f77b4')
        ax2.set_xlabel('Frequency [Hz]', fontsize=10)
        ax2.set_ylabel('Magnitude [dB]', fontsize=10)
        ax2.set_title('Current Spectrum (Logarithmic)', fontsize=11, fontweight='bold')
        ax2.grid(True, alpha=0.4)
        ax2.set_xlim(0, min(fs/2, 1000))
        ax2.set_ylim(-80, 5)
        
        # Find and annotate dominant frequencies
        # Skip DC (index 0) and find peaks
        peak_threshold = -30  # dB
        peaks_idx = []
        for i in range(1, len(magnitude_db) - 1):
            if (magnitude_db[i] > magnitude_db[i-1] and 
                magnitude_db[i] > magnitude_db[i+1] and
                magnitude_db[i] > peak_threshold):
                peaks_idx.append(i)
        
        # Annotate top 5 peaks
        peak_mags = [(i, magnitude_db[i]) for i in peaks_idx]
        peak_mags.sort(key=lambda x: x[1], reverse=True)
        
        peak_info = []
        for i, (idx, mag) in enumerate(peak_mags[:5]):
            freq = freqs[idx]
            if freq > 1:  # Skip very low frequencies
                ax2.annotate(
                    f'{freq:.1f} Hz',
                    xy=(freq, mag),
                    xytext=(5, 10),
                    textcoords='offset points',
                    fontsize=8,
                    color='#1f77b4',
                    fontweight='bold'
                )
                peak_info.append(f'{freq:.1f} Hz')
        
        # Add info box
        info_text = f'Sampling: {fs:.1f} Hz | Nyquist: {fs/2:.1f} Hz'
        if peak_info:
            info_text += f'\nDominant: {", ".join(peak_info[:3])}'
        
        ax1.text(
            0.98, 0.95, info_text,
            transform=ax1.transAxes,
            fontsize=8,
            verticalalignment='top',
            horizontalalignment='right',
            bbox=dict(boxstyle='round,pad=0.3', facecolor='white', 
                     edgecolor='#d62728', alpha=0.9)
        )
        
        plt.tight_layout()
        
        # Save to buffer
        buf = BytesIO()
        fig.savefig(buf, format='png', dpi=200, bbox_inches='tight',
                   facecolor='white', edgecolor='none')
        buf.seek(0)
        plt.close(fig)
        
        return Image(buf, width=170*mm, height=90*mm)
