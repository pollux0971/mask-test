idf_component_register(SRCS "vl53l7cx_test.c" "bone_mic.c" "uart_cmd.c" "uart_out.c" "fft_probe.c" "mel_filterbank.c"
                        INCLUDE_DIRS "."
                        REQUIRES vl53l7cx_uld esp_driver_i2c esp_driver_i2s mbedtls esp_driver_uart esp_timer esp-dsp)

# A07: fw= field of $STATUS. WORKING_DIRECTORY is pinned to the source tree
# (not the build dir, which may live entirely outside the repo -- e.g. a
# scratch build dir) so this works regardless of where -B points. Falls
# back to "unknown" if git itself fails (e.g. this tree isn't a git repo).
execute_process(COMMAND git rev-parse --short HEAD
                 WORKING_DIRECTORY ${CMAKE_CURRENT_SOURCE_DIR}
                 OUTPUT_VARIABLE FW_GIT_SHA
                 OUTPUT_STRIP_TRAILING_WHITESPACE
                 ERROR_QUIET
                 RESULT_VARIABLE FW_GIT_SHA_RESULT)
if(NOT FW_GIT_SHA_RESULT EQUAL 0 OR FW_GIT_SHA STREQUAL "")
    set(FW_GIT_SHA "unknown")
else()
    execute_process(COMMAND git status --porcelain
                     WORKING_DIRECTORY ${CMAKE_CURRENT_SOURCE_DIR}
                     OUTPUT_VARIABLE FW_GIT_DIRTY
                     OUTPUT_STRIP_TRAILING_WHITESPACE
                     ERROR_QUIET)
    if(NOT FW_GIT_DIRTY STREQUAL "")
        set(FW_GIT_SHA "${FW_GIT_SHA}-dirty")
    endif()
endif()
target_compile_definitions(${COMPONENT_LIB} PRIVATE FW_GIT_SHA="${FW_GIT_SHA}")
