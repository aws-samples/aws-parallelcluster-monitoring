"""End to end: main() with stub `aws`, `scontrol` and `squeue` executables,
the real runner and a scratch textfile directory, for a ParallelCluster and
a PCS fleet. Per-user plus idle plus unallocated equals the compute node
cost (AC 10), and every family is one block. Also the credentials of every
aws call (AC 53, U, and finding 65)."""
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
PCS_NODE_FEE = 0.08

# Node costs K of the priced compute instances.
PC_K_OD = C6I + (35 * GP3 + 3000 * GP3_IOPS + 125 * GP3_MIBPS + 50 * IO2 + 1000 * IO2_T1) / H
PC_K_SPOT = SPOT_6A + 35 * GP3 / H
PCS_K_OD = C6I + 30 * GP3 / H + PCS_NODE_FEE           # i-0b00000000000a002, Slurm node od-1
PCS_K_SPOT = SPOT_6B + 30 * GP3 / H + PCS_NODE_FEE     # i-0b00000000000a003, spot-1
PCS_K_LOGIN = T3M + 30 * GP3 / H + PCS_NODE_FEE        # i-0b00000000000a005, login group without monitoring-role


class StubHarness(fakes.Harness):
    """A harness whose aws, scontrol and squeue calls go to stub executables,
    through the real runner."""

    def __init__(self, platform):
        super().__init__(platform)
        fakes.write_python_stub(self.scratch.bin)
        self.stub_log = self.scratch.path("stub.log")
        self.slurm_log = self.scratch.path("slurm-stub.log")
        self.environ["PATH"] = self.scratch.bin + ":/usr/local/bin:/usr/bin:/bin"
        self.environ["COST_STUB_LOG"] = self.stub_log
        self.environ["COST_SLURM_STUB_LOG"] = self.slurm_log
        self.use_slurm(fakes.SLURM_NODES[platform], fakes.SLURM_JOBS[platform])

    def deps(self):
        return cc.Deps(clock=self.clock, owner_uid=self.owner_uid, environ=dict(self.environ),
                       stdout=self.stdout, stderr=self.stderr)

    def calls(self):
        return fakes.read_stub_log(self.stub_log)

    def slurm_calls(self):
        return fakes.read_stub_log(self.slurm_log)


def series_by(m, family, *names):
    return {tuple(labels.get(n) for n in names): v for labels, v in m.series(family)}


class AllocationChecks:
    """Checks shared by both fleets."""

    def check_invariant(self, m, node_cost):
        users = sum(v for _, v in m.series("cluster_cost_user_usd_per_hour"))
        rest = sum(v for _, v in m.series("cluster_cost_unallocated_usd_per_hour"))
        self.assertAlmostEqual(users + rest, node_cost, delta=1e-9)

    def check_one_block_per_family(self, m):
        # fakes.Exposition raises when a family appears in two blocks; the order is the fixed one.
        self.assertEqual(m.families, [f for f in cc.FAMILY_NAMES if f in m.families])
        self.assertEqual(len(m.families), len(set(m.families)))


class ParallelClusterEndToEndTest(unittest.TestCase, AllocationChecks):
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
        self.assertEqual(m.stages_ok()["slurm"], 1)
        self.assertEqual(m.sum("cluster_cost_nodes"), 5)
        self.assertEqual(stat.S_IMODE(os.stat(self.h.textfile_path()).st_mode), 0o644)
        self.assertEqual(sorted(os.listdir(self.h.scratch.state)),
                         [".lock", "last-run.json", "prices.json", "state.json"])
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.h.scratch.state, "state.json")).st_mode), 0o640)
        report = self.h.report()
        self.assertAlmostEqual(report["total_usd_per_hour"], sum(expected.values()), delta=1e-6)
        self.assertEqual(report["exit_code"], 0)
        ops = [" ".join(c["argv"][:2]) for c in self.h.calls()]
        self.assertEqual(ops.count("ec2 describe-instances"), 1)
        self.assertEqual(ops.count("ec2 describe-volumes"), 1)
        self.assertEqual(ops.count("ec2 describe-spot-price-history"), 1)
        self.assertEqual(ops.count("pricing get-products"), 6)       # 3 On-Demand keys, 3 EBS families
        self.assertEqual([c["argv"] for c in self.h.slurm_calls()],
                         [["scontrol", "show", "nodes", "--json"],
                          ["squeue", "--json", "--states=RUNNING,CONFIGURING,COMPLETING"]])

        # Allocation: od-st-od-cr-1 holds job 101 (1 of 2 CPUs; memory isn't
        # scheduled, so its full-node memory figure counts for nothing), the
        # whole spot node is job 102's, and the gpu node is unpriced.
        self.assertEqual(series_by(m, "cluster_cost_user_usd_per_hour", "username", "account").keys(),
                         {("user1", "none"), ("user2", "proj-a")})
        self.assertAlmostEqual(m.value("cluster_cost_user_usd_per_hour", username="user1", account="none"),
                               0.5 * PC_K_OD, delta=1e-9)
        self.assertAlmostEqual(m.value("cluster_cost_user_usd_per_hour", username="user2", account="proj-a"),
                               PC_K_SPOT, delta=1e-9)
        unallocated = series_by(m, "cluster_cost_unallocated_usd_per_hour", "reason", "queue")
        self.assertEqual(set(unallocated), {("idle_node", "od"), ("partial_node", "od"),
                                            ("idle_node", "spot"), ("partial_node", "spot")})
        self.assertAlmostEqual(unallocated[("partial_node", "od")], 0.5 * PC_K_OD, delta=1e-9)
        self.assertEqual([unallocated[k] for k in sorted(unallocated) if k != ("partial_node", "od")], [0, 0, 0])
        self.assertEqual(series_by(m, "cluster_cost_idle_nodes", "queue"), {("od",): 0, ("spot",): 0})
        self.check_invariant(m, PC_K_OD + PC_K_SPOT)
        self.check_one_block_per_family(m)
        slurm = report["slurm"]
        self.assertEqual((slurm["bin_dir"], slurm["found_by"]), (self.h.slurm_bin, "CLUSTER_COST_SLURM_BIN_DIR"))
        self.assertEqual(slurm["data_parser"], {"scontrol": "data_parser/v0.0.44", "squeue": "data_parser/v0.0.44"})
        self.assertEqual({r["node"]: (r["instance_id"], r["matched_by"]) for r in slurm["mapping"]}, {
            "od-st-od-cr-1": ("i-0a00000000000a002", "instance_id"),
            "spot-dy-spot-cr-1": ("i-0a00000000000a003", "instance_id"),
            "gpu-dy-gpu-cr-1": ("i-0a00000000000a004", "instance_id")})
        self.assertEqual(slurm["unmapped_nodes"], ["gpu-dy-gpu-cr-2", "od-dy-od-cr-1"])
        self.assertEqual(slurm["busy_unmapped_nodes"], [])
        self.assertRegex(self.h.log(), r"INFO run done: [0-9.]+ USD/h, 5 instances, 6 volumes, 2 users, ")

        # The first run starts tracking and integrates nothing; known zeros are exported.
        self.assertEqual(m.value("cluster_cost_period_start_timestamp_seconds", period="since_start"), fakes.START)
        self.assertEqual(m.value("cluster_cost_period_start_timestamp_seconds", period="month"), fakes.START)
        self.assertEqual(m.sum("cluster_cost_accumulated_usd"), 0)
        self.assertEqual(len(m.series("cluster_cost_accumulated_usd")), 2 * len(expected))
        self.assertEqual(m.value("cluster_cost_user_accumulated_usd", period="month", username="user1",
                                 account="none"), 0)
        self.assertEqual({(l["period"], l["family"]): v for l, v in m.series("cluster_cost_unaccounted_seconds")},
                         {(p, f): 0 for p in ("since_start", "month") for f in ("lines", "alloc")})

        # The next run uses the cache (no Pricing or Spot call) and integrates one minute.
        self.h.clock.advance(60)
        self.assertEqual(self.h.run(), 0)
        ops = [" ".join(c["argv"][:2]) for c in self.h.calls()]
        self.assertEqual(ops.count("pricing get-products"), 6)
        self.assertEqual(ops.count("ec2 describe-instances"), 2)
        m2 = self.h.metrics()
        self.assertEqual(m2.series("cluster_cost_usd_per_hour"), m.series("cluster_cost_usd_per_hour"))
        for period in ("since_start", "month"):
            self.assertAlmostEqual(m2.sum("cluster_cost_accumulated_usd", period=period),
                                   sum(expected.values()) / 60, delta=1e-8)
            self.assertAlmostEqual(m2.value("cluster_cost_user_accumulated_usd", period=period, username="user2",
                                            account="proj-a"), PC_K_SPOT / 60, delta=1e-9)
            users = m2.sum("cluster_cost_user_accumulated_usd", period=period)
            rest = m2.sum("cluster_cost_unallocated_accumulated_usd", period=period)
            self.assertAlmostEqual(users + rest, (PC_K_OD + PC_K_SPOT) / 60, delta=1e-9)
            self.assertEqual(m2.value("cluster_cost_unpriced_resource_seconds", period=period, kind="instance",
                                      reason="not_in_price_list"), 60)
        state = self.h.state()
        self.assertEqual(state["schema"], 1)
        self.assertEqual(state["cluster"], "parallelcluster/mycluster/ap-southeast-6")
        self.assertEqual(state["families"]["lines"]["last_at"], fakes.START + 60)
        self.assertEqual(sorted(os.listdir(self.h.scratch.state)),
                         [".lock", "last-run.json", "prices.json", "state.json", "state.json.bak"])

    def test_aws_cli_not_on_path(self):
        self.h.environ["PATH"] = self.h.scratch.path("empty")
        self.assertEqual(self.h.run(), 0)
        m = self.h.metrics()
        self.assertEqual(m.stage_errors(), {"discovery": "AwsCliNotFound"})
        self.assertEqual(m.value("cluster_cost_collector_up"), 0)
        self.assertEqual(self.h.slurm_calls(), [])                  # a failed discovery skips steps 4-7

    def test_slurm_client_missing(self):
        os.unlink(os.path.join(self.h.slurm_bin, "squeue"))
        self.assertEqual(self.h.run(), 0)
        m = self.h.metrics()
        self.assertEqual(m.stage_errors(), {"slurm": "SlurmNotFound"})
        for family in ("cluster_cost_user_usd_per_hour", "cluster_cost_unallocated_usd_per_hour",
                       "cluster_cost_idle_nodes"):
            self.assertEqual(m.series(family), [], family)
        self.assertTrue(m.series("cluster_cost_usd_per_hour"))      # cost lines are unaffected
        self.check_one_block_per_family(m)


class PcsEndToEndTest(unittest.TestCase, AllocationChecks):
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
        self.assertEqual(set(m.stages_ok()),
                         {"discovery", "ebs", "slurm", "on_demand_prices", "spot_prices", "pcs", "state"})
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

        # Allocation: od-1 holds jobs 201 (memory share 0.75) and 205 (CPU
        # share 0.5, UID 1005 doesn't resolve), scaled from 1.25 to 1; the
        # whole spot-1 is job 202's; the gpu, Capacity Block and host-tenancy
        # nodes are unpriced; the login group without monitoring-role has no
        # Slurm node and is idle.
        users = series_by(m, "cluster_cost_user_usd_per_hour", "username", "account")
        self.assertEqual(set(users), {("user1", "proj-a"), ("uid:1005", "none"), ("user2", "proj-b")})
        self.assertAlmostEqual(users[("user1", "proj-a")], 0.6 * PCS_K_OD, delta=1e-9)
        self.assertAlmostEqual(users[("uid:1005", "none")], 0.4 * PCS_K_OD, delta=1e-9)
        self.assertAlmostEqual(users[("user2", "proj-b")], PCS_K_SPOT, delta=1e-9)
        unallocated = series_by(m, "cluster_cost_unallocated_usd_per_hour", "reason", "queue")
        self.assertAlmostEqual(unallocated.pop(("idle_node", "login-extra")), PCS_K_LOGIN, delta=1e-9)
        self.assertEqual(unallocated, {("partial_node", "login-extra"): 0, ("idle_node", "od"): 0,
                                       ("partial_node", "od"): 0, ("idle_node", "spot"): 0,
                                       ("partial_node", "spot"): 0})
        self.assertEqual(series_by(m, "cluster_cost_idle_nodes", "queue"),
                         {("login-extra",): 1, ("od",): 0, ("spot",): 0})
        self.check_invariant(m, PCS_K_OD + PCS_K_SPOT + PCS_K_LOGIN)
        self.check_one_block_per_family(m)
        slurm = self.h.report()["slurm"]
        self.assertEqual(slurm["data_parser"], {"scontrol": "data_parser/v0.0.45", "squeue": "data_parser/v0.0.45"})
        self.assertEqual({r["node"]: (r["instance_id"], r["matched_by"]) for r in slurm["mapping"]}, {
            "od-1": ("i-0b00000000000a002", "address"), "od-2": ("i-0b00000000000a007", "address"),
            "spot-1": ("i-0b00000000000a003", "address"), "gpu-1": ("i-0b00000000000a004", "address"),
            "cb-1": ("i-0b00000000000a006", "address")})
        self.assertEqual(slurm["unmapped_nodes"], ["od-3", "spot-2"])
        od1 = [r for r in slurm["shares"] if r["node"] == "od-1"][0]
        self.assertEqual(od1["share_sum"], 1.25)
        self.assertEqual(sorted((s["job_id"], s["share"]) for s in od1["shares"]), [(201, 0.6), (205, 0.4)])

        # Ten more runs one minute apart: the month totals of both families agree.
        for _ in range(10):
            self.h.clock.advance(60)
            self.assertEqual(self.h.run(), 0)
        m = self.h.metrics()
        for period in ("since_start", "month"):
            users = m.sum("cluster_cost_user_accumulated_usd", period=period)
            rest = m.sum("cluster_cost_unallocated_accumulated_usd", period=period)
            self.assertAlmostEqual(users + rest, (PCS_K_OD + PCS_K_SPOT + PCS_K_LOGIN) * 10 / 60, delta=1e-9)
            self.assertAlmostEqual(m.value("cluster_cost_unpriced_resource_seconds", period=period, kind="instance",
                                           reason="capacity_block"), 600, delta=1e-6)
        self.check_one_block_per_family(m)


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
