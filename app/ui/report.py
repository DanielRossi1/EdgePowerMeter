"""Report generation for EdgePowerMeter."""

from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from datetime import datetime
from typing import List, TYPE_CHECKING
from io import BytesIO
import csv
import statistics as stats_module

from app.version import __version__, APP_NAME

if TYPE_CHECKING:
    pass


@dataclass
class MeasurementRecord:
    """Single measurement with timestamp."""
    timestamp: datetime
    unix_time: float
    voltage: float
    current: float
    power: float


@dataclass
class Statistics:
    """Statistical summary of measurements."""
    count: int
    duration_seconds: float
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
    
    @classmethod
    def from_records(cls, records: List[MeasurementRecord]) -> 'Statistics | None':
        """Calculate statistics from measurement records."""
        if len(records) < 2:
            return None
        
        voltages = [r.voltage for r in records]
        currents = [r.current for r in records]
        powers = [r.power for r in records]
        
        duration = records[-1].unix_time - records[0].unix_time
        if duration <= 0:
            duration = len(records) * 0.1
        
        # Calculate energy and charge using trapezoidal integration
        energy_ws = 0.0
        charge_as = 0.0
        for i in range(1, len(records)):
            dt = records[i].unix_time - records[i-1].unix_time
            avg_power = (records[i].power + records[i-1].power) / 2
            avg_current = (records[i].current + records[i-1].current) / 2
            energy_ws += avg_power * dt
            charge_as += avg_current * dt
        
        return cls(
            count=len(records),
            duration_seconds=duration,
            voltage_min=min(voltages),
            voltage_max=max(voltages),
            voltage_avg=stats_module.mean(voltages),
            voltage_std=stats_module.stdev(voltages) if len(voltages) > 1 else 0,
            current_min=min(currents),
            current_max=max(currents),
            current_avg=stats_module.mean(currents),
            current_std=stats_module.stdev(currents) if len(currents) > 1 else 0,
            power_min=min(powers),
            power_max=max(powers),
            power_avg=stats_module.mean(powers),
            power_std=stats_module.stdev(powers) if len(powers) > 1 else 0,
            energy_wh=energy_ws / 3600,
            charge_ah=charge_as / 3600,
        )


class CSVImporter:
    """Import measurement data from CSV files with auto-detection of format."""
    
    # Supported separators in order of priority
    SEPARATORS = [',', ';', '\t', ' ']
    
    # Supported timestamp formats
    TIMESTAMP_FORMATS = [
        '%Y-%m-%d %H:%M:%S.%f',
        '%Y-%m-%d %H:%M:%S',
        '%Y/%m/%d %H:%M:%S.%f',
        '%Y/%m/%d %H:%M:%S',
        '%d-%m-%Y %H:%M:%S.%f',
        '%d-%m-%Y %H:%M:%S',
        '%d/%m/%Y %H:%M:%S.%f',
        '%d/%m/%Y %H:%M:%S',
    ]
    
    @classmethod
    def detect_separator(cls, filepath: Path) -> str:
        """Detect the separator used in a CSV file.
        
        Args:
            filepath: Path to the CSV file
            
        Returns:
            Detected separator character
            
        Raises:
            ValueError: If no valid separator is detected
        """
        with open(filepath, 'r', encoding='utf-8') as f:
            # Read first few lines for detection
            lines = [f.readline() for _ in range(5)]
        
        # Filter out empty lines
        lines = [line.strip() for line in lines if line.strip()]
        if not lines:
            raise ValueError("File is empty")
        
        # Count separators in each line and find most consistent one
        best_separator = None
        best_score = 0
        
        for sep in cls.SEPARATORS:
            counts = [line.count(sep) for line in lines]
            # Good separator: consistent count >= 3 (need at least 4 columns)
            if counts and min(counts) >= 3 and max(counts) == min(counts):
                score = min(counts)
                if score > best_score:
                    best_score = score
                    best_separator = sep
        
        if best_separator is None:
            raise ValueError(
                "Could not detect CSV separator. "
                "Supported separators: comma, semicolon, tab, space"
            )
        
        return best_separator
    
    @classmethod
    def parse_timestamp(cls, ts_str: str) -> datetime:
        """Parse timestamp string trying multiple formats.
        
        Args:
            ts_str: Timestamp string
            
        Returns:
            Parsed datetime
            
        Raises:
            ValueError: If timestamp format is not recognized
        """
        ts_str = ts_str.strip()
        
        for fmt in cls.TIMESTAMP_FORMATS:
            try:
                return datetime.strptime(ts_str, fmt)
            except ValueError:
                continue
        
        raise ValueError(f"Unrecognized timestamp format: {ts_str}")
    
    @classmethod
    def import_csv(cls, filepath: Path) -> List[MeasurementRecord]:
        """Import measurements from a CSV file.
        
        Auto-detects separator and timestamp format. Expects columns:
        Timestamp, Voltage, Current, Power (header names are flexible).
        
        Args:
            filepath: Path to the CSV file
            
        Returns:
            List of MeasurementRecord objects
            
        Raises:
            ValueError: If file format is invalid
            FileNotFoundError: If file doesn't exist
        """
        if not filepath.exists():
            raise FileNotFoundError(f"File not found: {filepath}")
        
        separator = cls.detect_separator(filepath)
        records: List[MeasurementRecord] = []
        
        with open(filepath, 'r', encoding='utf-8') as f:
            reader = csv.reader(f, delimiter=separator)
            
            # Read header
            header = next(reader, None)
            if not header or len(header) < 4:
                raise ValueError(
                    "Invalid CSV format. Expected at least 4 columns: "
                    "Timestamp, Voltage, Current, Power"
                )
            
            # Parse data rows
            line_num = 1
            for row in reader:
                line_num += 1
                
                if len(row) < 4:
                    continue  # Skip incomplete rows
                
                try:
                    # Clean values (remove units if present)
                    ts_str = row[0].strip()
                    voltage = float(row[1].strip().replace(',', '.'))
                    current = float(row[2].strip().replace(',', '.'))
                    power = float(row[3].strip().replace(',', '.'))
                    
                    timestamp = cls.parse_timestamp(ts_str)
                    unix_time = timestamp.timestamp()
                    
                    records.append(MeasurementRecord(
                        timestamp=timestamp,
                        unix_time=unix_time,
                        voltage=voltage,
                        current=current,
                        power=power
                    ))
                except (ValueError, IndexError) as e:
                    # Skip invalid rows but continue processing
                    continue
        
        if not records:
            raise ValueError("No valid data rows found in CSV file")
        
        # Sort by timestamp to ensure correct order
        records.sort(key=lambda r: r.unix_time)
        
        return records


class ReportGenerator:
    """Generate PDF and CSV reports from measurement data."""
    
    def export_csv(self, filepath: Path, records: List[MeasurementRecord], 
                   separator: str = ',') -> None:
        """Export measurements to CSV file."""
        with open(filepath, 'w', newline='') as f:
            writer = csv.writer(f, delimiter=separator)
            writer.writerow(['Timestamp', 'Voltage[V]', 'Current[A]', 'Power[W]'])
            for r in records:
                writer.writerow([
                    r.timestamp.strftime('%Y-%m-%d %H:%M:%S.%f')[:-3],
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
        import matplotlib.dates as mdates
        from reportlab.platypus import Image
        from reportlab.lib.units import mm
        
        # Downsample if too many points (for performance)
        MAX_GRAPH_POINTS = 2000
        if len(records) > MAX_GRAPH_POINTS:
            step = len(records) // MAX_GRAPH_POINTS
            records = records[::step]
        
        # Extract data
        timestamps = [r.timestamp for r in records]
        voltages = [r.voltage for r in records]
        currents = [r.current for r in records]
        powers = [r.power for r in records]
        
        # Graph settings
        fig_width = 170 * mm / 25.4  # Convert mm to inches
        fig_height = 60 * mm / 25.4
        
        images = []
        
        # Define graph configurations
        graphs = [
            ('Voltage [V]', voltages, '#58a6ff'),  # Blue
            ('Current [A]', currents, '#f78166'),  # Orange
            ('Power [W]', powers, '#3fb950'),      # Green
        ]
        
        for title, data, color in graphs:
            fig, ax = plt.subplots(figsize=(fig_width, fig_height))
            
            ax.plot(timestamps, data, color=color, linewidth=0.8)
            ax.fill_between(timestamps, data, alpha=0.2, color=color)
            
            ax.set_ylabel(title, fontsize=10)
            ax.set_xlabel('Time', fontsize=9)
            ax.grid(True, alpha=0.3)
            
            # Format x-axis based on duration
            duration = (timestamps[-1] - timestamps[0]).total_seconds()
            if duration < 60:
                ax.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M:%S'))
            elif duration < 3600:
                ax.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M'))
            else:
                ax.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M'))
            
            plt.xticks(rotation=45, ha='right', fontsize=8)
            plt.yticks(fontsize=8)
            
            # Add min/max/avg annotations
            min_val = min(data)
            max_val = max(data)
            avg_val = sum(data) / len(data)
            
            ax.axhline(y=avg_val, color=color, linestyle='--', alpha=0.5, linewidth=0.8)
            ax.text(
                0.02, 0.95, 
                f'Min: {min_val:.4f}  Max: {max_val:.4f}  Avg: {avg_val:.4f}',
                transform=ax.transAxes,
                fontsize=8,
                verticalalignment='top',
                bbox=dict(boxstyle='round', facecolor='white', alpha=0.8)
            )
            
            plt.tight_layout()
            
            # Save to buffer
            buf = BytesIO()
            fig.savefig(buf, format='png', dpi=150, bbox_inches='tight')
            buf.seek(0)
            plt.close(fig)
            
            # Create reportlab Image
            img = Image(buf, width=170*mm, height=60*mm)
            images.append(img)
        
        return images
