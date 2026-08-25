idf_component_register(SRCS "vl53l7cx_test.c" "bone_mic.c" "uart_cmd.c" "uart_out.c" "fft_probe.c"
                        INCLUDE_DIRS "."
                        REQUIRES vl53l7cx_uld esp_driver_i2c esp_driver_i2s mbedtls esp_driver_uart esp_timer esp-dsp)
