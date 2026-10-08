/**
 * @file firmware.ino
 * @brief EdgePowerMeter firmware: INA226 power monitor streaming over USB-CDC
 * @version 2.0.0
 *
 * Streams every INA226 conversion to the host over the serial port, using the
 * protocol described in docs/PROTOCOL.md:
 *   - CSV mode (default at boot): legacy human-readable lines
 *       Timestamp,Voltage[V],Current[A],Power[W]
 *   - RAW mode (selected by the desktop app): compact lines with a sequence
 *     number, device time in µs and raw INA226 registers, plus one wall-clock
 *     time anchor per second from the DS3231.
 *
 * Design notes:
 *   - New conversions are detected by polling the INA226 conversion-ready
 *     flag, so every sample is a fresh conversion (no duplicates) and its
 *     timestamp is taken when the flag is observed.
 *   - Polling only starts shortly before the next conversion is due; the time
 *     in between ("slack") is used for background I2C work: RTC reads and a
 *     chunked, non-blocking OLED update. Nothing long-running ever blocks the
 *     sampling path.
 *   - If the host does not drain the serial port, data lines are dropped
 *     instead of blocking; the sequence number still increments so the host
 *     can count lost samples.
 *
 * Hardware:
 *   - MCU: ESP32-C3 SuperMini (single-core RISC-V @ 160 MHz, native USB-CDC)
 *   - Power monitor: INA226 (I2C 0x40), shunt 0.01 Ω (R2512)
 *   - RTC: DS3231 (I2C 0x68), SQW -> GPIO3
 *   - Display: SSD1306 128x32 (I2C 0x3C)
 *
 * Libraries: RTClib, Adafruit SSD1306, Adafruit GFX (the INA226 library is
 * no longer needed: see INA226Lite).
 *
 * @author Daniel Rossi
 * @license Apache-2.0
 */

#include <Wire.h>
#include <RTClib.h>
#include <Preferences.h>
#include <esp_timer.h>

#include "INA226Lite.h"
#include "OLEDStatus.h"
#include "PrecisionTime.h"

// =============================================================================
// Version
// =============================================================================

#define FIRMWARE_VERSION "2.0.0"
#define FIRMWARE_NAME "EdgePowerMeter"
#define PROTOCOL_VERSION 2

// Timing diagnostics for firmware development: when 1, every 5 s the
// firmware prints "# DIAG" lines with missed conversions attributed to the
// slowest task and per-phase I2C timings. Keep 0 in releases.
#ifndef EPM_DIAG
#define EPM_DIAG 0
#endif

// Serial is the native USB-Serial/JTAG CDC (HWCDC) on the ESP32-C3 when
// "USB CDC On Boot" is enabled.
#if ARDUINO_USB_CDC_ON_BOOT && ARDUINO_USB_MODE
#define EPM_SERIAL_IS_HWCDC 1
#else
#define EPM_SERIAL_IS_HWCDC 0
#endif

// =============================================================================
// Configuration
// =============================================================================

namespace Config {
    // Pins
    constexpr uint8_t SQW_PIN = 3;  // DS3231 SQW output -> ESP32 GPIO3 (A3)

    // Display
    constexpr uint8_t SCREEN_WIDTH = 128;
    constexpr uint8_t SCREEN_HEIGHT = 32;
    constexpr int8_t OLED_RESET = -1;
    constexpr uint8_t SCREEN_ADDRESS = 0x3C;
    constexpr int64_t OLED_FRAME_INTERVAL_US = 250000;  // 4 Hz

    // INA226
    constexpr uint8_t INA226_ADDRESS = 0x40;
    constexpr uint16_t DEFAULT_AVERAGES = 4;
    constexpr uint16_t DEFAULT_BUS_CONV_US = 140;
    constexpr uint16_t DEFAULT_SHUNT_CONV_US = 140;
    constexpr float DEFAULT_SHUNT_OHM = 0.010f;
    constexpr int64_t INA_RETRY_INTERVAL_US = 5000000;
    constexpr uint8_t INA_MAX_I2C_ERRORS = 10;

    // Serial
    // The baud rate is irrelevant for native USB-CDC; kept for UART builds.
    constexpr unsigned long SERIAL_BAUD = 2000000;
    constexpr size_t SERIAL_TX_BUFFER = 4096;
    constexpr size_t SERIAL_RX_BUFFER = 256;
    constexpr uint32_t HOST_WAIT_MS = 1500;
    constexpr int64_t RELIABLE_WRITE_TIMEOUT_US = 20000;

    // RTC
    // false = production behavior: the battery-backed DS3231 keeps its time
    // across reboots and only re-syncs to compile time if it actually lost
    // power (rtc.lostPower()). Set to true only for a one-time forced sync,
    // then flash back to false. The desktop app can also set the clock with
    // the SYNC command.
    constexpr bool FORCE_RTC_UPDATE = false;

    // Scheduling of background work between conversions
    constexpr uint32_t POLL_LEAD_PERCENT = 85;  // Start polling at 85% of the period
    constexpr int64_t SLACK_GUARD_US = 60;
    // Longest single RTC step (full 7-register read, ~300 µs at 400 kHz);
    // seconds-only polls are cheaper.
    constexpr int64_t RTC_WORK_COST_US = 350;

    // OLED: if spare time never suffices (very short conversion periods, or a
    // cost estimate inflated by one slow transfer), force a little progress
    // anyway, accepting a few skipped conversions.
    constexpr int64_t OLED_FORCE_AFTER_US = 1000000;
    constexpr uint32_t OLED_MAX_FRAME_SAMPLES = 1000000;

    // INA226 configuration read-back (detects a sensor power-on reset)
    constexpr int64_t INA_CONFIG_CHECK_US = 2000000;
    constexpr int64_t INA_CONFIG_CHECK_COST_US = 300;

    // Accepted shunt values (SET SHUNT, NVS)
    constexpr float SHUNT_MIN_OHM = 1e-5f;
    constexpr float SHUNT_MAX_OHM = 100.0f;
}

// =============================================================================
// Global Objects and State
// =============================================================================

RTC_DS3231 rtc;
INA226Lite ina(Config::INA226_ADDRESS);
OLEDStatus display(Config::SCREEN_WIDTH, Config::SCREEN_HEIGHT, Config::OLED_RESET);
PrecisionTime precisionTime(rtc, Config::SQW_PIN);

enum class OutputMode : uint8_t { Csv, Raw };

struct DeviceSettings {
    uint16_t averages;
    uint16_t busConvUs;
    uint16_t shuntConvUs;
    float shuntOhm;
    bool oledEnabled;
};

DeviceSettings settings = {
    Config::DEFAULT_AVERAGES,
    Config::DEFAULT_BUS_CONV_US,
    Config::DEFAULT_SHUNT_CONV_US,
    Config::DEFAULT_SHUNT_OHM,
    true,
};

OutputMode outputMode = OutputMode::Csv;
bool streamEnabled = true;
bool inaOk = false;
bool rtcOk = false;

// =============================================================================
// Output Helpers
// =============================================================================

namespace Out {
    char* putU32(char* p, uint32_t v) {
        char tmp[10];
        uint8_t n = 0;
        do {
            tmp[n++] = static_cast<char>('0' + v % 10);
            v /= 10;
        } while (v);
        while (n) *p++ = tmp[--n];
        return p;
    }

    char* putU64(char* p, uint64_t v) {
        char tmp[20];
        uint8_t n = 0;
        do {
            tmp[n++] = static_cast<char>('0' + v % 10);
            v /= 10;
        } while (v);
        while (n) *p++ = tmp[--n];
        return p;
    }

    char* putI64(char* p, int64_t v) {
        if (v < 0) {
            *p++ = '-';
            return putU64(p, static_cast<uint64_t>(-(v + 1)) + 1);
        }
        return putU64(p, static_cast<uint64_t>(v));
    }

    /** @brief Print value / 10^decimals with exactly `decimals` digits. */
    char* putScaled(char* p, int64_t value, uint8_t decimals) {
        if (value < 0) {
            *p++ = '-';
            value = -value;
        }
        uint64_t scale = 1;
        for (uint8_t i = 0; i < decimals; i++) scale *= 10;
        p = putU64(p, static_cast<uint64_t>(value) / scale);
        if (decimals) {
            *p++ = '.';
            uint64_t frac = static_cast<uint64_t>(value) % scale;
            for (int8_t i = decimals - 1; i >= 0; i--) {
                p[i] = static_cast<char>('0' + frac % 10);
                frac /= 10;
            }
            p += decimals;
        }
        return p;
    }

    char* putFixed(char* p, double value, uint8_t decimals) {
        double scale = 1.0;
        for (uint8_t i = 0; i < decimals; i++) scale *= 10.0;
        const double scaled = value * scale;
        const int64_t rounded = static_cast<int64_t>(scaled < 0 ? scaled - 0.5 : scaled + 0.5);
        return putScaled(p, rounded, decimals);
    }

    /** @brief Write a data line, or drop it if the TX buffer cannot take it. */
    bool writeData(const char* buf, size_t len) {
        if (Serial.availableForWrite() < static_cast<int>(len)) {
            return false;
        }
        Serial.write(reinterpret_cast<const uint8_t*>(buf), len);
        return true;
    }

    /** @brief Write a line that must not be lost (replies), waiting briefly for room. */
    void writeReliable(const char* buf, size_t len) {
        const int64_t deadline = esp_timer_get_time() + Config::RELIABLE_WRITE_TIMEOUT_US;
        while (Serial.availableForWrite() < static_cast<int>(len) && esp_timer_get_time() < deadline) {
            delayMicroseconds(200);
        }
        Serial.write(reinterpret_cast<const uint8_t*>(buf), len);
    }

    void line(const char* text) {
        char buf[128];
        const size_t n = strnlen(text, sizeof(buf) - 2);
        memcpy(buf, text, n);
        buf[n] = '\n';
        writeReliable(buf, n + 1);
    }

    /** @brief Informational line ("# ..."), ignored by host parsers. */
    void info(const char* text) {
        char buf[128];
        snprintf(buf, sizeof(buf), "# %s", text);
        line(buf);
    }
}

// =============================================================================
// Persistent Settings (NVS)
// =============================================================================

namespace Storage {
    constexpr const char* NAMESPACE = "epm";

    void load() {
        Preferences prefs;
        if (!prefs.begin(NAMESPACE, true)) {
            return;  // Nothing saved yet
        }
        const uint16_t avg = prefs.getUShort("avg", Config::DEFAULT_AVERAGES);
        const uint16_t vct = prefs.getUShort("vct", Config::DEFAULT_BUS_CONV_US);
        const uint16_t ict = prefs.getUShort("ict", Config::DEFAULT_SHUNT_CONV_US);
        const float shunt = prefs.getFloat("shunt", Config::DEFAULT_SHUNT_OHM);
        const bool oled = prefs.getBool("oled", true);
        prefs.end();

        if (INA226Lite::isValidAverage(avg)) settings.averages = avg;
        if (INA226Lite::isValidConversionTime(vct)) settings.busConvUs = vct;
        if (INA226Lite::isValidConversionTime(ict)) settings.shuntConvUs = ict;
        if (shunt >= Config::SHUNT_MIN_OHM && shunt <= Config::SHUNT_MAX_OHM) settings.shuntOhm = shunt;
        settings.oledEnabled = oled;
    }

    bool save() {
        Preferences prefs;
        if (!prefs.begin(NAMESPACE, false)) {
            return false;
        }
        bool ok = prefs.putUShort("avg", settings.averages) > 0;
        ok = prefs.putUShort("vct", settings.busConvUs) > 0 && ok;
        ok = prefs.putUShort("ict", settings.shuntConvUs) > 0 && ok;
        ok = prefs.putFloat("shunt", settings.shuntOhm) > 0 && ok;
        ok = prefs.putBool("oled", settings.oledEnabled) > 0 && ok;
        prefs.end();
        return ok;
    }
}

// =============================================================================
// Sampling
// =============================================================================

namespace Sampling {
    uint32_t seq = 0;
    uint32_t periodUs = 1120;
    int64_t lastReadyUs = 0;
    int64_t nextPollUs = 0;
    int64_t lastRetryUs = 0;
    int64_t lastConfigCheckUs = 0;
    uint8_t i2cErrors = 0;

    // Accumulators for the OLED frame (averages since the previous frame)
    uint64_t frameBusSum = 0;
    int64_t frameShuntSum = 0;
    double framePowerSum = 0.0;
    uint32_t frameSamples = 0;

    void restartTiming(int64_t now) {
        periodUs = ina.periodUs();
        lastReadyUs = now;
        nextPollUs = now + static_cast<int64_t>(periodUs) * Config::POLL_LEAD_PERCENT / 100;
    }

    /** @brief (Re)initialize the INA226 with the current settings. */
    bool initSensor() {
        inaOk = ina.begin()
            && ina.configure(settings.averages, settings.busConvUs, settings.shuntConvUs);
        i2cErrors = 0;
        restartTiming(esp_timer_get_time());
        return inaOk;
    }

    bool applyConfig() {
        if (!inaOk) {
            return true;  // Applied on the next successful (re)initialization
        }
        if (!ina.configure(settings.averages, settings.busConvUs, settings.shuntConvUs)) {
            return false;
        }
        restartTiming(esp_timer_get_time());
        return true;
    }

    /** @brief Time available for background work before the next poll. */
    int64_t slackUs(int64_t now) {
        if (!inaOk) {
            return INT32_MAX;
        }
        return nextPollUs - now - Config::SLACK_GUARD_US;
    }

    void onI2cError() {
        if (++i2cErrors >= Config::INA_MAX_I2C_ERRORS) {
            inaOk = false;
            lastRetryUs = esp_timer_get_time();
            Out::info("ERROR INA226 not responding");
        }
    }

    void emitSample(uint32_t s, int64_t t, uint16_t bus, int16_t shunt) {
        char buf[96];
        char* p = buf;

        if (outputMode == OutputMode::Raw) {
            *p++ = 'D';
            *p++ = ',';
            p = Out::putU32(p, s);
            *p++ = ',';
            p = Out::putI64(p, t);
            *p++ = ',';
            p = Out::putU32(p, bus);
            *p++ = ',';
            p = Out::putI64(p, shunt);
        } else {
            precisionTime.formatTimestamp(t, p, 24);
            p += strlen(p);
            *p++ = ',';
            // Bus LSB is exactly 1.25 mV = 125e-5 V
            p = Out::putScaled(p, static_cast<int64_t>(bus) * 125, 5);
            const double volts = bus * static_cast<double>(INA226Lite::BUS_LSB_V);
            const double amps = shunt * static_cast<double>(INA226Lite::SHUNT_LSB_V) / settings.shuntOhm;
            *p++ = ',';
            p = Out::putFixed(p, amps, 6);
            *p++ = ',';
            p = Out::putFixed(p, volts * amps, 6);
        }
        *p++ = '\n';
        Out::writeData(buf, static_cast<size_t>(p - buf));
    }

    void handleSample(int64_t t, uint16_t bus, int16_t shunt) {
        const uint32_t s = seq++;

        frameBusSum += bus;
        frameShuntSum += shunt;
        framePowerSum += (bus * static_cast<double>(INA226Lite::BUS_LSB_V))
            * (shunt * static_cast<double>(INA226Lite::SHUNT_LSB_V) / settings.shuntOhm);
        frameSamples++;

        if (streamEnabled) {
            emitSample(s, t, bus, shunt);
        }
    }

    /** @brief Poll the INA226 once; read and emit a sample if one is ready. */
#if EPM_DIAG
    uint32_t dPollMax = 0, dReadMax = 0, dEmitMax = 0, dPollSum = 0, dReadSum = 0, dEmitSum = 0, dN = 0, dPolls = 0;
#endif
    void step(int64_t now) {
        bool ready = false;
#if EPM_DIAG
        const int64_t p0 = esp_timer_get_time();
#endif
        const bool pollOk = ina.pollConversionReady(ready);
#if EPM_DIAG
        const uint32_t dp = static_cast<uint32_t>(esp_timer_get_time() - p0);
        dPolls++; dPollSum += dp; if (dp > dPollMax) dPollMax = dp;
#endif
        if (!pollOk) {
            onI2cError();
            return;
        }
        if (!ready) {
            // Conversions stopped (e.g. sensor power glitch reset its config)
            const int64_t stallLimit = 2 * static_cast<int64_t>(periodUs) + 100000;
            if (now - lastReadyUs > stallLimit) {
                Out::info("WARN INA226 stalled, reinitializing");
                initSensor();
            }
            return;
        }

        const int64_t t = esp_timer_get_time();
        uint16_t bus;
        int16_t shunt;
        if (!ina.readRaw(bus, shunt)) {
            onI2cError();
            return;
        }
#if EPM_DIAG
        const int64_t r1 = esp_timer_get_time();
#endif
        i2cErrors = 0;
        lastReadyUs = t;
        nextPollUs = t + static_cast<int64_t>(periodUs) * Config::POLL_LEAD_PERCENT / 100;
        handleSample(t, bus, shunt);
#if EPM_DIAG
        const uint32_t dr = static_cast<uint32_t>(r1 - t), de = static_cast<uint32_t>(esp_timer_get_time() - r1);
        dN++; dReadSum += dr; dEmitSum += de; if (dr > dReadMax) dReadMax = dr; if (de > dEmitMax) dEmitMax = de;
#endif
    }

    /**
     * @brief Read back the INA226 configuration register. A sensor power
     * glitch resets it to its defaults while conversions keep running, which
     * the stall check cannot notice; restore our configuration if so.
     */
    void checkConfig(int64_t now) {
        lastConfigCheckUs = now;
        uint16_t config = 0;
        if (!ina.readConfig(config)) {
            onI2cError();
            return;
        }
        if (config != ina.configValue()) {
            Out::info("WARN INA226 configuration lost (power glitch?), reconfiguring");
            if (!applyConfig()) {
                onI2cError();
            }
        }
    }

    bool configCheckDue(int64_t now) {
        if (!inaOk) return false;
        const int64_t since = now - lastConfigCheckUs;
        if (since < Config::INA_CONFIG_CHECK_US) return false;
        // Use spare time; without any, check anyway every few intervals
        return slackUs(now) >= Config::INA_CONFIG_CHECK_COST_US
            || since >= 5 * Config::INA_CONFIG_CHECK_US;
    }

    void retryIfDue(int64_t now) {
        if (now - lastRetryUs < Config::INA_RETRY_INTERVAL_US) return;
        lastRetryUs = now;
        if (initSensor()) {
            Out::info("INA226 initialized");
        } else {
            Out::info("ERROR INA226 not found");
        }
    }
}

// =============================================================================
// Time Anchors
// =============================================================================

namespace Anchors {
    void emit() {
        if (!precisionTime.hasAnchor()) return;
        char buf[64];
        char* p = buf;
        *p++ = 'T';
        *p++ = ',';
        p = Out::putI64(p, precisionTime.anchorUs());
        *p++ = ',';
        p = Out::putU32(p, precisionTime.anchorEpoch());
        *p++ = ',';
        *p++ = precisionTime.anchorSource() == PrecisionTime::Source::Sqw ? 'S' : 'P';
        *p++ = '\n';
        Out::writeData(buf, static_cast<size_t>(p - buf));
    }
}

// =============================================================================
// Display
// =============================================================================

namespace Display {
    int64_t nextFrameUs = 0;
    int64_t frameStartUs = 0;
    int64_t lastProgressUs = 0;
    float lastV = 0.0f;
    float lastI = 0.0f;
    float lastP = 0.0f;

    void render(int64_t now) {
        char status[24];
        float rateHz = 0.0f;
        if (Sampling::frameSamples > 0) {
            const uint32_t n = Sampling::frameSamples;
            lastV = static_cast<float>(Sampling::frameBusSum / static_cast<double>(n)) * INA226Lite::BUS_LSB_V;
            lastI = static_cast<float>(Sampling::frameShuntSum / static_cast<double>(n))
                * INA226Lite::SHUNT_LSB_V / settings.shuntOhm;
            lastP = static_cast<float>(Sampling::framePowerSum / n);
            if (now > frameStartUs) {
                rateHz = n * 1e6f / static_cast<float>(now - frameStartUs);
            }
        }
        Sampling::frameBusSum = 0;
        Sampling::frameShuntSum = 0;
        Sampling::framePowerSum = 0.0;
        Sampling::frameSamples = 0;
        frameStartUs = now;

        if (!inaOk) {
            snprintf(status, sizeof(status), "NO INA226");
        } else {
            const char* clock = !precisionTime.rtcPresent() ? "noRTC"
                : (precisionTime.usingSqw() ? "SQW" : "RTC");
            snprintf(status, sizeof(status), "%s %.0fHz %s%s",
                     outputMode == OutputMode::Raw ? "RAW" : "CSV",
                     rateHz,
                     clock,
                     streamEnabled ? "" : " PAUSE");
        }
        display.beginReadings(lastV, lastI, lastP, status);
    }

    void resetAccumulators(int64_t now) {
        Sampling::frameBusSum = 0;
        Sampling::frameShuntSum = 0;
        Sampling::framePowerSum = 0.0;
        Sampling::frameSamples = 0;
        frameStartUs = now;
    }

    /**
     * @brief Advance the OLED frame using only spare time.
     *
     * A frame is drawn one glyph at a time and sent 8 bytes at a time, each
     * step taking ~100-300 µs, so steps fit in the idle time after a sample
     * and sampling never misses a conversion because of the display. If no
     * step could run for OLED_FORCE_AFTER_US (no idle time at all, e.g.
     * AVG 1), one step is forced per loop so the display still updates.
     */
    void service(int64_t now) {
        // Keep the frame accumulators bounded while nothing is rendered
        if (Sampling::frameSamples > Config::OLED_MAX_FRAME_SAMPLES) {
            resetAccumulators(now);
        }
        if (!display.isPresent() || !display.isEnabled()) {
            lastProgressUs = now;
            return;
        }

        if (!display.renderActive() && !display.transferActive() && now >= nextFrameUs) {
            render(now);    // formats the text and clears the buffer (cheap)
            nextFrameUs = now + Config::OLED_FRAME_INTERVAL_US;
        }

        bool forced = now - lastProgressUs > Config::OLED_FORCE_AFTER_US;
        while (display.renderActive() || display.transferActive()) {
            const int64_t t = esp_timer_get_time();
            const uint32_t cost = display.renderActive() ? display.glyphCostUs()
                                                         : display.chunkCostUs();
            if (Sampling::slackUs(t) < static_cast<int64_t>(cost) && !forced) break;
            forced = false;
            bool ok = true;
            if (display.renderActive()) {
                display.renderStep();   // false only when the frame is complete
            } else {
                ok = display.pumpChunk();
            }
            lastProgressUs = esp_timer_get_time();
            if (!ok) break;
        }
        if (!display.renderActive() && !display.transferActive()) {
            lastProgressUs = esp_timer_get_time();
        }
    }
}

// =============================================================================
// Commands
// =============================================================================

namespace Commands {
    constexpr size_t MAX_LINE = 96;
    char lineBuf[MAX_LINE];
    size_t lineLen = 0;
    bool overflow = false;

    void replyOk(const char* command) {
        char buf[48];
        snprintf(buf, sizeof(buf), "!OK %s", command);
        Out::line(buf);
    }

    void replyErr(const char* command, const char* reason) {
        // The command token is truncated so the reason is never cut off
        char buf[96];
        snprintf(buf, sizeof(buf), "!ERR %.40s %s", command, reason);
        Out::line(buf);
    }

    void replyConfig() {
        char buf[160];
        snprintf(buf, sizeof(buf),
                 "!CFG,avg=%u,vct=%u,ict=%u,shunt=%.6g,oled=%d,stream=%d,mode=%s,sqw=%d,period_us=%lu",
                 settings.averages, settings.busConvUs, settings.shuntConvUs,
                 static_cast<double>(settings.shuntOhm),
                 settings.oledEnabled ? 1 : 0,
                 streamEnabled ? 1 : 0,
                 outputMode == OutputMode::Raw ? "RAW" : "CSV",
                 precisionTime.usingSqw() ? 1 : 0,
                 static_cast<unsigned long>(
                     (static_cast<uint32_t>(settings.busConvUs) + settings.shuntConvUs) * settings.averages));
        Out::line(buf);
    }

    bool parseUInt(const char* s, uint32_t& value) {
        if (!s || !*s) return false;
        char* end = nullptr;
        const unsigned long v = strtoul(s, &end, 10);
        if (*end != '\0') return false;
        value = v;
        return true;
    }

    bool parseBool(const char* s, bool& value) {
        if (s && strcmp(s, "1") == 0) { value = true; return true; }
        if (s && strcmp(s, "0") == 0) { value = false; return true; }
        return false;
    }

    void handleSet(char* key, char* arg) {
        if (!key) { replyErr("SET", "missing_argument"); return; }
        if (!arg) { replyErr("SET", "missing_argument"); return; }

        uint32_t u = 0;
        bool b = false;

        if (strcmp(key, "AVG") == 0 || strcmp(key, "VCT") == 0 || strcmp(key, "ICT") == 0) {
            if (!parseUInt(arg, u) || u > 0xFFFF) { replyErr("SET", "invalid_value"); return; }
            const uint16_t v = static_cast<uint16_t>(u);
            const bool isAvg = key[0] == 'A';
            if (isAvg ? !INA226Lite::isValidAverage(v) : !INA226Lite::isValidConversionTime(v)) {
                replyErr("SET", "invalid_value");
                return;
            }
            const DeviceSettings previous = settings;
            if (isAvg) settings.averages = v;
            else if (key[0] == 'V') settings.busConvUs = v;
            else settings.shuntConvUs = v;
            if (!Sampling::applyConfig()) {
                settings = previous;
                replyErr("SET", "i2c_error");
                return;
            }
            replyOk("SET");
        } else if (strcmp(key, "SHUNT") == 0) {
            char* end = nullptr;
            const float ohm = strtof(arg, &end);
            if (*end != '\0' || !(ohm >= Config::SHUNT_MIN_OHM && ohm <= Config::SHUNT_MAX_OHM)) {
                replyErr("SET", "invalid_value");
                return;
            }
            settings.shuntOhm = ohm;
            replyOk("SET");
        } else if (strcmp(key, "OLED") == 0) {
            if (!parseBool(arg, b)) { replyErr("SET", "invalid_value"); return; }
            settings.oledEnabled = b;
            display.setEnabled(b);
            replyOk("SET");
        } else if (strcmp(key, "STREAM") == 0) {
            if (!parseBool(arg, b)) { replyErr("SET", "invalid_value"); return; }
            streamEnabled = b;
            replyOk("SET");
            if (b && outputMode == OutputMode::Raw) {
                Anchors::emit();  // The host may hold a stale anchor after a pause
            }
        } else {
            replyErr("SET", "unknown_parameter");
        }
    }

    void handleSync(char* datePart, char* timePart) {
        if (!datePart || !timePart) { replyErr("SYNC", "missing_argument"); return; }
        int y, mo, d, h, mi, s;
        int usedDate = 0;
        int usedTime = 0;
        if (sscanf(datePart, "%d-%d-%d%n", &y, &mo, &d, &usedDate) != 3 || datePart[usedDate] != '\0'
            || sscanf(timePart, "%d:%d:%d%n", &h, &mi, &s, &usedTime) != 3 || timePart[usedTime] != '\0') {
            replyErr("SYNC", "invalid_value");
            return;
        }
        static const uint8_t DAYS[] = {31, 29, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31};
        if (y < 2000 || y > 2099 || mo < 1 || mo > 12 || d < 1 || d > DAYS[mo - 1]
            || h < 0 || h > 23 || mi < 0 || mi > 59 || s < 0 || s > 59) {
            replyErr("SYNC", "invalid_value");
            return;
        }
        if (mo == 2 && d == 29 && (y % 4) != 0) {
            replyErr("SYNC", "invalid_value");
            return;
        }
        if (!rtcOk) { replyErr("SYNC", "rtc_unavailable"); return; }

        const DateTime dt(y, mo, d, h, mi, s);
        if (!precisionTime.setTime(dt)) {
            replyErr("SYNC", "i2c_error");
            return;
        }
        replyOk("SYNC");
        if (outputMode == OutputMode::Raw && streamEnabled) {
            Anchors::emit();  // New time line starts now
        }
    }

    void handleMode(char* arg) {
        if (arg && strcmp(arg, "RAW") == 0) {
            outputMode = OutputMode::Raw;
            replyOk("MODE");
            Anchors::emit();  // Give the host a wall-clock reference right away
        } else if (arg && strcmp(arg, "CSV") == 0) {
            outputMode = OutputMode::Csv;
            replyOk("MODE");
            Out::line("Timestamp,Voltage[V],Current[A],Power[W]");
        } else {
            replyErr("MODE", arg ? "invalid_value" : "missing_argument");
        }
    }

    void execute(char* line) {
        for (char* c = line; *c; c++) {
            *c = static_cast<char>(toupper(static_cast<unsigned char>(*c)));
        }

        char* save = nullptr;
        char* cmd = strtok_r(line, " \t", &save);
        if (!cmd) return;  // Empty line
        char* arg1 = strtok_r(nullptr, " \t", &save);
        char* arg2 = strtok_r(nullptr, " \t", &save);

        if (strcmp(cmd, "HELLO") == 0) {
            char buf[64];
            snprintf(buf, sizeof(buf), "!HELLO,%s,%s,%d", FIRMWARE_NAME, FIRMWARE_VERSION, PROTOCOL_VERSION);
            Out::line(buf);
        } else if (strcmp(cmd, "GET") == 0) {
            replyConfig();
        } else if (strcmp(cmd, "MODE") == 0) {
            handleMode(arg1);
        } else if (strcmp(cmd, "SET") == 0) {
            handleSet(arg1, arg2);
        } else if (strcmp(cmd, "SYNC") == 0) {
            handleSync(arg1, arg2);
        } else if (strcmp(cmd, "SAVE") == 0) {
            if (Storage::save()) replyOk("SAVE");
            else replyErr("SAVE", "nvs_error");
        } else {
            replyErr(cmd, "unknown_command");
        }
    }

    /** @brief Accumulate received bytes and execute complete lines. */
    void poll() {
        while (Serial.available() > 0) {
            const int c = Serial.read();
            if (c < 0) break;
            if (c == '\r') continue;
            if (c == '\n') {
                if (overflow) {
                    replyErr("?", "line_too_long");
                } else {
                    lineBuf[lineLen] = '\0';
                    execute(lineBuf);
                }
                lineLen = 0;
                overflow = false;
                continue;
            }
            if (lineLen < MAX_LINE - 1) {
                lineBuf[lineLen++] = static_cast<char>(c);
            } else {
                overflow = true;
            }
        }
    }
}

// =============================================================================
// Initialization
// =============================================================================

namespace Init {
    bool rtc() {
        if (!::rtc.begin(&Wire)) {
            Out::info("ERROR DS3231 RTC not found");
            return false;
        }
        if (Config::FORCE_RTC_UPDATE || ::rtc.lostPower()) {
            ::rtc.adjust(DateTime(F(__DATE__), F(__TIME__)));
            Out::info("RTC synchronized to compile time");
        }
        ::rtc.disable32K();

        uint32_t epoch = 0;
        if (!precisionTime.readTime(epoch)) {
            Out::info("WARN DS3231 time unreadable");
            return true;  // Present; begin() retries the read
        }
        const DateTime now(epoch);
        char buf[48];
        snprintf(buf, sizeof(buf), "RTC time: %04u-%02u-%02u %02u:%02u:%02u",
                 now.year(), now.month(), now.day(), now.hour(), now.minute(), now.second());
        Out::info(buf);
        return true;
    }
}

// =============================================================================
// Setup & Main Loop
// =============================================================================

static void serviceOnce();

void setup() {
    // See loop(): the loop task never yields, so the idle task must not be
    // watched by the task watchdog.
    disableCore0WDT();

#if EPM_SERIAL_IS_HWCDC
    Serial.setTxBufferSize(Config::SERIAL_TX_BUFFER);
    Serial.setRxBufferSize(Config::SERIAL_RX_BUFFER);
#endif
    Serial.begin(Config::SERIAL_BAUD);
#if EPM_SERIAL_IS_HWCDC
    // Never block on writes when no host is reading
    Serial.setTxTimeoutMs(0);
#endif
    const uint32_t waitStart = millis();
    while (!Serial && millis() - waitStart < Config::HOST_WAIT_MS) {
        delay(10);
    }

    Out::info(FIRMWARE_NAME " v" FIRMWARE_VERSION " starting");

    Wire.begin();
    // 400 kHz (I2C Fast mode): the DS3231 and SSD1306 are rated for 400 kHz max
    Wire.setClock(400000);

    Storage::load();

    if (display.begin(Config::SCREEN_ADDRESS)) {
        if (!settings.oledEnabled) {
            display.setEnabled(false);
        }
        display.showMessage("Edge", "Power");
    } else {
        Out::info("WARN SSD1306 display not found, running without display");
    }

    rtcOk = Init::rtc();
    precisionTime.begin(rtcOk);

    if (Sampling::initSensor()) {
        Out::info("INA226 initialized");
    } else {
        Out::info("ERROR INA226 not found");
        display.showMessage("INA226", "Error");
    }
    Sampling::lastRetryUs = esp_timer_get_time();

    char buf[64];
    snprintf(buf, sizeof(buf), "%s v%s ready (protocol %d)", FIRMWARE_NAME, FIRMWARE_VERSION, PROTOCOL_VERSION);
    Out::info(buf);
    Out::line("Timestamp,Voltage[V],Current[A],Power[W]");

    const int64_t readyUs = esp_timer_get_time();
    Sampling::restartTiming(readyUs);
    Sampling::lastConfigCheckUs = readyUs;
    Display::frameStartUs = readyUs;
    Display::lastProgressUs = readyUs;
}

#if EPM_DIAG
namespace Diag {
    // Attribute missed conversions to the slowest task since the last sample.
    const char* names[] = {"poll", "cmd", "rtc", "cfg", "oled"};
    uint32_t blame[5] = {0};
    uint32_t maxUs[5] = {0};
    int64_t worstSince = 0;
    int worstTask = -1;
    int64_t lastSampleUs = 0;
    uint32_t lastSeq = 0;
    int64_t lastPrintUs = 0;
    uint32_t misses = 0;

    void task(int idx, int64_t dur) {
        if (dur > static_cast<int64_t>(maxUs[idx])) maxUs[idx] = static_cast<uint32_t>(dur);
        if (dur > worstSince) { worstSince = dur; worstTask = idx; }
    }
    void afterPoll() {
        if (Sampling::seq != lastSeq) {
            const int64_t t = Sampling::lastReadyUs;
            if (lastSampleUs && t - lastSampleUs > static_cast<int64_t>(Sampling::periodUs) * 3 / 2) {
                misses++;
                if (worstTask >= 0) blame[worstTask]++;
            }
            lastSampleUs = t;
            lastSeq = Sampling::seq;
            worstSince = 0;
            worstTask = -1;
        }
    }
    void print(int64_t now) {
        if (now - lastPrintUs < 5000000) return;
        lastPrintUs = now;
        char buf[160];
        snprintf(buf, sizeof(buf), "DIAG miss=%lu blame poll=%lu cmd=%lu rtc=%lu cfg=%lu oled=%lu | max us %lu %lu %lu %lu %lu",
                 (unsigned long)misses, (unsigned long)blame[0], (unsigned long)blame[1], (unsigned long)blame[2],
                 (unsigned long)blame[3], (unsigned long)blame[4], (unsigned long)maxUs[0], (unsigned long)maxUs[1],
                 (unsigned long)maxUs[2], (unsigned long)maxUs[3], (unsigned long)maxUs[4]);
        Out::info(buf);
        using namespace Sampling;
        snprintf(buf, sizeof(buf), "DIAG poll avg %lu max %lu (%lu/sample) | read avg %lu max %lu | emit avg %lu max %lu",
                 (unsigned long)(dPolls ? dPollSum / dPolls : 0), (unsigned long)dPollMax,
                 (unsigned long)(dN ? dPolls / dN : 0),
                 (unsigned long)(dN ? dReadSum / dN : 0), (unsigned long)dReadMax,
                 (unsigned long)(dN ? dEmitSum / dN : 0), (unsigned long)dEmitMax);
        Out::info(buf);
        dPollMax = dReadMax = dEmitMax = dPollSum = dReadSum = dEmitSum = dN = dPolls = 0;
        for (auto& b : blame) b = 0;
        for (auto& m : maxUs) m = 0;
        misses = 0;
    }
}
#define DIAG_T0() const int64_t _d0 = esp_timer_get_time()
#define DIAG_TASK(i) Diag::task(i, esp_timer_get_time() - _d0)
#else
#define DIAG_T0()
#define DIAG_TASK(i)
#endif

/**
 * @brief The Arduino core calls yieldIfNecessary() between loop() iterations,
 * which on single-core chips (ESP32-C3) sleeps 5 RTOS ticks (5 ms) every 2 s
 * and dropped ~4 INA226 conversions each time. loop() therefore never
 * returns; the idle-task watchdog that yield existed for is disabled in
 * setup(). Higher-priority tasks (esp_timer, USB/event handling, ISRs) still
 * preempt this loop normally.
 */
void loop() {
    for (;;) {
        serviceOnce();
    }
}

static void serviceOnce() {
    int64_t now = esp_timer_get_time();

    // 1. Sampling has priority
    if (inaOk) {
        if (now >= Sampling::nextPollUs) {
            DIAG_T0();
            Sampling::step(now);
            DIAG_TASK(0);
#if EPM_DIAG
            Diag::afterPoll();
#endif
        }
    } else {
        Sampling::retryIfDue(now);
    }

    // 2. Host commands (CPU only, cheap)
    {
        DIAG_T0();
        Commands::poll();
        DIAG_TASK(1);
    }

    // 3. Background I2C work, only in the slack before the next conversion
    now = esp_timer_get_time();
    precisionTime.update(now);
    if (precisionTime.workPending(now)
        && (Sampling::slackUs(now) >= Config::RTC_WORK_COST_US || precisionTime.overdue(now))) {
        DIAG_T0();
        if (precisionTime.service(now) && outputMode == OutputMode::Raw && streamEnabled) {
            Anchors::emit();
        }
        DIAG_TASK(2);
    }

    now = esp_timer_get_time();
    if (Sampling::configCheckDue(now)) {
        DIAG_T0();
        Sampling::checkConfig(now);
        DIAG_TASK(3);
    }

    {
        DIAG_T0();
        Display::service(esp_timer_get_time());
        DIAG_TASK(4);
    }
#if EPM_DIAG
    Diag::print(esp_timer_get_time());
#endif
}
