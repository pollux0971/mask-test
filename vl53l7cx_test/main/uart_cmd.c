#include "uart_cmd.h"

#include <stdio.h>
#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "driver/uart.h"
#include "driver/uart_vfs.h"
#include "esp_log.h"

#include "bone_mic.h"

static const char *TAG = "uart_cmd";

static void uart_cmd_task(void *arg)
{
    char line[64];
    while (1) {
        if (fgets(line, sizeof(line), stdin) == NULL) {
            vTaskDelay(pdMS_TO_TICKS(50));
            continue;
        }

        int seconds = 0;
        if (sscanf(line, "REC:%d", &seconds) == 1 && seconds > 0 && seconds <= 30) {
            ESP_LOGI(TAG, "recording request: %ds", seconds);
            bone_mic_request_recording((uint32_t)seconds);
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
