#include "bone_mic.h"

#include <inttypes.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"
#include "driver/i2s_pdm.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "mbedtls/base64.h"

#include "uart_out.h"

static const char *TAG = "bone_mic";

#define MIC_CLK_GPIO        GPIO_NUM_9
#define MIC_DATA_GPIO       GPIO_NUM_8
#define MIC_SAMPLE_RATE_HZ  16000
#define MIC_FRAME_SAMPLES   512   /* 32ms of audio per read at 16kHz */

static i2s_chan_handle_t s_rx_handle;
static QueueHandle_t s_record_queue; /* depth 1, holds a pending recording length in seconds */

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

static void mic_task(void *arg)
{
    int16_t *buf = malloc(MIC_FRAME_SAMPLES * sizeof(int16_t));
    if (buf == NULL) {
        ESP_LOGE(TAG, "out of memory for audio buffer");
        vTaskDelete(NULL);
        return;
    }
    dc_blocker_t stream_filter = { 0 };
    uint32_t seq = 0;

    while (1) {
        uint32_t requested_seconds;
        if (xQueueReceive(s_record_queue, &requested_seconds, 0) == pdTRUE) {
            bone_mic_record_and_dump(requested_seconds);
            continue;
        }

        size_t bytes_read = 0;
        esp_err_t err = i2s_channel_read(s_rx_handle, buf, MIC_FRAME_SAMPLES * sizeof(int16_t),
                                          &bytes_read, 1000);
        /* Captured immediately on return: i2s_channel_read() hands back a
         * buffer DMA already filled, so this timestamp marks the *end* of
         * the frame, not its start (see t_start below). */
        int64_t t_end = esp_timer_get_time();
        if (err != ESP_OK) {
            ESP_LOGW(TAG, "i2s_channel_read failed: %s", esp_err_to_name(err));
            continue;
        }

        size_t n = bytes_read / sizeof(int16_t);
        dc_blocker_apply(&stream_filter, buf, n);

        int64_t sum_sq = 0;
        int16_t peak = 0;
        for (size_t i = 0; i < n; i++) {
            int16_t s = buf[i];
            sum_sq += (int64_t)s * (int64_t)s;
            int16_t a = (s < 0) ? (int16_t)(-s) : s;
            if (a > peak) {
                peak = a;
            }
        }
        double rms = (n > 0) ? sqrt((double)sum_sq / (double)n) : 0.0;

        /* CONTRACTS.md 1.3: t_us is the timestamp of the frame's first
         * sample, not the read-return time -- cross-modal alignment with
         * ToF needs the frame's start, and this 32ms gap is close in
         * magnitude to the lip-to-voice onset delay being measured. */
        int64_t t_start = t_end - (int64_t)n * 1000000 / MIC_SAMPLE_RATE_HZ;

        /* One compact line per ~32ms frame for the live monitor panel
         * (monitor/): plain printf, no ESP_LOG prefix, '$' sentinel so the
         * host bridge can pick it out cleanly. rms/peak are both i16 raw
         * PCM amplitude per CONTRACTS.md 1.1 (protocol v2). */
        uart_out_lock();
        printf("$M,%" PRIu32 ",%" PRId64 ",%d,%d\n", seq, t_start, (int16_t)lround(rms), peak);
        uart_out_unlock();
        seq++;
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
