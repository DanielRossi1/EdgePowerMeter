/**
 * @file PrecisionTime.cpp
 * @brief Implementation of DS3231 time anchors with µs device time
 * @version 2.0.0
 *
 * @author Daniel Rossi
 * @license Apache-2.0
 */

#include "PrecisionTime.h"

#include <Wire.h>
#include <esp_timer.h>

PrecisionTime* PrecisionTime::_instance = nullptr;

namespace {
    bool isBcd(uint8_t v) {
        return (v & 0x0F) <= 9 && (v >> 4) <= 9;
    }

    uint8_t bcdToBin(uint8_t v) {
        return static_cast<uint8_t>((v >> 4) * 10 + (v & 0x0F));
    }

    uint8_t binToBcd(uint8_t v) {
        return static_cast<uint8_t>(((v / 10) << 4) | (v % 10));
    }

    /** Floor division of a signed µs interval into whole seconds. */
    int64_t floorSeconds(int64_t us) {
        int64_t s = us / 1000000;
        if (us % 1000000 < 0) s -= 1;
        return s;
    }
}

PrecisionTime::PrecisionTime(RTC_DS3231& rtc, uint8_t sqwPin)
    : _rtc(rtc)
    , _sqwPin(sqwPin)
    , _rtcOk(false)
    , _edgeUs(0)
    , _edgeCount(0)
    , _mux(portMUX_INITIALIZER_UNLOCKED)
    , _processedEdges(0)
    , _lastEdgeUs(0)
    , _haveEdgeRef(false)
    , _sqwActive(false)
    , _startUs(0)
    , _havePolledSecond(false)
    , _lastPolledSecond(0)
    , _lastPollUs(0)
    , _lastAttemptUs(0)
    , _lastChangeUs(0)
    , _nextPollUs(0)
    , _windowStartUs(0)
    , _windowTight(false)
    , _windowEndUs(0)
    , _boundaryPending(false)
    , _pendingBoundaryUs(0)
    , _anchorUs(0)
    , _anchorEpoch(NO_RTC_EPOCH)
    , _anchorSrc(Source::None)
    , _haveCoarseAnchor(false)
    , _implausibleReads(0)
    , _cachedEpoch(0)
    , _cachedDate{0}
{
    _instance = this;
}

// The DS3231 increments its seconds register on the falling edge of the
// 1 Hz SQW output (datasheet: after a seconds write the output goes high
// 500 ms later and falls at the next second), so a falling edge marks the
// start of a new RTC second.
//
// Note: the Arduino GPIO interrupt dispatcher is not placed in IRAM on this
// core, so an edge that occurs during a flash operation (SAVE) is
// timestamped late. service() rejects such edges by checking their spacing.
void IRAM_ATTR PrecisionTime::onSqwFallingEdge() {
    PrecisionTime* self = _instance;
    if (!self) return;
    const int64_t now = esp_timer_get_time();
    portENTER_CRITICAL_ISR(&self->_mux);
    if (self->_edgeCount == 0 || now - self->_edgeUs >= SQW_DEBOUNCE_US) {
        self->_edgeUs = now;
        self->_edgeCount = self->_edgeCount + 1;
    }
    portEXIT_CRITICAL_ISR(&self->_mux);
}

void PrecisionTime::begin(bool rtcAvailable) {
    _rtcOk = rtcAvailable;
    _startUs = esp_timer_get_time();
    _anchorUs = 0;
    _anchorEpoch = NO_RTC_EPOCH;
    _anchorSrc = Source::None;
    _haveCoarseAnchor = false;
    if (!_rtcOk) {
        return;
    }

    // Coarse anchor so CSV timestamps are valid immediately; refined by the
    // first SQW edge or polled boundary.
    for (uint8_t attempt = 0; attempt < 3 && !_haveCoarseAnchor; attempt++) {
        uint32_t epoch;
        if (readTime(epoch)) {
            _anchorEpoch = epoch;
            _anchorUs = esp_timer_get_time();
            _haveCoarseAnchor = true;
        }
    }

    _rtc.writeSqwPinMode(DS3231_SquareWave1Hz);
    pinMode(_sqwPin, INPUT_PULLUP);  // SQW is open drain
    attachInterrupt(digitalPinToInterrupt(_sqwPin), onSqwFallingEdge, FALLING);
    startSearch(esp_timer_get_time());
}

void PrecisionTime::readEdge(int64_t& edgeUs, uint32_t& edgeCount) const {
    portENTER_CRITICAL(&_mux);
    edgeUs = _edgeUs;
    edgeCount = _edgeCount;
    portEXIT_CRITICAL(&_mux);
}

void PrecisionTime::startSearch(int64_t nowUs) {
    _boundaryPending = false;
    _havePolledSecond = false;
    _nextPollUs = 0;
    _lastPollUs = nowUs;
    _lastAttemptUs = nowUs;
    _lastChangeUs = nowUs;
    _windowStartUs = 0;
    _windowTight = false;
    _windowEndUs = 0;
}

void PrecisionTime::openWindow(int64_t boundaryUs, bool tight) {
    _windowStartUs = boundaryUs + 1000000 - POLL_WINDOW_LEAD_US;
    _windowEndUs = boundaryUs + 1000000 + POLL_DEADLINE_US;
    _windowTight = tight;
    _nextPollUs = _windowStartUs;
}

void PrecisionTime::update(int64_t nowUs) {
    if (!_rtcOk) return;

    if (_sqwActive && nowUs - _lastEdgeUs > SQW_TIMEOUT_US) {
        // Edges stopped (SQW not wired or RTC fault): fall back to polling
        _sqwActive = false;
        startSearch(nowUs);
        return;
    }
    if (!_sqwActive && _windowTight && nowUs > _windowEndUs) {
        // The expected boundary was not seen in time: keep searching with
        // the slower forced polling instead of hammering the bus.
        _windowTight = false;
    }
}

bool PrecisionTime::workPending(int64_t nowUs) const {
    if (!_rtcOk) return false;

    int64_t edgeUs;
    uint32_t edgeCount;
    readEdge(edgeUs, edgeCount);
    if (edgeCount != _processedEdges) return true;

    if (_sqwActive) return false;

    // Give the SQW interrupt a chance before starting to poll
    if (_processedEdges == 0 && nowUs - _startUs < SQW_TIMEOUT_US) return false;

    return _boundaryPending || nowUs >= _nextPollUs;
}

bool PrecisionTime::overdue(int64_t nowUs) const {
    if (!workPending(nowUs)) return false;

    int64_t edgeUs;
    uint32_t edgeCount;
    readEdge(edgeUs, edgeCount);
    if (edgeCount != _processedEdges) {
        return nowUs - edgeUs > EDGE_DEADLINE_US;
    }

    if (_boundaryPending) {
        return nowUs - _pendingBoundaryUs > PENDING_READ_DEADLINE_US;
    }

    // Polling: make sure polls happen even without slack, often enough to
    // bracket the boundary tightly (inside a tight window) or to find it at
    // all (while searching). A stuck RTC (seconds never change) is polled
    // only rarely so it cannot starve sampling.
    const int64_t sinceAttempt = nowUs - _lastAttemptUs;
    if (_windowTight && _windowStartUs != 0) {
        return sinceAttempt >= POLL_FORCED_INTERVAL_US;
    }
    const bool stuck = nowUs - _lastChangeUs > 3000000;
    return sinceAttempt >= (stuck ? 200000 : POLL_SEARCH_FORCED_US);
}

bool PrecisionTime::readSeconds(uint8_t& seconds) {
    // Single transaction (repeated start): the bus is shared with sampling.
    Wire.beginTransmission(RTC_ADDRESS);
    Wire.write(static_cast<uint8_t>(0x00));
    Wire.endTransmission(false);
    if (Wire.requestFrom(RTC_ADDRESS, static_cast<uint8_t>(1)) != 1) return false;
    const uint8_t bcd = Wire.read() & 0x7F;
    if (!isBcd(bcd)) return false;
    seconds = bcdToBin(bcd);
    return seconds < 60;
}

bool PrecisionTime::readTime(uint32_t& epoch) {
    // Read registers 0x00-0x06 directly: RTClib's now() ignores I2C errors
    // and would return an uninitialized buffer as a date.
    Wire.beginTransmission(RTC_ADDRESS);
    Wire.write(static_cast<uint8_t>(0x00));
    Wire.endTransmission(false);
    if (Wire.requestFrom(RTC_ADDRESS, static_cast<uint8_t>(7)) != 7) return false;
    uint8_t r[7];
    for (uint8_t i = 0; i < 7; i++) {
        r[i] = static_cast<uint8_t>(Wire.read());
    }

    const uint8_t secB = r[0] & 0x7F;
    const uint8_t minB = r[1] & 0x7F;
    const uint8_t dayB = r[4] & 0x3F;
    const uint8_t monB = r[5] & 0x1F;  // Bit 7 is the century flag
    const uint8_t yearB = r[6];
    if (!isBcd(secB) || !isBcd(minB) || !isBcd(dayB) || !isBcd(monB) || !isBcd(yearB)) {
        return false;
    }

    uint8_t hour;
    if (r[2] & 0x40) {
        // 12-hour mode (RTClib uses 24 h, but accept what the chip holds)
        const uint8_t h12B = r[2] & 0x1F;
        if (!isBcd(h12B)) return false;
        const uint8_t h12 = bcdToBin(h12B);
        if (h12 < 1 || h12 > 12) return false;
        hour = static_cast<uint8_t>((h12 % 12) + ((r[2] & 0x20) ? 12 : 0));
    } else {
        const uint8_t h24B = r[2] & 0x3F;
        if (!isBcd(h24B)) return false;
        hour = bcdToBin(h24B);
    }

    const uint8_t sec = bcdToBin(secB);
    const uint8_t min = bcdToBin(minB);
    const uint8_t day = bcdToBin(dayB);
    const uint8_t mon = bcdToBin(monB);
    const uint8_t year = bcdToBin(yearB);
    if (sec > 59 || min > 59 || hour > 23 || day < 1 || day > 31 || mon < 1 || mon > 12 || year > 99) {
        return false;
    }
    epoch = DateTime(static_cast<uint16_t>(2000 + year), mon, day, hour, min, sec).unixtime();
    return true;
}

bool PrecisionTime::expectedEpoch(int64_t atUs, uint32_t& epoch) const {
    if (!_haveCoarseAnchor) return false;
    epoch = _anchorEpoch + static_cast<int32_t>(floorSeconds(atUs - _anchorUs));
    return true;
}

bool PrecisionTime::readTimeChecked(uint32_t& epoch, int64_t atUs) {
    if (!readTime(epoch)) return false;

    uint32_t expected;
    if (expectedEpoch(atUs, expected)) {
        const uint32_t diff = epoch > expected ? epoch - expected : expected - epoch;
        if (diff > PLAUSIBLE_DRIFT_S) {
            // Most likely a corrupted read. If it keeps happening, the RTC was
            // really changed (e.g. set by another tool): trust it again.
            if (++_implausibleReads < MAX_IMPLAUSIBLE_READS) {
                return false;
            }
        }
    }
    _implausibleReads = 0;
    return true;
}

bool PrecisionTime::service(int64_t nowUs) {
    if (!_rtcOk) return false;

    int64_t edgeUs;
    uint32_t edgeCount;
    readEdge(edgeUs, edgeCount);

    if (edgeCount != _processedEdges) {
        // Edges must be a whole number of seconds apart; anything else is a
        // glitch or an edge timestamped late (interrupt latency).
        bool consistent = true;
        if (_haveEdgeRef) {
            const int64_t delta = edgeUs - _lastEdgeUs;
            const int64_t n = (delta + 500000) / 1000000;
            const int64_t err = delta - n * 1000000;
            consistent = n >= 1 && err <= SQW_TOLERANCE_US && err >= -SQW_TOLERANCE_US;
        }
        _processedEdges = edgeCount;
        _lastEdgeUs = edgeUs;
        _haveEdgeRef = true;
        _sqwActive = true;
        if (!consistent) {
            return false;
        }

        // Resolve which second started at the edge. If this runs late, step
        // back by the whole seconds elapsed since the edge. The time is taken
        // before and after the read: if a second boundary fell inside the
        // read, the result is ambiguous and the expected value is used.
        const int64_t before = esp_timer_get_time();
        uint32_t rtcEpoch;
        const bool ok = readTimeChecked(rtcEpoch, before);
        const int64_t after = esp_timer_get_time();
        const int64_t s0 = floorSeconds(before - edgeUs);
        const int64_t s1 = floorSeconds(after - edgeUs);
        uint32_t epoch;
        if (ok && s0 == s1) {
            epoch = rtcEpoch - static_cast<uint32_t>(s0);
        } else if (hasAnchor()) {
            // Keep the time line continuous with the previous precise anchor
            // (the edge is a whole number of seconds after it).
            const int64_t secs = (edgeUs - _anchorUs + 500000) / 1000000;
            epoch = _anchorEpoch + static_cast<uint32_t>(secs);
        } else {
            return false;
        }
        _anchorEpoch = epoch;
        _anchorUs = edgeUs;
        _anchorSrc = Source::Sqw;
        _haveCoarseAnchor = true;
        return true;
    }

    if (_sqwActive) return false;

    // Polling fallback, step 2: full time read for a boundary found earlier.
    if (_boundaryPending) {
        _boundaryPending = false;
        const int64_t before = esp_timer_get_time();
        uint32_t epoch;
        if (!readTimeChecked(epoch, before)) {
            return false;
        }
        const int64_t after = esp_timer_get_time();
        const int64_t s0 = floorSeconds(before - _pendingBoundaryUs);
        if (floorSeconds(after - _pendingBoundaryUs) != s0) {
            return false;  // another boundary fell inside the read: ambiguous
        }
        _anchorEpoch = epoch - static_cast<uint32_t>(s0);
        _anchorUs = _pendingBoundaryUs;
        _anchorSrc = Source::Poll;
        _haveCoarseAnchor = true;
        return true;
    }

    // Polling fallback, step 1. The DS3231 latches its time registers at the
    // I2C START, so the moment just before the transaction is the sample time.
    const int64_t pollUs = esp_timer_get_time();
    _lastAttemptUs = pollUs;
    uint8_t seconds;
    if (!readSeconds(seconds)) {
        _nextPollUs = nowUs + POLL_COARSE_INTERVAL_US;
        return false;
    }

    const bool inWindow = _windowStartUs != 0 && pollUs >= _windowStartUs;
    if (!_havePolledSecond) {
        _havePolledSecond = true;
        _lastPolledSecond = seconds;
        _lastPollUs = pollUs;
        _nextPollUs = pollUs + POLL_COARSE_INTERVAL_US;
        return false;
    }

    if (seconds == _lastPolledSecond) {
        _lastPollUs = pollUs;
        _nextPollUs = pollUs + (inWindow ? POLL_FINE_INTERVAL_US : POLL_COARSE_INTERVAL_US);
        return false;
    }

    // The second changed between the previous poll and this one
    const int64_t previousPollUs = _lastPollUs;
    const int64_t gap = pollUs - previousPollUs;
    _lastPolledSecond = seconds;
    _lastPollUs = pollUs;
    _lastChangeUs = pollUs;

    if (gap > POLL_MAX_GAP_US) {
        // The boundary is somewhere in a wide interval: its midpoint would
        // be off by up to half a second. Don't publish it; look for the next
        // boundary starting where it can first occur.
        openWindow(previousPollUs, false);
        return false;
    }

    const int64_t boundaryUs = previousPollUs + gap / 2;
    openWindow(boundaryUs, true);
    _pendingBoundaryUs = boundaryUs;
    _boundaryPending = true;
    return false;
}

bool PrecisionTime::setTime(const DateTime& dt) {
    if (!_rtcOk) return false;

    uint8_t dow = dt.dayOfTheWeek();  // 0 = Sunday; the DS3231 uses 1..7
    if (dow == 0) dow = 7;

    Wire.beginTransmission(RTC_ADDRESS);
    Wire.write(static_cast<uint8_t>(0x00));
    Wire.write(binToBcd(dt.second()));
    Wire.write(binToBcd(dt.minute()));
    Wire.write(binToBcd(dt.hour()));
    Wire.write(dow);
    Wire.write(binToBcd(dt.day()));
    Wire.write(binToBcd(dt.month()));
    Wire.write(binToBcd(static_cast<uint8_t>(dt.year() - 2000U)));
    // Wire buffers the bytes and sends them in endTransmission(): the seconds
    // byte (which restarts the RTC countdown) is acknowledged about
    // SYNC_SECONDS_ACK_US after the transaction starts.
    const int64_t startUs = esp_timer_get_time();
    if (Wire.endTransmission() != 0) {
        return false;
    }
    const int64_t writeUs = startUs + SYNC_SECONDS_ACK_US;

    // Clear the Oscillator Stop Flag (best effort, like RTClib::adjust)
    Wire.beginTransmission(RTC_ADDRESS);
    Wire.write(static_cast<uint8_t>(0x0F));
    if (Wire.endTransmission() == 0 && Wire.requestFrom(RTC_ADDRESS, static_cast<uint8_t>(1)) == 1) {
        const uint8_t status = static_cast<uint8_t>(Wire.read());
        Wire.beginTransmission(RTC_ADDRESS);
        Wire.write(static_cast<uint8_t>(0x0F));
        Wire.write(static_cast<uint8_t>(status & ~0x80));
        Wire.endTransmission();
    }

    // Verify the write
    uint32_t readBack;
    const uint32_t target = dt.unixtime();
    if (!readTime(readBack) || readBack < target || readBack > target + 1) {
        return false;
    }

    // Edges captured before the write belong to the old time line
    int64_t edgeUs;
    uint32_t edgeCount;
    readEdge(edgeUs, edgeCount);
    _processedEdges = edgeCount;
    _lastEdgeUs = writeUs;
    _haveEdgeRef = true;

    _boundaryPending = false;
    _anchorEpoch = target;
    _anchorUs = writeUs;
    _anchorSrc = _sqwActive ? Source::Sqw : Source::Poll;
    _haveCoarseAnchor = true;
    _implausibleReads = 0;
    _cachedEpoch = 0;

    if (!_sqwActive) {
        _havePolledSecond = true;
        _lastPolledSecond = dt.second();
        _lastPollUs = writeUs;
        _lastAttemptUs = writeUs;
        _lastChangeUs = writeUs;
        openWindow(writeUs, true);
    }
    return true;
}

void PrecisionTime::formatTimestamp(int64_t tUs, char* buffer, size_t bufferSize) {
    const int64_t elapsed = tUs - _anchorUs;
    const int64_t secs = floorSeconds(elapsed);
    const int64_t rem = elapsed - secs * 1000000;
    const uint32_t epoch = _anchorEpoch + static_cast<int32_t>(secs);

    if (epoch != _cachedEpoch || _cachedDate[0] == '\0') {
        const DateTime dt(epoch);
        snprintf(_cachedDate, sizeof(_cachedDate), "%04u-%02u-%02u %02u:%02u:%02u",
                 dt.year(), dt.month(), dt.day(), dt.hour(), dt.minute(), dt.second());
        _cachedEpoch = epoch;
    }
    snprintf(buffer, bufferSize, "%s.%03u", _cachedDate, static_cast<unsigned>(rem / 1000));
}
