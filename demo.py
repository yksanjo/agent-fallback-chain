#!/usr/bin/env python3
"""Synthetic demo: an agent can call three fictional providers in order.
The primary keeps failing, its breaker opens, traffic falls through the
chain, and the probe comes back once the cooldown passes. All providers
are fictional."""

import json
import os
import tempfile

import fallback_chain as fc

CFG = {"chain": [
    {"name": "alpha", "max_failures": 2, "cooldown_seconds": 60},
    {"name": "beta", "max_failures": 2, "cooldown_seconds": 60},
    {"name": "gamma", "max_failures": 2, "cooldown_seconds": 60},
]}

tmpdir = tempfile.mkdtemp()
cfg_path = os.path.join(tmpdir, "chain.json")
with open(cfg_path, "w") as fh:
    json.dump(CFG, fh)

cfg = fc.load_config(cfg_path)
state = {}
T = 1_700_000_000

def show(t, note):
    print("t+%-4ds %-42s pick=%s" % (t - T, note, fc.pick(cfg, state, t)))

show(T, "start: all breakers closed")
state = fc.record_failure(cfg, state, "alpha", T)
state = fc.record_failure(cfg, state, "alpha", T + 5)
show(T + 5, "alpha failed twice, breaker opens")
state = fc.record_failure(cfg, state, "beta", T + 10)
state = fc.record_failure(cfg, state, "beta", T + 15)
show(T + 15, "beta also opens, gamma takes traffic")
state = fc.record_success(cfg, state, "gamma", T + 20)
show(T + 66, "alpha cooldown passed: half-open, but gamma closed")
state = fc.record_failure(cfg, state, "gamma", T + 70)
state = fc.record_failure(cfg, state, "gamma", T + 71)
show(T + 71, "gamma opens too: alpha gets the probe")
state = fc.record_success(cfg, state, "alpha", T + 72)
show(T + 72, "probe succeeded: alpha closed, back to first choice")

print("\n" + fc.status_report(cfg, state, T + 72))
print("\nThe chain prefers order over recovery: a closed provider always")
print("beats a half-open one, and each new outage doubles the cooldown.")
