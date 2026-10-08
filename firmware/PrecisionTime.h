/**
 * @file PrecisionTime.h
 * @brief Wall-clock time anchors from the DS3231 with microsecond device time
 * @version 2.0.0
 *
 * Maps the ESP32 monotonic timer (esp_timer_get_time(), µs since boot) to
 * the DS3231 wall-clock time through "anchors": pairs of (device µs at a
 * second boundary, RTC epoch of that second).
 *
 * Second boundaries come from the DS3231 1 Hz SQW output (falling edge,
 * captured in an ISR with µs accuracy). If no SQW edge is seen, the class
 * falls back to polling the RTC seconds register around the expected
 * boundary (≈ ms accuracy).
 *
 * All RTC I2C traffic happens in service(), which the main loop calls only
 * when it has time to spare between INA226 conversions (or when overdue()),
 * so time keeping never stalls sampling for long.
 *
 * Robustness rules:
 *   - RTC reads are checked at the I2C level and validated (BCD ranges and,
 *     once an anchor exists, plausibility against the expected time), so a
 *     failed transaction never becomes an anchor.
 *   - SQW edges are only published when they are a whole number of seconds
 *     (±2 ms) after the previous edge; glitches and edges timestamped late
 *     (interrupt latency during flash writes) are dropped.
 *   - A polled boundary is only accepted when the polls bracketing it are
 *     close together, so its midpoint is accurate.
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
    enum class Source : uint8_t {
        None,  // Coarse anchor from a single RTC read (sub-second part unknown)
        Sqw,   // Boundary captured by the SQW interrupt (µs accurate)
        Poll   // Boundary detected by polling the seconds register (≈ ms accurate)
    };

    PrecisionTime(RTC_DS3231& rtc, uint8_t sqwPin);

    /**
     * @brief Start time keeping.
     * @param rtcAvailable false if the DS3231 did not respond; timestamps then
     *        count from 2000-01-01 00:00:00 and no anchors are produced.
     */
    void begin(bool rtcAvailable);

    /** @brief Cheap state update (SQW/polling fallback); call every loop. */
    void update(int64_t nowUs);

    /** @brief True when service() has work to do (costs one or two RTC reads). */
    bool workPending(int64_t nowUs) const;

    /** @brief True when pending work must run now, even at the cost of sampling latency. */
    bool overdue(int64_t nowUs) const;

    /**
     * @brief Perform pending RTC work.
     * @return true when a new precise anchor (Sqw or Poll) is available
     */
    bool service(int64_t nowUs);

    /**
     * @brief Set the RTC and re-anchor on the write moment.
     *
     * Writing the seconds register restarts the DS3231 countdown chain, so
     * the write is the start of the new second. Pending SQW edges captured
     * before the write are discarded.
     *
     * @return false on I2C error or if the read-back does not match
     */
    bool setTime(const DateTime& dt);

    /**
     * @brief Read the RTC registers directly with I2C error checks.
     * @param epoch Seconds since 1970 of the RTC calendar (naive local time)
     * @return false on I2C error or out-of-range register values
     */
    bool readTime(uint32_t& epoch);

    /** @brief True once a precise (Sqw/Poll) anchor exists. */
    bool hasAnchor() const { return _rtcOk && _anchorSrc != Source::None; }
    bool rtcPresent() const { return _rtcOk; }
    int64_t anchorUs() const { return _anchorUs; }
    uint32_t anchorEpoch() const { return _anchorEpoch; }
    Source anchorSource() const { return _anchorSrc; }
    bool usingSqw() const { return _sqwActive; }

    /**
     * @brief Format the wall-clock time of a device timestamp.
     * @param tUs Device time (esp_timer_get_time())
     * @param buffer Output, at least 24 bytes: "YYYY-MM-DD HH:MM:SS.mmm"
     */
    void formatTimestamp(int64_t tUs, char* buffer, size_t bufferSize);

private:
    static constexpr uint8_t RTC_ADDRESS = 0x68;
    static constexpr int64_t SQW_TIMEOUT_US = 2500000;       // No edge for this long -> polling
    static constexpr int64_t SQW_DEBOUNCE_US = 900000;       // Ignore glitches between real edges
    static constexpr int64_t SQW_TOLERANCE_US = 2000;        // Edge spacing must be N s ± this
    static constexpr int64_t EDGE_DEADLINE_US = 100000;      // Resolve an edge within 100 ms
    static constexpr int64_t POLL_WINDOW_LEAD_US = 3000;     // Start polling 3 ms before a boundary
    static constexpr int64_t POLL_FINE_INTERVAL_US = 200;    // Poll spacing inside the window (slack)
    static constexpr int64_t POLL_COARSE_INTERVAL_US = 1000; // Poll spacing while searching (slack)
    static constexpr int64_t POLL_FORCED_INTERVAL_US = 2000; // Forced poll spacing inside a tight window
    static constexpr int64_t POLL_SEARCH_FORCED_US = 5000;   // Forced poll spacing while searching
    static constexpr int64_t POLL_MAX_GAP_US = 5500;         // Larger bracketing gap -> boundary rejected
    static constexpr int64_t POLL_DEADLINE_US = 20000;
    static constexpr int64_t PENDING_READ_DEADLINE_US = 100000; // Force the deferred full read       // Tight window expires this long after the boundary
    static constexpr int64_t SYNC_SECONDS_ACK_US = 80;       // Seconds byte ACK after the write starts
    static constexpr uint32_t PLAUSIBLE_DRIFT_S = 2;         // RTC read vs. expected time
    static constexpr uint8_t MAX_IMPLAUSIBLE_READS = 5;      // Then trust the RTC again
    static constexpr uint32_t NO_RTC_EPOCH = 946684800UL;    // 2000-01-01 00:00:00

    bool readSeconds(uint8_t& seconds);
    bool readTimeChecked(uint32_t& epoch, int64_t atUs);
    void readEdge(int64_t& edgeUs, uint32_t& edgeCount) const;
    void startSearch(int64_t nowUs);
    void openWindow(int64_t boundaryUs, bool tight);
    bool expectedEpoch(int64_t atUs, uint32_t& epoch) const;

    RTC_DS3231& _rtc;
    uint8_t _sqwPin;
    bool _rtcOk;

    // Shared with the ISR
    volatile int64_t _edgeUs;
    volatile uint32_t _edgeCount;
    mutable portMUX_TYPE _mux;

    uint32_t _processedEdges;
    int64_t _lastEdgeUs;      // Last edge seen (validated or not), reference for the next one
    bool _haveEdgeRef;
    bool _sqwActive;
    int64_t _startUs;

    // Polling fallback state
    bool _havePolledSecond;
    uint8_t _lastPolledSecond;
    int64_t _lastPollUs;      // Last successful poll (brackets the boundary)
    int64_t _lastAttemptUs;   // Last poll attempt (spacing of forced polls)
    int64_t _lastChangeUs;    // Last time the seconds value changed
    int64_t _nextPollUs;
    int64_t _windowStartUs;   // 0 = searching (no expected boundary yet)
    bool _windowTight;        // Window derived from an accurate boundary
    int64_t _windowEndUs;
    // Polling: boundary found, full time read deferred to the next idle slot
    // (a seconds read plus a full read back to back would not fit between
    // two conversions).
    bool _boundaryPending;
    int64_t _pendingBoundaryUs;

    // Current anchor
    int64_t _anchorUs;
    uint32_t _anchorEpoch;
    Source _anchorSrc;
    bool _haveCoarseAnchor;
    uint8_t _implausibleReads;

    // Cached "YYYY-MM-DD HH:MM:SS" for the last formatted second
    uint32_t _cachedEpoch;
    char _cachedDate[32];  // Sized for the worst case snprintf can see (uint8_t fields)

    static PrecisionTime* _instance;
    static void IRAM_ATTR onSqwFallingEdge();
};

#endif // PRECISION_TIME_H
