#pragma once

#include <stddef.h>
#include <stdint.h>

/* A07: prints the $STATUS,res=<dim>,proto=<n>,fw=<sha> handshake line
 * (CONTRACTS.md #1.1) that a host bridge uses for protocol version
 * negotiation. Called once at boot; CONTRACTS.md #1.1 also requires it be
 * resent on PING and after any SENS/MEL/switch config change -- those
 * triggers live in uart_cmd.c (A08), which should call this. Also resets
 * the A05 drop counters below (CONTRACTS.md #1.3: drop_* is cumulative
 * since the last $STATUS). */
void tof_print_status(void);

/* A05: cumulative ToF drop counters for sensor `idx` (0=A, 1=B), since the
 * last $STATUS. Out-of-range idx returns 0. Kept as two separate causes --
 * see vl53l7cx_test.c for how each is detected -- because they point at
 * different hardware problems and A06's $H line needs both summed in:
 *   notready -- sensor produced a frame we never picked up (CPU/poll rate)
 *   error    -- get_ranging_data() failed (I2C signal quality) */
uint32_t tof_get_drop_notready(size_t idx);
uint32_t tof_get_drop_error(size_t idx);
