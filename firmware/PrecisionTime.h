/**
 * @file PrecisionTime.h
 * @brief Millisecond-accurate timestamps using DS3231 SQW interrupt
 * @version 1.0.0
 * 
 * Uses the DS3231 RTC's 1Hz square wave output to synchronize with
 * millis() for sub-second precision. Falls back to polling if SQW
 * is not available.
 * 
 * @author Daniel Rossi
 * @license Apache-2.0
 */

#ifndef PRECISION_TIME_H
#define PRECISION_TIME_H

#include <Arduino.h>
#include <RTClib.h>

class PrecisionTime {
public:
    /**
     * @brief Construct with RTC reference and SQW pin
     * @param rtc Reference to initialized RTC_DS3231
     * @param sqwPin GPIO pin connected to DS3231 SQW output
     */
    PrecisionTime(RTC_DS3231& rtc, uint8_t sqwPin);
    
    /**
     * @brief Initialize precision timing
     * 
     * Configures the DS3231 SQW output and synchronizes timing.
     * Call this after RTC is initialized.
     * 
     * @return true if SQW sync successful, false if using polling fallback
     */
    bool begin();
    
    /**
     * @brief Update sync state - call from main loop
     * 
     * Handles ISR flags and updates time synchronization.
     * Should be called frequently for accurate timing.
     */
    void update();
    
    /**
     * @brief Get timestamp string with millisecond precision
     * @param buffer Output buffer (min 24 bytes)
     * @param bufferSize Size of buffer
     * 
     * Format: "YYYY-MM-DD HH:MM:SS.mmm"
     */
    void getTimestamp(char* buffer, size_t bufferSize);
    
    /**
     * @brief Get Unix timestamp with milliseconds
     * @return Unix time * 1000 + milliseconds
     */
    uint64_t getUnixMillis();
    
    /**
     * @brief Check if using SQW interrupt mode
     * @return true if SQW mode, false if polling fallback
     */
    bool isUsingSqw() const { return _usingSqw; }
    
    /**
     * @brief Check if initialized
     * @return true if timing system is ready
     */
    bool isInitialized() const { return _initialized; }

private:
    RTC_DS3231& _rtc;
    uint8_t _sqwPin;
    
    volatile bool _syncPending;
    volatile unsigned long _syncMillis;
    
    DateTime _syncedTime;
    unsigned long _lastSyncMillis;
    bool _initialized;
    bool _usingSqw;
    uint8_t _lastSecond;
    
    static PrecisionTime* _instance;
    static void IRAM_ATTR _onSqwPulse();
};

#endif // PRECISION_TIME_H
