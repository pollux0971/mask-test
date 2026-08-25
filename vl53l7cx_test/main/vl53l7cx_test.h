#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

/* A07: prints the $STATUS,res=<dim>,proto=<n>,fw=<sha> handshake line
 * (CONTRACTS.md #1.1) that a host bridge uses for protocol version
 * negotiation. Called once at boot; CONTRACTS.md #1.1 also requires it be
 * resent on PING and after any SENS/MEL/switch config change -- those
 * triggers live in uart_cmd.c (A08), which should call this. */
void tof_print_status(void);

/* A05: cumulative ToF drop counters for sensor `idx` (0=A, 1=B), since
 * boot (CONTRACTS.md #1.1/#1.3, revised: drop_* shares seq's session
 * boundary, NOT reset by $STATUS -- $STATUS is re-sent on every PING, and
 * resetting drop_* there would zero the health counters on every
 * heartbeat). Out-of-range idx returns 0. Kept as two separate causes --
 * see vl53l7cx_test.c for how each is detected -- because they point at
 * different hardware problems and A06's $H line needs both summed in:
 *   notready -- sensor produced a frame we never picked up (CPU/poll rate)
 *   error    -- get_ranging_data() failed (I2C signal quality) */
uint32_t tof_get_drop_notready(size_t idx);
uint32_t tof_get_drop_error(size_t idx);

/* A06: prints one $H,<t_us>,<drop_A>,<drop_B>,<drop_M>,<heap>,<temp_c> line
 * (CONTRACTS.md #1.1). Safe to call from any task; takes uart_out_lock() itself.
 *
 * TIMING CONSTRAINT (CONTRACTS.md #1.3 "PING 回應延遲"): t_us MUST be
 * sampled AFTER uart_out_lock() returns, not on entry. */
void tof_print_heartbeat(void);

/* A16: $A (ambient) stream on/off, default disabled (CONTRACTS.md #1.1.3 /
 * #1.2 AMB:<0|1>). Applies immediately (no pending-queue like SENS's actual
 * ranging on/off) -- there is no hardware to reconfigure, this only gates
 * whether the ToF loop also prints an ambient line from data it already
 * reads every frame for $T. Callable from any task. */
void tof_set_ambient_enabled(bool on);
bool tof_ambient_enabled(void);
