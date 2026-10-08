/**
 * @file INA226Lite.cpp
 * @brief Implementation of the minimal INA226 driver
 * @version 2.0.0
 *
 * @author Daniel Rossi
 * @license Apache-2.0
 */

#include "INA226Lite.h"

namespace {
    constexpr uint16_t AVERAGES[] = {1, 4, 16, 64, 128, 256, 512, 1024};
    constexpr uint16_t CONVERSION_US[] = {140, 204, 332, 588, 1100, 2116, 4156, 8244};

    constexpr uint16_t CONFIG_RESET = 0x8000;
    constexpr uint16_t CONFIG_RESERVED = 0x4000;  // Bits 14..12 read back as 100b
    constexpr uint16_t MODE_SHUNT_BUS_CONTINUOUS = 0x0007;
    constexpr uint16_t MASK_CVRF = 0x0008;
}

INA226Lite::INA226Lite(uint8_t address, TwoWire& wire)
    : _wire(wire)
    , _address(address)
    , _pointer(-1)
    , _avg(4)
    , _busConvUs(140)
    , _shuntConvUs(140)
    , _config(0)
{
}

int8_t INA226Lite::avgCode(uint16_t avg) {
    for (int8_t i = 0; i < 8; i++) {
        if (AVERAGES[i] == avg) return i;
    }
    return -1;
}

int8_t INA226Lite::convCode(uint16_t us) {
    for (int8_t i = 0; i < 8; i++) {
        if (CONVERSION_US[i] == us) return i;
    }
    return -1;
}

bool INA226Lite::begin() {
    _pointer = -1;
    uint16_t id = 0;
    if (!readRegister(REG_MANUFACTURER_ID, id) || id != MANUFACTURER_ID_TI) {
        return false;
    }
    if (!writeRegister(REG_CONFIG, CONFIG_RESET)) {
        return false;
    }
    delay(1);
    return true;
}

bool INA226Lite::configure(uint16_t avg, uint16_t busConvUs, uint16_t shuntConvUs) {
    const int8_t a = avgCode(avg);
    const int8_t vb = convCode(busConvUs);
    const int8_t vs = convCode(shuntConvUs);
    if (a < 0 || vb < 0 || vs < 0) {
        return false;
    }

    const uint16_t config = CONFIG_RESERVED
        | (static_cast<uint16_t>(a) << 9)
        | (static_cast<uint16_t>(vb) << 6)
        | (static_cast<uint16_t>(vs) << 3)
        | MODE_SHUNT_BUS_CONTINUOUS;

    if (!writeRegister(REG_CONFIG, config)) {
        return false;
    }
    _avg = avg;
    _busConvUs = busConvUs;
    _shuntConvUs = shuntConvUs;
    _config = config;
    return true;
}

bool INA226Lite::readConfig(uint16_t& config) {
    return readRegister(REG_CONFIG, config);
}

bool INA226Lite::pollConversionReady(bool& ready) {
    uint16_t mask = 0;
    if (!readRegister(REG_MASK_ENABLE, mask)) {
        return false;
    }
    ready = (mask & MASK_CVRF) != 0;
    return true;
}

bool INA226Lite::readRaw(uint16_t& busRaw, int16_t& shuntRaw) {
    uint16_t shunt = 0;
    uint16_t bus = 0;
    if (!readRegister(REG_SHUNT, shunt) || !readRegister(REG_BUS, bus)) {
        return false;
    }
    shuntRaw = static_cast<int16_t>(shunt);
    busRaw = bus;
    return true;
}

uint32_t INA226Lite::periodUs() const {
    return (static_cast<uint32_t>(_busConvUs) + _shuntConvUs) * _avg;
}

bool INA226Lite::writeRegister(uint8_t reg, uint16_t value) {
    _wire.beginTransmission(_address);
    _wire.write(reg);
    _wire.write(static_cast<uint8_t>(value >> 8));
    _wire.write(static_cast<uint8_t>(value & 0xFF));
    const bool ok = _wire.endTransmission() == 0;
    // A register write also moves the pointer to that register
    _pointer = ok ? reg : -1;
    return ok;
}

bool INA226Lite::readRegister(uint8_t reg, uint16_t& value) {
    // Pointer write + read in ONE transaction (repeated start). On the
    // ESP32 Arduino core every I2C transaction carries a large fixed
    // overhead, so this halves the bus time per register compared with a
    // separate pointer write (the sampling loop reads 3 registers per sample).
    _wire.beginTransmission(_address);
    _wire.write(reg);
    _wire.endTransmission(false);
    if (_wire.requestFrom(_address, static_cast<uint8_t>(2)) != 2) {
        _pointer = -1;
        return false;
    }
    _pointer = reg;
    const uint8_t hi = _wire.read();
    const uint8_t lo = _wire.read();
    value = (static_cast<uint16_t>(hi) << 8) | lo;
    return true;
}
