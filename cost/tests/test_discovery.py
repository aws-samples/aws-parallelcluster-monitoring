"""Discovery and classification (design "Discovery and classification", the
discovery cases of AC 44)."""
import unittest

import cluster_cost as cc

from . import fakes


def by_id(report):
    return {r["instance_id"]: r for r in report["instances"]}


class ParallelClusterTest(unittest.TestCase):
    def setUp(self):
        self.h = fakes.Harness("parallelcluster")

    def tearDown(self):
        self.h.close()

    def test_roles_queues_and_options(self):
        self.assertEqual(self.h.run(), 0)
        rows = by_id(self.h.report())
        self.assertEqual({k: (r["role"], r["queue"], r["purchase_option"]) for k, r in rows.items()}, {
            "i-0a00000000000a001": ("head", None, "on_demand"),
            "i-0a00000000000a002": ("compute", "od", "on_demand"),
            "i-0a00000000000a003": ("compute", "spot", "spot"),
            "i-0a00000000000a004": ("compute", "gpu", "on_demand"),
            "i-0a00000000000a005": ("login", None, "on_demand"),
        })
        # The unknown node type is skipped with a WARNING, and not counted anywhere.
        self.assertIn("WARNING instance i-0a00000000000a006 has an unknown node type 'Bastion': skipped", self.h.log())
        m = self.h.metrics()
        self.assertEqual(m.sum("cluster_cost_nodes"), 5)
        self.assertEqual(m.value("cluster_cost_nodes", node_role="head", ec2_instance_type="t3.medium",
                                 purchase_option="on_demand"), 1)
        self.assertEqual(m.stages_ok()["discovery"], 1)

    def test_one_call_with_the_designed_filters_and_query(self):
        self.h.run()
        calls = self.h.runner.find("describe-instances")
        self.assertEqual(len(calls), 1)
        argv = calls[0].argv
        self.assertEqual(argv[:3], ["aws", "ec2", "describe-instances"])
        self.assertIn("Name=tag:parallelcluster:cluster-name,Values=mycluster", argv)
        self.assertIn("Name=instance-state-name,Values=running", argv)
        self.assertEqual(argv[argv.index("--query") + 1], cc.DISCOVERY_QUERY)
        for flag, value in (("--region", "ap-southeast-6"), ("--output", "json"),
                            ("--cli-connect-timeout", "5"), ("--cli-read-timeout", "20")):
            self.assertEqual(argv[argv.index(flag) + 1], value)

    def test_missing_queue_tag_and_missing_node_type(self):
        recs = fakes.fixture("aws", "pc-describe-instances.json")
        recs[1]["Tags"] = [t for t in recs[1]["Tags"] if t["Key"] != "parallelcluster:queue-name"]
        recs[2]["Tags"] = [t for t in recs[2]["Tags"] if t["Key"] != "parallelcluster:node-type"]
        self.h.runner.on(("describe-instances",), fakes.ok(recs))
        self.h.run()
        rows = by_id(self.h.report())
        self.assertEqual(rows["i-0a00000000000a002"]["queue"], "unknown")
        self.assertNotIn("i-0a00000000000a003", rows)

    def test_client_side_tag_check(self):
        recs = fakes.fixture("aws", "pc-describe-instances.json")
        for t in recs[3]["Tags"]:
            if t["Key"] == "parallelcluster:cluster-name":
                t["Value"] = "othercluster"
        recs[4]["Tags"] = None
        self.h.runner.on(("describe-instances",), fakes.ok(recs))
        self.h.run()
        rows = by_id(self.h.report())
        self.assertNotIn("i-0a00000000000a004", rows)
        self.assertNotIn("i-0a00000000000a005", rows)
        self.assertIn("doesn't carry the tag parallelcluster:cluster-name=mycluster", self.h.log())
        self.assertNotIn(("instance", "invalid_record"), self.h.metrics().unpriced())

    def test_zero_instances(self):
        self.h.runner.on(("describe-instances",), fakes.ok([]))
        self.assertEqual(self.h.run(), 0)
        m = self.h.metrics()
        self.assertEqual(m.stage_errors(), {"discovery": "NoInstancesFound"})
        self.assertIn("ERROR discovery failed", self.h.log())

    def test_failed_discovery_skips_steps_4_to_7(self):
        self.h.runner.on(("describe-instances",), fakes.aws_error("UnauthorizedOperation", "DescribeInstances"))
        self.assertEqual(self.h.run(), 0)
        m = self.h.metrics()
        self.assertEqual(m.stages_ok(), {"discovery": 0, "state": 1})
        self.assertEqual(m.stage_errors(), {"discovery": "AccessDenied"})
        for family in ("cluster_cost_usd_per_hour", "cluster_cost_nodes", "cluster_cost_unpriced_resources",
                       "cluster_cost_price_age_seconds"):
            self.assertEqual(m.series(family), [], family)
        self.assertEqual(m.value("cluster_cost_collector_up"), 0)
        self.assertEqual([c.op for c in self.h.runner.calls], ["ec2 describe-instances"])

    def test_record_validation(self):
        base = fakes.fixture("aws", "pc-describe-instances.json")[1]
        cases = [
            ("InstanceId", "i-XYZ", "invalid"), ("InstanceId", None, "invalid"),
            ("InstanceType", "C6I.LARGE", "invalid"), ("InstanceType", "c6ilarge", "invalid"),
            ("Az", "", "invalid"), ("Az", None, "invalid"),
            ("UsageOperation", "RunInstances:00g", "invalid"), ("UsageOperation", "Windows", "invalid"),
            ("UsageOperation", None, None), ("UsageOperation", "RunInstances:0010", None),
            ("Tenancy", "shared", "invalid"), ("Tenancy", None, None), ("Tenancy", "host", None),
        ]
        tags = cc.parse_tags(base["Tags"])
        for field, value, want in cases:
            rec = dict(base)
            rec[field] = value
            inst, why = cc.parse_instance(rec, tags, "parallelcluster")
            self.assertEqual(why, want, (field, value))
        inst, _ = cc.parse_instance(dict(base, UsageOperation=None, Tenancy=None), tags, "parallelcluster")
        self.assertEqual((inst.usage_operation, inst.tenancy), ("RunInstances", "default"))

    def test_purchase_options(self):
        base = fakes.fixture("aws", "pc-describe-instances.json")[1]
        tags = cc.parse_tags(base["Tags"])
        for lifecycle, option in (("spot", "spot"), ("capacity-block", "capacity_block"),
                                  ("scheduled", "on_demand"), (None, "on_demand")):
            inst, _ = cc.parse_instance(dict(base, Lifecycle=lifecycle), tags, "parallelcluster")
            self.assertEqual(inst.purchase_option, option, lifecycle)


class PcsTest(unittest.TestCase):
    def setUp(self):
        self.h = fakes.Harness("pcs")

    def tearDown(self):
        self.h.close()

    def test_roles_and_merge(self):
        self.assertEqual(self.h.run(), 0)
        rows = by_id(self.h.report())
        self.assertEqual({k: (r["role"], r["queue"]) for k, r in rows.items()}, {
            "i-0b00000000000a001": ("login", None),              # node group login, monitoring-role=login
            "i-0b00000000000a002": ("compute", "od"),
            "i-0b00000000000a003": ("compute", "spot"),
            "i-0b00000000000a004": ("compute", "gpu"),
            "i-0b00000000000a005": ("compute", "login-extra"),   # login group without monitoring-role
            "i-0b00000000000a006": ("compute", "pcs_ng00000cb1"),  # group not listed: its ID
            "i-0b00000000000a007": ("compute", "od"),
            "i-0b00000000000a008": ("login", None),              # standalone login node
        })
        calls = self.h.runner.find("describe-instances")
        self.assertEqual(len(calls), 2)
        self.assertIn("Name=tag:aws:pcs:cluster-id,Values=pcs_abc123def4", calls[0].argv)
        self.assertIn("Name=tag:pcs-cluster-id,Values=pcs_abc123def4", calls[1].argv)
        m = self.h.metrics()
        self.assertEqual(m.unpriced()[("instance", "invalid_record")], 1)
        self.assertEqual(m.sum("cluster_cost_nodes", node_role="login"), 2)   # merged by InstanceId

    def test_standalone_login_node_has_no_node_fee(self):
        self.h.run()
        fees = [f for f in self.h.report()["fees"] if f["component"] == "pcs_node_management"]
        managed = sorted(f["instance_id"] for f in fees)
        self.assertNotIn("i-0b00000000000a008", managed)
        self.assertIn("i-0b00000000000a001", managed)
        self.assertEqual(len(managed), 7)
        rows = by_id(self.h.report())
        self.assertIsNotNone(rows["i-0b00000000000a008"]["usd_per_hour"])
        vols = {v["instance_id"] for v in self.h.report()["volumes"]}
        self.assertIn("i-0b00000000000a008", vols)

    def test_login_node_group_from_config(self):
        # LOGIN_NODE_GROUP_ID, the monitoring login node's own node group in
        # cluster-cost.env, makes login-extra's instance a login node.
        self.h.environ["LOGIN_NODE_GROUP_ID"] = "pcs_ng0login02"
        self.assertEqual(self.h.run(), 0)
        row = by_id(self.h.report())["i-0b00000000000a005"]
        self.assertEqual((row["role"], row["queue"]), ("login", None))
        m = self.h.metrics()
        self.assertEqual(m.sum("cluster_cost_nodes", node_role="login"), 3)
        self.assertEqual(m.sum("cluster_cost_nodes", node_role="compute"), 5)
        for family in ("cluster_cost_idle_nodes", "cluster_cost_unallocated_usd_per_hour"):
            self.assertTrue(m.series(family), family)
            self.assertEqual([labels for labels, _ in m.series(family) if labels.get("queue") == "login-extra"],
                             [], family)
        fees = [f for f in self.h.report()["fees"] if f["component"] == "pcs_node_management"]
        self.assertEqual(len(fees), 7)

    def test_first_call_failing_skips_the_second(self):
        self.h.runner.on(fakes.has_arg("tag:aws:pcs:cluster-id"), fakes.aws_error("RequestLimitExceeded", "DescribeInstances"))
        self.h.run()
        self.assertEqual(len(self.h.runner.find("describe-instances")), 1)
        self.assertEqual(self.h.metrics().stage_errors(), {"discovery": "Throttled"})

    def test_second_call_failing_discards_the_first(self):
        self.h.runner.on(fakes.has_arg("tag:pcs-cluster-id"), fakes.aws_error("AuthFailure", "DescribeInstances"))
        self.h.run()
        m = self.h.metrics()
        self.assertEqual(len(self.h.runner.find("describe-instances")), 2)
        self.assertEqual(m.stage_errors(), {"discovery": "CredentialsError"})
        self.assertEqual(m.series("cluster_cost_usd_per_hour"), [])
        self.assertEqual(m.series("cluster_cost_nodes"), [])
        self.assertEqual(self.h.report()["instances"], [])
        self.assertEqual(self.h.runner.find("describe-volumes"), [])

    def test_tag_check_per_call(self):
        # A record from the second call must carry pcs-cluster-id itself.
        second = fakes.fixture("aws", "pcs-describe-instances-pcs-cluster-id.json")
        second[1]["Tags"] = [{"Key": "pcs-cluster-id", "Value": "pcs_other00001"},
                             {"Key": "monitoring-role", "Value": "login"}]
        self.h.runner.on(fakes.has_arg("tag:pcs-cluster-id"), fakes.ok(second))
        self.h.run()
        self.assertNotIn("i-0b00000000000a008", by_id(self.h.report()))

    def test_classification_rules(self):
        cases = [
            ([("aws:pcs:cluster-id", "x"), ("aws:pcs:compute-node-group-id", "g1"), ("monitoring-role", "login")],
             ("login", True)),
            ([("aws:pcs:cluster-id", "x"), ("aws:pcs:compute-node-group-id", "g1")], ("compute", True)),
            ([("pcs-cluster-id", "x"), ("monitoring-role", "login")], ("login", False)),
            ([("pcs-cluster-id", "x")], ("login", False)),
            ([("aws:pcs:cluster-id", "x"), ("aws:pcs:compute-node-group-id", "g1"), ("monitoring-role", "compute")],
             ("compute", True)),
        ]
        base = fakes.fixture("aws", "pcs-describe-instances-cluster-id.json")[1]
        for pairs, want in cases:
            tags = dict(pairs)
            inst, _ = cc.parse_instance(base, tags, "pcs")
            self.assertEqual((inst.role, inst.pcs_managed), want, pairs)
        # With the login node's own node group (LOGIN_NODE_GROUP_ID).
        group_cases = [
            ([("aws:pcs:cluster-id", "x"), ("aws:pcs:compute-node-group-id", "g1")], "g1", ("login", True)),
            ([("aws:pcs:cluster-id", "x"), ("aws:pcs:compute-node-group-id", "g1")], "g2", ("compute", True)),
            ([("pcs-cluster-id", "x")], "g1", ("login", False)),
        ]
        for pairs, login_group, want in group_cases:
            inst, _ = cc.parse_instance(base, dict(pairs), "pcs", login_group)
            self.assertEqual((inst.role, inst.pcs_managed), want, (pairs, login_group))


if __name__ == "__main__":
    unittest.main()
