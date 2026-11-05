import json
import os
import subprocess
import sys
import tempfile
import unittest

import fallback_chain as fc

T0 = 1_700_000_000
CFG = {"chain": [
    {"name": "alpha", "max_failures": 2, "cooldown_seconds": 100},
    {"name": "beta", "max_failures": 1, "cooldown_seconds": 50},
    {"name": "gamma", "max_failures": 5, "cooldown_seconds": 300},
]}


def write_tmp(obj):
    fd, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w") as fh:
        fh.write(obj if isinstance(obj, str) else json.dumps(obj))
    return path


class ConfigTests(unittest.TestCase):
    def test_roundtrip_defaults(self):
        path = write_tmp({"chain": [{"name": "x"}]})
        cfg = fc.load_config(path)
        os.unlink(path)
        self.assertEqual(cfg["chain"][0]["max_failures"], 3)
        self.assertEqual(cfg["chain"][0]["cooldown_seconds"], 300)

    def test_empty_chain_rejected(self):
        path = write_tmp({"chain": []})
        with self.assertRaises(fc.ChainConfigError):
            fc.load_config(path)
        os.unlink(path)

    def test_duplicate_name_rejected(self):
        path = write_tmp({"chain": [{"name": "x"}, {"name": "x"}]})
        with self.assertRaises(fc.ChainConfigError):
            fc.load_config(path)
        os.unlink(path)

    def test_bad_numbers_rejected(self):
        for bad in ({"name": "x", "max_failures": 0},
                    {"name": "x", "cooldown_seconds": -5}):
            path = write_tmp({"chain": [bad]})
            with self.assertRaises(fc.ChainConfigError):
                fc.load_config(path)
            os.unlink(path)

    def test_invalid_json_rejected(self):
        path = write_tmp("{oops")
        with self.assertRaises(fc.ChainConfigError):
            fc.load_config(path)
        os.unlink(path)

    def test_missing_state_file_is_empty(self):
        self.assertEqual(fc.load_state("/nonexistent-state.json"), {})

    def test_unknown_provider_rejected(self):
        path = write_tmp(CFG)
        cfg = fc.load_config(path)
        os.unlink(path)
        with self.assertRaises(fc.ChainConfigError):
            fc.record_failure(cfg, {}, "nope", T0)


class BreakerTests(unittest.TestCase):
    def setUp(self):
        path = write_tmp(CFG)
        self.cfg = fc.load_config(path)
        os.unlink(path)

    def test_pick_order(self):
        self.assertEqual(fc.pick(self.cfg, {}, T0), "alpha")

    def test_closed_until_threshold(self):
        state = fc.record_failure(self.cfg, {}, "alpha", T0)
        self.assertEqual(fc.breaker_status(self.cfg, state, "alpha", T0), "closed")
        self.assertEqual(fc.pick(self.cfg, state, T0), "alpha")

    def test_opens_at_threshold_and_falls_through(self):
        state = fc.record_failure(self.cfg, {}, "alpha", T0)
        state = fc.record_failure(self.cfg, state, "alpha", T0 + 1)
        self.assertEqual(fc.breaker_status(self.cfg, state, "alpha", T0 + 2), "open")
        self.assertEqual(fc.pick(self.cfg, state, T0 + 2), "beta")

    def test_all_open_returns_none(self):
        state = fc.record_failure(self.cfg, {}, "alpha", T0)
        state = fc.record_failure(self.cfg, state, "alpha", T0)
        state = fc.record_failure(self.cfg, state, "beta", T0)
        state = fc.record_failure(self.cfg, state, "gamma", T0)
        state = fc.record_failure(self.cfg, state, "gamma", T0)
        state = fc.record_failure(self.cfg, state, "gamma", T0)
        state = fc.record_failure(self.cfg, state, "gamma", T0)
        state = fc.record_failure(self.cfg, state, "gamma", T0)
        self.assertIsNone(fc.pick(self.cfg, state, T0 + 1))

    def test_half_open_after_cooldown(self):
        state = fc.record_failure(self.cfg, {}, "beta", T0)  # max_failures=1
        self.assertEqual(fc.breaker_status(self.cfg, state, "beta", T0 + 49), "open")
        self.assertEqual(fc.breaker_status(self.cfg, state, "beta", T0 + 50),
                         "half_open")

    def test_probe_single_flight(self):
        # alpha open until T0+100, beta open until T0+50, gamma open until
        # T0+300: everything open, beta alone is half-open
        state = fc.record_failure(self.cfg, {}, "alpha", T0)
        state = fc.record_failure(self.cfg, state, "alpha", T0)
        state = fc.record_failure(self.cfg, state, "beta", T0)
        for _ in range(5):
            state = fc.record_failure(self.cfg, state, "gamma", T0)
        at = T0 + 60
        self.assertEqual(fc.pick(self.cfg, state, at), "beta")
        state = fc.begin_call(self.cfg, state, "beta", at)
        # second pick while the probe is in flight finds nothing
        self.assertIsNone(fc.pick(self.cfg, state, at))

    def test_closed_anywhere_beats_half_open(self):
        # alpha open until T0+100, beta half-open at T0+60, gamma closed:
        # the closed gamma wins over the half-open beta
        state = fc.record_failure(self.cfg, {}, "alpha", T0)
        state = fc.record_failure(self.cfg, state, "alpha", T0)
        state = fc.record_failure(self.cfg, state, "beta", T0)
        self.assertEqual(fc.pick(self.cfg, state, T0 + 60), "gamma")

    def test_pick_prefers_closed_over_half_open(self):
        state = fc.record_failure(self.cfg, {}, "beta", T0)
        # beta is half-open but alpha is closed and earlier in the chain
        self.assertEqual(fc.pick(self.cfg, state, T0 + 60), "alpha")

    def test_probe_success_closes(self):
        state = fc.record_failure(self.cfg, {}, "beta", T0)
        state = fc.begin_call(self.cfg, state, "beta", T0 + 60)
        state = fc.record_success(self.cfg, state, "beta", T0 + 61)
        self.assertEqual(fc.breaker_status(self.cfg, state, "beta", T0 + 61),
                         "closed")
        self.assertEqual(state["beta"]["failures"], 0)

    def test_probe_failure_reopens_with_double_cooldown(self):
        state = fc.record_failure(self.cfg, {}, "beta", T0)  # open until T0+50
        state = fc.record_failure(self.cfg, state, "beta", T0 + 60)
        self.assertEqual(state["beta"]["cooldown"], 100)
        self.assertEqual(state["beta"]["open_until"], T0 + 60 + 100)
        state = fc.record_failure(self.cfg, state, "beta", T0 + 200)
        self.assertEqual(state["beta"]["cooldown"], 200)

    def test_cooldown_capped(self):
        state = {"beta": {"failures": 99, "cooldown": 2 ** 40,
                          "open_until": T0, "probing": False}}
        state = fc.record_failure(self.cfg, state, "beta", T0 + 1)
        self.assertEqual(state["beta"]["cooldown"], fc.MAX_COOLDOWN_SECONDS)

    def test_success_resets_backoff(self):
        state = fc.record_failure(self.cfg, {}, "beta", T0)
        state = fc.record_failure(self.cfg, state, "beta", T0 + 60)
        state = fc.record_success(self.cfg, state, "beta", T0 + 200)
        state = fc.record_failure(self.cfg, state, "beta", T0 + 300)
        self.assertEqual(state["beta"]["cooldown"], 50)  # back to config base


class CliTests(unittest.TestCase):
    def setUp(self):
        self.cfg_path = write_tmp(CFG)
        fd, self.state_path = tempfile.mkstemp()
        os.close(fd)
        os.unlink(self.state_path)  # start absent

    def tearDown(self):
        os.unlink(self.cfg_path)
        if os.path.exists(self.state_path):
            os.unlink(self.state_path)

    def run_cli(self, *args):
        return subprocess.run([sys.executable, "fallback_chain.py", *args,
                               "--config", self.cfg_path,
                               "--state", self.state_path],
                              capture_output=True, text=True)

    def test_pick_first(self):
        r = self.run_cli("pick", "--at", str(T0))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "alpha")

    def test_failure_then_fallback(self):
        self.run_cli("failure", "--model", "alpha", "--at", str(T0))
        self.run_cli("failure", "--model", "alpha", "--at", str(T0 + 1))
        r = self.run_cli("pick", "--at", str(T0 + 2))
        self.assertEqual(r.stdout.strip(), "beta")

    def test_all_open_exit_1(self):
        self.run_cli("failure", "--model", "beta", "--at", str(T0))
        self.run_cli("failure", "--model", "alpha", "--at", str(T0))
        self.run_cli("failure", "--model", "alpha", "--at", str(T0))
        for i in range(5):
            self.run_cli("failure", "--model", "gamma", "--at", str(T0))
        r = self.run_cli("pick", "--at", str(T0 + 1))
        self.assertEqual(r.returncode, 1)
        self.assertIn("NONE", r.stderr)

    def test_status_report(self):
        self.run_cli("failure", "--model", "beta", "--at", str(T0))
        r = self.run_cli("status", "--at", str(T0 + 10))
        self.assertEqual(r.returncode, 0)
        self.assertIn("beta", r.stdout)
        self.assertIn("open", r.stdout)
        self.assertIn("retry after 40s", r.stdout)

    def test_status_json(self):
        r = self.run_cli("status", "--at", str(T0), "--json")
        data = json.loads(r.stdout)
        self.assertEqual(data["alpha"], "closed")

    def test_missing_model_flag_exit_2(self):
        r = self.run_cli("failure")
        self.assertEqual(r.returncode, 2)

    def test_missing_config_exit_2(self):
        r = subprocess.run([sys.executable, "fallback_chain.py", "status",
                            "--config", "/nonexistent.json",
                            "--state", self.state_path],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)


if __name__ == "__main__":
    unittest.main()
