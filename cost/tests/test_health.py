"""Health: collector_up, one error series per failed stage, skipped stages
not exported, degrading and non-degrading unpriced reasons, InternalError."""
import os
import unittest
from unittest import mock

import cluster_cost as cc

from . import fakes


def complete_pc_fleet(h):
    """Head, od and spot nodes: everything priced."""
    recs = fakes.fixture("aws", "pc-describe-instances.json")[:3]
    h.runner.on(("describe-instances",), fakes.ok(recs))


class CollectorUpTest(unittest.TestCase):
    def test_rules(self):
        health = cc.Health()
        names = ["discovery", "ebs", "on_demand_prices"]
        for n in names:
            health.stage(n)
        self.assertEqual(health.collector_up(names, {}), 1)
        self.assertEqual(health.collector_up(names + ["state"], {}), 0)       # a stage that didn't run
        for reason in cc.UNPRICED_REASONS:
            want = 1 if reason in ("capacity_block", "dedicated_host") else 0
            self.assertEqual(health.collector_up(names, {("instance", reason): 1}), want, reason)
        self.assertEqual(health.collector_up(names, {("volume", "volume_list_unavailable"): 0}), 1)
        health.stage("ebs").call_failed("AccessDenied", "x")
        self.assertEqual(health.collector_up(names, {}), 0)

    def test_complete_estimate(self):
        h = fakes.Harness("parallelcluster")
        try:
            complete_pc_fleet(h)
            self.assertEqual(h.run(), 0)
            m = h.metrics()
            self.assertEqual(m.value("cluster_cost_collector_up"), 1)
            self.assertEqual(m.unpriced(), {})
            self.assertEqual(m.stages_ok(), {"discovery": 1, "ebs": 1, "slurm": 1, "on_demand_prices": 1,
                                             "spot_prices": 1, "state": 1})
            self.assertEqual(m.series("cluster_cost_collector_stage_error"), [])
            self.assertEqual(m.value("cluster_cost_collector_info", platform="parallelcluster",
                                     cluster="mycluster", region="ap-southeast-6"), 1)
        finally:
            h.close()

    def test_slurm_stage_is_required(self):
        config = cc.Config(platform="pcs", cluster_id="pcs_abc123def4", region="ap-southeast-6")
        self.assertEqual(cc.applicable_stages(config, False),
                         ["discovery", "ebs", "slurm", "on_demand_prices", "spot_prices", "pcs", "state"])
        h = fakes.Harness("parallelcluster")
        try:
            complete_pc_fleet(h)
            h.runner.on(fakes.is_command("squeue"), cc.CommandResult(rc=1, stderr="squeue: error\n"))
            self.assertEqual(h.run(), 0)
            m = h.metrics()
            self.assertEqual(m.stage_errors(), {"slurm": "SlurmCommandFailed"})
            self.assertEqual(m.unpriced(), {})
            self.assertEqual(m.value("cluster_cost_collector_up"), 0)
            self.assertIsNone(m.value("cluster_cost_collector_last_success_timestamp_seconds"))
            self.assertIn("failed stages: slurm=SlurmCommandFailed", h.log())
        finally:
            h.close()

    def test_pcs_needs_the_pcs_stage(self):
        h = fakes.Harness("pcs")
        try:
            h.runner.on(("list-compute-node-groups",), fakes.aws_error("AccessDeniedException",
                                                                        "ListComputeNodeGroups"))
            h.run()
            m = h.metrics()
            self.assertEqual(m.stage_errors()["pcs"], "AccessDenied")
            self.assertEqual(m.stages_ok()["pcs"], 0)
            self.assertEqual(m.value("cluster_cost_collector_up"), 0)
            # node group IDs become the queue labels
            queues = {labels.get("queue") for labels, _ in m.series("cluster_cost_nodes")}
            self.assertIn("pcs_ng00000od1", queues)
        finally:
            h.close()


class StageSeriesTest(unittest.TestCase):
    def setUp(self):
        self.h = fakes.Harness("parallelcluster")

    def tearDown(self):
        self.h.close()

    def test_one_error_series_per_failed_stage(self):
        self.h.runner.on(("pricing",), fakes.DENIED)
        self.h.runner.on(("describe-spot-price-history",), fakes.THROTTLED)
        self.h.run()
        m = self.h.metrics()
        errors = m.series("cluster_cost_collector_stage_error")
        self.assertEqual(sorted((l["stage"], l["code"]) for l, _ in errors), [
            ("ebs", "AccessDenied"), ("on_demand_prices", "AccessDenied"), ("spot_prices", "Throttled")])
        self.assertEqual({v for _, v in errors}, {1})
        self.assertEqual(m.stages_ok(), {"discovery": 1, "ebs": 0, "slurm": 1, "on_demand_prices": 0,
                                         "spot_prices": 0, "state": 1})
        stages = self.h.report()["stages"]
        self.assertEqual(stages["on_demand_prices"]["why"], "call failed")
        self.assertTrue(stages["ebs"]["why"].startswith("source pricing backed off until "))
        self.assertEqual(stages["discovery"], {"ok": True})
        self.assertIn("failed stages: ebs=AccessDenied, on_demand_prices=AccessDenied, spot_prices=Throttled",
                      self.h.log())

    def test_skipped_stages_not_exported(self):
        self.h.runner.on(("describe-instances",), fakes.aws_error("ExpiredToken", "DescribeInstances"))
        self.h.run()
        m = self.h.metrics()
        self.assertEqual(m.stages_ok(), {"discovery": 0, "state": 1})
        self.assertEqual(m.stage_errors(), {"discovery": "CredentialsError"})
        self.assertEqual(m.value("cluster_cost_collector_up"), 0)
        self.assertIn("INFO run done: no estimate", self.h.log())

    def test_no_pcs_stage_on_parallelcluster(self):
        self.h.run()
        self.assertNotIn("pcs", self.h.metrics().stages_ok())
        self.assertEqual(self.h.runner.find("pcs"), [])

    def test_internal_error_fails_only_its_stage(self):
        with mock.patch.object(cc, "refresh_spot", side_effect=KeyError("boom")):
            self.assertEqual(self.h.run(), 0)
        m = self.h.metrics()
        self.assertEqual(m.stage_errors(), {"spot_prices": "InternalError"})
        self.assertEqual(m.stages_ok()["on_demand_prices"], 1)
        self.assertIn("ERROR stage spot_prices: internal error", self.h.log())
        self.assertIn("Traceback", self.h.log())
        self.assertEqual(self.h.report()["stages"]["spot_prices"], {"ok": False, "code": "InternalError",
                                                                    "why": "internal error"})

    def test_exception_outside_a_stage_writes_nothing(self):
        with mock.patch.object(cc, "build_cost_lines", side_effect=ValueError("boom")):
            self.assertEqual(self.h.run(), 1)
        self.assertEqual(os.listdir(self.h.scratch.textfile), [])
        self.assertEqual(sorted(os.listdir(self.h.scratch.state)), [cc.LOCK_FILE])
        self.assertIn("ERROR unexpected error outside a stage: nothing written", self.h.log())


class StageCodeTest(unittest.TestCase):
    def test_precedence(self):
        s = cc.Stage("x")
        self.assertIsNone(s.code)
        s.stored_failure("b|key", "AwsApiError")
        s.stored_failure("a|key", "InvalidResponse")
        self.assertEqual(s.code, "InvalidResponse")             # first in key order
        s.source_blocked("pricing", "Throttled", 100.0)
        s.source_blocked("pricing", "AccessDenied", 200.0)
        self.assertEqual(s.code, "Throttled")                   # the first blocking backoff
        s.call_failed("Timeout", "a")
        s.call_failed("AccessDenied", "b")
        self.assertEqual((s.code, s.call_message), ("Timeout", "a"))   # the first failed call
        s.fail("NoInstancesFound", "none")
        self.assertEqual(s.code, "NoInstancesFound")
        s.internal_error()
        self.assertEqual(s.code, "InternalError")
        self.assertIn(s.code, cc.ERROR_CODES)


if __name__ == "__main__":
    unittest.main()
