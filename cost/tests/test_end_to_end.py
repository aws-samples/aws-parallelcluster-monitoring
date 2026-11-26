"""End to end: main() with a stub `aws` executable on PATH, the real runner
and a scratch textfile directory, for a ParallelCluster and a PCS fleet.
Also the credentials of every aws call (AC 53, U, and finding 65)."""
import os
import stat
import unittest

import cluster_cost as cc

from . import fakes

H = 720.0   # hours in November 2026 (fakes.START)
GP3, IO1, IO2 = 0.1008, 0.1449, 0.1449
GP3_IOPS, GP3_MIBPS = 0.0063, 51.6096 / 1024
IO1_IOPS, IO2_T1, IO2_T2, IO2_T3 = 0.0756, 0.0756, 0.0529, 0.0367
C6I, T3M, SPOT_6A, SPOT_6B = 0.11655, 0.0554, 0.0412, 0.0388


class StubHarness(fakes.Harness):
    """A harness whose aws calls go to a stub executable, through the real runner."""

    def __init__(self, platform):
        super().__init__(platform)
        fakes.write_python_stub(self.scratch.bin)
        self.stub_log = self.scratch.path("stub.log")
        self.environ["PATH"] = self.scratch.bin + ":/usr/local/bin:/usr/bin:/bin"
        self.environ["COST_STUB_LOG"] = self.stub_log

    def deps(self):
        return cc.Deps(clock=self.clock, owner_uid=self.owner_uid, environ=dict(self.environ),
                       stdout=self.stdout, stderr=self.stderr)

    def calls(self):
        return fakes.read_stub_log(self.stub_log)


class ParallelClusterEndToEndTest(unittest.TestCase):
    def setUp(self):
        self.h = StubHarness("parallelcluster")

    def tearDown(self):
        self.h.close()

    def test_fleet(self):
        self.assertEqual(self.h.run(), 0)
        m = self.h.metrics()
        line = lambda **labels: m.value("cluster_cost_usd_per_hour", **labels)   # noqa: E731
        head = dict(node_role="head", ec2_instance_type="t3.medium", purchase_option="on_demand")
        login = dict(node_role="login", ec2_instance_type="t3.medium", purchase_option="on_demand")
        od = dict(node_role="compute", queue="od", ec2_instance_type="c6i.large", purchase_option="on_demand")
        spot = dict(node_role="compute", queue="spot", ec2_instance_type="c6i.large", purchase_option="spot")
        gpu = dict(node_role="compute", queue="gpu", ec2_instance_type="g4dn.xlarge", purchase_option="on_demand")
        expected = {
            ("instance", "head"): T3M, ("instance", "login"): T3M, ("instance", "od"): C6I,
            ("instance", "spot"): SPOT_6A,
            ("ebs", "head"): 40 * GP3 / H,
            ("ebs", "od"): (35 * GP3 + 3000 * GP3_IOPS + 125 * GP3_MIBPS + 50 * IO2 + 1000 * IO2_T1) / H,
            ("ebs", "spot"): 35 * GP3 / H,
            ("ebs", "gpu"): (100 * IO1 + 5000 * IO1_IOPS) / H,
            ("ebs", "login"): (200 * IO2 + 32000 * IO2_T1 + 32000 * IO2_T2 + 16000 * IO2_T3) / H,
        }
        labels = {"head": head, "login": login, "od": od, "spot": spot, "gpu": gpu}
        for (component, who), rate in expected.items():
            self.assertAlmostEqual(line(component=component, **labels[who]), rate, delta=1e-6,
                                   msg=(component, who))
        self.assertEqual(len(m.series("cluster_cost_usd_per_hour")), len(expected))
        self.assertIsNone(line(component="instance", **gpu))          # unpriced: absent, not 0
        self.assertEqual(m.unpriced(), {("instance", "not_in_price_list"): 1, ("volume", "invalid_record"): 1})
        self.assertEqual(m.value("cluster_cost_collector_up"), 0)
        self.assertEqual(m.stage_errors(), {})
        self.assertEqual(m.sum("cluster_cost_nodes"), 5)
        self.assertEqual(stat.S_IMODE(os.stat(self.h.textfile_path()).st_mode), 0o644)
        self.assertEqual(sorted(os.listdir(self.h.scratch.state)), [".lock", "last-run.json", "prices.json"])
        report = self.h.report()
        self.assertAlmostEqual(report["total_usd_per_hour"], sum(expected.values()), delta=1e-6)
        self.assertEqual(report["exit_code"], 0)
        ops = [" ".join(c["argv"][:2]) for c in self.h.calls()]
        self.assertEqual(ops.count("ec2 describe-instances"), 1)
        self.assertEqual(ops.count("ec2 describe-volumes"), 1)
        self.assertEqual(ops.count("ec2 describe-spot-price-history"), 1)
        self.assertEqual(ops.count("pricing get-products"), 6)       # 3 On-Demand keys, 3 EBS families
        # The next run uses the cache: no Pricing or Spot call.
        self.h.clock.advance(60)
        self.assertEqual(self.h.run(), 0)
        ops = [" ".join(c["argv"][:2]) for c in self.h.calls()]
        self.assertEqual(ops.count("pricing get-products"), 6)
        self.assertEqual(ops.count("ec2 describe-instances"), 2)
        self.assertEqual(self.h.metrics().series("cluster_cost_usd_per_hour"), m.series("cluster_cost_usd_per_hour"))

    def test_aws_cli_not_on_path(self):
        self.h.environ["PATH"] = self.h.scratch.path("empty")
        self.assertEqual(self.h.run(), 0)
        m = self.h.metrics()
        self.assertEqual(m.stage_errors(), {"discovery": "AwsCliNotFound"})
        self.assertEqual(m.value("cluster_cost_collector_up"), 0)


class PcsEndToEndTest(unittest.TestCase):
    def setUp(self):
        self.h = StubHarness("pcs")

    def tearDown(self):
        self.h.close()

    def test_fleet(self):
        self.assertEqual(self.h.run(), 0)
        m = self.h.metrics()
        total = (3 * T3M + C6I + SPOT_6B) + (6 * 0.08 + 0.64) + 8 * 30 * GP3 / H + 0.5825 + 0.17
        self.assertAlmostEqual(sum(v for _, v in m.series("cluster_cost_usd_per_hour")), total, delta=1e-6)
        self.assertEqual(m.value("cluster_cost_usd_per_hour", component="pcs_controller"), 0.5825)
        self.assertEqual(m.value("cluster_cost_usd_per_hour", component="pcs_accounting"), 0.17)
        self.assertEqual(m.unpriced(), {("instance", "not_in_price_list"): 1, ("instance", "capacity_block"): 1,
                                        ("instance", "dedicated_host"): 1, ("instance", "invalid_record"): 1})
        self.assertEqual(m.stage_errors(), {})
        self.assertEqual(set(m.stages_ok()), {"discovery", "ebs", "on_demand_prices", "spot_prices", "pcs", "state"})
        self.assertEqual(m.sum("cluster_cost_nodes"), 8)                # the invalid record is not a node
        ops = [" ".join(c["argv"][:2]) for c in self.h.calls()]
        self.assertEqual(ops.count("ec2 describe-instances"), 2)
        self.assertEqual(ops.count("pcs get-cluster"), 1)
        self.assertEqual(ops.count("pcs list-compute-node-groups"), 1)
        self.assertEqual(ops.count("pricing get-products"), 5)
        for call in self.h.calls():
            argv = call["argv"]
            region = argv[argv.index("--region") + 1]
            self.assertEqual(region, "us-east-1" if argv[0] == "pricing" else "ap-southeast-6")


class CredentialsTest(unittest.TestCase):
    """AC 53 (U) and finding 65."""

    def test_aws_env(self):
        caller = {name: "x" for name in cc.CREDENTIAL_ENV_VARS}
        caller.update(PATH="/usr/bin", HOME="/root", AWS_CONFIG_FILE="/root/.aws/config",
                      AWS_SHARED_CREDENTIALS_FILE="/root/.aws/credentials", AWS_PAGER="less",
                      AWS_ENDPOINT_URL_PRICING="http://127.0.0.1:18080", AWS_ENDPOINT_URL="http://127.0.0.1:1",
                      HTTPS_PROXY="http://proxy:3128", NO_PROXY="169.254.169.254", https_proxy="http://p:1",
                      AWS_RETRY_MODE="standard", AWS_MAX_ATTEMPTS="2", AWS_REGION="eu-west-1")
        env = cc.aws_env(caller)
        for name in cc.CREDENTIAL_ENV_VARS:
            self.assertNotIn(name, env)
        self.assertEqual(len(cc.CREDENTIAL_ENV_VARS), 14)
        self.assertEqual((env["AWS_CONFIG_FILE"], env["AWS_SHARED_CREDENTIALS_FILE"], env["AWS_PAGER"]),
                         ("/nonexistent", "/nonexistent", ""))
        for kept in ("PATH", "HOME", "AWS_ENDPOINT_URL_PRICING", "AWS_ENDPOINT_URL", "HTTPS_PROXY", "NO_PROXY",
                     "https_proxy", "AWS_RETRY_MODE", "AWS_MAX_ATTEMPTS"):
            self.assertEqual(env[kept], caller[kept], kept)
        self.assertEqual(caller["AWS_CONFIG_FILE"], "/root/.aws/config")    # the caller's copy is unchanged

    def test_stub_sees_only_the_instance_profile_chain(self):
        h = StubHarness("parallelcluster")
        try:
            home = h.scratch.path("home")
            os.makedirs(os.path.join(home, ".aws"))
            creds = os.path.join(home, ".aws", "credentials")
            with open(creds, "w") as f:
                f.write("[default]\naws_access_key_id = made-up-key-id\n"
                        "aws_secret_access_key = made-up-secret-for-tests\n")
            h.environ.update(HOME=home, AWS_CONFIG_FILE=creds, AWS_SHARED_CREDENTIALS_FILE=creds,
                             AWS_ACCESS_KEY_ID="made-up-key-id-2",
                             AWS_SECRET_ACCESS_KEY="made-up-secret", AWS_PROFILE="admin",
                             AWS_ENDPOINT_URL_PRICING="http://127.0.0.1:18080")
            self.assertEqual(h.run(), 0)
            calls = h.calls()
            self.assertTrue(calls)
            for call in calls:
                env = call["env"]
                self.assertEqual(env["AWS_CONFIG_FILE"], "/nonexistent")
                self.assertEqual(env["AWS_SHARED_CREDENTIALS_FILE"], "/nonexistent")
                self.assertEqual(env["AWS_ENDPOINT_URL_PRICING"], "http://127.0.0.1:18080")
                self.assertEqual(env["AWS_PAGER"], "")
                for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_PROFILE"):
                    self.assertNotIn(name, env)
            self.assertEqual(h.metrics().stage_errors(), {})
        finally:
            h.close()


if __name__ == "__main__":
    unittest.main()
