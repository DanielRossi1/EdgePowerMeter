# EdgePowerMeter Serial Protocol

Firmware 2.x speaks two output formats over the USB-CDC serial port
(2000000 baud nominal; the baud rate is irrelevant for native USB-CDC):

| Mode  | Purpose                                                  | Default at boot |
|-------|----------------------------------------------------------|-----------------|
| `CSV` | Legacy, human-readable lines (compatible with app ≤ 1.7) | yes             |
| `RAW` | Compact machine format with device time and sequence     | no              |

The desktop app ≥ 1.8 performs a handshake and switches the device to `RAW`.
Old firmware (1.x) ignores the handshake; the app then falls back to parsing
the legacy CSV format.

All lines are ASCII and terminated by `\n` (a preceding `\r` is tolerated).

## Device → host

### Comment / log lines

Any line starting with `#` is informational and must be ignored by parsers.
(Firmware 1.x used `[INFO] ...`, `[ERROR] ...` and `=== ... ===` lines, which
parsers must ignore as well.)

### Legacy CSV data (mode `CSV`)

```
Timestamp,Voltage[V],Current[A],Power[W]          <- header, printed once
2026-10-08 12:34:56.123,12.3450,0.1234,1.5234
```

Timestamp is the RTC wall-clock time (local time, millisecond resolution).

### Raw data (mode `RAW`)

```
D,<seq>,<t_us>,<bus_raw>,<shunt_raw>
```

| Field       | Type   | Meaning                                                               |
|-------------|--------|-----------------------------------------------------------------------|
| `seq`       | uint32 | Conversion counter, +1 for every conversion read from the INA226. Wraps at 2^32. A gap means samples were dropped on the device (e.g. TX buffer full). |
| `t_us`      | int64  | Device monotonic time in µs since boot (`esp_timer_get_time()`) at which the conversion-ready flag was observed. Never wraps in practice. |
| `bus_raw`   | uint16 | INA226 bus voltage register. LSB = 1.25 mV.                           |
| `shunt_raw` | int16  | INA226 shunt voltage register (signed). LSB = 2.5 µV.                 |

The host computes physical values (allowing user calibration):

```
V = bus_raw * 1.25e-3 * voltage_gain + voltage_offset
I = shunt_raw * 2.5e-6 / shunt_ohm * current_gain + current_offset
P = V * I
```

### Time anchor (mode `RAW`)

```
T,<t_us>,<rtc_epoch_s>,<src>
```

Emitted once per second (anchors that fail validation are skipped, so a
second can occasionally have none). `t_us` is the device time of the second boundary,
`rtc_epoch_s` the RTC time at that boundary, as seconds since 1970-01-01
**interpreting the RTC calendar as naive local time** (the RTC holds local
wall-clock time). `src` is `S` when the boundary was captured by the DS3231
SQW interrupt (µs accurate) or `P` when detected by polling (≈ ms accurate).

The host maps any sample to wall-clock time with the most recent anchor:
`wall = rtc_epoch_s + (t_us_sample - t_us_anchor) / 1e6`.

### Command replies

Every command gets exactly one reply line starting with `!`. `<COMMAND>` is
the first word of the command, upper-cased (`!OK SET`, `!OK MODE`,
`!ERR FOO unknown_command`):

```
!OK <COMMAND>
!ERR <COMMAND> <reason>
!HELLO,EdgePowerMeter,<firmware_version>,<protocol_version>
!CFG,avg=<n>,vct=<us>,ict=<us>,shunt=<ohm>,oled=<0|1>,stream=<0|1>,mode=<CSV|RAW>,sqw=<0|1>,period_us=<us>
```

`period_us` is the nominal conversion period: `(vct + ict) * avg`.

Error reasons: `missing_argument`, `invalid_value`, `unknown_parameter`,
`unknown_command`, `i2c_error`, `rtc_unavailable`, `nvs_error`,
`line_too_long` (lines over 95 characters, reported as `!ERR ? line_too_long`).

After `!OK MODE` the device re-sends the latest `T` anchor (RAW) or the CSV
header (CSV). In RAW mode a `T` anchor is also sent right after `!OK SYNC`
(the new time line) and after `!OK SET` for `SET STREAM 1`.

## Host → device commands

Case-insensitive, one per line.

| Command                         | Effect                                                     |
|---------------------------------|------------------------------------------------------------|
| `HELLO`                         | Identify. Replies `!HELLO,...`.                            |
| `GET`                           | Replies `!CFG,...` with the current configuration.         |
| `MODE RAW` / `MODE CSV`         | Select output format.                                      |
| `SET AVG <n>`                   | INA226 averaging: 1, 4, 16, 64, 128, 256, 512, 1024.        |
| `SET VCT <us>` / `SET ICT <us>` | Bus / shunt conversion time: 140, 204, 332, 588, 1100, 2116, 4156, 8244. |
| `SET SHUNT <ohm>`               | Shunt resistance used for CSV mode and the OLED (RAW is unaffected). Accepted range 0.00001–100 Ω. |
| `SET OLED <0\|1>`                | Enable/disable the OLED display.                           |
| `SET STREAM <0\|1>`              | Pause/resume data output.                                  |
| `SYNC YYYY-MM-DD HH:MM:SS`      | Set the RTC (local time). Hosts should send it right at a second boundary. The device reads the time back and replies `!ERR SYNC i2c_error` if the write did not take. |
| `SAVE`                          | Persist AVG/VCT/ICT/SHUNT/OLED to flash (NVS); restored at boot. |

Changing `AVG`, `VCT` or `ICT` restarts the conversion pipeline; `seq` keeps
counting. While `STREAM 0` is active sampling continues (OLED) and `seq` keeps
counting, so hosts must not count the gap after `STREAM 1` as lost samples.

## Data loss accounting

* A `seq` gap means the device read conversions but dropped the lines because
  the USB TX buffer was full (host not reading fast enough).
* With very short conversion periods (below ~0.5 ms, e.g. `AVG 1` at 140 µs)
  the firmware cannot read every conversion; skipped conversions never get a
  `seq` and show up only as `t_us` gaps larger than `period_us`.

## Boot sequence

The device waits up to 1.5 s for the USB host, prints `#` info lines ending
with `# EdgePowerMeter v2.x.y ready (protocol 2)`, then the CSV header and CSV
data. A `HELLO` sent during boot is answered once the main loop starts. If the
INA226 is missing the device keeps running, answers commands, prints
`# ERROR INA226 not found` and retries every 5 s.
