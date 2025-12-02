/**
 * @file PrecisionTime.cpp
 * @brief Implementation of millisecond-accurate timing with DS3231 SQW
 * @version 1.0.0
 * 
 * @author Daniel Rossi
 * @license Apache-2.0
 */

#include "PrecisionTime.h"

// Static instance pointer for ISR
PrecisionTime* PrecisionTime::_instance = nullptr;

PrecisionTime::PrecisionTime(RTC_DS3231& rtc, uint8_t sqwPin)
    : _rtc(rtc)
    , _sqwPin(sqwPin)
    , _syncPending(false)
    , _syncMillis(0)
    , _lastSyncMillis(0)
    , _initialized(false)
    , _usingSqw(false)
    , _lastSecond(255)
{
    _instance = this;
}

void IRAM_ATTR PrecisionTime::_onSqwPulse() {
    if (_instance) {
        _instance->_syncMillis = millis();
        _instance->_syncPending = true;
    }
}

bool PrecisionTime::begin() {
    // Configure SQW pin as input with pullup
    pinMode(_sqwPin, INPUT_PULLUP);
    
    // Configure DS3231 to output 1Hz square wave
    _rtc.writeSqwPinMode(DS3231_SquareWave1Hz);
    delay(10);
    
    // Wait for first pulse to synchronize
    Serial.println(F("[PrecisionTime] Waiting for SQW sync..."));
    
    unsigned long startWait = millis();
    unsigned long timeout = startWait + 2500;
    
    int transitionCount = 0;
    int lastState = digitalRead(_sqwPin);
    
    while (millis() < timeout && transitionCount < 2) {
        int currentState = digitalRead(_sqwPin);
        if (currentState != lastState) {
            transitionCount++;
            
            // Sync on falling edge (HIGH -> LOW)
            if (lastState == HIGH && currentState == LOW) {
                _lastSyncMillis = millis();
                _syncedTime = _rtc.now();
                _initialized = true;
                _usingSqw = true;
                
                // Attach interrupt
                attachInterrupt(digitalPinToInterrupt(_sqwPin), _onSqwPulse, FALLING);
                
                Serial.print(F("[PrecisionTime] SQW sync OK ("));
                Serial.print(millis() - startWait);
                Serial.println(F("ms)"));
                return true;
            }
            lastState = currentState;
        }
        delayMicroseconds(100);
    }
    
    // SQW sync failed - use polling fallback
    Serial.println(F("[PrecisionTime] SQW not detected - using polling"));
    
    _syncedTime = _rtc.now();
    _lastSyncMillis = millis();
    _lastSecond = _syncedTime.second();
    _initialized = true;
    _usingSqw = false;
    
    return false;
}

void PrecisionTime::update() {
    if (_usingSqw && _syncPending) {
        // SQW interrupt mode
        noInterrupts();
        unsigned long capturedMillis = _syncMillis;
        _syncPending = false;
        interrupts();
        
        _syncedTime = _rtc.now();
        _lastSyncMillis = capturedMillis;
    } else if (!_usingSqw && _initialized) {
        // Polling fallback mode
        DateTime now = _rtc.now();
        if (now.second() != _lastSecond) {
            _lastSecond = now.second();
            _syncedTime = now;
            _lastSyncMillis = millis();
        }
    }
}

void PrecisionTime::getTimestamp(char* buffer, size_t bufferSize) {
    if (!_initialized) {
        DateTime now = _rtc.now();
        snprintf(buffer, bufferSize, "%04d-%02d-%02d %02d:%02d:%02d.000",
                 now.year(), now.month(), now.day(),
                 now.hour(), now.minute(), now.second());
        return;
    }
    
    unsigned long currentMillis = millis();
    unsigned long elapsed = currentMillis - _lastSyncMillis;
    
    uint16_t ms = elapsed % 1000;
    uint32_t extraSeconds = elapsed / 1000;
    
    DateTime now = _syncedTime + TimeSpan(extraSeconds);
    
    snprintf(buffer, bufferSize, "%04d-%02d-%02d %02d:%02d:%02d.%03d",
             now.year(), now.month(), now.day(),
             now.hour(), now.minute(), now.second(), ms);
}

uint64_t PrecisionTime::getUnixMillis() {
    if (!_initialized) {
        return (uint64_t)_rtc.now().unixtime() * 1000;
    }
    
    unsigned long currentMillis = millis();
    unsigned long elapsed = currentMillis - _lastSyncMillis;
    
    uint16_t ms = elapsed % 1000;
    uint32_t extraSeconds = elapsed / 1000;
    
    DateTime now = _syncedTime + TimeSpan(extraSeconds);
    return (uint64_t)now.unixtime() * 1000 + ms;
}
