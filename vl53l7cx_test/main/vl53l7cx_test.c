#include <stdio.h>
#include <string.h>
#include <stdbool.h>
#include <inttypes.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "driver/i2c_master.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "esp_system.h"

#include "vl53l7cx_api.h"
#include "bone_mic.h"
#include "uart_cmd.h"
#include "uart_out.h"
#include "vl53l7cx_test.h"

#ifndef FW_GIT_SHA
#define FW_GIT_SHA "unknown"
#endif
#define TOF_PROTO_VERSION 2

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

/* A05: drop counters, cumulative since the last $STATUS (reset in
 * tof_print_status(), per CONTRACTS.md #1.3 "drop_* 是自上次 $STATUS 起算
 * 的累積值"). Two independent causes, kept separate because they point at
 * different hardware problems (see tof_get_drop_notready/tof_get_drop_error
 * in the header):
 *   notready -- the sensor produced a frame we never picked up. There is no
 *     application-visible per-frame counter to detect this directly: the
 *     ULD's only "streamcount" field lives on VL53L7CX_Configuration (an
 *     internal, "do not touch" driver handle documented for its own I2C
 *     bookkeeping), not on VL53L7CX_ResultsData, so it is not a usable
 *     frame sequence number here. Falls back to A05.md's time-based method:
 *     a gap between consecutive check_data_ready()-true timestamps that is
 *     more than 1.5x the sensor's ranging period implies missed cycles in
 *     between.
 *   error -- get_ranging_data() returned non-zero (I2C read failure). */
static uint32_t s_drop_notready[NUM_SENSORS];
static uint32_t s_drop_error[NUM_SENSORS];
static int64_t  s_last_ready_t_us[NUM_SENSORS];
static bool     s_have_last_ready_t_us[NUM_SENSORS];

#define TOF_EXPECTED_PERIOD_US (1000000 / TOF_RANGING_FREQUENCY_HZ)

/* A15: last uart_out_bytes_since_boot() reading, so tof_print_heartbeat()
 * can report a delta (bytes sent since the previous $H) instead of a raw
 * cumulative total -- the host can turn that into a rate using the t_us
 * of two consecutive $H lines, which is robust to $H itself firing off a
 * strict 1 Hz cadence (e.g. a PING landing between ticks). */
static uint32_t s_bw_bytes_last_h;

/* A16: $A (ambient) stream state. Default disabled (CONTRACTS.md #1.1.3).
 * A single bool flag applied immediately -- no locking, same reasoning as
 * bone_mic.c's s_mel_enabled: a torn read just delays one frame's decision
 * by a beat, never a crash. `s_amb_seq`/`s_last_amb_us` are per-sensor,
 * mirroring $T's independent-stream-per-sensor pattern (CONTRACTS.md
 * #1.1.3 "seq 不與 $T 共用"). */
static volatile bool s_amb_enabled = false;
static uint32_t s_amb_seq[NUM_SENSORS];
static int64_t s_last_amb_us[NUM_SENSORS];

void tof_set_ambient_enabled(bool on)
{
    s_amb_enabled = on;
}

bool tof_ambient_enabled(void)
{
    return s_amb_enabled;
}

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
    int len = 0;

    uart_out_lock();
    len += printf("$T,%c,%" PRIu32 ",%" PRId64 ",%d", sensor_letter, seq, t_us, dim);
    for (int i = 0; i < dim; i++) {
        bool valid = (res->target_status[i] == 5 || res->target_status[i] == 9);
        len += printf(",%d", valid ? res->distance_mm[i] : -1);
    }
    for (int i = 0; i < dim; i++) {
        bool valid = (res->target_status[i] == 5 || res->target_status[i] == 9);
        len += printf(",%d", valid ? (int)((res->signal_per_spad[i] + 50) / 100) : -1);
    }
    len += printf("\n");
    uart_out_add_bytes((size_t)len);   /* A15: bandwidth accounting, see uart_out.h */
    uart_out_unlock();
}

/* Wire format frozen in CONTRACTS.md #1.1.3:
 *   $A,<A|B>,<seq:u32>,<t_us:i64>,<dim>,<a0>..<aN>
 * Same valid/invalid + rounding convention as $T's signal field: -1 for a
 * zone whose target_status isn't {5,9}, otherwise ambient_per_spad/100
 * rounded (not truncated). Called from the same ranging-data read that
 * feeds $T -- no extra I2C transaction, this is a different view of data
 * already in `res`. */
static void print_ambient_line(char sensor_letter, uint32_t seq, int64_t t_us,
                                const VL53L7CX_ResultsData *res)
{
    const int dim = TOF_GRID_DIM * TOF_GRID_DIM;
    int len = 0;

    uart_out_lock();
    len += printf("$A,%c,%" PRIu32 ",%" PRId64 ",%d", sensor_letter, seq, t_us, dim);
    for (int i = 0; i < dim; i++) {
        bool valid = (res->target_status[i] == 5 || res->target_status[i] == 9);
        len += printf(",%d", valid ? (int)((res->ambient_per_spad[i] + 50) / 100) : -1);
    }
    len += printf("\n");
    uart_out_add_bytes((size_t)len);   /* A15: bandwidth accounting, see uart_out.h */
    uart_out_unlock();
}

void tof_print_status(void)
{
    /* A05/CONTRACTS.md #1.1+#1.3 (revised): drop_* counters are cumulative
     * since BOOT, not since the last $STATUS -- $STATUS is re-sent on every
     * PING, and resetting drop_* there would zero the health counters on
     * every heartbeat request, exactly when B05/B03 need them most. They
     * share seq's session boundary instead (reset only on restart). */
    /* CONTRACTS.md #1.1.2: self-describing audio frame params, sourced from
     * bone_mic.c (not duplicated here) so an A14-style hop change can't
     * silently desync $STATUS from what mic_task is actually doing. */
    uint32_t sr;
    uint16_t mel_win, mel_hop, mic_hop;
    bone_mic_frame_params(&sr, &mel_win, &mel_hop, &mic_hop);

    uart_out_lock();
    int len = printf("$STATUS,res=%d,proto=%d,fw=%s,sr=%" PRIu32 ",mel=%d,mel_win=%u,mel_hop=%u,mic_hop=%u\n",
                      TOF_GRID_DIM, TOF_PROTO_VERSION, FW_GIT_SHA,
                      sr, bone_mic_mel_enabled() ? 1 : 0, mel_win, mel_hop, mic_hop);
    uart_out_add_bytes((size_t)len);   /* A15: bandwidth accounting, see uart_out.h */
    uart_out_unlock();
}

uint32_t tof_get_drop_notready(size_t idx)
{
    return (idx < NUM_SENSORS) ? s_drop_notready[idx] : 0;
}

uint32_t tof_get_drop_error(size_t idx)
{
    return (idx < NUM_SENSORS) ? s_drop_error[idx] : 0;
}

void tof_print_heartbeat(void)
{
    uart_out_lock();
    /* CONTRACTS.md #1.3 "PING 回應延遲": t_us must be sampled after the
     * lock is held, not on entry. Up to ~2 ms of queuing delay while
     * waiting for the lock would otherwise become a systematic, always-
     * early bias baked into the timestamp itself -- "take the minimum" on
     * the host side only filters jitter in round-trip time, it can't
     * remove a bias that's inside t_us before the trip even starts. */
    int64_t t_us = esp_timer_get_time();

    /* A15/CONTRACTS.md #1.1 changelog: bandwidth accounting. Only bytes
     * from callers that opted into uart_out_add_bytes() are reflected here
     * -- as of this story that's $T/$STATUS/$H, NOT $M/$F or the recording
     * dump (see uart_out.h). This undercounts real bandwidth; it's a lower
     * bound, not a true total, until bone_mic.c/uart_cmd.c opt in too. */
    uint32_t bw_bytes_now = uart_out_bytes_since_boot();
    uint32_t bw_bytes_since_last = bw_bytes_now - s_bw_bytes_last_h;
    s_bw_bytes_last_h = bw_bytes_now;

    int len = printf("$H,%" PRId64 ",%" PRIu32 ",%" PRIu32 ",%" PRIu32 ",%" PRIu32 ",%d,%" PRIu32 "\n",
                      t_us,
                      tof_get_drop_notready(0) + tof_get_drop_error(0),
                      tof_get_drop_notready(1) + tof_get_drop_error(1),
                      bone_mic_drop_count(),
                      (uint32_t)esp_get_free_heap_size(),
                      (int)s_results[0].silicon_temp_degc,
                      bw_bytes_since_last);
    uart_out_add_bytes((size_t)len);   /* A15: bandwidth accounting, see uart_out.h */
    uart_out_unlock();
}

void app_main(void)
{
    /* Must be ready before any task (including this one) prints a $-line:
     * the ToF loop below and the mic task both write to the same UART
     * from different tasks, and a long recording dump needs to not get
     * spliced mid-line by a $TOF line landing in the middle of it. */
    uart_out_init();

    /* Send $STATUS before sensor init, not after: sensor bring-up (I2C
     * firmware upload into each VL53L7CX) can itself take a few hundred ms
     * per sensor, and the host needs proto/fw for version negotiation
     * (CONTRACTS.md #1.1) well within the "first second" acceptance bound
     * regardless of whether sensor init succeeds. */
    tof_print_status();

    /* Give the sensors time to boot after power-on before talking to them. */
    vTaskDelay(pdMS_TO_TICKS(100));

    bool ok[NUM_SENSORS];
    /* A08: ok[] means "initialised"; ranging[] means "VCSEL currently on".
     * They only diverge once the host sends SENS. drop_next[] swallows the
     * first frame after a restart -- the sensor needs 1-2 ranging periods
     * to settle and that frame is not trustworthy. */
    bool ranging[NUM_SENSORS];
    bool drop_next[NUM_SENSORS] = { false };
    for (size_t i = 0; i < NUM_SENSORS; i++) {
        ok[i] = init_bus_and_sensor(i);
        ranging[i] = ok[i];
    }

    bone_mic_init();
    bone_mic_start_monitor();
    uart_cmd_start();

    /* A06: periodic 1 Hz $H heartbeat. The PING-triggered heartbeat is a
     * separate path handled in uart_cmd.c (A09) -- both call the same
     * tof_print_heartbeat(), this just adds the unsolicited once-a-second
     * one CONTRACTS.md #1.1 implies for a live "still alive" signal. */
    int64_t last_heartbeat_us = esp_timer_get_time();
    unsigned heartbeat_count = 0;

    while (1) {
        int64_t now_us = esp_timer_get_time();
        if (now_us - last_heartbeat_us >= 1000000) {
            tof_print_heartbeat();
            last_heartbeat_us = now_us;
            heartbeat_count++;

            /* A15: stack headroom for THIS task (app_main / the ToF loop).
             * Logged every 10s, not every heartbeat, so a 5-minute
             * regression run doesn't flood the monitor log. mic_task's own
             * headroom is bone_mic.c's (A12/A14) to log -- this task can
             * only read its own stack. */
            if (heartbeat_count % 10 == 0) {
                UBaseType_t words_free = uxTaskGetStackHighWaterMark(NULL);
                ESP_LOGI(TAG, "a15_perf: tof_task stack headroom = %u bytes",
                         (unsigned)(words_free * sizeof(StackType_t)));
            }
        }

        for (size_t i = 0; i < NUM_SENSORS; i++) {
            if (!ok[i]) {
                continue;
            }
            /* A08: apply a pending SENS request. Compared against ranging[]
             * rather than applied blindly -- two commands can land between
             * two polls, so the pending edge may cancel itself out.
             * $STATUS is re-sent by uart_cmd.c, not here. */
            bool want;
            if (uart_cmd_sensor_take_pending(i, &want) && want != ranging[i]) {
                uint8_t st = want ? vl53l7cx_start_ranging(&s_dev[i])
                                  : vl53l7cx_stop_ranging(&s_dev[i]);
                if (st == 0) {
                    ranging[i] = want;
                    drop_next[i] = want;
                    if (want) {
                        /* A05: the gap across a stop/start is expected and
                         * not a real drop -- reseed the timing baseline on
                         * the next ready read instead of diffing across it. */
                        s_have_last_ready_t_us[i] = false;
                    }
                    ESP_LOGI(TAG, "[%s] ranging %s", pins[i].name,
                             want ? "started" : "stopped");
                } else {
                    ESP_LOGE(TAG, "[%s] %s_ranging failed, status=%u", pins[i].name,
                             want ? "start" : "stop", st);
                }
            }
            if (!ranging[i]) {
                continue;
            }
            uint8_t ready = 0;
            uint8_t status = vl53l7cx_check_data_ready(&s_dev[i], &ready);
            if (status == 0 && ready) {
                /* Sampled here, not after get_ranging_data(): the I2C
                 * transfer is ~2-4 ms and taking t_us after it would bake
                 * that in as a systematic bias (CONTRACTS.md #1.3). */
                int64_t t_us = esp_timer_get_time();

                /* A05 notready: a gap between consecutive ready timestamps
                 * bigger than 1.5 ranging periods implies whole cycles were
                 * missed in between (CPU too slow to poll, or the sensor
                 * stalled) -- see A05.md, this is its own suggested method. */
                if (s_have_last_ready_t_us[i]) {
                    int64_t delta = t_us - s_last_ready_t_us[i];
                    if (delta > (int64_t)(TOF_EXPECTED_PERIOD_US * 3 / 2)) {
                        s_drop_notready[i] += (uint32_t)(delta / TOF_EXPECTED_PERIOD_US) - 1;
                    }
                } else {
                    s_have_last_ready_t_us[i] = true;
                }
                s_last_ready_t_us[i] = t_us;

                status = vl53l7cx_get_ranging_data(&s_dev[i], &s_results[i]);
                if (status == 0) {
                    if (drop_next[i]) {
                        drop_next[i] = false;   /* unsettled first frame */
                    } else {
                        print_tof_line(pins[i].letter, s_seq[i], t_us, &s_results[i]);
                        s_seq[i]++;

                        /* A16: throttled to ~1 Hz by comparing t_us against
                         * the last emission, not by counting frames -- the
                         * ranging frequency differs by resolution (30 Hz
                         * 4x4, 10 Hz 8x8) and a frame-count divisor would
                         * drift depending which mode is flashed. */
                        if (s_amb_enabled && (t_us - s_last_amb_us[i] >= 1000000)) {
                            print_ambient_line(pins[i].letter, s_amb_seq[i], t_us, &s_results[i]);
                            s_amb_seq[i]++;
                            s_last_amb_us[i] = t_us;
                        }
                    }
                } else {
                    s_drop_error[i]++;   /* A05: I2C read failure */
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
