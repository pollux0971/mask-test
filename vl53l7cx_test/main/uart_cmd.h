#pragma once

/* Switches the console UART to interrupt-driven mode (so stdin reads block
 * a FreeRTOS task instead of busy-polling) and spawns a task that parses
 * simple text commands sent from the host bridge (monitor/bridge_server.py)
 * over the same serial link already used for $TOF/$MIC/BEGIN_WAV_B64
 * output. Currently understands:
 *   REC:<seconds>\n   -- trigger bone_mic_request_recording(seconds)
 */
void uart_cmd_start(void);
