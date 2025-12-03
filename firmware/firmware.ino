/**
 * @file EdgePowerMeter.ino
 * @brief Real-time power monitoring firmware for embedded AI workload analysis
 * @version 1.3.0
 * 
 * This firmware reads voltage, current, and power measurements from an INA226
 * power monitor and outputs CSV-formatted data via serial for logging and analysis.
 * 
 * Architecture:
 *   ESP32-C3 is single-core RISC-V. Optimization is achieved through:
 *   - High baudrate serial (921600) for faster data transfer
 *   - Efficient buffered output to minimize blocking
 *   - Optimized measurement timing with hardware interrupts
 * 
 * Timing System:
 *   Uses DS3231 SQW pin (1Hz) for precise second boundaries, combined with
 *   millis() for sub-second resolution. This provides millisecond-accurate
 *   timestamps with minimal drift (DS3231 accuracy: ±2ppm = ~1 min/year).
 * 
 * Hardware:
 *   - MCU: ESP32-C3 SuperMini (single-core RISC-V @ 160MHz)
 *   - Power Monitor: INA226 (I2C address 0x40)
 *   - Display: SSD1306 OLED 128x32 (I2C address 0x3C)
 *   - RTC: DS3231 (I2C) with SQW connected to GPIO
 *   - Shunt Resistor: 0.01Ω (R2512)
 * 
 * Serial Output Format (921600 baud):
 *   Timestamp,Voltage[V],Current[A],Power[W]
 *   2025-11-30 12:34:56.123,12.345,1.234,15.234
 * 
 * Libraries:
 *   This firmware uses reusable libraries from lib/:
 *   - PrecisionTime: Millisecond-accurate timestamps with DS3231 SQW
 *   - OLEDStatus: Status display utilities for SSD1306
 * 
 * @author Daniel Rossi
 * @license Apache-2.0
 */

#include <Wire.h>
#include <RTClib.h>
#include <INA226.h>

// Local library files (in same folder as sketch)
#include "PrecisionTime.h"
#include "OLEDStatus.h"

// =============================================================================
// Version
// =============================================================================

#define FIRMWARE_VERSION "1.3.0"
#define FIRMWARE_NAME "EdgePowerMeter"

// =============================================================================
// Configuration
// =============================================================================

namespace Config {
    // Pin configuration
    constexpr uint8_t SQW_PIN = 3;  // DS3231 SQW output -> ESP32 GPIO3 (A3)
    
    // Display settings
    constexpr uint8_t SCREEN_WIDTH = 128;
    constexpr uint8_t SCREEN_HEIGHT = 32;
    constexpr int8_t OLED_RESET = -1;
    constexpr uint8_t SCREEN_ADDRESS = 0x3C;
    
    // INA226 settings
    constexpr uint8_t INA226_ADDRESS = 0x40;
    constexpr float SHUNT_RESISTANCE_OHM = 0.010f;
    constexpr float CURRENT_LSB_MA = 0.100f;
    constexpr float CURRENT_ZERO_OFFSET_MA = 0.0f;
    constexpr uint16_t BUS_VOLTAGE_SCALING = 10000;
    
    // Timing intervals (milliseconds)
    // MEASUREMENT_INTERVAL_MS: how often to read the INA226 sensor
    // OUTPUT_INTERVAL_MS: how often to send data via serial (set to same as measurement for max speed)
    constexpr unsigned long MEASUREMENT_INTERVAL_MS = 10;   // 100 Hz measurement
    constexpr unsigned long OUTPUT_INTERVAL_MS = 10;        // 100 Hz output (was 100ms = 10Hz)
    constexpr unsigned long DISPLAY_INTERVAL_MS = 100;      // 10 Hz display update (OLED is slow)
    
    // Serial settings
    // 921600 baud for faster data transfer (ESP32-C3 USB-CDC supports high speeds)
    constexpr unsigned long SERIAL_BAUD = 921600;
    
    // RTC settings
    // Set to true to force RTC update on every upload (useful for initial setup)
    constexpr bool FORCE_RTC_UPDATE = true;
}

// =============================================================================
// Data Structures
// =============================================================================

/**
 * @brief Holds a single power measurement with millisecond timestamp
 */
struct Measurement {
    char timestamp[24];  // "YYYY-MM-DD HH:MM:SS.mmm"
    float voltage;       // Bus voltage in Volts
    float current;       // Current in Amperes  
    float power;         // Power in Watts
};

// =============================================================================
// Global Objects
// =============================================================================

RTC_DS3231 rtc;
INA226 powerMonitor(Config::INA226_ADDRESS);
OLEDStatus display(Config::SCREEN_WIDTH, Config::SCREEN_HEIGHT, Config::OLED_RESET);
PrecisionTime precisionTime(rtc, Config::SQW_PIN);

Measurement currentMeasurement;
unsigned long lastMeasurementTime = 0;
unsigned long lastOutputTime = 0;
unsigned long lastDisplayTime = 0;

// =============================================================================
// Power Monitor Functions
// =============================================================================

namespace PowerMonitor {
    bool initialize() {
        if (!powerMonitor.begin()) {
            Serial.println(F("[ERROR] INA226 not found"));
            return false;
        }
        
        powerMonitor.configure(
            Config::SHUNT_RESISTANCE_OHM,
            Config::CURRENT_LSB_MA,
            Config::CURRENT_ZERO_OFFSET_MA,
            Config::BUS_VOLTAGE_SCALING
        );
        
        powerMonitor.setModeShuntBusContinuous();
        powerMonitor.setAverage(INA226_16_SAMPLES);
        
        Serial.println(F("[INFO] INA226 initialized successfully"));
        return true;
    }
    
    Measurement read() {
        Measurement m;
        precisionTime.getTimestamp(m.timestamp, sizeof(m.timestamp));
        m.voltage = powerMonitor.getBusVoltage();
        m.current = powerMonitor.getCurrent();
        m.power = powerMonitor.getPower();
        return m;
    }
}

// =============================================================================
// RTC Functions
// =============================================================================

namespace RTC {
    bool initialize() {
        if (!rtc.begin()) {
            Serial.println(F("[ERROR] DS3231 RTC not found"));
            return false;
        }
        
        if (Config::FORCE_RTC_UPDATE || rtc.lostPower()) {
            rtc.adjust(DateTime(F(__DATE__), F(__TIME__)));
            Serial.println(F("[INFO] RTC synchronized to compile time"));
        }
        
        // Disable 32kHz output, enable SQW
        rtc.disable32K();
        
        DateTime now = rtc.now();
        Serial.print(F("[INFO] RTC time: "));
        Serial.print(now.year());
        Serial.print('-');
        Serial.print(now.month());
        Serial.print('-');
        Serial.print(now.day());
        Serial.print(' ');
        Serial.print(now.hour());
        Serial.print(':');
        Serial.print(now.minute());
        Serial.print(':');
        Serial.println(now.second());
        
        return true;
    }
}

// =============================================================================
// Serial Output Functions
// =============================================================================

namespace SerialOutput {
    void printCSV(const Measurement& m) {
        Serial.print(m.timestamp);
        Serial.print(',');
        Serial.print(m.voltage, 4);
        Serial.print(',');
        Serial.print(m.current, 4);
        Serial.print(',');
        Serial.println(m.power, 4);
    }
}

// =============================================================================
// Setup & Main Loop
// =============================================================================

void setup() {
    Serial.begin(Config::SERIAL_BAUD);
    while (!Serial && millis() < 3000);
    
    Serial.println();
    Serial.println(F("=== EdgePowerMeter ==="));
    Serial.println(F("[INFO] Initializing..."));
    
    Wire.begin();
    
    // Initialize display (using OLEDStatus library)
    if (!display.begin(Config::SCREEN_ADDRESS)) {
        Serial.println(F("[WARNING] Running without display"));
    } else {
        display.showMessage("Edge", "Power");
        delay(500);
    }
    
    // Initialize RTC
    display.showMessage("RTC", "Init...");
    if (!RTC::initialize()) {
        display.showError("RTC", "Failed!");
    }
    delay(100);
    
    // Initialize precision timing (using PrecisionTime library)
    display.showMessage("SQW", "Sync...");
    if (!precisionTime.begin()) {
        Serial.println(F("[WARNING] SQW not detected - using polling fallback"));
    }
    delay(100);
    
    // Initialize power monitor
    display.showMessage("INA226", "Init...");
    if (!PowerMonitor::initialize()) {
        display.showError("INA226", "Failed!");
    }
    delay(100);
    
    // Ready
    display.showMessage("Ready!", "");
    Serial.print(F("[INFO] "));
    Serial.print(F(FIRMWARE_NAME));
    Serial.print(F(" v"));
    Serial.print(F(FIRMWARE_VERSION));
    Serial.println(F(" ready"));
    Serial.println(F("Timestamp,Voltage[V],Current[A],Power[W]"));
    
    delay(300);
    
    lastMeasurementTime = millis();
    lastDisplayTime = millis();
}

void loop() {
    // Update precision time sync (handles ISR flag)
    precisionTime.update();
    
    unsigned long now = millis();
    
    // Read measurement and output at configured interval
    if (now - lastMeasurementTime >= Config::MEASUREMENT_INTERVAL_MS) {
        currentMeasurement = PowerMonitor::read();
        lastMeasurementTime = now;
        
        // Output data at measurement rate (or slower if OUTPUT_INTERVAL_MS > MEASUREMENT_INTERVAL_MS)
        if (now - lastOutputTime >= Config::OUTPUT_INTERVAL_MS) {
            SerialOutput::printCSV(currentMeasurement);
            lastOutputTime = now;
        }
    }
    
    // Update display at slower rate (OLED is slow)
    if (now - lastDisplayTime >= Config::DISPLAY_INTERVAL_MS) {
        display.showPower(currentMeasurement.power);
        lastDisplayTime = now;
    }
}
