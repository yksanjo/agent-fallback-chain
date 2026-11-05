#!/usr/bin/env python3
"""fallback_chain - circuit-breaker fallback selection across a chain of
model or tool providers.

An agent that can call several providers needs a honest, boring answer to
"which one do I try next": try them in a fixed order, skip the ones whose
breaker is open, probe a half-open breaker once, and back off longer after
each repeated outage. This module manages that decision and the state file
behind it. It never calls a provider; the caller records outcomes.

Config file (JSON):
    {"chain": [{"name": "alpha", "max_failures": 3, "cooldown_seconds": 300},
               {"name": "beta",  "max_failures": 2, "cooldown_seconds": 600}]}

Library usage:
    import fallback_chain as fc
    cfg = fc.load_config("chain.json")
    state = fc.load_state("state.json")
    model = fc.pick(cfg, state, now=fc.now())
    ... call model yourself ...
    state = fc.record_success(cfg, state, model)   # or record_failure
    fc.save_state("state.json", state)
"""

import argparse
import json
import sys
import time

MAX_COOLDOWN_SECONDS = 24 * 3600  # backoff cap; see README


class ChainConfigError(Exception):
    """Bad chain config."""


def now():
    return int(time.time())


def load_config(path):
    with open(path, "r", encoding="utf-8") as fh:
        try:
            cfg = json.load(fh)
        except json.JSONDecodeError as exc:
            raise ChainConfigError("config: invalid JSON: %s" % exc)
    chain = cfg.get("chain")
    if not isinstance(chain, list) or not chain:
        raise ChainConfigError("config: chain must be a non-empty list")
    seen = set()
    for i, entry in enumerate(chain):
        if not isinstance(entry, dict):
            raise ChainConfigError("config: chain entry %d must be an object" % i)
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            raise ChainConfigError("config: chain entry %d needs a name" % i)
        if name in seen:
            raise ChainConfigError("config: duplicate provider %r" % name)
        seen.add(name)
        mf = entry.get("max_failures", 3)
        cd = entry.get("cooldown_seconds", 300)
        if not isinstance(mf, int) or mf <= 0:
            raise ChainConfigError("config: %r max_failures must be a positive int"
                                   % name)
        if not isinstance(cd, (int, float)) or cd <= 0:
            raise ChainConfigError("config: %r cooldown_seconds must be positive"
                                   % name)
        entry["max_failures"], entry["cooldown_seconds"] = mf, cd
    return cfg


def _entry(cfg, model):
    for e in cfg["chain"]:
        if e["name"] == model:
            return e
    raise ChainConfigError("unknown provider %r" % model)


def load_state(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            state = json.load(fh)
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as exc:
        raise ChainConfigError("state: invalid JSON: %s" % exc)
    if not isinstance(state, dict):
        raise ChainConfigError("state: expected a JSON object")
    return state


def save_state(path, state):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, sort_keys=True)


def _slot(state, model):
    return state.setdefault(model, {"failures": 0, "cooldown": None,
                                    "open_until": None, "probing": False})


def breaker_status(cfg, state, model, at):
    """closed | open | half_open for one provider at time `at`."""
    slot = state.get(model)
    if not slot or slot.get("open_until") is None:
        return "closed"
    if at < slot["open_until"]:
        return "open"
    return "half_open"


def pick(cfg, state, at=None):
    """Provider to call now, or None when all breakers are open.

    Two passes in chain order: closed breakers first; only when every
    breaker is open or probing does a half-open provider yield a probe, and
    only when no probe is already in flight for it.
    """
    at = now() if at is None else at
    for entry in cfg["chain"]:
        if breaker_status(cfg, state, entry["name"], at) == "closed":
            return entry["name"]
    for entry in cfg["chain"]:
        name = entry["name"]
        if (breaker_status(cfg, state, name, at) == "half_open"
                and not state[name].get("probing")):
            return name
    return None


def begin_call(cfg, state, model, at=None):
    """Mark a call as starting; sets the probe flag on a half-open breaker."""
    at = now() if at is None else at
    slot = _slot(state, model)
    if breaker_status(cfg, state, model, at) == "half_open":
        slot["probing"] = True
    return state


def record_success(cfg, state, model, at=None):
    """Close the breaker and reset backoff for a provider."""
    _entry(cfg, model)
    slot = _slot(state, model)
    slot.update({"failures": 0, "cooldown": None, "open_until": None,
                 "probing": False})
    return state


def record_failure(cfg, state, model, at=None):
    """Count a failure; open the breaker at max_failures.

    Each consecutive outage doubles the cooldown, capped at
    MAX_COOLDOWN_SECONDS. A probe failure re-opens immediately.
    """
    entry = _entry(cfg, model)
    at = now() if at is None else at
    slot = _slot(state, model)
    slot["probing"] = False
    slot["failures"] = slot.get("failures", 0) + 1
    if slot["failures"] >= entry["max_failures"]:
        base = slot.get("cooldown") or entry["cooldown_seconds"]
        cooldown = min(base * (2 if slot.get("open_until") is not None else 1),
                       MAX_COOLDOWN_SECONDS)
        slot["cooldown"] = cooldown
        slot["open_until"] = at + cooldown
    return state


def status_report(cfg, state, at=None):
    """Human-readable breaker table."""
    at = now() if at is None else at
    lines = []
    for entry in cfg["chain"]:
        name = entry["name"]
        slot = state.get(name, {})
        st = breaker_status(cfg, state, name, at)
        extra = ""
        if st == "open":
            extra = " (retry after %ds)" % (slot["open_until"] - at)
        elif st == "half_open":
            extra = " (probe in flight)" if slot.get("probing") else " (probe allowed)"
        lines.append("%-16s %-9s failures=%d%s"
                     % (name, st, slot.get("failures", 0), extra))
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="fallback_chain",
        description="Circuit-breaker fallback selection over a provider chain.")
    parser.add_argument("command",
                        choices=["pick", "success", "failure", "status"])
    parser.add_argument("--config", required=True, help="chain config JSON")
    parser.add_argument("--state", required=True, help="breaker state JSON file")
    parser.add_argument("--model", default=None,
                        help="provider name (required for success/failure)")
    parser.add_argument("--at", type=int, default=None,
                        help="unix timestamp override (default: now)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    try:
        cfg = load_config(args.config)
        state = load_state(args.state)
    except (ChainConfigError, OSError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2

    if args.command == "status":
        if args.json:
            print(json.dumps({e["name"]: breaker_status(cfg, state, e["name"],
                                                        args.at or now())
                              for e in cfg["chain"]}, indent=2, sort_keys=True))
        else:
            print(status_report(cfg, state, args.at))
        return 0

    if args.command == "pick":
        model = pick(cfg, state, args.at)
        if model is None:
            print("NONE: all providers are circuit-open", file=sys.stderr)
            return 1
        state = begin_call(cfg, state, model, args.at)
        save_state(args.state, state)
        print(model)
        return 0

    if not args.model:
        print("error: --model is required for success/failure", file=sys.stderr)
        return 2
    try:
        if args.command == "success":
            state = record_success(cfg, state, args.model, args.at)
        else:
            state = record_failure(cfg, state, args.model, args.at)
    except ChainConfigError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    save_state(args.state, state)
    print(status_report(cfg, state, args.at))
    return 0


if __name__ == "__main__":
    sys.exit(main())
