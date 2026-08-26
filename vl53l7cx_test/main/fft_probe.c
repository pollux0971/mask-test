#include <math.h>
#include <stdbool.h>
#include "esp_log.h"
#include "esp_timer.h"
#include "esp_dsp.h"
#include "fft_probe.h"

static const char *TAG = "fft_probe";

#define FFT_PROBE_N               512
#define FFT_PROBE_SAMPLE_RATE_HZ  16000
#define FFT_PROBE_TEST_FREQ_HZ    1000
/* 1 kHz @ 16 kHz, N=512 -> bin = 1000 * 512 / 16000 = 32 (per A10.md) */
#define FFT_PROBE_EXPECT_BIN      32
#define FFT_PROBE_BIN_TOLERANCE   1

void fft_probe_run(void)
{
    /* Interleaved re/im pairs, 4 KB -- too big for a task stack. */
    static float x[FFT_PROBE_N * 2];

    for (int i = 0; i < FFT_PROBE_N; i++) {
        x[i * 2]     = sinf(2.0f * (float)M_PI * FFT_PROBE_TEST_FREQ_HZ * i / FFT_PROBE_SAMPLE_RATE_HZ);
        x[i * 2 + 1] = 0.0f;
    }

    esp_err_t err = dsps_fft2r_init_fc32(NULL, FFT_PROBE_N);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "dsps_fft2r_init_fc32 failed: %d", err);
        return;
    }

    int64_t t0 = esp_timer_get_time();
    dsps_fft2r_fc32(x, FFT_PROBE_N);
    dsps_bit_rev_fc32(x, FFT_PROBE_N);
    int64_t dt_us = esp_timer_get_time() - t0;

    /* Real input -> spectrum is mirrored past N/2, so only scan the first half. */
    int peak_bin = 0;
    float peak_mag = 0.0f;
    for (int i = 0; i < FFT_PROBE_N / 2; i++) {
        float re = x[i * 2];
        float im = x[i * 2 + 1];
        float mag = re * re + im * im;
        if (mag > peak_mag) {
            peak_mag = mag;
            peak_bin = i;
        }
    }

    bool bin_ok = (peak_bin >= FFT_PROBE_EXPECT_BIN - FFT_PROBE_BIN_TOLERANCE) &&
                  (peak_bin <= FFT_PROBE_EXPECT_BIN + FFT_PROBE_BIN_TOLERANCE);

    ESP_LOGI(TAG, "N=%d expect_bin=%d(+-%d) got_bin=%d [%s] fft+bitrev=%lld us",
             FFT_PROBE_N, FFT_PROBE_EXPECT_BIN, FFT_PROBE_BIN_TOLERANCE, peak_bin,
             bin_ok ? "OK" : "MISMATCH", (long long)dt_us);

    /* No dsps_fft2r_deinit_fc32() here: the twiddle/bit-rev tables it
     * allocates are a single global in esp-dsp (dsps_fft_w_table_fc32 /
     * dsps_fft2r_ram_rev_table), shared with bone_mic.c's own FFT (also
     * N=512). Deiniting after boot-time-only use was harmless when nothing
     * else had claimed the tables yet, but this function is now also
     * reachable from a live command (FFTPROBE) after mic_task has already
     * initialised and is actively using them -- freeing dsps_fft2r_ram_rev_table
     * out from under mic_task's next dsps_bit_rev_fc32() call would be a
     * use-after-free. Leaving init'd is safe either way: at boot this just
     * means mic_task's own dsps_fft2r_init_fc32() call later becomes a
     * cheap no-op (already-initialised check), and via command it never
     * touches state mic_task doesn't already own for its own lifetime. */
}
