# EdgePowerMeter ⚡️

![Prototype photo](assets/prototype/prototype.jpg)

A friendly, lightweight power meter to log voltage, current and power of embedded boards while they run inference workloads — ideal for measuring FPS-per-watt (FPS/W) of AI models at the edge.

---

## 🔎 Quick Overview

- **What it does:** Measures bus voltage, current and power and logs CSV lines with timestamps.
- **Primary use case:** Compare efficiency (FPS/W) of ML models on embedded hardware.
- **Example hardware:** `ESP32C3-SuperMini`, `INA226`, `SSD1306` OLED, `DS3231` RTC.

---

## ❗ Safety — Warning

**Warning:** This project shares multiple power and load connections on the same wiring. Incorrect wiring, accidental shorts, or handling while powered can cause electric shock, damage to hardware, fire, or other hazards.

**Recommended precautions**:
- This circuit is not designed to operate on AC voltages.
- Disconnect all power before changing wiring or connections.
- Use fuses or over-current protection on inputs/outputs where practical.
- Verify polarity and continuity with a multimeter before powering.
- Avoid making or breaking connections while the device is powered.
- Do not connect incompatible power sources or allow backfeeding between ports.
- Keep the device dry and away from conductive contaminants.
- If you are not experienced with electronics, seek assistance from a qualified technician.

**The author is not responsible for any damage, injury or loss resulting from improper or inexperienced use.**

---

## ✨ Key Features

- Live power readout on an OLED 128×32 (SSD1306)
- INA226 power monitor (I2C) with onboard `0.01 Ω` shunt (R2512)
- DS3231 RTC for accurate timestamps
- CSV serial output: `Timestamp, Voltage[V], Current[A], Power[W]` at `115200` baud

---

## 🧩 Hardware & Project Files

- **Shunt resistor:** `0.01 Ω` (BOM entry `R2512`)
- **INA226 I2C address:** `0x40`
- **SSD1306 I2C address:** `0x3C` (128×32)
- **RTC:** DS3231 (I2C)

Useful files in this repository:

- PCB BOM: `Manufacture/BOM.csv`
- PCB Gerber: `Manufacture/gerber.zip`
- PCB Pick-and-place: `Manufacture/PickAndPlace.csv`
- DIY Wiring schematics: `Schematics/EdgePowerMeter.jpg`
- DIY Wiring components: `Schematics/EdgePowerMeter_bom.csv`
- Prototype photo: `assets/prototype/prototype.jpg`
- Firmware: `EdgePowerMeter.ino`
- License: `LICENSE` (Apache-2.0)

---

## 🔌 Wiring (summary)

All I2C modules share SDA and SCL. Use a common 3.3V rail and common GND for MCU, INA226, OLED and RTC. INA226 monitors the voltage drop across the shunt via `VIN+` (shunt high) and `VIN-` (shunt low).

---

## 🛠 Firmware

- Main sketch: `EdgePowerMeter.ino`
- Libraries: `INA226.h`, `Wire.h`, `Adafruit_GFX.h`, `Adafruit_SSD1306.h`, `RTClib.h`

Important configuration in the sketch:

```cpp
#define SCREEN_WIDTH 128  // OLED display width, in pixels
#define SCREEN_HEIGHT 32  // OLED display height, in pixels

#define OLED_RESET -1        // Reset pin # (or -1 if sharing Arduino reset pin)
#define SCREEN_ADDRESS 0x3C  ///< See datasheet for Address; 0x3D for 128x64, 0x3C for 128x32

#define INA226_INTERVAL 10
#define DISPLAY_INTERVAL 10

Adafruit_SSD1306 display(SCREEN_WIDTH, SCREEN_HEIGHT, &Wire, OLED_RESET);
INA226 ina226(0x40);
RTC_DS3231 rtc;

float shunt = 0.010;               /* shunt (Shunt Resistance in Ohms) */
float current_LSB_mA = 0.100;      /* current_LSB_mA (Current Least Significant Bit in milli Amperes) */
float current_zero_offset_mA = 0;  /* current_zero_offset_mA (Current Zero Offset in milli Amperes) */
uint16_t bus_V_scaling_e4 = 10000; /* bus_V_scaling_e4 (Bus Voltage Scaling Factor) */

```

---

## 📈 Serial log format

The firmware prints CSV lines on the serial port in this format:

`Timestamp,Voltage[V],Current[A],Power[W]`

Example:

`2025-11-29 12:34:56,5.000,0.250,1.250`

To view logs on Linux (replace `/dev/ttyUSB0` with your serial port):

```bash
screen /dev/ttyUSB0 115200
```

---

## 🧮 Calculating FPS per Watt (FPS/W)

1. Measure FPS (or inferences per second) on your embedded board.
2. Compute average power from the CSV `Power[W]` column.

Formula:

`FPS_per_W = FPS / Power_W`

Example: `10 FPS` with `2.5 W` average → `4 FPS/W`.

```cpp
// Note that samples are already averaged by the INA219 module through
ina226.setAverage(INA226_16_SAMPLES);
```


---

## ⚙️ Build & Upload

Supported toolchains: Arduino IDE, Arduino CLI, PlatformIO.

Arduino CLI example (replace FQBN and port):

```bash
arduino-cli compile --fqbn esp32:esp32:esp32c3:esp32c3s2_mini EdgePowerMeter
arduino-cli upload -p /dev/ttyUSB0 --fqbn esp32:esp32:esp32c3:esp32c3s2_mini EdgePowerMeter
```

PlatformIO:

```bash
pio run --target upload
```

---

## ⚠️ Notes & Tips

- Make sure the RTC is initialized and set for accurate timestamps.
- If you see a current offset, adjust `current_LSB_mA` and `current_zero_offset_mA` in the sketch.

---

## 📁 Support the project

TODO list:
- script to capture the data and calculate statistics
- 3D printed enclosure
- More safety!
- Better PCB design

---

## 📜 License

This project is licensed under the Apache License 2.0 — see `LICENSE`.

---

Enjoy! 😄
