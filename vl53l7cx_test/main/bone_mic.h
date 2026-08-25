#pragma once

#include <stdbool.h>
#include <stdint.h>

/* Sets up I2S PDM RX on the bone-conduction MEMS mic (CLK=GPIO9, DATA=GPIO8).
 * Must be called once before bone_mic_record_and_dump() or
 * bone_mic_start_monitor(). */
void bone_mic_init(void);

/* Synchronously records `seconds` of 16kHz/16-bit/mono audio into RAM, then
 * dumps it as base64 PCM over the console UART between BEGIN_WAV_B64 / END_WAV_B64
 * markers, for a host-side script to capture and turn into a .wav file. */
void bone_mic_record_and_dump(uint32_t seconds);

/* Spawns a background task that continuously streams seq/timestamp/RMS/peak
 * ($M lines, protocol v2) for the live monitor panel. Also owns the on-demand
 * recording flow: bone_mic_request_recording() can be called from any
 * other task to have this same task pause streaming, capture `seconds` of
 * audio, dump it (see bone_mic_record_and_dump), and resume streaming. */
void bone_mic_start_monitor(void);

/* Thread-safe: queues a recording request for the monitor task to pick up
 * on its next loop iteration. A new request overwrites any not yet
 * started. Must be called after bone_mic_start_monitor(). */
void bone_mic_request_recording(uint32_t seconds);

/* A13: turns $F (Mel) streaming on/off; $M keeps streaming regardless.
 * Defaults to enabled. Callable from any task (e.g. the uart_cmd.c handler
 * for MEL:<0|1>) -- a lone bool flag, no locking needed for this kind of
 * single-writer-many-reader on/off switch. */
void bone_mic_set_mel_enabled(bool on);

/* Current $F streaming state, for whoever builds the $STATUS line to
 * report it (CONTRACTS.md §1.1: "MEL 改變輸出組態後要重發 $STATUS"). */
bool bone_mic_mel_enabled(void);

/* Count of mic frames that have failed to be captured since boot -- never
 * reset (CONTRACTS.md §1.3: drop_* is a since-boot cumulative counter, not
 * reset on $STATUS, so repeated PING-triggered $STATUS reissues don't
 * zero it out from under B05). Safe to call from any task. */
uint32_t bone_mic_drop_count(void);

/* A15/CONTRACTS.md #1.1.2: the audio frame parameters $STATUS needs to
 * self-describe which version of the $F/$M format the firmware is sending
 * (A14 changed mel_hop from 512 to 256 samples without changing the wire
 * format, so the host can't tell versions apart otherwise). Exposed as a
 * getter rather than duplicating the constants in vl53l7cx_test.c so a
 * future change to any of these can't silently desync the two files. Any
 * output pointer may be NULL if that value isn't needed by the caller.
 *   sr       -- MIC_SAMPLE_RATE_HZ (Hz)
 *   win      -- MIC_FRAME_SAMPLES, the FFT/analysis window (samples)
 *   mel_hop  -- MIC_HOP_SAMPLES, $F's frame spacing (samples)
 *   mic_hop  -- $M's frame spacing (samples) -- 2x mel_hop, since mic_task
 *               emits $M on every other hop (see mic_task's emit_m_this_hop) */
void bone_mic_frame_params(uint32_t *sr, uint16_t *win, uint16_t *mel_hop, uint16_t *mic_hop);
