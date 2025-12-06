"""PDF report generation for EdgePowerMeter."""

from __future__ import annotations
from pathlib import Path
from datetime import datetime
from typing import List
from io import BytesIO
import csv
import numpy as np

from ..version import __version__, APP_NAME
from ..core import Statistics, MeasurementRecord, HarmonicAnalyzer, PowerSupplyAnalyzer


class ReportGenerator:
    """Generate PDF and CSV reports from measurement data."""
    
    def __init__(self):
        self.include_fft = False  # Set by caller based on settings
        self.include_harmonic_analysis = False
        self.include_psu_analysis = True  # Power supply quality analysis
        self.harmonic_max_order = 10
        self.harmonic_signal = "current"
        self.nominal_voltage = None  # Auto-detect if None
    
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
        
        # Frequency Spectrum Analysis (if enabled)
        if self.include_harmonic_analysis:
            story.append(PageBreak())
            story.append(Paragraph("Frequency Spectrum Analysis", section_style))
            story.append(Spacer(1, 5))
            
            # Perform frequency spectrum analysis
            signal_name = self.harmonic_signal.capitalize()
            analyzer = HarmonicAnalyzer(max_harmonics=self.harmonic_max_order)
            harmonic_result = analyzer.analyze_signal(records, self.harmonic_signal, max_display_freq=25.0)
            
            if harmonic_result:
                # Add description
                story.append(Paragraph(
                    f"Frequency spectrum analysis of {signal_name.lower()} signal. "
                    f"Shows dominant frequencies in the signal variations and overall modulation characteristics.",
                    ParagraphStyle(
                        name='HarmonicInfo',
                        parent=styles['Normal'],
                        fontSize=10,
                        textColor=colors.HexColor('#8b949e'),
                    )
                ))
                story.append(Spacer(1, 10))
                
                # Spectrum Summary Table
                thd_data = [
                    ["Metric", "Value"],
                    ["Dominant Frequency", f"{harmonic_result.fundamental_freq:.2f} Hz"],
                    ["Dominant Amplitude", f"{harmonic_result.fundamental_amplitude:.4f} {signal_name[0]}"],
                    ["Modulation Depth", f"{harmonic_result.thd_percent:.2f}%"],
                ]
                
                thd_table = Table(thd_data, colWidths=[150, 200])
                thd_table.setStyle(TableStyle([
                    ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#58a6ff')),
                    ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
                    ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                    ('FONTNAME', (0, 1), (-1, -1), 'Helvetica'),
                    ('FONTSIZE', (0, 0), (-1, -1), 10),
                    ('ALIGN', (0, 0), (0, -1), 'RIGHT'),
                    ('ALIGN', (1, 0), (1, -1), 'LEFT'),
                    ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#30363d')),
                    ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#f6f8fa')]),
                    ('TOPPADDING', (0, 0), (-1, -1), 8),
                    ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
                ]))
                story.append(thd_table)
                story.append(Spacer(1, 15))
                
                # Frequency spectrum graph
                harmonic_graph = self._generate_harmonic_graph(harmonic_result, signal_name, records)
                if harmonic_graph:
                    story.append(harmonic_graph)
            else:
                story.append(Paragraph(
                    f"⚠ Frequency spectrum analysis could not be performed on {signal_name.lower()} signal.",
                    ParagraphStyle(name='Warning', parent=styles['Normal'], 
                                  fontSize=11, textColor=colors.HexColor('#f85149'), 
                                  fontName='Helvetica-Bold')
                ))
                story.append(Spacer(1, 8))
                story.append(Paragraph(
                    "Possible reasons:<br/>"
                    "• Signal is too constant - requires measurable variations for spectrum analysis<br/>"
                    "• Signal amplitude is too low (&lt; 0.1mV/mA/mW)<br/>"
                    "• Insufficient data points (&lt; 100 samples)<br/>"
                    "<br/>"
                    "Suggestions:<br/>"
                    "• Ensure the system has dynamic load variations<br/>"
                    "• Increase measurement duration for better frequency resolution<br/>"
                    "• Try analyzing 'current' signal for switching power supplies",
                    ParagraphStyle(name='WarningDetails', parent=styles['Normal'], 
                                  fontSize=9, textColor=colors.HexColor('#8b949e'),
                                  leftIndent=20, bulletIndent=10)
                ))
        
        # Power Supply Quality Analysis (for DC systems)
        if self.include_psu_analysis:
            story.append(PageBreak())
            story.append(Paragraph("Power Supply Quality Analysis", section_style))
            story.append(Spacer(1, 5))
            
            # Perform PSU quality analysis
            psu_analyzer = PowerSupplyAnalyzer()
            psu_quality = psu_analyzer.analyze_voltage_quality(records, self.nominal_voltage)
            
            if psu_quality:
                # Add description
                story.append(Paragraph(
                    "DC power supply quality metrics including voltage regulation, ripple, "
                    "and load regulation analysis. These metrics help evaluate if the power "
                    "supply meets the requirements for your application.",
                    ParagraphStyle(
                        name='PSUInfo',
                        parent=styles['Normal'],
                        fontSize=10,
                        textColor=colors.HexColor('#8b949e'),
                    )
                ))
                story.append(Spacer(1, 10))
                
                # Quality Summary Table
                psu_data = [
                    ["Metric", "Value", "Status"],
                    ["Nominal Voltage", f"{psu_quality.nominal_voltage:.3f} V", ""],
                    ["Voltage Range", f"{psu_quality.min_voltage:.3f} - {psu_quality.max_voltage:.3f} V", ""],
                    ["Voltage Ripple (p-p)", f"{psu_quality.voltage_ripple_mv:.2f} mV ({psu_quality.voltage_ripple_percent:.3f}%)", ""],
                    ["RMS Noise", f"{psu_quality.rms_noise*1000:.2f} mV", ""],
                    ["Stability Rating", psu_quality.stability_rating, self._get_rating_symbol(psu_quality.stability_rating)],
                ]
                
                if psu_quality.load_regulation_percent is not None:
                    psu_data.append(["Load Regulation", f"{psu_quality.load_regulation_percent:.3f}%", ""])
                if psu_quality.settling_time_ms is not None:
                    psu_data.append(["Settling Time", f"{psu_quality.settling_time_ms:.1f} ms", ""])
                
                psu_table = Table(psu_data, colWidths=[150, 150, 80])
                psu_table.setStyle(TableStyle([
                    ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#58a6ff')),
                    ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
                    ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                    ('FONTNAME', (0, 1), (-1, -1), 'Helvetica'),
                    ('FONTSIZE', (0, 0), (-1, -1), 10),
                    ('ALIGN', (0, 0), (0, -1), 'RIGHT'),
                    ('ALIGN', (1, 0), (1, -1), 'LEFT'),
                    ('ALIGN', (2, 0), (2, -1), 'CENTER'),
                    ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#30363d')),
                    ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#f6f8fa')]),
                    ('TOPPADDING', (0, 0), (-1, -1), 8),
                    ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
                ]))
                story.append(psu_table)
                story.append(Spacer(1, 15))
                
                # Compliance Check
                story.append(Paragraph("Specification Compliance", 
                                      ParagraphStyle(name='PSUCompliance', parent=styles['Heading3'], 
                                                    fontSize=12, textColor=colors.HexColor('#58a6ff'))))
                story.append(Spacer(1, 8))
                
                compliance_data = [
                    ["Specification", "Requirement", "Status"],
                    ["Precision PSU", "< 0.05% ripple", "✓ Pass" if psu_quality.meets_005percent_spec else "✗ Fail"],
                    ["Linear PSU", "< 0.1% ripple", "✓ Pass" if psu_quality.meets_01percent_spec else "✗ Fail"],
                    ["Switching PSU", "< 1% ripple", "✓ Pass" if psu_quality.meets_1percent_spec else "✗ Fail"],
                ]
                
                compliance_table = Table(compliance_data, colWidths=[150, 120, 110])
                compliance_table.setStyle(TableStyle([
                    ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#58a6ff')),
                    ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
                    ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                    ('FONTNAME', (0, 1), (-1, -1), 'Helvetica'),
                    ('FONTSIZE', (0, 0), (-1, -1), 10),
                    ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
                    ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#30363d')),
                    ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#f6f8fa')]),
                    ('TOPPADDING', (0, 0), (-1, -1), 8),
                    ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
                ]))
                story.append(compliance_table)
                story.append(Spacer(1, 15))
                
                # Recommendations
                recommendations = PowerSupplyAnalyzer.get_quality_recommendations(psu_quality)
                if recommendations:
                    story.append(Paragraph("Recommendations", 
                                          ParagraphStyle(name='PSURecommendations', parent=styles['Heading3'], 
                                                        fontSize=12, textColor=colors.HexColor('#58a6ff'))))
                    story.append(Spacer(1, 8))
                    
                    for rec in recommendations:
                        story.append(Paragraph(
                            rec,
                            ParagraphStyle(name='Recommendation', parent=styles['Normal'], 
                                          fontSize=10, leftIndent=10, bulletIndent=5)
                        ))
                        story.append(Spacer(1, 3))
        
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
    
    @staticmethod
    def _get_rating_symbol(rating: str) -> str:
        """Get symbol for stability rating."""
        symbols = {
            "Excellent": "✓✓",
            "Good": "✓",
            "Fair": "~",
            "Poor": "✗"
        }
        return symbols.get(rating, "")
    
    def _generate_harmonic_graph(self, harmonic_result, signal_name: str, records: List[MeasurementRecord] = None):
        """Generate comprehensive frequency spectrum analysis graphs.
        
        Args:
            harmonic_result: HarmonicAnalysis object
            signal_name: Name of signal (e.g., "Current")
            records: Optional measurement records for waveform plot
            
        Returns:
            reportlab Image object or None
        """
        try:
            import matplotlib
            matplotlib.use('Agg')
            import matplotlib.pyplot as plt
            import numpy as np
            from reportlab.platypus import Image
            from reportlab.lib.units import mm
            
            # Create figure with 4 subplots
            fig = plt.figure(figsize=(10, 10))
            gs = fig.add_gridspec(3, 2, hspace=0.35, wspace=0.3)
            
            # 1. Signal Waveform (top, full width)
            ax_wave = fig.add_subplot(gs[0, :])
            if records and len(records) > 0:
                # Extract signal
                times = np.array([r.relative_time for r in records])
                if signal_name.lower() == 'current':
                    signal = np.array([r.current for r in records])
                    unit = 'A'
                elif signal_name.lower() == 'voltage':
                    signal = np.array([r.voltage for r in records])
                    unit = 'V'
                else:
                    signal = np.array([r.power for r in records])
                    unit = 'W'
                
                # Downsample if too many points
                if len(times) > 2000:
                    step = len(times) // 2000
                    times = times[::step]
                    signal = signal[::step]
                
                ax_wave.plot(times, signal, color='#58a6ff', linewidth=1.5, alpha=0.8)
                ax_wave.set_xlabel('Time (s)', fontsize=10, fontweight='bold')
                ax_wave.set_ylabel(f'{signal_name} ({unit})', fontsize=10, fontweight='bold')
                ax_wave.set_title(f'{signal_name} Waveform', fontsize=11, fontweight='bold')
                ax_wave.grid(True, alpha=0.3, linestyle='--')
            else:
                ax_wave.text(0.5, 0.5, 'Waveform data not available', 
                           ha='center', va='center', fontsize=10)
                ax_wave.set_xticks([])
                ax_wave.set_yticks([])
            
            # 2. Harmonic Bar Chart (middle left)
            ax_bar = fig.add_subplot(gs[1, 0])
            orders = [h.order for h in harmonic_result.harmonics]
            amplitudes = [h.amplitude for h in harmonic_result.harmonics]
            colors_list = ['#58a6ff' if o == 1 else '#f85149' for o in orders]
            
            bars = ax_bar.bar(orders, amplitudes, color=colors_list, alpha=0.8, 
                            edgecolor='black', linewidth=0.5)
            ax_bar.set_xlabel('Harmonic Order', fontsize=10, fontweight='bold')
            ax_bar.set_ylabel('Amplitude', fontsize=10, fontweight='bold')
            ax_bar.set_title('Harmonic Amplitudes', fontsize=11, fontweight='bold')
            ax_bar.grid(True, alpha=0.3, linestyle='--', axis='y')
            ax_bar.set_xticks(orders)
            
            # Add value labels on significant bars
            for bar, amp in zip(bars, amplitudes):
                if amp > max(amplitudes) * 0.1:  # Only label significant harmonics
                    height = bar.get_height()
                    ax_bar.text(bar.get_x() + bar.get_width()/2., height,
                              f'{amp:.3f}',
                              ha='center', va='bottom', fontsize=7)
            
            # 3. Percentage with IEC limits (middle right)
            ax_pct = fig.add_subplot(gs[1, 1])
            percentages = [h.percentage for h in harmonic_result.harmonics]
            
            if len(orders) > 1:  # Only plot if we have harmonics beyond fundamental
                ax_pct.bar(orders[1:], percentages[1:], color='#f85149', alpha=0.8, 
                         edgecolor='black', linewidth=0.5, label='Measured')
                
                # Add IEC limits
                iec_limits = HarmonicAnalyzer.get_harmonic_limits_iec()
                limit_orders = []
                limit_values = []
                for order, limit in iec_limits.items():
                    if order in orders[1:]:
                        limit_orders.append(order)
                        limit_values.append(limit)
                
                if limit_orders:
                    ax_pct.plot(limit_orders, limit_values, 'o--', color='#3fb950', 
                              linewidth=2, markersize=6, alpha=0.8, label='IEC Limit')
                
                ax_pct.set_xlabel('Harmonic Order', fontsize=10, fontweight='bold')
                ax_pct.set_ylabel('% of Fundamental', fontsize=10, fontweight='bold')
                ax_pct.set_title('Harmonic Distortion', fontsize=11, fontweight='bold')
                ax_pct.grid(True, alpha=0.3, linestyle='--')
                ax_pct.set_xticks(orders[1:])
                ax_pct.legend(loc='upper right', fontsize=8)
                
                # Add value labels
                for order, pct in zip(orders[1:], percentages[1:]):
                    if pct > max(percentages[1:]) * 0.1:
                        ax_pct.text(order, pct, f'{pct:.1f}%',
                                  ha='center', va='bottom', fontsize=7)
            else:
                ax_pct.text(0.5, 0.5, 'No harmonics detected', 
                          ha='center', va='center', fontsize=10)
                ax_pct.set_xticks([])
                ax_pct.set_yticks([])
            
            # 4. Full FFT Spectrum (bottom, full width)
            ax_fft = fig.add_subplot(gs[2, :])
            if harmonic_result.frequencies is not None and harmonic_result.magnitudes is not None:
                # Plot full spectrum
                freqs = harmonic_result.frequencies
                mags = harmonic_result.magnitudes
                
                # Limit to reasonable frequency range for visibility
                max_freq = min(1000, harmonic_result.fundamental_freq * 20)
                mask = freqs <= max_freq
                
                ax_fft.plot(freqs[mask], mags[mask], color='#8b949e', linewidth=1, alpha=0.6)
                ax_fft.fill_between(freqs[mask], mags[mask], alpha=0.2, color='#58a6ff')
                
                # Mark harmonic frequencies
                for h in harmonic_result.harmonics[:10]:  # First 10 harmonics
                    ax_fft.axvline(h.frequency, color='#f85149', linestyle='--', 
                                 linewidth=1, alpha=0.5)
                    if h.order <= 5:  # Label first 5
                        ax_fft.text(h.frequency, max(mags[mask]) * 0.9, f'{h.order}',
                                  ha='center', fontsize=8, color='#f85149', fontweight='bold')
                
                ax_fft.set_xlabel('Frequency (Hz)', fontsize=10, fontweight='bold')
                ax_fft.set_ylabel('Magnitude', fontsize=10, fontweight='bold')
                ax_fft.set_title(f'Full FFT Spectrum (Fundamental: {harmonic_result.fundamental_freq:.2f} Hz)', 
                               fontsize=11, fontweight='bold')
                ax_fft.grid(True, alpha=0.3, linestyle='--')
                ax_fft.set_xlim(0, max_freq)
            else:
                ax_fft.text(0.5, 0.5, 'FFT spectrum not available', 
                          ha='center', va='center', fontsize=10)
                ax_fft.set_xticks([])
                ax_fft.set_yticks([])
            
            plt.suptitle(f'Frequency Spectrum Analysis - {signal_name}', fontsize=13, fontweight='bold', y=0.995)
            
            # Save to buffer
            buf = BytesIO()
            plt.savefig(buf, format='png', dpi=150, bbox_inches='tight',
                       facecolor='white', edgecolor='none')
            buf.seek(0)
            plt.close(fig)
            
            return Image(buf, width=180*mm, height=180*mm)
            
            # Save to buffer
            buf = BytesIO()
            plt.savefig(buf, format='png', dpi=150, bbox_inches='tight',
                       facecolor='white', edgecolor='none')
            buf.seek(0)
            plt.close(fig)
            
            return Image(buf, width=170*mm, height=130*mm)
        
        except Exception as e:
            print(f"[WARNING] Failed to generate harmonic graph: {e}")
            return None

