/**
 * @file EdgePowerMeter.ino
 * @brief Real-time power monitoring firmware for embedded AI workload analysis
 * @version 1.1.0
 * 
 * This firmware reads voltage, current, and power measurements from an INA226
 * power monitor and outputs CSV-formatted data via serial for logging and analysis.
 * 
 * Timing System:
 *   Uses DS3231 SQW pin (1Hz) for precise second boundaries, combined with
 *   millis() for sub-second resolution. This provides millisecond-accurate
 *   timestamps with minimal drift (DS3231 accuracy: ±2ppm = ~1 min/year).
 * 
 * Hardware:
 *   - MCU: ESP32-C3 SuperMini (or compatible)
 *   - Power Monitor: INA226 (I2C address 0x40)
 *   - Display: SSD1306 OLED 128x32 (I2C address 0x3C)
 *   - RTC: DS3231 (I2C) with SQW connected to GPIO
 *   - Shunt Resistor: 0.01Ω (R2512)
 * 
 * Serial Output Format (115200 baud):
 *   Timestamp,Voltage[V],Current[A],Power[W]
 *   2025-11-30 12:34:56.123,12.345,1.234,15.234
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

#define FIRMWARE_VERSION "1.1.0"
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
    constexpr unsigned long MEASUREMENT_INTERVAL_MS = 10;
    constexpr unsigned long DISPLAY_INTERVAL_MS = 100;
    
    // Serial settings
    constexpr unsigned long SERIAL_BAUD = 115200;
    
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

Adafruit_SSD1306 display(Config::SCREEN_WIDTH, Config::SCREEN_HEIGHT, 
                          &Wire, Config::OLED_RESET);
INA226 powerMonitor(Config::INA226_ADDRESS);
RTC_DS3231 rtc;

Measurement currentMeasurement;
unsigned long lastMeasurementTime = 0;
unsigned long lastDisplayTime = 0;

// =============================================================================
// Precision Timing System
// =============================================================================

namespace PrecisionTime {
    // Synchronized time state
    volatile bool syncPending = false;      // Flag set by ISR
    volatile unsigned long syncMillis = 0;  // millis() at last SQW pulse
    
    DateTime syncedTime;                    // RTC time at last sync
    unsigned long lastSyncMillis = 0;       // millis() when syncedTime was read
    bool initialized = false;
    bool usingSqw = false;                  // True if SQW interrupt is active
    uint8_t lastSecond = 255;               // For polling fallback
    
    /**
     * @brief ISR for SQW 1Hz signal - marks second boundary
     */
    void IRAM_ATTR onSqwPulse() {
        syncMillis = millis();
        syncPending = true;
    }
    
    /**
     * @brief Initialize precision timing with SQW interrupt
     * @return true if successful
     */
    bool initialize() {
        // Configure SQW pin as input with pullup
        pinMode(Config::SQW_PIN, INPUT_PULLUP);
        
        // Configure DS3231 to output 1Hz square wave
        rtc.writeSqwPinMode(DS3231_SquareWave1Hz);
        delay(10);  // Give DS3231 time to configure
        
        // Debug: check initial SQW state
        int sqwState = digitalRead(Config::SQW_PIN);
        Serial.print(F("[DEBUG] SQW pin initial state: "));
        Serial.println(sqwState ? "HIGH" : "LOW");
        
        // Wait for first pulse to synchronize
        Serial.println(F("[INFO] Waiting for SQW sync..."));
        
        unsigned long startWait = millis();
        unsigned long timeout = startWait + 2500;  // 2.5 seconds to catch at least 2 edges
        
        // Wait for a complete cycle: HIGH -> LOW -> HIGH (falling edge)
        int transitionCount = 0;
        int lastState = sqwState;
        
        while (millis() < timeout && transitionCount < 2) {
            int currentState = digitalRead(Config::SQW_PIN);
            if (currentState != lastState) {
                transitionCount++;
                Serial.print(F("[DEBUG] SQW transition #"));
                Serial.print(transitionCount);
                Serial.print(F(" to "));
                Serial.print(currentState ? "HIGH" : "LOW");
                Serial.print(F(" at +"));
                Serial.print(millis() - startWait);
                Serial.println(F("ms"));
                
                // Sync on falling edge (HIGH -> LOW)
                if (lastState == HIGH && currentState == LOW) {
                    lastSyncMillis = millis();
                    syncedTime = rtc.now();
                    initialized = true;
                    usingSqw = true;
                    
                    // Attach interrupt
                    attachInterrupt(digitalPinToInterrupt(Config::SQW_PIN), onSqwPulse, FALLING);
                    
                    Serial.print(F("[INFO] SQW precision timing active (sync took "));
                    Serial.print(millis() - startWait);
                    Serial.println(F("ms)"));
                    return true;
                }
                lastState = currentState;
            }
            delayMicroseconds(100);  // Small delay to avoid spinning too fast
        }
        
        // SQW sync failed - use polling fallback
        Serial.println(F("[WARNING] SQW not detected - using polling fallback"));
        Serial.print(F("[DEBUG] Final SQW state: "));
        Serial.println(digitalRead(Config::SQW_PIN) ? "HIGH (stuck?)" : "LOW (stuck?)");
        
        // Initialize with polling
        syncedTime = rtc.now();
        lastSyncMillis = millis();
        lastSecond = syncedTime.second();
        initialized = true;
        usingSqw = false;
        
        Serial.println(F("[INFO] Polling-based timing active"));
        return true;  // Still return true since polling works
    }
    
    /**
     * @brief Update sync state (call from main loop, not ISR)
     */
    void update() {
        if (usingSqw && syncPending) {
            // SQW interrupt mode
            noInterrupts();
            unsigned long capturedMillis = syncMillis;
            syncPending = false;
            interrupts();
            
            // Read RTC time (do this outside ISR)
            syncedTime = rtc.now();
            lastSyncMillis = capturedMillis;
        } else if (!usingSqw && initialized) {
            // Polling fallback mode - detect second change
            DateTime now = rtc.now();
            if (now.second() != lastSecond) {
                lastSecond = now.second();
                syncedTime = now;
                lastSyncMillis = millis();
            }
        }
    }
    
    /**
     * @brief Get current timestamp with millisecond precision
     * @param buffer Output buffer for timestamp string
     * @param bufferSize Size of buffer (min 24 bytes)
     */
    void getTimestamp(char* buffer, size_t bufferSize) {
        if (!initialized) {
            // Fallback: use RTC directly without ms
            DateTime now = rtc.now();
            snprintf(buffer, bufferSize, "%04d-%02d-%02d %02d:%02d:%02d.000",
                     now.year(), now.month(), now.day(),
                     now.hour(), now.minute(), now.second());
            return;
        }
        
        // Calculate milliseconds since last sync
        unsigned long currentMillis = millis();
        unsigned long elapsed = currentMillis - lastSyncMillis;
        
        // Calculate current time
        uint16_t ms = elapsed % 1000;
        uint32_t extraSeconds = elapsed / 1000;
        
        // Add extra seconds to synced time
        DateTime now = syncedTime + TimeSpan(extraSeconds);
        
        snprintf(buffer, bufferSize, "%04d-%02d-%02d %02d:%02d:%02d.%03d",
                 now.year(), now.month(), now.day(),
                 now.hour(), now.minute(), now.second(), ms);
    }
    
    /**
     * @brief Get Unix timestamp with millisecond precision
     * @return Unix timestamp * 1000 + milliseconds
     */
    uint64_t getUnixMillis() {
        if (!initialized) {
            return (uint64_t)rtc.now().unixtime() * 1000;
        }
        
        unsigned long currentMillis = millis();
        unsigned long elapsed = currentMillis - lastSyncMillis;
        
        uint16_t ms = elapsed % 1000;
        uint32_t extraSeconds = elapsed / 1000;
        
        DateTime now = syncedTime + TimeSpan(extraSeconds);
        return (uint64_t)now.unixtime() * 1000 + ms;
    }
}

// =============================================================================
// Display Functions
// =============================================================================

namespace Display {
    bool initialize() {
        if (!display.begin(SSD1306_SWITCHCAPVCC, Config::SCREEN_ADDRESS)) {
            Serial.println(F("[ERROR] SSD1306 display initialization failed"));
            return false;
        }
        display.clearDisplay();
        display.display();
        return true;
    }
    
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
    
    void showError(const char* component, const char* message) {
        showMessage(component, message);
        Serial.print(F("[FATAL] "));
        Serial.print(component);
        Serial.print(F(": "));
        Serial.println(message);
        while (true) { delay(1000); }
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
        PrecisionTime::getTimestamp(m.timestamp, sizeof(m.timestamp));
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
    
    // Initialize display
    if (!Display::initialize()) {
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
    delay(100);
    
    // Initialize precision timing
    Display::showMessage("SQW", "Sync...");
    if (!PrecisionTime::initialize()) {
        Serial.println(F("[WARNING] Running without SQW - ms timing degraded"));
    }
    delay(100);
    
    // Initialize power monitor
    Display::showMessage("INA226", "Init...");
    if (!PowerMonitor::initialize()) {
        Display::showError("INA226", "Failed!");
    }
    delay(100);
    
    // Ready
    Display::showMessage("Ready!", "");
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
    PrecisionTime::update();
    
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
