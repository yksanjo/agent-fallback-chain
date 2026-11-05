# agent-fallback-chain

Circuit-breaker fallback selection across a chain of model or tool
providers, in dependency-free Python.

An agent that can call several providers needs a boring, honest answer to
"which one do I try next": try them in a fixed order, skip the ones whose
breaker is open, probe a half-open breaker only when nothing is healthy,
and back off longer after each repeated outage. This module manages that
decision and the JSON state file behind it. It never calls a provider; the
caller records outcomes.

## How it works

Config (JSON) defines the chain in preference order:

```json
{"chain": [{"name": "primary", "max_failures": 3, "cooldown_seconds": 300},
           {"name": "secondary", "max_failures": 2, "cooldown_seconds": 600}]}
```

Breaker semantics:

- Each provider starts **closed** (callable).
- `max_failures` consecutive recorded failures **open** the breaker for
  `cooldown_seconds`.
- After the cooldown the breaker is **half-open**: it yields one probe
  call, and only when no probe is already in flight.
- Selection is two passes in chain order: any closed provider beats every
  half-open one. A probe happens only when nothing is closed.
- A probe failure re-opens the breaker with a doubled cooldown (capped at
  24h). A probe success closes it and resets the backoff to the config
  base.

## Usage

```bash
# Which provider do I call now?
python3 fallback_chain.py pick --config chain.json --state state.json

# Record the outcome of the call you just made
python3 fallback_chain.py success --config chain.json --state state.json --model primary
python3 fallback_chain.py failure --config chain.json --state state.json --model primary

python3 fallback_chain.py status --config chain.json --state state.json
```

`pick` exits `1` when every breaker is open. Library API mirrors the CLI:
`pick`, `begin_call`, `record_success`, `record_failure`,
`breaker_status`, `status_report`.

`python3 demo.py` runs a synthetic three-provider chain through an outage:
failover, probing, recovery.

## Limits

- **You record the outcomes.** The tool never calls a provider and never
  measures latency, cost, or quality. "Failure" means whatever your caller
  decides to record: timeouts, 5xx, bad output. Garbage records, garbage
  breakers.
- **Chain order is static.** No scoring, no load balancing, no cheapest-
  first. If you want ranking, that is a policy layer above this.
- **Single-writer state file.** The JSON state has no locking. Two agents
  sharing one state file can lose updates; give each process its own file
  or serialize access yourself.
- **Cooldowns are wall-clock** on the machine writing the state file.
  Clock jumps confuse the math.
- A long full outage means long waits: with all breakers open, `pick`
  returns nothing until a cooldown passes. That is the intended backstop,
  not a stall to alert on silently.

## Tests

```bash
python3 -m unittest test_fallback_chain -v
```

26 tests cover config validation, open/half-open/closed transitions,
threshold and cooldown math, single-flight probing, backoff doubling and
caps, reset after success, two-pass selection, and the CLI.

## License

MIT
