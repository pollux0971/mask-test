idf_component_register(
    SRCS "vl53l7cx_api.c" "vl53l7cx_platform.c"
    INCLUDE_DIRS "include"
    REQUIRES esp_driver_i2c
)

# vl53l7cx_buffers.h contains the ~540KB firmware/config blob as const arrays;
# keep optimization on for it regardless of the project's global debug settings.
target_compile_options(${COMPONENT_LIB} PRIVATE -O2)
