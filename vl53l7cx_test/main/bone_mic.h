#pragma once

#include <stdint.h>

/* Sets up I2S PDM RX on the bone-conduction MEMS mic (CLK=GPIO9, DATA=GPIO8).
 * Must be called once before bone_mic_record_and_dump() or
 * bone_mic_start_monitor(). */
void bone_mic_init(void);

/* Synchronously records `seconds` of 16kHz/16-bit/mono audio into RAM, then
 * dumps it as base64 PCM over the console UART between BEGIN_WAV_B64 / END_WAV_B64
 * markers, for a host-side script to capture and turn into a .wav file. */
void bone_mic_record_and_dump(uint32_t seconds);

/* Spawns a background task that continuously streams RMS/peak amplitude
 * ($MIC lines) for the live monitor panel. Also owns the on-demand
 * recording flow: bone_mic_request_recording() can be called from any
 * other task to have this same task pause streaming, capture `seconds` of
 * audio, dump it (see bone_mic_record_and_dump), and resume streaming. */
void bone_mic_start_monitor(void);

/* Thread-safe: queues a recording request for the monitor task to pick up
 * on its next loop iteration. A new request overwrites any not yet
 * started. Must be called after bone_mic_start_monitor(). */
void bone_mic_request_recording(uint32_t seconds);
