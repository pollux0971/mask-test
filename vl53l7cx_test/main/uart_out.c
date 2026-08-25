#include "uart_out.h"

#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"

static SemaphoreHandle_t s_mutex;
static uint32_t s_bytes_since_boot;

void uart_out_init(void)
{
    s_mutex = xSemaphoreCreateMutex();
}

void uart_out_lock(void)
{
    if (s_mutex != NULL) {
        xSemaphoreTake(s_mutex, portMAX_DELAY);
    }
}

void uart_out_unlock(void)
{
    if (s_mutex != NULL) {
        xSemaphoreGive(s_mutex);
    }
}

void uart_out_add_bytes(size_t n)
{
    s_bytes_since_boot += (uint32_t)n;
}

uint32_t uart_out_bytes_since_boot(void)
{
    return s_bytes_since_boot;
}
