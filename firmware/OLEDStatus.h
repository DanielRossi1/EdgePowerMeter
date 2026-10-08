/**
 * @file OLEDStatus.h
 * @brief SSD1306 status display with non-blocking frame transfer
 * @version 2.0.0
 *
 * A full 128x32 frame is 512 bytes; pushing it with Adafruit display() keeps
 * the shared I2C bus busy for ~13 ms at 400 kHz, which would make the main
 * loop miss INA226 conversions. Instead, frames are rendered into the
 * Adafruit buffer and sent in small chunks (one I2C transaction per call to
 * pumpChunk()), which the main loop interleaves with sampling.
 *
 * showMessage() is still blocking and meant for boot-time messages only.
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
    // 8 data bytes per I2C transaction (~250 µs at 400 kHz): small enough to
    // fit in the idle time between two INA226 conversions.
    static constexpr uint8_t CHUNK_BYTES = 8;

    OLEDStatus(uint8_t width = 128, uint8_t height = 32, int8_t resetPin = -1);

    /** @brief Initialize the panel (blocking). @return false if not present. */
    bool begin(uint8_t i2cAddr = 0x3C);

    bool isPresent() const { return _display != nullptr; }

    /** @brief Two lines of large text, sent immediately (blocking, boot only). */
    void showMessage(const char* line1, const char* line2 = "");

    /** @brief Turn the panel on/off. A disabled panel ignores render requests. */
    void setEnabled(bool enabled);
    bool isEnabled() const { return _enabled; }

    /**
     * @brief Render a readings frame into the buffer and start its transfer.
     * Ignored while a previous frame is still being transferred.
     * @param status Short status text for the bottom line (≤ 21 chars)
     */
    void renderReadings(float volts, float amps, float watts, const char* status);

    /**
     * @brief Start an incremental render of a readings frame. The text is
     * formatted and the buffer cleared now; glyphs are drawn one per
     * renderStep() call so the CPU is never busy for more than ~100 µs, and
     * the transfer starts automatically after the last glyph.
     * Ignored while a frame is being rendered or transferred.
     */
    void beginReadings(float volts, float amps, float watts, const char* status);

    /** @brief True while glyphs of the current frame remain to be drawn. */
    bool renderActive() const { return _renderActive; }

    /** @brief Draw the next glyph. @return false if nothing was left. */
    bool renderStep();

    /** @brief Running estimate of the cost of one renderStep() call, in µs. */
    uint32_t glyphCostUs() const { return _glyphCostUs; }

    /** @brief True while a frame is waiting to be (fully) sent. */
    bool transferActive() const { return _transferActive; }

    /**
     * @brief Send the next piece of the current frame (one I2C transaction).
     * @return false on I2C error (the frame is abandoned)
     */
    bool pumpChunk();

    /** @brief Running estimate of the cost of one pumpChunk() call, in µs. */
    uint32_t chunkCostUs() const { return _chunkCostUs; }

private:
    Adafruit_SSD1306* _display;
    uint8_t _width;
    uint8_t _height;
    int8_t _resetPin;
    uint8_t _address;
    bool _enabled;

    bool _transferActive;
    bool _renderActive;
    char _lines[3][24];
    uint8_t _line;
    uint8_t _pos;
    uint32_t _glyphCostUs;
    int16_t _offset;  // -1 = address window not sent yet
    uint32_t _chunkCostUs;

    bool sendCommand(uint8_t command);
};

#endif // OLED_STATUS_H
