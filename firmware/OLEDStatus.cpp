/**
 * @file OLEDStatus.cpp
 * @brief Implementation of the SSD1306 status display
 * @version 2.0.0
 *
 * @author Daniel Rossi
 * @license Apache-2.0
 */

#include "OLEDStatus.h"

#include <Wire.h>
#include <esp_timer.h>
#include <math.h>

namespace {
    constexpr uint8_t SSD1306_CONTROL_COMMAND = 0x00;
    constexpr uint8_t SSD1306_CONTROL_DATA = 0x40;
    constexpr uint8_t SSD1306_COLUMN_ADDR = 0x21;
    constexpr uint8_t SSD1306_PAGE_ADDR = 0x22;
    constexpr uint8_t SSD1306_DISPLAY_OFF = 0xAE;
    constexpr uint8_t SSD1306_DISPLAY_ON = 0xAF;
    constexpr uint32_t INITIAL_CHUNK_COST_US = 600;
    // A 16-byte chunk takes ~0.5 ms at 400 kHz; anything slower is an
    // interruption or an I2C timeout and must not stick in the estimate.
    constexpr uint32_t MAX_CHUNK_COST_US = 1000;
}

OLEDStatus::OLEDStatus(uint8_t width, uint8_t height, int8_t resetPin)
    : _display(nullptr)
    , _width(width)
    , _height(height)
    , _resetPin(resetPin)
    , _address(0x3C)
    , _enabled(true)
    , _transferActive(false)
    , _renderActive(false)
    , _lines{}
    , _line(0)
    , _pos(0)
    , _glyphCostUs(150)
    , _offset(-1)
    , _chunkCostUs(INITIAL_CHUNK_COST_US)
{
}

bool OLEDStatus::begin(uint8_t i2cAddr) {
    _address = i2cAddr;

    // Probe first: Adafruit begin() does not fail on a missing panel
    Wire.beginTransmission(_address);
    if (Wire.endTransmission() != 0) {
        return false;
    }

    // clkAfter = 400 kHz: the Adafruit default (100 kHz) would silently slow
    // down every INA226/RTC transaction after the first display() call.
    _display = new Adafruit_SSD1306(_width, _height, &Wire, _resetPin, 400000UL, 400000UL);
    if (!_display->begin(SSD1306_SWITCHCAPVCC, i2cAddr)) {
        delete _display;
        _display = nullptr;
        return false;
    }
    _display->setTextWrap(false);
    _display->clearDisplay();
    _display->display();
    return true;
}

void OLEDStatus::showMessage(const char* line1, const char* line2) {
    if (!_display || !_enabled) return;
    _transferActive = false;
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

bool OLEDStatus::sendCommand(uint8_t command) {
    Wire.beginTransmission(_address);
    Wire.write(SSD1306_CONTROL_COMMAND);
    Wire.write(command);
    return Wire.endTransmission() == 0;
}

void OLEDStatus::setEnabled(bool enabled) {
    _enabled = enabled;
    if (!_display) return;
    if (!enabled) {
        _transferActive = false;
    }
    sendCommand(enabled ? SSD1306_DISPLAY_ON : SSD1306_DISPLAY_OFF);
}

void OLEDStatus::renderReadings(float volts, float amps, float watts, const char* status) {
    if (!_display || !_enabled || _transferActive) return;

    char text[24];
    _display->clearDisplay();
    _display->setTextColor(SSD1306_WHITE);

    // Line 1 (large): power
    const float absW = fabsf(watts);
    if (absW < 10.0f) {
        snprintf(text, sizeof(text), "%.3f W", watts);
    } else if (absW < 100.0f) {
        snprintf(text, sizeof(text), "%.2f W", watts);
    } else {
        snprintf(text, sizeof(text), "%.1f W", watts);
    }
    _display->setTextSize(2);
    _display->setCursor(0, 0);
    _display->print(text);

    // Line 2: voltage and current
    if (fabsf(amps) < 1.0f) {
        snprintf(text, sizeof(text), "%.3fV  %.1fmA", volts, amps * 1000.0f);
    } else {
        snprintf(text, sizeof(text), "%.3fV  %.3fA", volts, amps);
    }
    _display->setTextSize(1);
    _display->setCursor(0, 17);
    _display->print(text);

    // Line 3: status
    _display->setCursor(0, 25);
    _display->print(status);

    _offset = -1;
    _transferActive = true;
}

void OLEDStatus::beginReadings(float volts, float amps, float watts, const char* status) {
    if (!_display || !_enabled || _transferActive || _renderActive) return;

    const float absW = fabsf(watts);
    if (absW < 10.0f) {
        snprintf(_lines[0], sizeof(_lines[0]), "%.3f W", watts);
    } else if (absW < 100.0f) {
        snprintf(_lines[0], sizeof(_lines[0]), "%.2f W", watts);
    } else {
        snprintf(_lines[0], sizeof(_lines[0]), "%.1f W", watts);
    }
    if (fabsf(amps) < 1.0f) {
        snprintf(_lines[1], sizeof(_lines[1]), "%.3fV  %.1fmA", volts, amps * 1000.0f);
    } else {
        snprintf(_lines[1], sizeof(_lines[1]), "%.3fV  %.3fA", volts, amps);
    }
    snprintf(_lines[2], sizeof(_lines[2]), "%s", status);

    _display->clearDisplay();
    _line = 0;
    _pos = 0;
    _renderActive = true;
}

bool OLEDStatus::renderStep() {
    if (!_display || !_renderActive) return false;

    // Same layout as renderReadings(): large power line, two small lines
    static const uint8_t SIZE[3] = {2, 1, 1};
    static const int16_t Y[3] = {0, 17, 25};

    while (_line < 3 && _lines[_line][_pos] == '\0') {
        _line++;
        _pos = 0;
    }
    if (_line >= 3) {
        _renderActive = false;
        _offset = -1;
        _transferActive = true;
        return false;
    }

    const int64_t start = esp_timer_get_time();
    const uint8_t size = SIZE[_line];
    const int16_t x = static_cast<int16_t>(_pos) * 6 * size;
    if (x + 6 * size <= _width) {
        // fg == bg draws a transparent glyph (the buffer is already clear)
        _display->drawChar(x, Y[_line], static_cast<unsigned char>(_lines[_line][_pos]),
                           SSD1306_WHITE, SSD1306_WHITE, size);
    }
    _pos++;

    uint32_t cost = static_cast<uint32_t>(esp_timer_get_time() - start);
    if (cost > MAX_CHUNK_COST_US) cost = MAX_CHUNK_COST_US;
    _glyphCostUs = cost > _glyphCostUs ? cost : (_glyphCostUs * 15 + cost) / 16;
    return true;
}

bool OLEDStatus::pumpChunk() {
    if (!_display || !_transferActive) return true;

    const int64_t start = esp_timer_get_time();
    bool ok;

    if (_offset < 0) {
        // Horizontal addressing mode is set by Adafruit begin(); define the
        // full-screen window once per frame.
        Wire.beginTransmission(_address);
        Wire.write(SSD1306_CONTROL_COMMAND);
        Wire.write(SSD1306_COLUMN_ADDR);
        Wire.write(static_cast<uint8_t>(0));
        Wire.write(static_cast<uint8_t>(_width - 1));
        Wire.write(SSD1306_PAGE_ADDR);
        Wire.write(static_cast<uint8_t>(0));
        Wire.write(static_cast<uint8_t>(_height / 8 - 1));
        ok = Wire.endTransmission() == 0;
        _offset = 0;
    } else {
        const int16_t size = static_cast<int16_t>(_width) * _height / 8;
        const int16_t n = min(static_cast<int16_t>(CHUNK_BYTES), static_cast<int16_t>(size - _offset));
        Wire.beginTransmission(_address);
        Wire.write(SSD1306_CONTROL_DATA);
        Wire.write(_display->getBuffer() + _offset, n);
        ok = Wire.endTransmission() == 0;
        _offset += n;
        if (_offset >= size) {
            _transferActive = false;
        }
    }

    // Track the worst recent cost (fast attack, slow decay), clamped
    uint32_t cost = static_cast<uint32_t>(esp_timer_get_time() - start);
    if (cost > MAX_CHUNK_COST_US) cost = MAX_CHUNK_COST_US;
    _chunkCostUs = cost > _chunkCostUs ? cost : (_chunkCostUs * 15 + cost) / 16;

    if (!ok) {
        _transferActive = false;
    }
    return ok;
}
