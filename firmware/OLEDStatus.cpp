/**
 * @file OLEDStatus.cpp
 * @brief Implementation of status display for SSD1306 OLED
 * @version 1.0.0
 * 
 * @author Daniel Rossi
 * @license Apache-2.0
 */

#include "OLEDStatus.h"

OLEDStatus::OLEDStatus(uint8_t width, uint8_t height, int8_t resetPin)
    : _display(nullptr)
    , _width(width)
    , _height(height)
    , _resetPin(resetPin)
{
}

bool OLEDStatus::begin(uint8_t i2cAddr) {
    // Create display object now that Wire is initialized
    _display = new Adafruit_SSD1306(_width, _height, &Wire, _resetPin);
    if (!_display->begin(SSD1306_SWITCHCAPVCC, i2cAddr)) {
        delete _display;
        _display = nullptr;
        return false;
    }
    _display->clearDisplay();
    _display->display();
    return true;
}

void OLEDStatus::showMessage(const char* line1, const char* line2) {
    if (!_display) return;
    _display->clearDisplay();
    _display->setTextSize(2);
    _display->setTextColor(SSD1306_WHITE);
    _display->setCursor(0, 0);
    _display->print(line1);
    
    if (line2 && line2[0] != '\0') {
        _display->setCursor(0, 18);
        _display->print(line2);
    }
    
    _display->display();
}

void OLEDStatus::showValue(const char* label, float value, const char* unit, uint8_t decimals) {
    if (!_display) return;
    _display->clearDisplay();
    _display->setTextSize(2);
    _display->setTextColor(SSD1306_WHITE);
    _display->setCursor(0, 0);
    _display->print(label);
    _display->setCursor(0, 18);
    _display->print(value, decimals);
    _display->print(' ');
    _display->print(unit);
    _display->display();
}

void OLEDStatus::showPower(float powerWatts) {
    showValue("Power", powerWatts, "W", 2);
}

void OLEDStatus::showVoltage(float voltage) {
    showValue("Voltage", voltage, "V", 2);
}

void OLEDStatus::showCurrent(float current) {
    showValue("Current", current, "A", 3);
}

void OLEDStatus::showError(const char* component, const char* message) {
    showMessage(component, message);
    
    Serial.print(F("[FATAL] "));
    Serial.print(component);
    Serial.print(F(": "));
    Serial.println(message);
    
    while (true) {
        delay(1000);
    }
}

void OLEDStatus::clear() {
    if (!_display) return;
    _display->clearDisplay();
    _display->display();
}
