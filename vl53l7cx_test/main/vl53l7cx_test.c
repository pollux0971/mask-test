#include <stdio.h>
#include <string.h>
#include <stdbool.h>
#include <inttypes.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "driver/i2c_master.h"
#include "esp_log.h"
#include "esp_timer.h"

#include "vl53l7cx_api.h"
#include "bone_mic.h"
#include "uart_cmd.h"
#include "uart_out.h"

static const char *TAG = "vl53l7cx";

#define VL53L7CX_I2C_ADDR_7BIT   0x29

/* Resolution mode: 4 or 8. Edited by monitor/bridge_server.py's /switch
 * endpoint and reflashed -- the ULD driver has no runtime-switchable grid
 * size that the panel can just ask for, so a resolution change means a
 * rebuild+reflash, by design. Do not hand-edit casually; if you do, keep
 * the value as a bare integer literal so the regex in bridge_server.py
 * keeps matching. */
#define TOF_RESOLUTION_MODE 4

#if TOF_RESOLUTION_MODE == 4
#define TOF_GRID_DIM            4
#define TOF_RESOLUTION           VL53L7CX_RESOLUTION_4X4
#define TOF_RANGING_FREQUENCY_HZ 30
#elif TOF_RESOLUTION_MODE == 8
#define TOF_GRID_DIM            8
#define TOF_RESOLUTION           VL53L7CX_RESOLUTION_8X8
#define TOF_RANGING_FREQUENCY_HZ 10
#else
#error "TOF_RESOLUTION_MODE must be 4 or 8"
#endif

typedef struct {
    const char *name;
    char letter;
    gpio_num_t scl;
    gpio_num_t sda;
    i2c_port_num_t port;
} sensor_pins_t;

/* Physical wiring has SDA/SCL crossed relative to the original 4,5 / 6,7
 * plan -- confirmed during the earlier bring-up test and compensated here
 * in software instead of re-soldering. */
static const sensor_pins_t pins[] = {
    { "Sensor A", 'A', GPIO_NUM_5, GPIO_NUM_4, I2C_NUM_0 },
    { "Sensor B", 'B', GPIO_NUM_7, GPIO_NUM_6, I2C_NUM_1 },
};

#define NUM_SENSORS (sizeof(pins) / sizeof(pins[0]))

static VL53L7CX_Configuration s_dev[NUM_SENSORS];
static VL53L7CX_ResultsData s_results[NUM_SENSORS];
/* Per-sensor frame counter for the $T line (CONTRACTS.md #1.1/#1.3). Only
 * incremented once a frame is actually sent, so a stall or read failure
 * shows up as a gap in seq rather than a repeated or skipped-silently value. */
static uint32_t s_seq[NUM_SENSORS];

static bool init_bus_and_sensor(size_t idx)
{
    const sensor_pins_t *cfg = &pins[idx];

    i2c_master_bus_config_t bus_cfg = {
        .i2c_port = cfg->port,
        .sda_io_num = cfg->sda,
        .scl_io_num = cfg->scl,
        .clk_source = I2C_CLK_SRC_DEFAULT,
        .glitch_ignore_cnt = 7,
        .flags.enable_internal_pullup = true,
    };
    i2c_master_bus_handle_t bus;
    esp_err_t err = i2c_new_master_bus(&bus_cfg, &bus);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "[%s] i2c_new_master_bus failed: %s", cfg->name, esp_err_to_name(err));
        return false;
    }

    i2c_device_config_t dev_cfg = {
        .dev_addr_length = I2C_ADDR_BIT_LEN_7,
        .device_address = VL53L7CX_I2C_ADDR_7BIT,
        .scl_speed_hz = 400000,
    };
    i2c_master_dev_handle_t dev;
    err = i2c_master_bus_add_device(bus, &dev_cfg, &dev);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "[%s] i2c_master_bus_add_device failed: %s", cfg->name, esp_err_to_name(err));
        return false;
    }

    memset(&s_dev[idx], 0, sizeof(s_dev[idx]));
    s_dev[idx].platform.address = VL53L7CX_DEFAULT_I2C_ADDRESS;
    s_dev[idx].platform.i2c_dev = dev;

    uint8_t alive = 0;
    uint8_t status = vl53l7cx_is_alive(&s_dev[idx], &alive);
    if (status != 0 || !alive) {
        ESP_LOGE(TAG, "[%s] is_alive failed (status=%u alive=%u)", cfg->name, status, alive);
        return false;
    }

    ESP_LOGI(TAG, "[%s] loading firmware into sensor...", cfg->name);
    status = vl53l7cx_init(&s_dev[idx]);
    if (status != 0) {
        ESP_LOGE(TAG, "[%s] vl53l7cx_init failed, status=%u", cfg->name, status);
        return false;
    }

    status = vl53l7cx_set_resolution(&s_dev[idx], TOF_RESOLUTION);
    status |= vl53l7cx_set_ranging_frequency_hz(&s_dev[idx], TOF_RANGING_FREQUENCY_HZ);
    if (status != 0) {
        ESP_LOGE(TAG, "[%s] configuration failed, status=%u", cfg->name, status);
        return false;
    }

    status = vl53l7cx_start_ranging(&s_dev[idx]);
    if (status != 0) {
        ESP_LOGE(TAG, "[%s] start_ranging failed, status=%u", cfg->name, status);
        return false;
    }

    ESP_LOGI(TAG, "[%s] ranging started (%dx%d @ %dHz)", cfg->name,
             TOF_GRID_DIM, TOF_GRID_DIM, TOF_RANGING_FREQUENCY_HZ);
    return true;
}

/* Wire format frozen in CONTRACTS.md #1.1:
 *   $T,<A|B>,<seq:u32>,<t_us:i64>,<dim>,<d0>..<dN>,<s0>..<sN>
 * dim is the zone COUNT (16 or 64), not the grid edge length. d is distance
 * in mm, s is signal_per_spad/100 (rounded, not truncated -- CONTRACTS.md
 * #1.3 "signal rate 縮放"). Per #1.1 "無效值語意", a zone with
 * target_status outside {5, 9} reports -1 for BOTH d and s together, never
 * one without the other. Printed with plain printf (no ESP_LOG prefix) so
 * the host bridge can pick out lines by their '$' sentinel without fighting
 * log formatting. */
static void print_tof_line(char sensor_letter, uint32_t seq, int64_t t_us,
                            const VL53L7CX_ResultsData *res)
{
    const int dim = TOF_GRID_DIM * TOF_GRID_DIM;

    uart_out_lock();
    printf("$T,%c,%" PRIu32 ",%" PRId64 ",%d", sensor_letter, seq, t_us, dim);
    for (int i = 0; i < dim; i++) {
        bool valid = (res->target_status[i] == 5 || res->target_status[i] == 9);
        printf(",%d", valid ? res->distance_mm[i] : -1);
    }
    for (int i = 0; i < dim; i++) {
        bool valid = (res->target_status[i] == 5 || res->target_status[i] == 9);
        printf(",%d", valid ? (int)((res->signal_per_spad[i] + 50) / 100) : -1);
    }
    printf("\n");
    uart_out_unlock();
}

void app_main(void)
{
    /* Must be ready before any task (including this one) prints a $-line:
     * the ToF loop below and the mic task both write to the same UART
     * from different tasks, and a long recording dump needs to not get
     * spliced mid-line by a $TOF line landing in the middle of it. */
    uart_out_init();

    /* Give the sensors time to boot after power-on before talking to them. */
    vTaskDelay(pdMS_TO_TICKS(100));

    bool ok[NUM_SENSORS];
    for (size_t i = 0; i < NUM_SENSORS; i++) {
        ok[i] = init_bus_and_sensor(i);
    }

    bone_mic_init();
    bone_mic_start_monitor();
    uart_cmd_start();

    uart_out_lock();
    printf("$STATUS,res=%d\n", TOF_GRID_DIM);
    uart_out_unlock();

    while (1) {
        for (size_t i = 0; i < NUM_SENSORS; i++) {
            if (!ok[i]) {
                continue;
            }
            uint8_t ready = 0;
            uint8_t status = vl53l7cx_check_data_ready(&s_dev[i], &ready);
            if (status == 0 && ready) {
                /* Sampled here, not after get_ranging_data(): the I2C
                 * transfer is ~2-4 ms and taking t_us after it would bake
                 * that in as a systematic bias (CONTRACTS.md #1.3). */
                int64_t t_us = esp_timer_get_time();
                status = vl53l7cx_get_ranging_data(&s_dev[i], &s_results[i]);
                if (status == 0) {
                    print_tof_line(pins[i].letter, s_seq[i], t_us, &s_results[i]);
                    s_seq[i]++;
                }
            }
        }
        /* CONFIG_FREERTOS_HZ=100 -> 1 tick = 10 ms. pdMS_TO_TICKS() truncates
         * (integer division), so anything under 10 ms yields 0 ticks and
         * vTaskDelay(0) does NOT block -- it busy-spins and starves IDLE,
         * tripping the task watchdog. 10 ms is the shortest real delay here:
         * 100 Hz polling, 3.3x oversampling of the 30 Hz sensor. */
        vTaskDelay(pdMS_TO_TICKS(10));
    }
}
