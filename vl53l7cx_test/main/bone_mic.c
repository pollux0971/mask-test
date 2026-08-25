#include "bone_mic.h"

#include <inttypes.h>
#include <math.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"
#include "driver/i2s_pdm.h"
#include "esp_dsp.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "mbedtls/base64.h"

#include "mel_filterbank.h"
#include "uart_out.h"

static const char *TAG = "bone_mic";

#define MIC_CLK_GPIO        GPIO_NUM_9
#define MIC_DATA_GPIO       GPIO_NUM_8
#define MIC_SAMPLE_RATE_HZ  16000
#define MIC_FRAME_SAMPLES   512   /* 32ms FFT/Mel analysis window @16kHz, unchanged by A14 */
#define MIC_HOP_SAMPLES     256   /* A14: 16ms hop (50% overlap) -> $F @62.5Hz, CONTRACTS.md §3.1 */

static i2s_chan_handle_t s_rx_handle;
static QueueHandle_t s_record_queue; /* depth 1, holds a pending recording length in seconds */

/* A13: $F on/off switch, default enabled. A single bool flag read once per
 * ~32ms frame -- no locking, a torn read just means one frame's decision
 * is a beat late, never a crash. */
static volatile bool s_mel_enabled = true;

/* A13/A14: count of mic frames dropped (i2s_channel_read failure) since
 * boot, for $H's drop_M -- CONTRACTS.md §1.3 defines drop_* as cumulative
 * since boot, never reset by $STATUS. The spinlock protects the ++ (a
 * read-modify-write) against a concurrent reader/writer; a lone aligned
 * uint32_t load in bone_mic_drop_count() doesn't need one. */
static portMUX_TYPE s_drop_spinlock = portMUX_INITIALIZER_UNLOCKED;
static uint32_t s_drop_count = 0;

void bone_mic_set_mel_enabled(bool on)
{
    s_mel_enabled = on;
}

bool bone_mic_mel_enabled(void)
{
    return s_mel_enabled;
}

uint32_t bone_mic_drop_count(void)
{
    return s_drop_count;
}

void bone_mic_frame_params(uint32_t *sr, uint16_t *win, uint16_t *mel_hop, uint16_t *mic_hop)
{
    if (sr) {
        *sr = MIC_SAMPLE_RATE_HZ;
    }
    if (win) {
        *win = MIC_FRAME_SAMPLES;
    }
    if (mel_hop) {
        *mel_hop = MIC_HOP_SAMPLES;
    }
    if (mic_hop) {
        /* $M is emitted every other hop (mic_task's emit_m_this_hop). */
        *mic_hop = MIC_HOP_SAMPLES * 2;
    }
}

/* ESP32-S3's I2S PDM RX has no hardware high-pass filter
 * (SOC_I2S_SUPPORTS_PDM_RX_HP_FILTER is not defined for this chip), so DC
 * bias and sub-100Hz rumble -- desk vibration, handling, the bone
 * conduction contact shifting slightly -- pass straight through
 * unfiltered. FFT'd recordings showed exactly this: dominant energy at
 * 10-40Hz with no 50/60Hz mains-hum signature, i.e. mechanical noise, not
 * electrical. A simple 1-pole DC-blocking/high-pass filter (~100Hz cutoff
 * at 16kHz) removes it in software. */
typedef struct {
    float prev_x;
    float prev_y;
} dc_blocker_t;

#define DC_BLOCKER_R 0.96f /* cutoff ~= (1-R)*rate/(2*pi) =~ 102 Hz at 16kHz */

static void dc_blocker_apply(dc_blocker_t *f, int16_t *samples, size_t n)
{
    for (size_t i = 0; i < n; i++) {
        float x = (float)samples[i];
        float y = x - f->prev_x + DC_BLOCKER_R * f->prev_y;
        f->prev_x = x;
        f->prev_y = y;
        if (y > 32767.0f) {
            y = 32767.0f;
        } else if (y < -32768.0f) {
            y = -32768.0f;
        }
        samples[i] = (int16_t)y;
    }
}

void bone_mic_init(void)
{
    i2s_chan_config_t chan_cfg = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_0, I2S_ROLE_MASTER);
    esp_err_t err = i2s_new_channel(&chan_cfg, NULL, &s_rx_handle);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "i2s_new_channel failed: %s", esp_err_to_name(err));
        return;
    }

    i2s_pdm_rx_config_t pdm_rx_cfg = {
        .clk_cfg = I2S_PDM_RX_CLK_DEFAULT_CONFIG(MIC_SAMPLE_RATE_HZ),
        .slot_cfg = I2S_PDM_RX_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_MONO),
        .gpio_cfg = {
            .clk = MIC_CLK_GPIO,
            .din = MIC_DATA_GPIO,
            .invert_flags = {
                .clk_inv = false,
            },
        },
    };
    err = i2s_channel_init_pdm_rx_mode(s_rx_handle, &pdm_rx_cfg);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "i2s_channel_init_pdm_rx_mode failed: %s", esp_err_to_name(err));
        return;
    }

    err = i2s_channel_enable(s_rx_handle);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "i2s_channel_enable failed: %s", esp_err_to_name(err));
        return;
    }

    ESP_LOGI(TAG, "PDM mic started: CLK=GPIO%d DATA=GPIO%d %dHz mono 16-bit",
             MIC_CLK_GPIO, MIC_DATA_GPIO, MIC_SAMPLE_RATE_HZ);
}

void bone_mic_record_and_dump(uint32_t seconds)
{
    size_t total_bytes = (size_t)seconds * MIC_SAMPLE_RATE_HZ * sizeof(int16_t);

    ESP_LOGI(TAG, "recording %u seconds (%u bytes)...", (unsigned)seconds, (unsigned)total_bytes);
    /* Machine-readable heads-up for the live monitor panel: recording eats
     * the mic's normal $MIC stream for the next `seconds` (plus a bit more
     * to transfer it), this tells the bridge to show that instead of
     * treating the gap as a dead link. */
    uart_out_lock();
    printf("$REC,start,%u\n", (unsigned)seconds);
    uart_out_unlock();

    /* Captured and base64-encoded one small chunk at a time instead of into
     * one big buffer up front: this board's PSRAM isn't enabled
     * (CONFIG_SPIRAM unset), and a whole-recording buffer plus its base64
     * blowup (~4/3x) don't reliably fit in internal SRAM alongside
     * everything else running -- that silently failed the malloc before.
     * REC_CHUNK_PCM_BYTES is a multiple of 3 so every full chunk's base64
     * comes out padding-free; only the final, possibly-short chunk pads,
     * which is valid since it's the true end of the stream. */
    #define REC_CHUNK_PCM_BYTES 1536
    uint8_t *pcm_chunk = malloc(REC_CHUNK_PCM_BYTES);
    size_t b64_cap = REC_CHUNK_PCM_BYTES * 4 / 3 + 4;
    unsigned char *b64_chunk = malloc(b64_cap);
    if (pcm_chunk == NULL || b64_chunk == NULL) {
        ESP_LOGE(TAG, "no memory for recording chunk buffers");
        free(pcm_chunk);
        free(b64_chunk);
        return;
    }

    uart_out_lock();
    printf("BEGIN_WAV_B64 rate=%d bits=16 channels=1 bytes=%u\n",
           MIC_SAMPLE_RATE_HZ, (unsigned)total_bytes);
    uart_out_unlock();

    dc_blocker_t filter = { 0 };
    size_t remaining = total_bytes;
    while (remaining > 0) {
        size_t want = (remaining < REC_CHUNK_PCM_BYTES) ? remaining : REC_CHUNK_PCM_BYTES;
        size_t filled = 0;
        while (filled < want) {
            size_t bytes_read = 0;
            esp_err_t err = i2s_channel_read(s_rx_handle, pcm_chunk + filled, want - filled, &bytes_read, 1000);
            if (err != ESP_OK) {
                ESP_LOGW(TAG, "i2s_channel_read failed during recording: %s", esp_err_to_name(err));
                continue;
            }
            filled += bytes_read;
        }

        dc_blocker_apply(&filter, (int16_t *)pcm_chunk, filled / sizeof(int16_t));

        size_t b64_len = 0;
        mbedtls_base64_encode(b64_chunk, b64_cap, &b64_len, pcm_chunk, filled);
        uart_out_lock();
        fwrite(b64_chunk, 1, b64_len, stdout);
        fwrite("\n", 1, 1, stdout);
        uart_out_unlock();

        remaining -= filled;
    }

    uart_out_lock();
    printf("END_WAV_B64\n");
    fflush(stdout);
    uart_out_unlock();

    free(pcm_chunk);
    free(b64_chunk);
    ESP_LOGI(TAG, "dump complete");
}

/* Periodic (non-symmetric) Hann window, precomputed once in mic_task()
 * before the streaming loop starts -- CONTRACTS.md §3.1 window function,
 * matches librosa.filters.get_window("hann", N, fftbins=True). */
static float s_hann_window[MIC_FRAME_SAMPLES];

/* A14: rolling 512-sample analysis window built from 50%-overlapping
 * 256-sample hops -- [older 256 | newer 256]. Zero-initialized (static),
 * so the very first couple of hops analyze a window that's part silence;
 * that's an expected, brief startup transient, not a bug. `static`, not
 * stack-local, same reasoning as the FFT/power buffers below. */
static int16_t s_ring[MIC_FRAME_SAMPLES];

/* FFT scratch (interleaved re/im) and the power spectrum derived from it.
 * `static`, not stack-local: fft_probe.c's spike already established that
 * a 4 KB FFT buffer doesn't belong on a task stack. Reused every frame. */
static float s_fft_buf[MIC_FRAME_SAMPLES * 2];
static float s_power[MEL_N_FFT_BINS];

/* Hann -> FFT -> power spectrum -> sparse mel matmul -> log10 -> x100 int16,
 * per CONTRACTS.md §3.1. `samples` must be exactly MIC_FRAME_SAMPLES long
 * (already DC-blocked by the caller, same filtered signal that ends up in
 * the recorded WAV dump, so a host-side reference run against that WAV
 * lines up with what this function computes). */
static void mic_compute_mel_frame(const int16_t *samples, int16_t out_m[MEL_N_FILTERS])
{
    for (int i = 0; i < MIC_FRAME_SAMPLES; i++) {
        s_fft_buf[i * 2]     = (float)samples[i] * s_hann_window[i];
        s_fft_buf[i * 2 + 1] = 0.0f;
    }

    dsps_fft2r_fc32(s_fft_buf, MIC_FRAME_SAMPLES);
    dsps_bit_rev_fc32(s_fft_buf, MIC_FRAME_SAMPLES);

    for (int k = 0; k < MEL_N_FFT_BINS; k++) {
        float re = s_fft_buf[k * 2];
        float im = s_fft_buf[k * 2 + 1];
        s_power[k] = re * re + im * im;
    }

    for (int m = 0; m < MEL_N_FILTERS; m++) {
        const mel_filter_t *f = &mel_filterbank[m];
        float acc = 0.0f;
        for (int j = 0; j < f->len; j++) {
            acc += f->w[j] * s_power[f->start + j];
        }
        float log_mel = log10f(fmaxf(acc, 1e-10f));
        out_m[m] = (int16_t)lroundf(log_mel * 100.0f);
    }
}

static void mic_task(void *arg)
{
    dc_blocker_t stream_filter = { 0 };
    uint32_t m_seq = 0;
    uint32_t f_seq = 0;
    /* A14: $M stays at half $F's rate (CONTRACTS.md still wants ~31.25 Hz
     * for $M) -- emit it on every other hop. $F and $M now keep separate
     * seq counters (each just "how many of this line have been sent"),
     * decoupled per the A14 design discussion: CONTRACTS.md §1.1 never
     * required them to share one, and cross-modal alignment is t_us's job. */
    bool emit_m_this_hop = true;
    /* A15: this task's own stack headroom, logged periodically (not every
     * hop -- at 62.5 Hz that would flood the monitor log during a 5-minute
     * regression run). uxTaskGetStackHighWaterMark() only reads the
     * calling task's own stack, so this has to live inside mic_task. */
    int64_t last_stack_log_us = esp_timer_get_time();

    for (int i = 0; i < MIC_FRAME_SAMPLES; i++) {
        s_hann_window[i] = 0.5f - 0.5f * cosf(2.0f * (float)M_PI * i / MIC_FRAME_SAMPLES);
    }

    /* Initialized once for the life of this task (never deinit -- mic_task
     * never returns); a failed init just disables $F output, $M keeps
     * streaming regardless (see fft_ready below). */
    bool fft_ready = (dsps_fft2r_init_fc32(NULL, MIC_FRAME_SAMPLES) == ESP_OK);
    if (!fft_ready) {
        ESP_LOGE(TAG, "dsps_fft2r_init_fc32 failed, $F output disabled");
    }

    while (1) {
        uint32_t requested_seconds;
        if (xQueueReceive(s_record_queue, &requested_seconds, 0) == pdTRUE) {
            bone_mic_record_and_dump(requested_seconds);
            continue;
        }

        /* A14: slide the ring by one hop -- the previous hop's new half
         * becomes the old half, then this hop's samples are read straight
         * into the freed tail. No separate read buffer needed. */
        memmove(s_ring, s_ring + MIC_HOP_SAMPLES, MIC_HOP_SAMPLES * sizeof(int16_t));

        size_t bytes_read = 0;
        esp_err_t err = i2s_channel_read(s_rx_handle, s_ring + MIC_HOP_SAMPLES,
                                          MIC_HOP_SAMPLES * sizeof(int16_t), &bytes_read, 1000);
        /* Captured immediately on return: i2s_channel_read() hands back a
         * buffer DMA already filled, so this timestamp marks the *end* of
         * the frame, not its start (see t_start below). */
        int64_t t_end = esp_timer_get_time();
        if (err != ESP_OK) {
            ESP_LOGW(TAG, "i2s_channel_read failed: %s", esp_err_to_name(err));
            portENTER_CRITICAL(&s_drop_spinlock);
            s_drop_count++;
            portEXIT_CRITICAL(&s_drop_spinlock);
            continue;
        }

        size_t n = bytes_read / sizeof(int16_t);
        dc_blocker_apply(&stream_filter, s_ring + MIC_HOP_SAMPLES, n);

        /* CONTRACTS.md 1.3: t_us is the timestamp of the *window's* first
         * sample, not the read-return time. The ring's oldest sample is
         * MIC_FRAME_SAMPLES (not just this hop's n) behind t_end -- this is
         * the start of the full 512-sample analysis window that both $F
         * and (when emitted) $M below describe. */
        int64_t t_start = t_end - (int64_t)MIC_FRAME_SAMPLES * 1000000 / MIC_SAMPLE_RATE_HZ;

        /* $F needs a full hop of fresh samples; a short read is treated
         * like the failure case above -- no $F for this hop rather than
         * feeding the FFT a partially-stale ring. */
        int16_t mel_out[MEL_N_FILTERS];
        bool have_mel = false;
        int64_t mel_us = 0;
        if (fft_ready && s_mel_enabled && n == MIC_HOP_SAMPLES) {
            int64_t t0 = esp_timer_get_time();
            mic_compute_mel_frame(s_ring, mel_out);
            mel_us = esp_timer_get_time() - t0;
            have_mel = true;
        }

        /* A14: $M only on every other hop (~31.25 Hz), computed over the
         * same 512-sample ring $F just analyzed -- not just this hop's 256
         * new samples, so it reflects the full window rather than half of
         * it going unused. */
        bool have_m = false;
        int16_t m_rms = 0;
        int16_t m_peak = 0;
        if (emit_m_this_hop && n == MIC_HOP_SAMPLES) {
            int64_t sum_sq = 0;
            int16_t peak = 0;
            for (int i = 0; i < MIC_FRAME_SAMPLES; i++) {
                int16_t s = s_ring[i];
                sum_sq += (int64_t)s * (int64_t)s;
                int16_t a = (s < 0) ? (int16_t)(-s) : s;
                if (a > peak) {
                    peak = a;
                }
            }
            double rms = sqrt((double)sum_sq / (double)MIC_FRAME_SAMPLES);
            m_rms = (int16_t)lround(rms);
            m_peak = peak;
            have_m = true;
        }
        emit_m_this_hop = !emit_m_this_hop;

        /* One compact line per hop for the live monitor panel (monitor/):
         * plain printf, no ESP_LOG prefix, '$' sentinel so the host bridge
         * can pick it out cleanly. rms/peak are both i16 raw PCM amplitude
         * per CONTRACTS.md 1.1 (protocol v2). $F and $M now keep
         * independent seq counters (A14) but share this hop's t_start,
         * emitted under the same lock so they can't be interleaved with
         * output from another task. */
        uart_out_lock();
        if (have_m) {
            printf("$M,%" PRIu32 ",%" PRId64 ",%d,%d\n", m_seq, t_start, m_rms, m_peak);
            m_seq++;
        }
        if (have_mel) {
            printf("$F,%" PRIu32 ",%" PRId64, f_seq, t_start);
            for (int m = 0; m < MEL_N_FILTERS; m++) {
                printf(",%d", mel_out[m]);
            }
            printf("\n");
            f_seq++;
        }
        uart_out_unlock();

        if (have_mel) {
            /* A10's GO/NO-GO still needs an on-hardware number; this is
             * where it'll come from once someone raises this tag's log
             * level (ESP_LOGD is compiled in but filtered by default).
             * With A14's 50% overlap, this now runs twice as often -- the
             * number to watch for whether hop 256 holds up in real time. */
            ESP_LOGD(TAG, "mel frame fft+mel=%lld us", (long long)mel_us);
        }

        if (t_end - last_stack_log_us >= 10 * 1000000) {
            UBaseType_t words_free = uxTaskGetStackHighWaterMark(NULL);
            ESP_LOGI(TAG, "a15_perf: mic_task stack headroom = %u bytes",
                     (unsigned)(words_free * sizeof(StackType_t)));
            last_stack_log_us = t_end;
        }
    }
}

void bone_mic_start_monitor(void)
{
    s_record_queue = xQueueCreate(1, sizeof(uint32_t));
    xTaskCreate(mic_task, "bone_mic", 6144, NULL, 5, NULL);
}

void bone_mic_request_recording(uint32_t seconds)
{
    if (s_record_queue != NULL) {
        xQueueOverwrite(s_record_queue, &seconds);
    }
}
