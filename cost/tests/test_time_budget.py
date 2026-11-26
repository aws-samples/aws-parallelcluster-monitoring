"""Bounded runs (AC 31, U): real stub executables that hang, and a fake
monotonic clock for the call windows of design "Time budget"."""
import os
import time
import unittest

import cluster_cost as cc

from . import fakes


class RealStubTest(unittest.TestCase):
    """main() with the real runner and clock, and a stub `aws` on PATH that
    answers from the fixtures except for the call it hangs on."""

    def setUp(self):
        self.h = fakes.Harness("parallelcluster")
        self.log = self.h.scratch.path("stub.log")
        fakes.write_python_stub(self.h.scratch.bin, name="aws-fixtures")
        self.h.environ["PATH"] = self.h.scratch.bin + ":/usr/local/bin:/usr/bin:/bin"
        self.h.environ["COST_STUB_LOG"] = self.log

    def tearDown(self):
        self.h.close()

    def hang_on(self, word):
        fakes.write_shell_stub(self.h.scratch.bin, (
            'case " $* " in\n'
            '  *" %s "*) echo "hang $1 $2" >> "$COST_STUB_LOG"; sleep 300; echo never ;;\n'
            '  *) exec "$(dirname "$0")/aws-fixtures" "$@" ;;\n'
            'esac\n') % word)

    def run_real(self):
        deps = cc.Deps(owner_uid=self.h.owner_uid, environ=dict(self.h.environ), stderr=self.h.stderr)
        start = time.monotonic()
        rc = cc.main([], dict(self.h.environ), deps)
        return rc, time.monotonic() - start

    def hung_calls(self):
        with open(self.log) as f:
            return [line for line in f if line.startswith("hang ")]

    def test_pricing_that_sleeps_300s(self):
        self.hang_on("pricing")
        rc, elapsed = self.run_real()
        self.assertEqual(rc, 0)
        self.assertLess(elapsed, 130)
        self.assertGreaterEqual(elapsed, 29)
        text = self.h.textfile()
        self.assertIn('cluster_cost_collector_stage_error{code="Timeout",stage="on_demand_prices"} 1', text)
        self.assertEqual(len(self.hung_calls()), 1)                  # no second Pricing call
        m = self.h.metrics()
        self.assertEqual(m.stage_errors()["ebs"], "Timeout")          # EBS prices blocked by the backoff
        self.assertLess(m.value("cluster_cost_collector_run_duration_seconds"), 130)
        backoff = self.h.prices()["backoff"]["pricing"]
        self.assertEqual(backoff["code"], "Timeout")
        self.assertIn("killed after 30 s", self.h.log())

    def test_describe_instances_that_hangs(self):
        self.hang_on("describe-instances")
        rc, elapsed = self.run_real()
        self.assertEqual(rc, 0)
        self.assertLess(elapsed, 60)
        self.assertGreaterEqual(elapsed, 29)
        m = self.h.metrics()
        self.assertEqual(m.stage_errors(), {"discovery": "Timeout"})
        self.assertTrue(os.path.exists(self.h.textfile_path()))
        self.assertEqual(len(self.hung_calls()), 1)


class FakeClockTest(unittest.TestCase):
    def setUp(self):
        self.h = fakes.Harness("parallelcluster")

    def tearDown(self):
        self.h.close()

    def timeouts(self, op):
        return [c.timeout for c in self.h.runner.calls if c.op == op]

    def test_full_windows(self):
        self.h.run()
        self.assertEqual(self.timeouts("ec2 describe-instances"), [30.0])
        self.assertEqual(self.timeouts("ec2 describe-volumes"), [30.0])
        self.assertEqual(set(self.timeouts("pricing get-products")), {30.0})
        self.assertEqual(self.timeouts("ec2 describe-spot-price-history"), [30.0])

    def test_required_call_gets_the_time_left_and_optional_calls_wait(self):
        self.h.runner.on(("describe-instances",), fakes.ok(fakes.fixture_text("aws", "pc-describe-instances.json")),
                         seconds=80)
        self.assertEqual(self.h.run(), 0)
        self.assertEqual(self.timeouts("ec2 describe-volumes"), [20.0])
        self.assertEqual(self.h.runner.find("pricing"), [])
        self.assertEqual(self.h.runner.find("describe-spot-price-history"), [])
        m = self.h.metrics()
        self.assertEqual(m.stage_errors(), {})                       # deferrals leave the stages OK
        self.assertEqual(m.unpriced()[("instance", "price_lookup_pending")], 5)   # 4 On-Demand, 1 Spot
        self.assertEqual(m.value("cluster_cost_collector_up"), 0)
        self.assertIn("deferred: less than 30 s left", self.h.log())
        self.assertEqual(len(self.h.report()["deferred"]), 7)        # 3 On-Demand keys, Spot, 3 EBS families

    def test_optional_call_at_exactly_30s_left(self):
        self.h.runner.on(("describe-volumes",), fakes.ok(fakes.fixture_text("aws", "pc-describe-volumes.json")),
                         seconds=70)
        self.h.runner.on(("pricing",), lambda call: fakes.ok(fakes.stub_aws.route(call.argv)[1]), seconds=1)
        self.h.run()
        self.assertEqual(len(self.h.runner.find("pricing", "get-products")), 1)   # 30 s left: one call
        self.assertEqual(self.timeouts("pricing get-products"), [30.0])

    def test_required_call_cannot_start(self):
        self.h.runner.on(("describe-instances",), fakes.ok(fakes.fixture_text("aws", "pc-describe-instances.json")),
                         seconds=96)
        self.assertEqual(self.h.run(), 0)
        self.assertEqual(self.h.runner.find("describe-volumes"), [])
        self.assertEqual(self.h.runner.find("--json"), [])                  # scontrol and squeue are required too
        m = self.h.metrics()
        self.assertEqual(m.stage_errors(), {"ebs": "Timeout", "slurm": "Timeout"})
        self.assertIn("scontrol skipped: run deadline", self.h.log())
        self.assertEqual(m.unpriced(), {("volume", "volume_list_unavailable"): 5,
                                        ("instance", "price_lookup_pending"): 5})
        self.assertIn("ec2 describe-volumes skipped: run deadline", self.h.log())

    def test_second_pcs_discovery_call_cannot_start(self):
        self.h.close()
        self.h = fakes.Harness("pcs")
        self.h.runner.on(fakes.has_arg("tag:aws:pcs:cluster-id"),
                         fakes.ok(fakes.fixture_text("aws", "pcs-describe-instances-cluster-id.json")), seconds=97)
        self.h.run()
        m = self.h.metrics()
        self.assertEqual(m.stage_errors(), {"discovery": "Timeout"})
        self.assertEqual(m.series("cluster_cost_nodes"), [])
        self.assertEqual(len(self.h.runner.find("describe-instances")), 1)

    def test_deadline_rules(self):
        clock = fakes.FakeClock()
        d = cc.Deadline(clock)
        self.assertEqual(d.required_timeout(), 30.0)
        self.assertTrue(d.optional_allowed())
        clock.advance(70.5)
        self.assertEqual(d.required_timeout(), 29.5)
        self.assertFalse(d.optional_allowed())
        clock.advance(24.5)
        self.assertEqual(d.required_timeout(), 5.0)
        clock.advance(0.1)
        self.assertIsNone(d.required_timeout())
        self.assertEqual(cc.RUN_DEADLINE - cc.CALL_WINDOW, 20.0)


if __name__ == "__main__":
    unittest.main()
