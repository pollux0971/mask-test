#pragma once

/* A07: prints the $STATUS,res=<dim>,proto=<n>,fw=<sha> handshake line
 * (CONTRACTS.md #1.1) that a host bridge uses for protocol version
 * negotiation. Called once at boot; CONTRACTS.md #1.1 also requires it be
 * resent on PING and after any SENS/MEL/switch config change -- those
 * triggers live in uart_cmd.c (A08), which should call this. */
void tof_print_status(void);
