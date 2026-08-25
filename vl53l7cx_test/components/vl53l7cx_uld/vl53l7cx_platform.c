/**
 * ESP-IDF implementation of the VL53L7CX ULD platform layer, using the
 * new esp_driver_i2c "i2c_master" API. One VL53L7CX_Platform maps to one
 * i2c_master_dev_handle_t created by the application (i2c_master_bus_add_device),
 * so this file has no knowledge of which I2C bus/pins are in use.
 */

#include "platform.h"
#include <stdlib.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#define VL53L7CX_I2C_XFER_TIMEOUT_MS   2000

uint8_t VL53L7CX_RdByte(
    VL53L7CX_Platform *p_platform,
    uint16_t RegisterAdress,
    uint8_t *p_value)
{
    return VL53L7CX_RdMulti(p_platform, RegisterAdress, p_value, 1);
}

uint8_t VL53L7CX_WrByte(
    VL53L7CX_Platform *p_platform,
    uint16_t RegisterAdress,
    uint8_t value)
{
    return VL53L7CX_WrMulti(p_platform, RegisterAdress, &value, 1);
}

uint8_t VL53L7CX_WrMulti(
    VL53L7CX_Platform *p_platform,
    uint16_t RegisterAdress,
    uint8_t *p_values,
    uint32_t size)
{
    /* VL53L7CX registers are addressed as one flat 16-bit address space;
     * a write is [addr_hi, addr_lo, payload...] in a single transaction.
     * Firmware upload during vl53l7cx_init() writes up to 0x8000 bytes in
     * one call, so this buffer is sized dynamically. */
    uint8_t *buf = malloc((size_t)size + 2);
    if (buf == NULL) {
        return 1;
    }

    buf[0] = (uint8_t)(RegisterAdress >> 8);
    buf[1] = (uint8_t)(RegisterAdress & 0xFF);
    memcpy(&buf[2], p_values, size);

    esp_err_t err = i2c_master_transmit(p_platform->i2c_dev, buf, (size_t)size + 2,
                                         VL53L7CX_I2C_XFER_TIMEOUT_MS);
    free(buf);
    return (err == ESP_OK) ? 0 : 1;
}

uint8_t VL53L7CX_RdMulti(
    VL53L7CX_Platform *p_platform,
    uint16_t RegisterAdress,
    uint8_t *p_values,
    uint32_t size)
{
    uint8_t reg_addr[2] = { (uint8_t)(RegisterAdress >> 8), (uint8_t)(RegisterAdress & 0xFF) };
    esp_err_t err = i2c_master_transmit_receive(p_platform->i2c_dev, reg_addr, sizeof(reg_addr),
                                                 p_values, size, VL53L7CX_I2C_XFER_TIMEOUT_MS);
    return (err == ESP_OK) ? 0 : 1;
}

uint8_t VL53L7CX_Reset_Sensor(VL53L7CX_Platform *p_platform)
{
    (void)p_platform;
    /* LPn is tied straight to VCC on this board (no host GPIO control),
     * so there is no reset line for us to toggle here. */
    return 0;
}

void VL53L7CX_SwapBuffer(uint8_t *buffer, uint16_t size)
{
    uint32_t i, tmp;

    for (i = 0; i < size; i = i + 4) {
        tmp = ((uint32_t)buffer[i] << 24)
              | ((uint32_t)buffer[i + 1] << 16)
              | ((uint32_t)buffer[i + 2] << 8)
              | (uint32_t)buffer[i + 3];
        memcpy(&(buffer[i]), &tmp, 4);
    }
}

uint8_t VL53L7CX_WaitMs(VL53L7CX_Platform *p_platform, uint32_t TimeMs)
{
    (void)p_platform;
    vTaskDelay(pdMS_TO_TICKS(TimeMs));
    return 0;
}
