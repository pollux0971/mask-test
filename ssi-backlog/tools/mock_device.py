#!/usr/bin/env python3
"""T04 — synthetic ESP32 mask-sensor device.

Opens a pty, prints the slave path, and streams synthetic
$T/$M/$F/$H/$STATUS lines (protocol v2, see CONTRACTS.md chapter 1) or,
with --proto v1, the
legacy $TOF/$MIC/$STATUS lines that the current, unmodified
vl53l7cx_test/monitor/bridge_server.py already parses.

--proto v1 exists so the mock can be pointed at bridge_server.py *today*,
before B01 upgrades the bridge to speak v2 -- and it doubles as the fixture
B02 (v1/v2 dual-protocol compatibility) will need later. Both modes share
the same synthetic scenario/fault engine; only the line-formatting layer
differs.

The v1 wire format replays the pre-T01 firmware exactly (baseline commit
fb286d1, vl53l7cx_test/main/):

    $TOF,<A|B>,<side 4|8>,<side^2 distances>   # side, not zone count; no signal
    $MIC,<rms %.1f>,<peak int>                 # rms was a float back then
    $STATUS,res=<side>                         # no proto=, no fw=, printed once

There is no $H and no $F in v1, and PING/SENS/MEL are ignored rather than
answered (see resend_status). Anything else would be a format no firmware
has ever spoken.

$H's bw_bytes_since_last (A15) is counted here in _write, so it covers
*every* byte the mock puts on the wire. The real firmware currently only
accounts for $T/$STATUS/$H -- $M, $F and the recording dump are not wired
into its counter yet -- so on real hardware that field is a LOWER BOUND,
not the total. Expect the mock to report a higher (and more accurate)
utilisation than a board running the same configuration; that gap is the
firmware's missing call sites, not a bug in either side. Do not spend an
afternoon debugging why the two disagree.

Usage:
    python3 mock_device.py --fps 30 --dim 4 --scenario round --drop-rate 0.02
    # -> prints "[mock_device] pty ready: /dev/pts/N", then:
    python3 monitor/bridge_server.py --port /dev/pts/N

Fault injection (all optional, off by default):
    --drop-rate 0.05          ~5% of scheduled $T/$M lines are silently
                               skipped (seq still advances, so the gap is
                               visible on the host side)
    --invalid-zone-rate 0.1   ~10% of ToF zones per frame report -1/-1
    --fault clock-jump        every --clock-jump-interval seconds, t_us
                               jumps by a random +/- --clock-jump-max-ms
    --fault disconnect        after --disconnect-after seconds, the pty is
                               closed and the process exits (simulates an
                               unplugged board)
    --fault clock-jump,disconnect   both, comma-separated

$F (Mel) streaming is off at boot (--mel 1, or MEL:1 at runtime, turns it
on) and $STATUS's mel= field always reflects the current state. The 40
bands are a synthetic formant SHAPE, not speech -- see MelModel. Turning it
on costs ~15.6 KB/s (#1.4), which bw_bytes_since_last will show: 4x4@30Hz
goes from roughly 25% to 60% of the 460800-baud budget.
"""

import argparse
import math
import os
import pty
import random
import select
import sys
import time

PROTO_VERSION = 2

# $STATUS self-description fields (CONTRACTS.md #1.1.2). Fixed rather than
# CLI-configurable: they describe the firmware the host should expect to be
# talking to. mel_hop is 256 -- the post-A14 value, matching the current
# firmware -- and the $F emission rate is DERIVED from these two constants
# (16000/256 = 62.5 Hz), so what the mock advertises and what it actually
# sends can never drift apart.
AUDIO_SR_HZ = 16000
MEL_WIN_SAMPLES = 512
MEL_HOP_SAMPLES = 256
MIC_HOP_SAMPLES = 512


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--proto", choices=["v1", "v2"], default="v2",
                   help="wire format: v2 (default, CONTRACTS.md ch.1) or v1 (legacy $TOF/$MIC, for unmodified bridge_server.py / B02)")
    p.add_argument("--fps", type=float, default=30.0, help="ToF frames/sec per sensor (default 30)")
    p.add_argument("--mic-fps", type=float, default=20.0, help="mic stat frames/sec (default 20)")
    p.add_argument("--heartbeat-interval", type=float, default=1.0, help="seconds between $H lines (default 1.0)")
    p.add_argument("--mel", choices=["0", "1"], default="0",
                   help="stream $F Mel frames at boot (default 0 = off, matching the "
                        "firmware's own default). MEL:1 turns it on at runtime either way.")
    p.add_argument("--dim", type=int, choices=[4, 8], default=4, help="grid side: 4 (16 zones) or 8 (64 zones)")
    p.add_argument("--scenario", choices=["idle", "round", "spread", "random"], default="idle",
                   help="synthetic lip-shape pattern (default idle)")
    p.add_argument("--drop-rate", type=float, default=0.0, help="probability [0,1] a scheduled $T/$M line is dropped")
    p.add_argument("--invalid-zone-rate", type=float, default=0.0, help="probability [0,1] a ToF zone reports -1/-1")
    p.add_argument("--fault", default="", help="comma-separated: clock-jump, disconnect")
    p.add_argument("--clock-jump-interval", type=float, default=5.0, help="seconds between clock-jump events")
    p.add_argument("--clock-jump-max-ms", type=float, default=500.0, help="max |jump| in ms")
    p.add_argument("--disconnect-after", type=float, default=15.0, help="seconds before the disconnect fault fires")
    p.add_argument("--fw-sha", default=None, help="fake git short sha for $STATUS (default: random 7 hex chars)")
    p.add_argument("--seed", type=int, default=None, help="RNG seed for reproducible runs")
    return p.parse_args()


# ---------------------------------------------------------------------------
# Zone geometry: dim=16 -> 4x4 grid, dim=64 -> 8x8 grid.

def zone_weights(dim):
    side = int(round(math.sqrt(dim)))
    center = (side - 1) / 2.0
    max_dist = math.hypot(center, center)
    center_w, edge_w = [], []
    for z in range(dim):
        row, col = divmod(z, side)
        dist = math.hypot(row - center, col - center)
        cw = 1.0 - (dist / max_dist if max_dist else 0.0)
        center_w.append(cw)
        edge_w.append(1.0 - cw)
    return center_w, edge_w


def bump(tp, dur=0.4):
    """Half-sine pulse: 1 at tp=dur/2, 0 at tp<=0 or tp>=dur."""
    if tp < 0 or tp > dur:
        return 0.0
    return math.sin(math.pi * tp / dur)


PERIOD = 2.0  # seconds between synthetic "utterances"


class Scenario:
    """Per-zone ToF distance model. idle/round/spread match the shapes
    sketched in T04.md; random re-rolls a round/spread/none mix each period
    from a period-indexed RNG so repeated runs with the same --seed replay
    identically."""

    BASELINE_MM = 17.0

    def __init__(self, name, dim, rng):
        self.name = name
        self.center_w, self.edge_w = zone_weights(dim)
        self.rng = rng

    def delta(self, t, zone):
        tp = t % PERIOD
        b = bump(tp)
        cw, ew = self.center_w[zone], self.edge_w[zone]
        if self.name == "idle":
            return 0.0
        if self.name == "round":
            return -3.0 * b * cw
        if self.name == "spread":
            return 1.2 * b * ew
        if self.name == "random":
            period_idx = int(t // PERIOD)
            prng = random.Random((id(self), period_idx))
            kind = prng.choice(["round", "spread", "none"])
            amp = prng.uniform(0.5, 3.0)
            if kind == "round":
                return -amp * b * cw
            if kind == "spread":
                return amp * b * ew
            return 0.0
        return 0.0

    def distance_mm(self, t, zone, rng):
        noise = rng.gauss(0, 0.15)
        val = self.BASELINE_MM + self.delta(t, zone) + noise
        return max(0.0, min(4000.0, val))

    def signal(self, distance_mm, rng):
        val = 140.0 - 6.0 * (distance_mm - self.BASELINE_MM) + rng.gauss(0, 5)
        return int(max(1, min(250, round(val))))


class MelModel:
    """Synthetic 40-band log-mel frames for $F (CONTRACTS.md #1.1 / #3.1).

    This is a *shape*, not speech. No FFT runs here and no audio exists to
    transform -- the bands are drawn from a formant-like model driven by the
    same --scenario / bump() envelope that moves the ToF distances, so a
    frame's Mel energy rises and falls in step with the synthetic "utterance"
    the ToF grid is showing. That coupling is the point: C10's PCA trajectory
    and C08's waterfall need structure that correlates across modalities, and
    pure noise would show none. Do not treat these numbers as acoustically
    meaningful -- they will not match reference_mel.py on any real audio.

    Wire encoding is #3.1's: int16 = round(log_mel * 100), log_mel being
    log10(max(power, 1e-10)), so the value range is [-1000, 0].
    """

    N_MELS = 40
    NOISE_FLOOR_LOG = -6.0      # quiet room, well above the 1e-10 clamp
    PEAK_LOG = -1.2             # loud band during an utterance

    # Formant-ish band centres per scenario. "round" (lip-rounded /u/-like)
    # keeps energy low; "spread" (/i/-like) pushes the second formant up.
    FORMANTS = {
        "idle": [],
        "round": [(3, 2.2, 1.0), (7, 2.8, 0.72)],
        "spread": [(2, 1.8, 0.95), (21, 4.5, 0.85), (30, 5.0, 0.35)],
    }

    def __init__(self, rng):
        self.rng = rng

    def _shape(self, name, t):
        if name == "random":
            # Seed with a str, not a tuple: random.Random() rejects tuples.
            # (Scenario.delta has the same pattern with a tuple and raises --
            #  see the completion report; not fixed here, it is not this
            #  story's file section.)
            prng = random.Random(f"mel-{int(t // PERIOD)}")
            name = prng.choice(["round", "spread", "idle"])
        return self.FORMANTS.get(name, [])

    def frame(self, t, scenario_name):
        """Return 40 int16 values for the $F line at time `t`."""
        env = bump(t % PERIOD)
        bands = []
        for band in range(self.N_MELS):
            # Gentle spectral tilt: high bands sit a little below low ones
            # even in silence, like a real noise floor.
            value = self.NOISE_FLOOR_LOG - 0.9 * (band / (self.N_MELS - 1))
            # Formants combine by max(), not by sum: two overlapping peaks
            # summing linearly would push bands past PEAK_LOG and clip at the
            # 0 ceiling, flattening the very structure C10/C08 need to see.
            gain = 0.0
            for centre, width, weight in self._shape(scenario_name, t):
                gain = max(gain, weight * math.exp(-0.5 * ((band - centre) / width) ** 2))
            value += (self.PEAK_LOG - self.NOISE_FLOOR_LOG) * env * gain
            value += self.rng.gauss(0, 0.06)
            value = max(-10.0, min(0.0, value))
            bands.append(int(round(value * 100)))
        return bands


class MicModel:
    def __init__(self, rng):
        self.rng = rng

    def sample(self, t):
        b = bump(t % PERIOD)
        rms = 300 + 800 * b + self.rng.gauss(0, 30)
        rms = max(0.0, min(32767.0, rms))
        peak = max(rms, min(32767.0, rms * 1.6 + self.rng.gauss(0, 40)))
        return rms, int(round(peak))


class DropClockFaults:
    def __init__(self, args, rng):
        self.rng = rng
        self.drop_rate = args.drop_rate
        self.invalid_zone_rate = args.invalid_zone_rate
        faults = {f.strip() for f in args.fault.split(",") if f.strip()}
        self.clock_jump = "clock-jump" in faults
        self.disconnect = "disconnect" in faults
        self.disconnect_after = args.disconnect_after
        self.jump_interval = args.clock_jump_interval
        self.jump_max_ms = args.clock_jump_max_ms
        self.clock_offset_us = 0
        self._next_jump = time.monotonic() + self.jump_interval if self.clock_jump else None

    def should_drop(self):
        return self.rng.random() < self.drop_rate

    def zone_invalid(self):
        return self.rng.random() < self.invalid_zone_rate

    def maybe_jump(self, now):
        if not self.clock_jump or now < self._next_jump:
            return
        jump_ms = self.rng.uniform(-self.jump_max_ms, self.jump_max_ms)
        self.clock_offset_us += int(jump_ms * 1000)
        print(f"[mock_device] fault: clock-jump {jump_ms:+.1f} ms (offset now {self.clock_offset_us} us)",
              file=sys.stderr)
        self._next_jump = now + self.jump_interval

    def disconnect_due(self, elapsed):
        return self.disconnect and elapsed >= self.disconnect_after


class MockDevice:
    def __init__(self, args):
        self.args = args
        self.rng = random.Random(args.seed)
        self.dim = args.dim * args.dim
        self.scenario = Scenario(args.scenario, self.dim, self.rng)
        self.mic = MicModel(self.rng)
        self.faults = DropClockFaults(args, self.rng)
        self.fw_sha = args.fw_sha or "".join(self.rng.choice("0123456789abcdef") for _ in range(7))

        # A15 bw_bytes_since_last: bytes written since the last $H went out.
        # Reset inside emit_heartbeat, so the counter always describes exactly
        # the interval between two consecutive $H lines.
        self.bw_bytes = 0

        self.sensor_enabled = {"A": True, "B": True}
        # Default off, matching the firmware's own default -- and $STATUS says
        # mel=0 at boot, which the host relies on. MEL:1 (or --mel 1) turns it on.
        self.mel_enabled = (args.mel == "1")
        self.mel = MelModel(self.rng)

        # $F has its OWN seq (CONTRACTS.md #1.1.1: four independent streams).
        # It must not be shared with $M -- they run at different rates, so any
        # fixed relationship between the two counters would be a lie.
        self.seq = {"A": 0, "B": 0, "M": 0, "F": 0}
        self.drop = {"A": 0, "B": 0, "M": 0}

        self.master_fd, self.slave_fd = pty.openpty()
        self.port_path = os.ttyname(self.slave_fd)
        self._running = True
        self._t0 = time.monotonic()

    def now_us(self):
        return int((time.monotonic() - self._t0) * 1e6) + self.faults.clock_offset_us

    # -- line formatting -----------------------------------------------

    def _write(self, line):
        payload = (line + "\n").encode("ascii")
        try:
            os.write(self.master_fd, payload)
        except OSError as exc:
            print(f"[mock_device] write failed, treating as disconnect: {exc}", file=sys.stderr)
            self._running = False
            return
        # A15's bw_bytes_since_last: every byte that actually made it onto the
        # wire, counted at the single choke point all output goes through.
        self.bw_bytes += len(payload)

    def emit_status(self):
        if self.args.proto == "v2":
            self._write(
                f"$STATUS,res={self.args.dim},proto={PROTO_VERSION},fw={self.fw_sha}"
                f",sr={AUDIO_SR_HZ},mel={1 if self.mel_enabled else 0}"
                f",mel_win={MEL_WIN_SAMPLES},mel_hop={MEL_HOP_SAMPLES}"
                f",mic_hop={MIC_HOP_SAMPLES}"
            )
        else:
            self._write(f"$STATUS,res={self.args.dim}")

    def resend_status(self):
        """Re-send $STATUS after a PING or a config change (CONTRACTS.md #1.1).

        v2 only. --proto v1 emulates the unmodified pre-A09 firmware, whose
        uart_cmd.c understood REC: and nothing else -- it ignored PING/SENS/
        MEL rather than answering them. Re-sending here would make v1 mode
        behave like firmware that does not exist, which defeats the point of
        having it (B02's dual-protocol compatibility fixture).
        """
        if self.args.proto == "v2":
            self.emit_status()

    def emit_tof(self, sensor, t):
        if self.faults.should_drop():
            self.drop[sensor] += 1
            self.seq[sensor] += 1
            return
        d_vals, s_vals = [], []
        for z in range(self.dim):
            if self.faults.zone_invalid():
                d_vals.append(-1)
                s_vals.append(-1)
                continue
            d_mm = self.scenario.distance_mm(t, z, self.rng)
            d_vals.append(int(round(d_mm)))
            s_vals.append(self.scenario.signal(d_mm, self.rng))

        seq = self.seq[sensor]
        self.seq[sensor] += 1
        t_us = self.now_us()
        if self.args.proto == "v2":
            vals = ",".join(str(v) for v in d_vals + s_vals)
            self._write(f"$T,{sensor},{seq},{t_us},{self.dim},{vals}")
        else:
            # v1 is a faithful replay of the pre-T01 firmware, not a stripped
            # -down v2. That firmware (fb286d1:vl53l7cx_test/main/
            # vl53l7cx_test.c:135) printed the grid *side* (4|8, i.e.
            # TOF_GRID_DIM) and distances only -- signal_per_spad was never on
            # the v1 wire at all. Emitting zone-count + signal here made v1
            # mode a format no firmware has ever spoken, which defeats the
            # point of having it: anyone using --proto v1 as the reference for
            # "what the old firmware sends" would be misled.
            vals = ",".join(str(v) for v in d_vals)
            self._write(f"$TOF,{sensor},{self.args.dim},{vals}")

    def emit_mic(self, t):
        if self.faults.should_drop():
            self.drop["M"] += 1
            self.seq["M"] += 1
            return
        rms, peak = self.mic.sample(t)
        seq = self.seq["M"]
        self.seq["M"] += 1
        t_us = self.now_us()
        if self.args.proto == "v2":
            self._write(f"$M,{seq},{t_us},{int(round(rms))},{peak}")
        else:
            self._write(f"$MIC,{rms:.1f},{peak}")

    def emit_mel(self, t):
        """$F,<seq>,<t_us>,<m0>..<m39> -- 40 int16 log-mel*100 values (#3.1).

        Not dropped by --drop-rate: $H carries drop_A/drop_B/drop_M and has no
        drop_F counter, so a dropped $F would be invisible except as a seq gap.
        Inventing a fault mode the firmware may not have -- and that the host
        has no counter to cross-check against -- is how a fixture stops being
        a faithful stand-in. If A-track adds drop_F to $H, revisit this.
        """
        if not self.mel_enabled or self.args.proto != "v2":
            return
        seq = self.seq["F"]
        self.seq["F"] += 1
        t_us = self.now_us()
        bands = ",".join(str(v) for v in self.mel.frame(t, self.args.scenario))
        self._write(f"$F,{seq},{t_us},{bands}")

    def emit_heartbeat(self, t):
        t_us = self.now_us()
        heap = int(150000 + 20000 * math.sin(t / 37.0) + self.rng.gauss(0, 500))
        temp_c = int(round(38 + 6 * math.sin(t / 97.0) + self.rng.gauss(0, 0.5)))
        temp_c = max(-40, min(125, temp_c))
        if self.args.proto == "v2":
            # Snapshot and reset before writing: the $H line itself belongs to
            # the *next* interval, exactly like the firmware's counter.
            bw = self.bw_bytes
            self.bw_bytes = 0
            self._write(
                f"$H,{t_us},{self.drop['A']},{self.drop['B']},{self.drop['M']}"
                f",{heap},{temp_c},{bw}"
            )
        # v1 firmware never sent $H; skip it in v1 mode to match the legacy stream exactly.

    # -- host -> device commands -----------------------------------------

    def handle_command(self, line):
        line = line.strip()
        if not line:
            return
        if line == "PING":
            # $H first, then $STATUS: CONTRACTS.md #1.1 requires a $STATUS
            # re-send on every PING, but $H is the reply B05 times its clock
            # alignment against, so anything ahead of it is added latency.
            # The A09 firmware emits them in this order; the mock has to
            # match it or the host sees a different stream than the board.
            self.emit_heartbeat(time.monotonic() - self._t0)
            self.resend_status()
            return
        if line.startswith("SENS:"):
            # Validated as strictly as the firmware validates it (A08's
            # uart_cmd.c rejects anything that is not exactly A|B and 0|1).
            # Without the membership checks "SENS:C=1" would quietly invent a
            # third sensor here and answer with a $STATUS, which no real
            # board would ever do.
            try:
                sensor, val = line[len("SENS:"):].split("=")
            except ValueError:
                return
            if sensor not in self.sensor_enabled or val not in ("0", "1"):
                print(f"[mock_device] ignoring malformed SENS: {line}", file=sys.stderr)
                return
            self.sensor_enabled[sensor] = (val == "1")
            print(f"[mock_device] SENS {sensor}={val}", file=sys.stderr)
            self.resend_status()  # output config changed (CONTRACTS.md #1.1)
            return
        if line.startswith("MEL:"):
            # Same strictness as SENS above: endswith("1") alone would take
            # "MEL:garbage1" as an enable and answer with a $STATUS claiming
            # mel=1, which no real board would do.
            val = line[len("MEL:"):]
            if val not in ("0", "1"):
                print(f"[mock_device] ignoring malformed MEL: {line}", file=sys.stderr)
                return
            self.mel_enabled = (val == "1")
            print(f"[mock_device] MEL={val} ($F streaming {'on' if self.mel_enabled else 'off'})",
                  file=sys.stderr)
            self.resend_status()  # output config changed (CONTRACTS.md #1.1)
            return
        if line.startswith("REC:"):
            print(f"[mock_device] REC ignored (WAV dump simulation out of T04 scope): {line}", file=sys.stderr)
            return

    def _drain_commands(self):
        try:
            while True:
                r, _, _ = select.select([self.master_fd], [], [], 0)
                if not r:
                    return
                chunk = os.read(self.master_fd, 4096)
                if not chunk:
                    return
                self._cmd_buf = getattr(self, "_cmd_buf", "") + chunk.decode("ascii", errors="replace")
                while "\n" in self._cmd_buf:
                    line, self._cmd_buf = self._cmd_buf.split("\n", 1)
                    self.handle_command(line)
        except OSError:
            pass

    # -- main loop --------------------------------------------------------

    def run(self):
        print(f"[mock_device] pty ready: {self.port_path}", file=sys.stdout)
        print(f"[mock_device] proto={self.args.proto} dim={self.args.dim}x{self.args.dim} "
              f"scenario={self.args.scenario} drop_rate={self.args.drop_rate} "
              f"invalid_zone_rate={self.args.invalid_zone_rate} fault={self.args.fault or 'none'}",
              file=sys.stdout)
        sys.stdout.flush()

        self.emit_status()

        now0 = time.monotonic()
        next_a = next_b = now0
        next_m = now0
        next_f = now0
        next_h = now0
        tof_period = 1.0 / self.args.fps
        mic_period = 1.0 / self.args.mic_fps
        # Derived from the very constants $STATUS advertises, not from a
        # separate CLI knob: 16000/256 = 62.5 Hz. If the mock streamed $F at
        # some other rate while telling the host mel_hop=256, B06 would
        # compute the wrong frame spacing and the mock would be lying about
        # itself -- exactly the class of bug #1.1.2 was added to prevent.
        mel_period = MEL_HOP_SAMPLES / AUDIO_SR_HZ

        try:
            while self._running:
                now = time.monotonic()
                t = now - self._t0

                self.faults.maybe_jump(now)
                if self.faults.disconnect_due(t):
                    print("[mock_device] fault: disconnect", file=sys.stderr)
                    break

                if self.sensor_enabled["A"] and now >= next_a:
                    self.emit_tof("A", t)
                    next_a += tof_period
                if self.sensor_enabled["B"] and now >= next_b:
                    self.emit_tof("B", t)
                    next_b += tof_period
                if now >= next_m:
                    self.emit_mic(t)
                    next_m += mic_period
                if now >= next_f:
                    self.emit_mel(t)
                    next_f += mel_period
                    if next_f < now:
                        # Fell behind -- either the loop was slow, or $F was
                        # off for a while (next_f is excluded from `due` then,
                        # so it only advances one period per iteration). Snap
                        # forward instead of catching up: a backlog would come
                        # out as a burst of frames with bunched-up t_us, which
                        # is not something a real device ever does.
                        next_f = now + mel_period
                if now >= next_h:
                    self.emit_heartbeat(t)
                    next_h += self.args.heartbeat_interval

                self._drain_commands()

                due = min(next_a if self.sensor_enabled["A"] else now + 1,
                          next_b if self.sensor_enabled["B"] else now + 1,
                          next_m,
                          next_f if self.mel_enabled else now + 1,
                          next_h)
                time.sleep(max(0.0, min(0.02, due - time.monotonic())))
        except KeyboardInterrupt:
            pass
        finally:
            self.shutdown()

    def shutdown(self):
        print(f"[mock_device] stopping. seq A={self.seq['A']} B={self.seq['B']} "
              f"M={self.seq['M']} F={self.seq['F']} "
              f"drop A={self.drop['A']} B={self.drop['B']} M={self.drop['M']}", file=sys.stderr)
        for fd in (self.slave_fd, self.master_fd):
            try:
                os.close(fd)
            except OSError:
                pass


def main():
    args = parse_args()
    device = MockDevice(args)
    device.run()


if __name__ == "__main__":
    main()
