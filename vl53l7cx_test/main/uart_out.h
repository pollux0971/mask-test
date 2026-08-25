#pragma once

#include <stddef.h>
#include <stdint.h>

/* The ToF loop (app_main), the mic's live $M/$F stream, and an on-demand
 * recording dump all print to the same console UART from different
 * FreeRTOS tasks. A single printf/fwrite call isn't guaranteed atomic
 * against another task's concurrent write once it's more than a few dozen
 * bytes (the base64 recording dump writes ~2KB lines), so without this lock
 * two tasks' output can interleave mid-line and corrupt both. Wrap every
 * "one protocol line" unit of output between uart_out_lock()/unlock(). */
void uart_out_init(void);
void uart_out_lock(void);
void uart_out_unlock(void);

/* A15: bandwidth accounting for CONTRACTS.md #1.4 (used e.g. to fill $H's
 * bw_bytes_since_last field, CONTRACTS.md #1.1 changelog A15). This does
 * NOT hook the UART driver to observe bytes automatically -- true counts
 * only exist as each printf() call's own return value, and a caller not
 * already holding uart_out_lock()/unlock() has nothing to report. So this
 * is opt-in per caller: after printing a line inside the lock, pass the
 * summed printf() return values to uart_out_add_bytes() before unlocking.
 * Only callers that opt in are reflected in uart_out_bytes_since_boot() --
 * as of A16 that's every $-line producer: $T/$STATUS/$H/$A
 * (vl53l7cx_test.c), $M/$F (bone_mic.c), and the on-demand recording dump
 * ($REC/BEGIN_WAV_B64/base64 chunks/END_WAV_B64, also bone_mic.c -- NOT
 * uart_cmd.c, which has no printf output at all). bw_bytes_since_last is
 * now a true total, not a lower bound. Must be called while holding the
 * lock (the accumulator itself isn't separately synchronized). */
void uart_out_add_bytes(size_t n);
uint32_t uart_out_bytes_since_boot(void);
