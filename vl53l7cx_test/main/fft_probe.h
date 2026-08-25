#pragma once

/* A10 spike: generates a synthetic 1 kHz sine sampled at 16 kHz, runs a
 * 512-point esp-dsp FFT on it, and logs whether the energy peak lands at
 * bin 32 (+-1) along with the FFT duration. This answers a single yes/no
 * question -- can esp-dsp's FFT run correctly on this board -- before any
 * Mel work is built on top of it (see stories/A-firmware/A10.md).
 *
 * Deliberately not wired into app_main: A10's scope is the probe itself,
 * not integration. Call this once from wherever you want to run the spike
 * (e.g. a temporary call at the top of app_main) when flashing to hardware. */
void fft_probe_run(void);
