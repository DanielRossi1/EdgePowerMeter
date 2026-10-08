/**
 * @file INA226Lite.h
 * @brief Minimal, fast INA226 driver (raw registers only)
 * @version 2.0.0
 *
 * Talks to the INA226 directly over Wire, without the calibration/current/
 * power registers: the host converts the raw shunt and bus registers into
 * physical units, so no precision is lost to the INA226 power register LSB.
 *
 * The ALERT pin is not wired on EdgePowerMeter, so new conversions are
 * detected by polling the Conversion Ready Flag (CVRF) in Mask/Enable.
 * The register pointer is cached: repeated polls of Mask/Enable only cost
 * a 2-byte read, without re-sending the pointer.
 *
 * @author Daniel Rossi
 * @license Apache-2.0
 */

#ifndef INA226_LITE_H
#define INA226_LITE_H

#include <Arduino.h>
#include <Wire.h>

class INA226Lite {
public:
    // Register map
    static constexpr uint8_t REG_CONFIG = 0x00;
    static constexpr uint8_t REG_SHUNT = 0x01;
    static constexpr uint8_t REG_BUS = 0x02;
    static constexpr uint8_t REG_MASK_ENABLE = 0x06;
    static constexpr uint8_t REG_MANUFACTURER_ID = 0xFE;

    static constexpr uint16_t MANUFACTURER_ID_TI = 0x5449;

    // Physical LSBs of the raw registers
    static constexpr float BUS_LSB_V = 1.25e-3f;
    static constexpr float SHUNT_LSB_V = 2.5e-6f;

    explicit INA226Lite(uint8_t address, TwoWire& wire = Wire);

    /**
     * @brief Check the device answers with the TI manufacturer ID and reset it.
     */
    bool begin();

    /**
     * @brief Configure continuous shunt+bus mode.
     *
     * Writing the configuration register also clears CVRF and restarts the
     * conversion pipeline.
     *
     * @param avg Number of averages (1, 4, 16, 64, 128, 256, 512, 1024)
     * @param busConvUs Bus conversion time in µs (140 ... 8244)
     * @param shuntConvUs Shunt conversion time in µs (140 ... 8244)
     * @return false on invalid values or I2C error
     */
    bool configure(uint16_t avg, uint16_t busConvUs, uint16_t shuntConvUs);

    /**
     * @brief Poll the Conversion Ready Flag (reading it clears it).
     * @param ready Set to true when a new conversion result is available
     * @return false on I2C error
     */
    bool pollConversionReady(bool& ready);

    /**
     * @brief Read the raw bus and shunt registers of the latest conversion.
     * @return false on I2C error
     */
    bool readRaw(uint16_t& busRaw, int16_t& shuntRaw);

    /** @brief Read back the configuration register. */
    bool readConfig(uint16_t& config);

    /** @brief Configuration register value written by the last configure(). */
    uint16_t configValue() const { return _config; }

    /** @brief Nominal conversion period in µs: (vct + ict) * avg. */
    uint32_t periodUs() const;

    uint16_t averages() const { return _avg; }
    uint16_t busConversionUs() const { return _busConvUs; }
    uint16_t shuntConversionUs() const { return _shuntConvUs; }

    static bool isValidAverage(uint16_t avg) { return avgCode(avg) >= 0; }
    static bool isValidConversionTime(uint16_t us) { return convCode(us) >= 0; }

private:
    static int8_t avgCode(uint16_t avg);
    static int8_t convCode(uint16_t us);

    bool writeRegister(uint8_t reg, uint16_t value);
    bool readRegister(uint8_t reg, uint16_t& value);

    TwoWire& _wire;
    uint8_t _address;
    int16_t _pointer;  // Register pointer currently selected in the device (-1 = unknown)

    uint16_t _avg;
    uint16_t _busConvUs;
    uint16_t _shuntConvUs;
    uint16_t _config;
};

#endif // INA226_LITE_H
