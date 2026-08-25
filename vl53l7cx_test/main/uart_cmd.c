#include "uart_cmd.h"

#include <stdio.h>
#include <string.h>
#include <ctype.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "driver/uart.h"
#include "driver/uart_vfs.h"
#include "esp_log.h"

#include "bone_mic.h"

static const char *TAG = "uart_cmd";

/* Requested per-sensor state, written by uart_cmd_task and read by the ToF
 * loop on another task. `s_pending` and `s_enabled` have to move together
 * (a reader that saw the flag but the old value would apply the wrong
 * transition), so both are guarded by one spinlock. The critical sections
 * are a handful of instructions -- cheaper than a mutex for something the
 * ToF loop touches 100 times a second. */
static portMUX_TYPE s_sensor_mux = portMUX_INITIALIZER_UNLOCKED;
static bool s_enabled[UART_CMD_SENSOR_COUNT] = { true, true };
static bool s_pending[UART_CMD_SENSOR_COUNT];

bool uart_cmd_sensor_enabled(size_t idx)
{
    if (idx >= UART_CMD_SENSOR_COUNT) {
        return true;
    }
    taskENTER_CRITICAL(&s_sensor_mux);
    bool enabled = s_enabled[idx];
    taskEXIT_CRITICAL(&s_sensor_mux);
    return enabled;
}

bool uart_cmd_sensor_take_pending(size_t idx, bool *out_enable)
{
    if (idx >= UART_CMD_SENSOR_COUNT || out_enable == NULL) {
        return false;
    }
    taskENTER_CRITICAL(&s_sensor_mux);
    bool pending = s_pending[idx];
    if (pending) {
        *out_enable = s_enabled[idx];
        s_pending[idx] = false;
    }
    taskEXIT_CRITICAL(&s_sensor_mux);
    return pending;
}

static void sensor_request(size_t idx, bool enable)
{
    taskENTER_CRITICAL(&s_sensor_mux);
    bool changed = (s_enabled[idx] != enable);
    if (changed) {
        s_enabled[idx] = enable;
        s_pending[idx] = true;
    }
    taskEXIT_CRITICAL(&s_sensor_mux);

    /* Logged either way: the host asking twice for the same state is not an
     * error, but "nothing happened" is worth seeing in the monitor. */
    ESP_LOGI(TAG, "SENS:%c=%d (%s)", (char)('A' + idx), enable ? 1 : 0,
             changed ? "queued" : "already in that state");
}

/* Strips leading and trailing whitespace in place (fgets keeps the '\n',
 * and the host bridge may send CRLF). Returns the start of the trimmed
 * string, which may differ from `line`. */
static char *trim(char *line)
{
    while (*line != '\0' && isspace((unsigned char)*line)) {
        line++;
    }
    size_t len = strlen(line);
    while (len > 0 && isspace((unsigned char)line[len - 1])) {
        line[--len] = '\0';
    }
    return line;
}

/* Parses exactly "SENS:<A|B>=<0|1>" (CONTRACTS.md #1.2) from an already
 * trimmed line. Anything else -- lowercase letter, a third sensor, a value
 * other than 0/1, trailing junk -- is rejected rather than guessed at, so a
 * garbled line can never silently switch off a sensor mid-experiment. */
static bool parse_sens(const char *line, size_t *out_idx, bool *out_enable)
{
    static const char prefix[] = "SENS:";
    const size_t prefix_len = sizeof(prefix) - 1;

    if (strncmp(line, prefix, prefix_len) != 0) {
        return false;
    }
    const char *p = line + prefix_len;

    size_t idx;
    if (p[0] == 'A') {
        idx = 0;
    } else if (p[0] == 'B') {
        idx = 1;
    } else {
        return false;
    }
    if (p[1] != '=') {
        return false;
    }
    if (p[2] == '0') {
        *out_enable = false;
    } else if (p[2] == '1') {
        *out_enable = true;
    } else {
        return false;
    }
    if (p[3] != '\0') {
        return false;
    }

    *out_idx = idx;
    return true;
}

static void uart_cmd_task(void *arg)
{
    char line[64];
    while (1) {
        if (fgets(line, sizeof(line), stdin) == NULL) {
            vTaskDelay(pdMS_TO_TICKS(50));
            continue;
        }

        char *cmd = trim(line);
        if (*cmd == '\0') {
            continue;
        }

        size_t idx;
        bool enable;
        int seconds = 0;

        if (parse_sens(cmd, &idx, &enable)) {
            sensor_request(idx, enable);
        } else if (sscanf(cmd, "REC:%d", &seconds) == 1 && seconds > 0 && seconds <= 30) {
            ESP_LOGI(TAG, "recording request: %ds", seconds);
            bone_mic_request_recording((uint32_t)seconds);
        } else {
            ESP_LOGW(TAG, "unrecognised command: '%s'", cmd);
        }
    }
}

void uart_cmd_start(void)
{
    /* Console UART0 is already configured (pins/baud) by the SDK startup
     * path for stdout; installing the interrupt-driven driver here just
     * adds a proper RX ring buffer so fgets() blocks this task instead of
     * busy-polling and starving everything else on this core. */
    uart_driver_install(UART_NUM_0, 512, 0, 0, NULL, 0);
    uart_vfs_dev_use_driver(UART_NUM_0);

    xTaskCreate(uart_cmd_task, "uart_cmd", 3072, NULL, 5, NULL);
}
