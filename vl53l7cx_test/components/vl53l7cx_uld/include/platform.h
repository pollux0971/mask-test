/**
  * ESP-IDF platform adapter for the STMicroelectronics VL53L7CX ULD driver.
  * Based on the platform.h template shipped with the official VL53L7CX ULD
  * package (STSW-IMG036, BSD-3-Clause) -- the VL53L7CX_Platform struct and
  * the function bodies are customer-supplied; this is our ESP-IDF I2C
  * implementation (see vl53l7cx_platform.c).
  */

#ifndef _PLATFORM_H_
#define _PLATFORM_H_
#pragma once

#include <stdint.h>
#include <string.h>
#include "driver/i2c_master.h"

/**
 * @brief Structure VL53L7CX_Platform needs to be filled by the customer,
 * depending on his platform. At least, it contains the VL53L7CX I2C address.
 * Some additional fields can be added, as descriptors, or platform
 * dependencies. Anything added into this structure is visible into the platform
 * layer.
 */

typedef struct {
    /* 8-bit I2C address (ST convention: 7-bit address << 1). Kept for API
     * compatibility with vl53l7cx_set_i2c_address(); actual bus addressing
     * is driven by i2c_dev below, which is bound to the 7-bit address at
     * i2c_master_bus_add_device() time. */
    uint16_t address;

    /* ESP-IDF I2C master device handle for this sensor, created by the
     * application before calling vl53l7cx_init(). */
    i2c_master_dev_handle_t i2c_dev;
} VL53L7CX_Platform;

/*
 * @brief The macro below is used to define the number of target per zone sent
 * through I2C. This value can be changed by user, in order to tune I2C
 * transaction, and also the total memory size (a lower number of target per
 * zone means a lower RAM). The value must be between 1 and 4.
 */

#define     VL53L7CX_NB_TARGET_PER_ZONE        1U

/*
 * @brief The macro below can be used to avoid data conversion into the driver.
 * By default there is a conversion between firmware and user data. Using this macro
 * allows to use the firmware format instead of user format. The firmware format allows
 * an increased precision.
 */

// #define     VL53L7CX_USE_RAW_FORMAT

/*
 * @brief All macro below are used to configure the sensor output. User can
 * define some macros if he wants to disable selected output, in order to reduce
 * I2C access.
 */

// #define VL53L7CX_DISABLE_AMBIENT_PER_SPAD
// #define VL53L7CX_DISABLE_NB_SPADS_ENABLED
// #define VL53L7CX_DISABLE_NB_TARGET_DETECTED
// #define VL53L7CX_DISABLE_SIGNAL_PER_SPAD
// #define VL53L7CX_DISABLE_RANGE_SIGMA_MM
// #define VL53L7CX_DISABLE_DISTANCE_MM
// #define VL53L7CX_DISABLE_REFLECTANCE_PERCENT
// #define VL53L7CX_DISABLE_TARGET_STATUS
// #define VL53L7CX_DISABLE_MOTION_INDICATOR

/**
 * @param (VL53L7CX_Platform*) p_platform : Pointer of VL53L7CX platform
 * structure.
 * @param (uint16_t) Address : I2C location of value to read.
 * @param (uint8_t) *p_values : Pointer of value to read.
 * @return (uint8_t) status : 0 if OK
 */

uint8_t VL53L7CX_RdByte(
    VL53L7CX_Platform *p_platform,
    uint16_t RegisterAdress,
    uint8_t *p_value);

/**
 * @brief Mandatory function used to write one single byte.
 */

uint8_t VL53L7CX_WrByte(
    VL53L7CX_Platform *p_platform,
    uint16_t RegisterAdress,
    uint8_t value);

/**
 * @brief Mandatory function used to read multiples bytes.
 */

uint8_t VL53L7CX_RdMulti(
    VL53L7CX_Platform *p_platform,
    uint16_t RegisterAdress,
    uint8_t *p_values,
    uint32_t size);

/**
 * @brief Mandatory function used to write multiples bytes.
 */

uint8_t VL53L7CX_WrMulti(
    VL53L7CX_Platform *p_platform,
    uint16_t RegisterAdress,
    uint8_t *p_values,
    uint32_t size);

/**
 * @brief Optional function, only used to perform a hardware reset of the
 * sensor. Not used by this port: LPn is wired straight to VCC on this board
 * (no host GPIO control), so there is nothing to toggle here.
 */

uint8_t VL53L7CX_Reset_Sensor(
    VL53L7CX_Platform *p_platform);

/**
 * @brief Mandatory function, used to swap a buffer. The buffer size is always a
 * multiple of 4 (4, 8, 12, 16, ...).
 */

void VL53L7CX_SwapBuffer(
    uint8_t         *buffer,
    uint16_t         size);

/**
 * @brief Mandatory function, used to wait during an amount of time.
 */

uint8_t VL53L7CX_WaitMs(
    VL53L7CX_Platform *p_platform,
    uint32_t TimeMs);

#endif  // _PLATFORM_H_
