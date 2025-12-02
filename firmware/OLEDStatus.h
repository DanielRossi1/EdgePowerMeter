/**
 * @file OLEDStatus.h
 * @brief Status display utilities for SSD1306 OLED
 * @version 1.0.0
 * 
 * Provides simple methods for displaying status messages,
 * values, and error states on small OLED displays.
 * 
 * @author Daniel Rossi
 * @license Apache-2.0
 */

#ifndef OLED_STATUS_H
#define OLED_STATUS_H

#include <Arduino.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>

class OLEDStatus {
public:
    /**
     * @brief Construct with display dimensions
     * @param width Display width in pixels (default 128)
     * @param height Display height in pixels (default 32)
     * @param resetPin Reset pin (-1 if none)
     */
    OLEDStatus(uint8_t width = 128, uint8_t height = 32, int8_t resetPin = -1);
    
    /**
     * @brief Initialize the display
     * @param i2cAddr I2C address (default 0x3C)
     * @return true if successful
     */
    bool begin(uint8_t i2cAddr = 0x3C);
    
    /**
     * @brief Display two lines of text
     * @param line1 First line (top)
     * @param line2 Second line (bottom)
     */
    void showMessage(const char* line1, const char* line2 = "");
    
    /**
     * @brief Display a value with label and unit
     * @param label Label text (e.g., "Power")
     * @param value Numeric value
     * @param unit Unit string (e.g., "W")
     * @param decimals Decimal places (default 2)
     */
    void showValue(const char* label, float value, const char* unit, uint8_t decimals = 2);
    
    /**
     * @brief Display power reading
     * @param powerWatts Power in watts
     */
    void showPower(float powerWatts);
    
    /**
     * @brief Display voltage reading
     * @param voltage Voltage in volts
     */
    void showVoltage(float voltage);
    
    /**
     * @brief Display current reading
     * @param current Current in amperes
     */
    void showCurrent(float current);
    
    /**
     * @brief Display error and halt
     * @param component Component name
     * @param message Error message
     * 
     * Note: This function does not return - enters infinite loop
     */
    void showError(const char* component, const char* message);
    
    /**
     * @brief Clear the display
     */
    void clear();
    
    /**
     * @brief Get the underlying display object
     * @return Pointer to Adafruit_SSD1306 display (nullptr if not initialized)
     */
    Adafruit_SSD1306* getDisplay() { return _display; }
    
    /**
     * @brief Check if display is initialized
     */
    bool isInitialized() const { return _display != nullptr; }

private:
    Adafruit_SSD1306* _display;
    uint8_t _width;
    uint8_t _height;
    int8_t _resetPin;
};

#endif // OLED_STATUS_H
