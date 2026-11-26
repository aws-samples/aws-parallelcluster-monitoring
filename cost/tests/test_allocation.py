"""Allocation (design "Allocation (users, accounts, idle)"): dominant
shares, scaling, idle and partial nodes, instances without a Slurm node,
known zeros, the D6 mapping with its tie-break, the mapping check with its
exclusions, truncation, the invariant of AC 10 (U) to 1e-9, and the
mapping failure of AC 39."""
import copy
import random
import unittest

import cluster_cost as cc

from . import fakes, stub_slurm


def instance(iid, role="compute", queue="q1", itype="c6i.large", option="on_demand", ip=None, dns=None):
    return cc.Instance(instance_id=iid, instance_type=itype, az="ap-southeast-6a", tenancy="default",
                       usage_operation="RunInstances", purchase_option=option, role=role,
                       queue=queue if role == "compute" else None, private_ip=ip, private_dns=dns)


def node(name, cpus=4, memory=16000, gpus=0, address=None, hostname=None, instance_id=None, states=("MIXED",)):
    return cc.SlurmNode(name, cpus, memory, gpus, address, hostname, instance_id, frozenset(states))


def entry(job, node_name, user="user1", account="proj-a", cpus=0, memory=0, gpus=0, whole=False,
          memory_counts=False):
    return cc.SlurmShare(job, user, account, node_name, cpus, memory, gpus, whole, memory_counts)


def make_run():
    config = cc.Config(platform="parallelcluster", cluster_id=fakes.PC_CLUSTER, region=fakes.REGION,
                       state_dir="/nonexistent", textfile_dir="/nonexistent")
    clock = fakes.FakeClock()
    return cc.Run(config, cc.Deps(runner=fakes.FakeRunner(clock), clock=clock, environ={}))


class World:
    """Instances, Slurm nodes, job allocations and node costs for allocate()."""

    def __init__(self):
        self.instances = []
        self.nodes = {}
        self.shares = []
        self.node_cost = {}

    def add(self, inst, k=None, slurm_node=None):
        self.instances.append(inst)
        if inst.role == "compute":
            self.node_cost[inst.instance_id] = k
        if slurm_node is not None:
            self.nodes[slurm_node.name] = slurm_node
        return inst

    def allocate(self):
        slurm = cc.SlurmData()
        slurm.nodes = self.nodes
        slurm.shares = self.shares
        slurm.holding = {e.node for e in self.shares}
        self.instances.sort(key=lambda i: i.instance_id)
        slurm.mapping = cc.map_slurm_nodes(self.nodes, self.instances, slurm.holding)
        cost = cc.CostResult()
        cost.node_cost = dict(self.node_cost)
        run = make_run()
        self.slurm = slurm
        return cc.allocate(run, run.health.stage("slurm"), slurm, cc.Discovery(self.instances, 0), cost)


def users(alloc):
    return {(dict(k)["username"], dict(k)["account"]): v for k, v in alloc.users.items()}


def unallocated(alloc):
    return {(dict(k)["reason"], dict(k)["queue"]): v for k, v in alloc.unallocated.items()}


def idle(alloc):
    return {dict(k)["queue"]: v for k, v in alloc.idle_nodes.items()}


def total(alloc):
    return sum(alloc.users.values()) + sum(alloc.unallocated.values())


class ShareTest(unittest.TestCase):
    def one_node(self, *entries, **node_args):
        w = World()
        w.add(instance("i-00000001", ip="10.0.1.1"), k=2.0, slurm_node=node("n1", address="10.0.1.1", **node_args))
        w.shares = list(entries)
        return w.allocate()

    def test_partial_cpu(self):
        a = self.one_node(entry(1, "n1", cpus=1))
        self.assertEqual(users(a), {("user1", "proj-a"): 0.5})
        self.assertEqual(unallocated(a), {("partial_node", "q1"): 1.5, ("idle_node", "q1"): 0.0})
        self.assertEqual(idle(a), {"q1": 0})

    def test_whole_gpu(self):
        a = self.one_node(entry(1, "n1", cpus=1, gpus=1), gpus=1)
        self.assertEqual(users(a), {("user1", "proj-a"): 2.0})
        self.assertEqual(unallocated(a)[("partial_node", "q1")], 0.0)

    def test_memory_consumable_and_not(self):
        a = self.one_node(entry(1, "n1", cpus=1, memory=12000, memory_counts=True))
        self.assertEqual(users(a), {("user1", "proj-a"): 1.5})
        a = self.one_node(entry(1, "n1", cpus=1, memory=12000, memory_counts=False))
        self.assertEqual(users(a), {("user1", "proj-a"): 0.5})
        a = self.one_node(entry(1, "n1", cpus=1, memory=12000, memory_counts=True), memory=0)
        self.assertEqual(users(a), {("user1", "proj-a"): 0.5})         # M <= 0: memory ignored

    def test_whole_node(self):
        a = self.one_node(entry(1, "n1", cpus=1, whole=True))
        self.assertEqual(users(a), {("user1", "proj-a"): 2.0})

    def test_clamped_to_one(self):
        a = self.one_node(entry(1, "n1", cpus=9))
        self.assertEqual(users(a), {("user1", "proj-a"): 2.0})
        self.assertEqual(unallocated(a)[("partial_node", "q1")], 0.0)

    def test_oversubscribed_node_scaled_to_one(self):
        a = self.one_node(entry(1, "n1", cpus=3), entry(2, "n1", user="user2", cpus=3))
        self.assertEqual(users(a), {("user1", "proj-a"): 1.0, ("user2", "proj-a"): 1.0})
        self.assertEqual(unallocated(a)[("partial_node", "q1")], 0.0)
        row = [r for r in a.nodes if r["node"] == "n1"][0]
        self.assertEqual(row["share_sum"], 1.5)
        self.assertEqual([s["share"] for s in row["shares"]], [0.5, 0.5])

    def test_same_user_and_account_add_up(self):
        a = self.one_node(entry(1, "n1", cpus=1), entry(2, "n1", cpus=2))
        self.assertEqual(users(a), {("user1", "proj-a"): 1.5})

    def test_idle_node(self):
        a = self.one_node()
        self.assertEqual(users(a), {})
        self.assertEqual(unallocated(a), {("idle_node", "q1"): 2.0, ("partial_node", "q1"): 0.0})
        self.assertEqual(idle(a), {"q1": 1})

    def test_zero_share_job_leaves_the_node_idle(self):
        a = self.one_node(entry(1, "n1"))
        self.assertEqual(users(a), {("user1", "proj-a"): 0.0})
        self.assertEqual(idle(a), {"q1": 1})


class NodeCostTest(unittest.TestCase):
    def test_instances_without_a_slurm_node_are_idle(self):
        w = World()
        w.add(instance("i-00000001", queue="q1", ip="10.0.1.1"), k=1.0)
        w.add(instance("i-00000002", queue="q2", ip="10.0.1.2", option="spot"), k=0.5,
              slurm_node=node("n2", address="10.0.1.2"))
        w.shares = [entry(1, "n2", cpus=4)]
        a = w.allocate()
        self.assertEqual(idle(a), {"q1": 1, "q2": 0})
        self.assertEqual(unallocated(a)[("idle_node", "q1")], 1.0)
        self.assertEqual(users(a), {("user1", "proj-a"): 0.5})
        self.assertIn({"node": None, "instance_id": "i-00000001", "matched_by": None, "usd_per_hour": 1.0,
                       "share_sum": 0.0, "shares": []}, a.nodes)

    def test_unpriced_instances_are_left_out(self):
        w = World()
        w.add(instance("i-00000001", queue="gpu", ip="10.0.1.1"), k=None, slurm_node=node("g1", address="10.0.1.1"))
        w.add(instance("i-00000002", queue="gpu", ip="10.0.1.2"), k=None)       # no Slurm node, unpriced
        w.shares = [entry(1, "g1", cpus=4)]
        a = w.allocate()
        self.assertEqual((a.users, a.unallocated, a.idle_nodes), ({}, {}, {}))  # no known zero for the group
        self.assertEqual(a.nodes[0]["usd_per_hour"], None)                      # the shares are still reported
        self.assertEqual(a.nodes[0]["shares"][0]["share"], 1.0)

    def test_known_zeros_per_group(self):
        w = World()
        w.add(instance("i-00000001", queue="q1", ip="10.0.1.1"), k=1.0, slurm_node=node("n1", address="10.0.1.1"))
        w.add(instance("i-00000002", queue="q1", itype="c6i.xlarge", ip="10.0.1.2"), k=2.0,
              slurm_node=node("n2", address="10.0.1.2"))
        w.shares = [entry(1, "n1", cpus=4), entry(2, "n2", cpus=4)]
        a = w.allocate()
        self.assertEqual(len(a.idle_nodes), 2)
        self.assertEqual(set(a.idle_nodes.values()), {0})
        self.assertEqual(len(a.unallocated), 4)
        self.assertEqual(set(a.unallocated.values()), {0.0})

    def test_head_and_login_nodes_are_shared_cost(self):
        w = World()
        w.add(instance("i-00000001", role="head", ip="10.0.0.10"), slurm_node=node("head", address="10.0.0.10"))
        w.add(instance("i-00000002", role="login", ip="10.0.0.20"), slurm_node=node("login", address="10.0.0.20"))
        w.shares = [entry(1, "head", cpus=4), entry(2, "login", cpus=4)]
        a = w.allocate()
        self.assertEqual((a.users, a.unallocated, a.idle_nodes, a.nodes), ({}, {}, {}, []))
        self.assertEqual(set(w.slurm.mapping), {"head", "login"})                # mapped, so not busy-unmapped

    def test_truncated_keys_merge(self):
        w = World()
        w.add(instance("i-00000001", queue="q" * 130, ip="10.0.1.1"), k=1.0, slurm_node=node("n1", address="10.0.1.1"))
        w.add(instance("i-00000002", queue="q" * 129, ip="10.0.1.2"), k=1.0, slurm_node=node("n2", address="10.0.1.2"))
        a = w.allocate()
        self.assertEqual(idle(a), {"q" * 128: 2})
        self.assertEqual(unallocated(a)[("idle_node", "q" * 128)], 2.0)


class MappingTest(unittest.TestCase):
    def setUp(self):
        self.i1 = instance("i-00000001", ip="10.0.1.1", dns="ip-10-0-1-1.ap-southeast-6.compute.internal")
        self.i2 = instance("i-00000002", ip="10.0.1.2", dns="ip-10-0-1-2.ap-southeast-6.compute.internal")

    def mapping(self, nodes, holding=()):
        result = cc.map_slurm_nodes({n.name: n for n in nodes}, [self.i1, self.i2], set(holding))
        return {name: (inst.instance_id, how) for name, (inst, how) in result.items()}

    def test_keys_in_order(self):
        got = self.mapping([node("a", instance_id="i-00000001", address="10.0.1.2"),
                            node("b", address="10.0.1.2", hostname="ip-10-0-1-1"),
                            node("c", hostname="ip-10-0-1-1.ap-southeast-6.compute.internal"),
                            node("d", instance_id="i-gone", address="10.9.9.9", hostname="nowhere")])
        self.assertEqual(got["a"], ("i-00000001", "instance_id"))      # instance_id first
        self.assertEqual(got["b"], ("i-00000002", "address"))           # then the private IP
        self.assertNotIn("c", got)            # a second claim on i-00000001, by hostname: unmapped
        self.assertNotIn("d", got)
        got = self.mapping([node("c", hostname="ip-10-0-1-1.ap-southeast-6.compute.internal"),
                            node("e", hostname="ip-10-0-1-2")])
        self.assertEqual(got, {"c": ("i-00000001", "hostname"), "e": ("i-00000002", "hostname")})

    def test_tie_break_instance_id_wins(self):
        got = self.mapping([node("a", address="10.0.1.1"), node("z", instance_id="i-00000001")], holding=["a"])
        self.assertEqual(got, {"z": ("i-00000001", "instance_id")})

    def test_tie_break_the_node_with_jobs(self):
        got = self.mapping([node("a", address="10.0.1.1"), node("b", hostname="ip-10-0-1-1")], holding=["b"])
        self.assertEqual(got, {"b": ("i-00000001", "hostname")})

    def test_tie_break_lowest_name_with_a_warning(self):
        with self.assertLogs(cc.LOG, "WARNING") as logs:
            got = self.mapping([node("b", address="10.0.1.1"), node("a", address="10.0.1.1")], holding=["a", "b"])
        self.assertEqual(got, {"a": ("i-00000001", "address")})
        self.assertIn("Slurm nodes a, b map to instance i-00000001 and all hold jobs: a kept", logs.output[0])

    def test_tie_break_without_jobs(self):
        got = self.mapping([node("b", address="10.0.1.1"), node("a", address="10.0.1.1")])
        self.assertEqual(got, {"a": ("i-00000001", "address")})


class InvariantTest(unittest.TestCase):
    """AC 10 (U): per-user + idle + unallocated = the node cost of the
    priced compute instances, to 1e-9."""

    def test_random_fleets(self):
        rng = random.Random(20261125)
        for trial in range(40):
            w = World()
            expected = 0.0
            for i in range(rng.randint(1, 30)):
                role = rng.choice(["compute"] * 8 + ["head", "login"])
                k = None if rng.random() < 0.15 else round(rng.uniform(0.01, 40.0), 6)
                ip = "10.0.%d.%d" % (trial % 250, i + 1)
                inst = instance("i-%08x" % (trial * 1000 + i), role=role, queue=rng.choice(["a", "b", "c"]),
                                option=rng.choice(["on_demand", "spot"]), ip=ip)
                slurm_node = None
                if rng.random() < 0.85:
                    slurm_node = node("n%03d" % i, cpus=rng.choice([2, 4, 96]), memory=rng.choice([0, 3600, 768000]),
                                      gpus=rng.choice([0, 0, 1, 8]), address=ip)
                w.add(inst, k=k if role == "compute" else None, slurm_node=slurm_node)
                if role == "compute" and k is not None:
                    expected += k
                if slurm_node is not None and rng.random() < 0.2:      # a stale duplicate of the same IP
                    w.nodes["dup%03d" % i] = node("dup%03d" % i, address=ip)
            names = sorted(w.nodes)
            for j in range(rng.randint(0, 80) if names else 0):
                name = rng.choice(names)
                n = w.nodes[name]
                w.shares.append(entry(j, name, user=rng.choice(["user1", "user2", "uid:1005"]),
                                      account=rng.choice(["proj-a", "none"]), cpus=rng.randint(0, n.cpus),
                                      memory=rng.randint(0, max(n.memory, 1)), gpus=rng.randint(0, max(n.gpus, 1)),
                                      whole=rng.random() < 0.1, memory_counts=rng.random() < 0.5))
            a = w.allocate()
            self.assertAlmostEqual(total(a), expected, delta=1e-9, msg=trial)
            self.assertTrue(all(v >= 0 for v in list(a.users.values()) + list(a.unallocated.values())))


def pc_fleet_without(*instance_ids):
    recs = [r for r in fakes.fixture("aws", "pc-describe-instances.json") if r["InstanceId"] not in instance_ids]
    return fakes.ok(recs)


class MappingCheckTest(unittest.TestCase):
    """Allocation step 8 on the ParallelCluster fleet, when the spot node's
    instance is gone: busy nodes fail the stage, booting and lost ones don't."""

    def setUp(self):
        self.h = fakes.Harness("parallelcluster")
        self.h.runner.on(("describe-instances",), pc_fleet_without("i-0a00000000000a003"))
        self.nodes = stub_slurm.load("nodes-v0.0.44.json")
        self.jobs = stub_slurm.load("jobs-v0.0.44.json")

    def tearDown(self):
        self.h.close()

    def spot_node(self, doc):
        return [n for n in doc["nodes"] if n["name"] == "spot-dy-spot-cr-1"][0]

    def spot_job(self, doc):
        return [j for j in doc["jobs"] if j["job_id"] == 102][0]

    def stage(self):
        self.h.use_slurm(self.nodes, self.jobs)
        self.h.clock.advance(60)
        self.h.run()
        return self.h.metrics().stage_errors().get("slurm")

    def test_busy_unmapped_node_fails_the_stage(self):
        self.assertEqual(self.stage(), "NodeMappingIncomplete")
        self.assertIn("WARNING slurm failed: NodeMappingIncomplete: busy Slurm nodes map to no running cluster "
                      "instance: spot-dy-spot-cr-1", self.h.log())
        self.assertEqual(self.h.report()["slurm"]["busy_unmapped_nodes"], ["spot-dy-spot-cr-1"])

    def test_job_states_that_dont_count(self):
        for state in (["RUNNING", "CONFIGURING"], ["COMPLETED", "COMPLETING"], ["RUNNING", "COMPLETING"]):
            self.spot_job(self.jobs)["job_state"] = state
            self.assertIsNone(self.stage(), state)

    def test_node_states_that_dont_count(self):
        for flag in ("POWERING_UP", "POWERING_DOWN", "POWERED_DOWN", "DOWN", "NOT_RESPONDING"):
            self.spot_node(self.nodes)["state"] = ["ALLOCATED", "CLOUD", flag]
            self.assertIsNone(self.stage(), flag)
        self.spot_node(self.nodes)["state"] = ["ALLOCATED", "CLOUD", "DRAIN"]
        self.assertEqual(self.stage(), "NodeMappingIncomplete")


class MappingFailureTest(unittest.TestCase):
    """AC 39: a run whose mapping check fails exports no user, unallocated or
    idle-node series, doesn't integrate the alloc family, and leaves the cost
    lines and their integration alone."""

    def test_mapping_failure(self):
        h = fakes.Harness("parallelcluster")
        try:
            h.use_slurm("nodes-v0.0.44.json", "jobs-v0.0.44.json")
            h.run()
            h.clock.advance(60)
            h.run()
            good = h.metrics()
            before = h.state()
            h.clock.advance(60)
            h.runner.on(("describe-instances",), pc_fleet_without("i-0a00000000000a003"))
            self.assertEqual(h.run(), 0)
            m = h.metrics()
            self.assertEqual(m.stage_errors(), {"slurm": "NodeMappingIncomplete"})
            for family in ("cluster_cost_user_usd_per_hour", "cluster_cost_unallocated_usd_per_hour",
                           "cluster_cost_idle_nodes"):
                self.assertEqual(m.series(family), [], family)
            after = h.state()
            self.assertEqual(after["families"]["alloc"], before["families"]["alloc"])
            for period in ("since_start", "month"):
                for bucket in ("users", "unallocated"):
                    self.assertEqual(after["accumulated"][period][bucket], before["accumulated"][period][bucket])
                # the accumulated user series are still exported, unchanged
                self.assertEqual(m.sum("cluster_cost_user_accumulated_usd", period=period),
                                 good.sum("cluster_cost_user_accumulated_usd", period=period))
            self.assertEqual(after["families"]["lines"]["last_at"], fakes.START + 120)    # lines integrated
            self.assertGreater(m.sum("cluster_cost_accumulated_usd", period="month"),
                               good.sum("cluster_cost_accumulated_usd", period="month"))
            self.assertTrue(m.series("cluster_cost_usd_per_hour"))
            spot = dict(node_role="compute", queue="spot", ec2_instance_type="c6i.large", purchase_option="spot")
            self.assertIsNone(m.value("cluster_cost_usd_per_hour", component="instance", **spot))
            report = h.report()["slurm"]
            self.assertEqual(report["busy_unmapped_nodes"], ["spot-dy-spot-cr-1"])
            self.assertIn({"node": "od-st-od-cr-1", "instance_id": "i-0a00000000000a002", "role": "compute",
                           "matched_by": "instance_id"}, report["mapping"])
            self.assertTrue([r for r in report["shares"] if r["node"] == "od-st-od-cr-1"][0]["shares"])
            # The next good run integrates the alloc family from its last good run.
            h.clock.advance(60)
            h.runner.on(("describe-instances",), fakes.ok(fakes.fixture_text("aws", "pc-describe-instances.json")))
            h.run()
            self.assertEqual(h.metrics().stage_errors(), {})
            self.assertEqual(h.state()["families"]["alloc"]["last_at"], fakes.START + 180)
            users_now = h.metrics().sum("cluster_cost_user_accumulated_usd", period="month")
            rate = sum(v for _, v in good.series("cluster_cost_user_usd_per_hour"))
            self.assertAlmostEqual(users_now - good.sum("cluster_cost_user_accumulated_usd", period="month"),
                                   rate * 120 / 3600, delta=4e-9)              # values printed with 9 decimals
        finally:
            h.close()

    def test_zero_rows_only_after_a_good_stage(self):
        h = fakes.Harness("pcs")
        try:
            h.run()
            m = h.metrics()
            self.assertEqual(m.stages_ok()["slurm"], 1)
            self.assertTrue(m.series("cluster_cost_idle_nodes"))
            h.clock.advance(60)
            h.runner.on(fakes.is_command("squeue"), cc.CommandResult(rc=1, stderr="squeue: error\n"))
            h.run()
            m = h.metrics()
            self.assertEqual(m.stage_errors()["slurm"], "SlurmCommandFailed")
            self.assertEqual(m.series("cluster_cost_idle_nodes"), [])
            self.assertEqual(m.series("cluster_cost_unallocated_usd_per_hour"), [])
        finally:
            h.close()

    def test_truncation_before_merging_in_the_file(self):
        h = fakes.Harness("pcs")
        try:
            jobs = copy.deepcopy(stub_slurm.load("jobs-v0.0.45.json"))
            for job in jobs["jobs"]:
                if job["job_id"] in (201, 205):                         # both on od-1
                    job["user_name"] = "u" * 128 + str(job["job_id"])
                    job["account"] = "a" * 128 + str(job["job_id"])
            h.use_slurm("nodes-v0.0.45.json", jobs)
            h.run()
            m = h.metrics()
            long_users = [(l, v) for l, v in m.series("cluster_cost_user_usd_per_hour") if l["username"] == "u" * 128]
            self.assertEqual(len(long_users), 1)
            self.assertEqual(long_users[0][0]["account"], "a" * 128)
            od1 = [r for r in h.report()["slurm"]["shares"] if r["node"] == "od-1"][0]
            self.assertAlmostEqual(long_users[0][1], od1["usd_per_hour"], delta=1e-9)   # 0.6 + 0.4 of the node
        finally:
            h.close()


if __name__ == "__main__":
    unittest.main()
