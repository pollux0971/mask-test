#pragma once

#include <stdbool.h>
#include <stddef.h>

/* Switches the console UART to interrupt-driven mode (so stdin reads block
 * a FreeRTOS task instead of busy-polling) and spawns a task that parses
 * simple text commands sent from the host bridge (monitor/bridge_server.py)
 * over the same serial link already used for $T/$M/BEGIN_WAV_B64
 * output. Commands understood (CONTRACTS.md #1.2):
 *   REC:<seconds>\n     -- trigger bone_mic_request_recording(seconds)
 *   SENS:<A|B>=<0|1>\n  -- enable/disable one ToF sensor (see below)
 */
void uart_cmd_start(void);

/* --- SENS: per-sensor enable (A08) -------------------------------------
 *
 * `SENS:B=0` must really call vl53l7cx_stop_ranging() -- merely skipping the
 * $T printf leaves B's VCSEL firing, so the crosstalk experiment (C0) would
 * measure the interference it is trying to remove and conclude there is
 * none. But the VL53L7CX_Configuration handles live in the ToF loop's
 * translation unit, not here, so this module only owns the *requested*
 * state and hands the transition to whoever drives the sensors.
 *
 * Index 0 is sensor 'A', index 1 is sensor 'B' -- the same order as the
 * ToF loop's sensor table. Both sensors start enabled.
 *
 * Consumer contract (the ToF loop):
 *   1. Once per iteration, call uart_cmd_sensor_take_pending() for each
 *      sensor. It returns true at most once per host command.
 *   2. On true, compare against the state you actually applied and, if it
 *      differs, call vl53l7cx_start_ranging() / vl53l7cx_stop_ranging().
 *      Compare rather than trusting the edge blindly: two commands can
 *      arrive between two polls, so the pending flag can survive a
 *      request that cancels itself out.
 *   3. After a restart, discard the first frame -- the sensor needs 1-2
 *      ranging periods to settle and the first result is not trustworthy.
 *   4. Skip check_data_ready()/get_ranging_data() for a disabled sensor,
 *      and re-emit $STATUS after applying a change (CONTRACTS.md #1.1
 *      "版本協商": the device re-sends $STATUS after every SENS/MEL/switch).
 */
#define UART_CMD_SENSOR_COUNT 2

/* Latest state requested by the host for sensor `idx`. Safe from any task.
 * Out-of-range idx reports true (enabled), matching the default. */
bool uart_cmd_sensor_enabled(size_t idx);

/* Consumes a pending enable/disable request for sensor `idx`. Returns true
 * and writes the requested state to *out_enable if the host asked for a
 * change that has not been picked up yet; returns false and leaves
 * *out_enable untouched otherwise. Clears the pending flag, so a given
 * request is reported exactly once. */
bool uart_cmd_sensor_take_pending(size_t idx, bool *out_enable);
