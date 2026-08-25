#pragma once

/* The ToF loop (app_main), the mic's live $MIC stream, and an on-demand
 * recording dump all print to the same console UART from different
 * FreeRTOS tasks. A single printf/fwrite call isn't guaranteed atomic
 * against another task's concurrent write once it's more than a few dozen
 * bytes (the base64 recording dump writes ~2KB lines), so without this lock
 * two tasks' output can interleave mid-line and corrupt both. Wrap every
 * "one protocol line" unit of output between uart_out_lock()/unlock(). */
void uart_out_init(void);
void uart_out_lock(void);
void uart_out_unlock(void);
