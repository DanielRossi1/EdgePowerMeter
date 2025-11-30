/**
 * @file EdgePowerMeter.ino
 * @brief Real-time power monitoring firmware for embedded AI workload analysis
 * @version 1.0.0
 * 
 * This firmware reads voltage, current, and power measurements from an INA226
 * power monitor and outputs CSV-formatted data via serial for logging and analysis.
 * 
 * Hardware:
 *   - MCU: ESP32-C3 SuperMini (or compatible)
 *   - Power Monitor: INA226 (I2C address 0x40)
 *   - Display: SSD1306 OLED 128x32 (I2C address 0x3C)
 *   - RTC: DS3231 (I2C)
 *   - Shunt Resistor: 0.01Ω (R2512)
 * 
 * Serial Output Format (115200 baud):
 *   Timestamp,Voltage[V],Current[A],Power[W]
 *   2025-11-30 12:34:56,12.345,1.234,15.234
 * 
 * @author Daniel Rossi
 * @license Apache-2.0
 */

#include <Wire.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>
#include <RTClib.h>
#include <INA226.h>

// =============================================================================
// Version
// =============================================================================

#define FIRMWARE_VERSION "1.0.0"
#define FIRMWARE_NAME "EdgePowerMeter"

// =============================================================================
// Configuration
// =============================================================================

namespace Config {
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
    constexpr unsigned long MEASUREMENT_INTERVAL_MS = 10;
    constexpr unsigned long DISPLAY_INTERVAL_MS = 100;
    
    // Serial settings
    constexpr unsigned long SERIAL_BAUD = 115200;
    
    // Set to true to force RTC update on every upload (useful for initial setup)
    // Set to false after RTC is synchronized to preserve time across resets
    constexpr bool FORCE_RTC_UPDATE = true;
}

// =============================================================================
// Data Structures
// =============================================================================

/**
 * @brief Holds a single power measurement with timestamp
 */
struct Measurement {
    char timestamp[20];  // "YYYY-MM-DD HH:MM:SS"
    float voltage;       // Bus voltage in Volts
    float current;       // Current in Amperes  
    float power;         // Power in Watts
};

// =============================================================================
// Global Objects
// =============================================================================

Adafruit_SSD1306 display(Config::SCREEN_WIDTH, Config::SCREEN_HEIGHT, 
                          &Wire, Config::OLED_RESET);
INA226 powerMonitor(Config::INA226_ADDRESS);
RTC_DS3231 rtc;

Measurement currentMeasurement;
unsigned long lastMeasurementTime = 0;
unsigned long lastDisplayTime = 0;

// =============================================================================
// Display Functions
// =============================================================================

namespace Display {
    /**
     * @brief Initialize the OLED display
     * @return true if successful, false otherwise
     */
    bool initialize() {
        if (!display.begin(SSD1306_SWITCHCAPVCC, Config::SCREEN_ADDRESS)) {
            Serial.println(F("[ERROR] SSD1306 display initialization failed"));
            return false;
        }
        
        display.clearDisplay();
        display.display();
        return true;
    }
    
    /**
     * @brief Display a two-line message
     */
    void showMessage(const char* line1, const char* line2) {
        display.clearDisplay();
        display.setTextSize(2);
        display.setTextColor(SSD1306_WHITE);
        
        display.setCursor(0, 0);
        display.print(line1);
        
        display.setCursor(0, 18);
        display.print(line2);
        
        display.display();
    }
    
    /**
     * @brief Display current power reading
     */
    void showPower(float powerWatts) {
        display.clearDisplay();
        display.setTextSize(2);
        display.setTextColor(SSD1306_WHITE);
        
        display.setCursor(0, 0);
        display.print(F("Power"));
        
        display.setCursor(0, 18);
        display.print(powerWatts, 2);
        display.print(F(" W"));
        
        display.display();
    }
    
    /**
     * @brief Display error message and halt
     */
    void showError(const char* component, const char* message) {
        showMessage(component, message);
        Serial.print(F("[FATAL] "));
        Serial.print(component);
        Serial.print(F(": "));
        Serial.println(message);
        
        // Halt execution
        while (true) {
            delay(1000);
        }
    }
}

// =============================================================================
// RTC Functions
// =============================================================================

namespace RTC {
    /**
     * @brief Initialize and synchronize RTC
     * @return true if successful
     */
    bool initialize() {
        if (!rtc.begin()) {
            Serial.println(F("[ERROR] DS3231 RTC not found"));
            return false;
        }
        
        // Check if RTC needs to be set
        bool needsUpdate = Config::FORCE_RTC_UPDATE || rtc.lostPower();
        
        if (needsUpdate) {
            // Set RTC to compile time
            rtc.adjust(DateTime(F(__DATE__), F(__TIME__)));
            Serial.println(F("[INFO] RTC synchronized to compile time"));
        }
        
        // Print current RTC time
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
    
    /**
     * @brief Get formatted timestamp string
     */
    void getTimestamp(char* buffer, size_t bufferSize) {
        DateTime now = rtc.now();
        snprintf(buffer, bufferSize, "%04d-%02d-%02d %02d:%02d:%02d",
                 now.year(), now.month(), now.day(),
                 now.hour(), now.minute(), now.second());
    }
}

// =============================================================================
// Power Monitor Functions
// =============================================================================

namespace PowerMonitor {
    /**
     * @brief Initialize INA226 power monitor
     * @return true if successful
     */
    bool initialize() {
        if (!powerMonitor.begin()) {
            Serial.println(F("[ERROR] INA226 not found"));
            return false;
        }
        
        // Configure INA226
        int configResult = powerMonitor.configure(
            Config::SHUNT_RESISTANCE_OHM,
            Config::CURRENT_LSB_MA,
            Config::CURRENT_ZERO_OFFSET_MA,
            Config::BUS_VOLTAGE_SCALING
        );
        
        if (configResult != 0) {
            Serial.println(F("[WARNING] INA226 configuration values may be out of range"));
        }
        
        // Set measurement mode
        powerMonitor.setModeShuntBusContinuous();
        powerMonitor.setAverage(INA226_16_SAMPLES);
        
        Serial.println(F("[INFO] INA226 initialized successfully"));
        return true;
    }
    
    /**
     * @brief Read current measurement from INA226
     */
    Measurement read() {
        Measurement m;
        
        RTC::getTimestamp(m.timestamp, sizeof(m.timestamp));
        m.voltage = powerMonitor.getBusVoltage();
        m.current = powerMonitor.getCurrent();
        m.power = powerMonitor.getPower();
        
        return m;
    }
}

// =============================================================================
// Serial Output Functions
// =============================================================================

namespace SerialOutput {
    /**
     * @brief Print measurement as CSV line
     * Format: Timestamp,Voltage,Current,Power
     */
    void printCSV(const Measurement& m) {
        Serial.print(m.timestamp);
        Serial.print(',');
        Serial.print(m.voltage, 3);
        Serial.print(',');
        Serial.print(m.current, 3);
        Serial.print(',');
        Serial.println(m.power, 3);
    }
    
    /**
     * @brief Print measurement for Arduino Serial Plotter
     * Format: Voltage Current Power (space-separated)
     */
    void printPlotter(const Measurement& m) {
        Serial.print(m.voltage, 3);
        Serial.print(' ');
        Serial.print(m.current, 3);
        Serial.print(' ');
        Serial.println(m.power, 3);
    }
}

// =============================================================================
// Setup & Main Loop
// =============================================================================

void setup() {
    // Initialize serial communication
    Serial.begin(Config::SERIAL_BAUD);
    while (!Serial && millis() < 3000) {
        ; // Wait for serial port (with timeout for standalone operation)
    }
    
    Serial.println();
    Serial.println(F("=== EdgePowerMeter ==="));
    Serial.println(F("[INFO] Initializing..."));
    
    // Initialize I2C
    Wire.begin();
    
    // Initialize display
    if (!Display::initialize()) {
        // Continue without display if it fails
        Serial.println(F("[WARNING] Running without display"));
    } else {
        Display::showMessage("Edge", "Power");
        delay(500);
    }
    
    // Initialize RTC
    Display::showMessage("RTC", "Init...");
    if (!RTC::initialize()) {
        Display::showError("RTC", "Failed!");
    }
    delay(300);
    
    // Initialize power monitor
    Display::showMessage("INA226", "Init...");
    if (!PowerMonitor::initialize()) {
        Display::showError("INA226", "Failed!");
    }
    delay(300);
    
    // Ready
    Display::showMessage("Ready!", "");
    Serial.print(F("[INFO] "));
    Serial.print(F(FIRMWARE_NAME));
    Serial.print(F(" v"));
    Serial.print(F(FIRMWARE_VERSION));
    Serial.println(F(" ready"));
    Serial.println(F("Timestamp,Voltage[V],Current[A],Power[W]"));
    
    delay(500);
    
    // Initialize timing
    lastMeasurementTime = millis();
    lastDisplayTime = millis();
}

void loop() {
    unsigned long now = millis();
    
    // Read measurement at configured interval
    if (now - lastMeasurementTime >= Config::MEASUREMENT_INTERVAL_MS) {
        currentMeasurement = PowerMonitor::read();
        lastMeasurementTime = now;
    }
    
    // Update display and output at configured interval
    if (now - lastDisplayTime >= Config::DISPLAY_INTERVAL_MS) {
        Display::showPower(currentMeasurement.power);
        SerialOutput::printCSV(currentMeasurement);
        lastDisplayTime = now;
    }
}
