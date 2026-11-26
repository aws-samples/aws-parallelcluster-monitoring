"""The slurm stage: Slurm parsing (AC 35, AC 44) for data_parser v0.0.42,
v0.0.44 and v0.0.45 and the edge cases, the commands and their failure
codes, and the Slurm binaries of design "Slurm binaries"."""
import json
import os
import unittest

import cluster_cost as cc

from . import fakes, stub_slurm


def instances(platform):
    """The valid instances of a fleet fixture, as discovery returns them."""
    names = (["pc-describe-instances.json"] if platform == "parallelcluster"
             else ["pcs-describe-instances-cluster-id.json", "pcs-describe-instances-pcs-cluster-id.json"])
    out = {}
    for name in names:
        for rec in fakes.fixture("aws", name):
            inst, _ = cc.parse_instance(rec, cc.parse_tags(rec["Tags"]), platform)
            if inst is not None:
                out[inst.instance_id] = inst
    return [out[k] for k in sorted(out)]


def make_run(platform="parallelcluster", root="/nonexistent-root", slurm_bin_dir=None, path=""):
    config = cc.Config(platform=platform, cluster_id=fakes.PC_CLUSTER if platform == "parallelcluster"
                       else fakes.PCS_CLUSTER, region=fakes.REGION, state_dir="/nonexistent",
                       textfile_dir="/nonexistent", slurm_bin_dir=slurm_bin_dir)
    clock = fakes.FakeClock()
    return cc.Run(config, cc.Deps(runner=fakes.FakeRunner(clock), clock=clock, root=root,
                                  environ={"PATH": path}))


def parsed(nodes, jobs):
    """SlurmData from two fixtures, parsed as the stage does."""
    slurm = cc.SlurmData("/x", "test")
    slurm.nodes = cc.parse_slurm_nodes(stub_slurm.load(nodes)["nodes"])
    cc.parse_slurm_jobs(slurm, stub_slurm.load(jobs)["jobs"])
    return slurm


def shares(slurm):
    """(job_id, node) -> share of the node, before scaling."""
    return {(e.job_id, e.node): cc.share_of(e, slurm.nodes[e.node]) for e in slurm.shares}


class MemoryIsConsumableTest(unittest.TestCase):
    """AC 35, and the flag forms of design-checks/logic_check.py."""

    def test_forms(self):
        for select_type in (["CPU", "MEMORY"], ["CORE", "MEMORY"], ["CPU", "MEMORY", "PACK_NODES"],
                            ["CR_CPU_MEMORY"], ["CR_CORE_MEMORY", "CR_PACK_NODES"], ["CR_MEMORY"], "CR_CPU_MEMORY"):
            self.assertTrue(cc.memory_is_consumable(select_type), select_type)
        for select_type in (["CPU"], ["CR_CPU"], ["CR_CORE", "CR_ONE_TASK_PER_CORE", "ENFORCE_BINDING_GRES"], [],
                            None, "CR_CPU", {"MEMORY": 1}, 7, [None, 3]):
            self.assertFalse(cc.memory_is_consumable(select_type), select_type)


class GresTest(unittest.TestCase):
    def test_gpu_counts(self):
        cases = {
            "gpu:T4:1(S:0)": 1,
            "gpu:a100:8(S:0-1)": 8,
            "gpu:a100:4(S:0,2),gpu:1g.10gb:7(S:1)": 11,             # MIG devices, a comma inside parentheses
            "gpu:a100:2(IDX:0,2),gpu:1g.10gb:2(IDX:5-6)": 4,
            "gpu:h100:4(IDX:0,2,4,6)": 4,
            "gpu:a10g:1(S:0),gpu:1g.5gb:2(S:0),shard:a10g:4(S:0)": 3,
            "gpu:4": 4, "gpu:a100": 1, "gpu": 1, "gpu:a100:0": 0,
            "nvme:1,mps:100(S:0)": 0, "": 0, "(null)": 0, "gpu:a100:x": 0, "gpu:t4:²": 0,
        }
        for text, want in cases.items():
            self.assertEqual(cc.gpu_count(text), want, text)
        self.assertEqual(cc.gpu_count(None), 0)
        self.assertEqual(cc.split_gres("a(1,2),b,(c,(d,e)),f"), ["a(1,2)", "b", "(c,(d,e))", "f"])

    def test_tres_gpus(self):
        self.assertEqual(cc.tres_gpus("cpu=8,mem=16G,node=2,billing=8,gres/gpu=4,gres/gpu:a100=4"), 4)
        self.assertEqual(cc.tres_gpus("cpu=8,gres/gpu:a100=4"), 0)
        self.assertEqual(cc.tres_gpus(None), 0)

    def test_numbers(self):
        self.assertEqual(cc.slurm_int({"set": True, "infinite": False, "number": 4}, 1), 4)
        self.assertIsNone(cc.slurm_int({"set": False, "infinite": False, "number": 4}, 1))
        self.assertIsNone(cc.slurm_int({"set": True, "infinite": True, "number": 4}, 1))
        self.assertIsNone(cc.slurm_int(True, 0))
        self.assertIsNone(cc.slurm_int(2.0, 0))
        self.assertIsNone(cc.slurm_int(0, 1))
        self.assertEqual(cc.slurm_int(0, 0), 0)
        self.assertEqual(cc.state_flags("RUNNING"), {"RUNNING"})
        self.assertEqual(cc.state_flags(["RUNNING", "CONFIGURING", 3]), {"RUNNING", "CONFIGURING"})
        self.assertEqual(cc.state_flags(None), frozenset())


class FixtureParsingTest(unittest.TestCase):
    """The field paths are the same in data_parser v0.0.42, v0.0.44 and
    v0.0.45; select_type uses CR_TYPE flags in the first two and
    SELECT_TYPE_PARAM flags in v0.0.45."""

    def test_v0_0_42(self):
        s = parsed("nodes-v0.0.42.json", "jobs-v0.0.42.json")
        n = s.nodes["compute-st-c6i-1"]
        self.assertEqual((n.cpus, n.memory, n.gpus), (2, 6576, 0))      # effective_cpus; real - specialized memory
        self.assertEqual((n.address, n.hostname, n.instance_id), ("10.0.1.21", "ip-10-0-1-21", None))
        g = s.nodes["gpu-st-p4d-1"]
        self.assertEqual((g.cpus, g.memory, g.gpus, g.address), (96, 1100000, 11, "gpu-st-p4d-1"))
        self.assertEqual(s.nodes["compute-dy-c6i-2"].states, {"DOWN", "CLOUD", "NOT_RESPONDING"})
        self.assertEqual(len(s.nodes), 5)
        self.assertEqual(s.jobs, 4)
        got = shares(s)
        self.assertEqual(got[(301, "compute-st-c6i-1")], 0.75)          # memory 4932 / 6576 dominates
        self.assertAlmostEqual(got[(302, "gpu-st-p4d-1")], 4 / 11.0, places=12)   # 2 A100 + 2 MIG of 11
        self.assertEqual(got[(303, "compute-st-c6i-2")], 1.0)           # nodes.whole
        self.assertEqual(got[(305, "compute-st-c6i-1")], 0.5)
        self.assertTrue(all(e.memory_counts for e in s.shares))          # ["CORE", "MEMORY"]
        self.assertEqual(s.busy, {"compute-st-c6i-1", "compute-st-c6i-2", "gpu-st-p4d-1", "compute-dy-c6i-2"})

    def test_v0_0_44(self):
        s = parsed("nodes-v0.0.44.json", "jobs-v0.0.44.json")
        self.assertEqual(s.nodes["od-st-od-cr-1"].instance_id, "i-0a00000000000a002")
        self.assertEqual(s.nodes["gpu-dy-gpu-cr-1"].gpus, 1)
        got = shares(s)
        # ["CPU"]: memory isn't scheduled, so the job's full-node memory figure reserves nothing.
        self.assertEqual(got[(101, "od-st-od-cr-1")], 0.5)
        self.assertFalse(any(e.memory_counts for e in s.shares))
        self.assertEqual(got[(103, "gpu-dy-gpu-cr-1")], 1.0)            # the node's only GPU
        self.assertEqual(got[(102, "spot-dy-spot-cr-1")], 1.0)
        job101 = [e for e in s.shares if e.job_id == 101][0]
        self.assertEqual((job101.username, job101.account), ("user1", "none"))   # the job name is text, not JSON
        # CONFIGURING holds resources but isn't busy for the mapping check.
        self.assertIn("gpu-dy-gpu-cr-2", s.holding)
        self.assertNotIn("gpu-dy-gpu-cr-2", s.busy)

    def test_v0_0_45(self):
        s = parsed("nodes-v0.0.45.json", "jobs-v0.0.45.json")
        self.assertEqual(s.nodes["cb-1"].gpus, 8)
        self.assertIsNone(s.nodes["od-1"].instance_id)
        got = shares(s)
        self.assertEqual(got[(201, "od-1")], 0.75)                      # ["CR_CPU_MEMORY"]: memory counts
        self.assertEqual(got[(205, "od-1")], 0.5)
        self.assertEqual(got[(204, "cb-1")], 0.5)                       # 4 of 8 GPUs, commas inside parentheses
        uid = [e for e in s.shares if e.job_id == 205][0]
        self.assertEqual((uid.username, uid.account), ("uid:1005", "none"))
        self.assertIn("od-2", s.holding)                                # COMPLETING holds resources...
        self.assertNotIn("od-2", s.busy)                                # ...but isn't busy

    def test_edge_cases(self):
        s = parsed("nodes-edge.json", "jobs-edge.json")
        self.assertEqual(set(s.nodes), {"e-wrapped", "e-unset", "e-nomem"})     # invalid records skipped
        w, u, n = s.nodes["e-wrapped"], s.nodes["e-unset"], s.nodes["e-nomem"]
        self.assertEqual((w.cpus, w.memory, w.gpus, w.states), (6, 30000, 3, {"MIXED"}))  # wrapped, string state
        self.assertEqual((u.cpus, u.memory), (4, 16000))     # unset effective_cpus, no specialized_memory
        self.assertEqual(n.memory, -512)
        self.assertEqual(s.jobs, 5)                          # 403 PENDING, 406 bad user_name, 407 no resources
        got = shares(s)
        self.assertEqual(got[(401, "e-wrapped")], 0.5)       # string job_state, wrapped numbers, tres_alloc_str
        j401 = [e for e in s.shares if e.job_id == 401][0]
        self.assertEqual((j401.cpus, j401.memory, j401.gpus), (3, 15000, 1.0))
        self.assertEqual(got[(402, "e-unset")], 0.25)        # ["CR_CPU"]: the full-node memory counts for nothing
        j402 = [e for e in s.shares if e.job_id == 402][0]
        self.assertEqual((j402.username, j402.account), ("uid:1002", "none"))
        self.assertAlmostEqual(got[(404, "e-wrapped")], 2 / 3.0, places=12)    # gres/gpu=4 over 2 nodes
        self.assertEqual(got[(404, "e-unset")], 0.25)        # no GPU on the node: CPU and memory only
        self.assertEqual([(e.job_id, e.node) for e in s.shares if e.job_id == 405], [(405, "e-nomem")])
        self.assertEqual(got[(405, "e-nomem")], 0.5)         # M <= 0: memory ignored
        self.assertEqual(got[(408, "e-unset")], 1.0)
        self.assertNotIn(403, {e.job_id for e in s.shares})
        self.assertIn("e-nomem", s.busy)                     # job 406 is skipped, but its node is busy

    def test_truncation_before_merging(self):
        doc = stub_slurm.load("jobs-v0.0.45.json")
        for job, name in zip(doc["jobs"][:2], ("u" * 128 + "-a", "u" * 128 + "-b")):
            job["user_name"] = name
            job["account"] = "p" * 200
        slurm = cc.SlurmData()
        slurm.nodes = cc.parse_slurm_nodes(stub_slurm.load("nodes-v0.0.45.json")["nodes"])
        cc.parse_slurm_jobs(slurm, doc["jobs"])
        self.assertEqual({(e.username, e.account) for e in slurm.shares if e.job_id in (201, 202)},
                         {("u" * 128, "p" * 128)})


class SlurmCommandTest(unittest.TestCase):
    """The commands, their timeouts and their failure codes."""

    def setUp(self):
        self.h = fakes.Harness("pcs")
        self.h.use_slurm("nodes-v0.0.45.json", "jobs-v0.0.45.json")

    def tearDown(self):
        self.h.close()

    def slurm_calls(self):
        return [c for c in self.h.runner.calls if os.path.basename(c.argv[0]) in ("scontrol", "squeue")]

    def test_commands_and_timeouts(self):
        self.assertEqual(self.h.run(), 0)
        calls = self.slurm_calls()
        self.assertEqual([c.argv for c in calls], [
            [os.path.join(self.h.slurm_bin, "scontrol"), "show", "nodes", "--json"],
            [os.path.join(self.h.slurm_bin, "squeue"), "--json", "--states=RUNNING,CONFIGURING,COMPLETING"]])
        self.assertEqual([c.timeout for c in calls], [30.0, 30.0])
        self.assertNotIn("AWS_ACCESS_KEY_ID", calls[0].env)
        self.assertEqual(self.h.metrics().stages_ok()["slurm"], 1)

    def test_timeout_is_the_time_left(self):
        self.h.runner.on(fakes.has_arg("tag:aws:pcs:cluster-id"),
                         fakes.ok(fakes.fixture_text("aws", "pcs-describe-instances-cluster-id.json")), seconds=82)
        self.h.run()
        self.assertEqual([c.timeout for c in self.slurm_calls()], [18.0, 18.0])

    def test_cannot_start_before_the_deadline(self):
        self.h.runner.on(fakes.has_arg("tag:aws:pcs:cluster-id"),
                         fakes.ok(fakes.fixture_text("aws", "pcs-describe-instances-cluster-id.json")), seconds=90)
        self.h.runner.on(("describe-volumes",), fakes.ok({"Volumes": []}), seconds=6)
        self.h.run()
        self.assertEqual(self.slurm_calls(), [])
        self.assertEqual(self.h.metrics().stage_errors()["slurm"], "Timeout")
        self.assertIn("WARNING scontrol skipped: run deadline", self.h.log())

    def failure(self, command, answer):
        self.h.runner.on(fakes.is_command(command), answer)
        self.assertEqual(self.h.run(), 0)
        m = self.h.metrics()
        for family in ("cluster_cost_user_usd_per_hour", "cluster_cost_unallocated_usd_per_hour",
                       "cluster_cost_idle_nodes"):
            self.assertEqual(m.series(family), [], family)
        self.assertTrue(m.series("cluster_cost_usd_per_hour"))
        self.assertEqual(m.value("cluster_cost_collector_up"), 0)
        return m.stage_errors()["slurm"]

    def test_errors_array_with_exit_0(self):
        """AC 44: exit 0 and a non-empty errors array fail the stage."""
        answer = fakes.ok(stub_slurm.read("jobs-errors.json"))
        self.assertEqual(self.failure("squeue", answer), "SlurmCommandFailed")
        self.assertIn("WARNING squeue failed: SlurmCommandFailed: squeue reported an error: "
                      "Unable to contact slurm controller (connect failure)", self.h.log())
        self.assertNotIn("Retrying the connection", self.h.log())       # warnings only at DEBUG

    def test_errors_array_on_scontrol(self):
        doc = stub_slurm.load("nodes-v0.0.45.json")
        doc["errors"] = [{"description": "Invalid node name specified", "error_number": 2017}]
        self.assertEqual(self.failure("scontrol", fakes.ok(doc)), "SlurmCommandFailed")
        self.assertEqual(len(self.slurm_calls()), 1)                    # squeue isn't run

    def test_non_zero_exit(self):
        answer = cc.CommandResult(rc=1, stderr="squeue: error: Unable to contact slurm controller\n")
        self.assertEqual(self.failure("squeue", answer), "SlurmCommandFailed")
        self.assertIn("squeue exited with 1: squeue: error: Unable to contact slurm controller", self.h.log())

    def test_timed_out(self):
        self.assertEqual(self.failure("scontrol", cc.CommandResult(rc=-9, timed_out=True)), "Timeout")
        self.assertIn("scontrol killed after 30 s", self.h.log())

    def test_cannot_be_executed(self):
        answer = cc.CommandResult(rc=127, stderr="[Errno 8] Exec format error", not_found=True)
        self.assertEqual(self.failure("squeue", answer), "SlurmNotFound")

    def test_invalid_json(self):
        for answer in (fakes.ok("squeue: no JSON plugin"), fakes.ok("[]"), fakes.ok({"jobs": {}}),
                       fakes.ok({"jobs": [], "errors": "x"}), fakes.ok({"nodes": []})):
            self.h.runner.reset()
            self.assertEqual(self.failure("squeue", answer), "SlurmJsonInvalid", answer.stdout)

    def test_warnings_at_debug_only(self):
        self.h.use_slurm("nodes-edge.json", "jobs-edge.json")
        self.h.run()
        self.assertNotIn("has no job_resources", self.h.log())
        self.h.environ["CLUSTER_COST_DEBUG"] = "1"
        self.h.clock.advance(60)
        self.h.run()
        self.assertIn("DEBUG squeue warning: Job 407 has no job_resources", self.h.log())
        self.assertIn("DEBUG scontrol warning: Job 407 has no job_resources", self.h.log())

    def test_data_parser_in_the_report(self):
        self.h.run()
        slurm = self.h.report()["slurm"]
        self.assertEqual(slurm["data_parser"], {"scontrol": "data_parser/v0.0.45", "squeue": "data_parser/v0.0.45"})
        self.assertEqual((slurm["nodes"], slurm["jobs"]), (7, 6))


class SlurmBinariesTest(unittest.TestCase):
    """design "Slurm binaries", through the injected file-system root."""

    def setUp(self):
        self.scratch = fakes.Scratch()
        self.root = self.scratch.path("root")

    def tearDown(self):
        self.scratch.cleanup()

    def client(self, path, names=("squeue", "scontrol")):
        directory = self.root + path if path.startswith("/opt") else path
        os.makedirs(directory, exist_ok=True)
        for name in names:
            fakes.write_executable(os.path.join(directory, name), "#!/bin/sh\nexit 0\n")
        return directory

    def tree(self, version):
        return self.client("/opt/aws/pcs/scheduler/slurm-%s/bin" % version)

    def link(self, target):
        os.makedirs(self.root + "/etc/aws/pcs/scheduler", exist_ok=True)
        os.symlink(target, self.root + "/etc/aws/pcs/scheduler/slurm")

    def find(self, platform="pcs", version=None, override=None, path=""):
        run = make_run(platform, root=self.root, slurm_bin_dir=override, path=path)
        if version is not None:
            run.cache.record_success("pcs|cluster", {"size": "SMALL", "status": "ACTIVE", "accounting_mode": "NONE",
                                                     "scheduler_version": version}, "", run.now)
        return cc.find_slurm_bin(run)

    def test_parallelcluster(self):
        self.assertEqual(self.find("parallelcluster"), (None, None))
        d = self.client("/opt/slurm/bin")
        self.assertEqual(self.find("parallelcluster"), (d, "/opt/slurm/bin"))

    def test_symlink_target_wins(self):
        for v in ("25.05", "25.11", "26.05"):
            self.tree(v)
        self.link("/opt/aws/pcs/scheduler/slurm-26.05")
        self.assertEqual(self.find(version="25.11"),
                         (self.root + "/opt/aws/pcs/scheduler/slurm-26.05/bin", "symlink /etc/aws/pcs/scheduler/slurm"))

    def test_relative_symlink(self):
        self.tree("25.11")
        self.tree("26.05")
        self.link("slurm-25.11/")
        self.assertEqual(self.find()[0], self.root + "/opt/aws/pcs/scheduler/slurm-25.11/bin")

    def test_scheduler_version_from_the_cache(self):
        for v in ("25.05", "25.11", "26.05"):
            self.tree(v)
        self.assertEqual(self.find(version="25.11"),
                         (self.root + "/opt/aws/pcs/scheduler/slurm-25.11/bin", "pcs scheduler.version"))

    def test_single_tree(self):
        self.tree("26.05")
        self.assertEqual(self.find(), (self.root + "/opt/aws/pcs/scheduler/slurm-26.05/bin",
                                       "only tree under /opt/aws/pcs/scheduler"))

    def test_several_trees_are_not_guessed(self):
        self.tree("25.11")
        self.tree("26.05")
        self.assertEqual(self.find(), (None, None))
        run = make_run("pcs", root=self.root)
        stage = run.health.stage("slurm")
        with self.assertLogs(cc.LOG, "DEBUG") as logs:
            self.assertIsNone(cc.read_slurm(run, stage, instances("pcs")))
        self.assertEqual(stage.code, "SlurmNotFound")
        self.assertEqual(run.deps.runner.calls, [])
        self.assertTrue(any("2 Slurm trees" in line for line in logs.output))
        self.assertTrue(any(line.startswith("WARNING:cluster_cost:slurm failed: SlurmNotFound")
                            for line in logs.output))

    def test_values_that_fail_the_checks_are_skipped(self):
        self.tree("25.11")
        self.tree("26.05")
        self.client("/opt/aws/pcs/scheduler/slurm-latest/bin")
        self.link("/opt/aws/pcs/scheduler/slurm-latest")              # basename doesn't match
        with self.assertLogs(cc.LOG, "DEBUG") as logs:
            self.assertEqual(self.find(version="25.11.1"), (None, None))     # nor does the version
        self.assertTrue(any("points to 'slurm-latest': skipped" in line for line in logs.output))
        self.assertTrue(any("scheduler version '25.11.1': skipped" in line for line in logs.output))

    def test_missing_tree_falls_through(self):
        self.tree("25.11")
        self.link("/opt/aws/pcs/scheduler/slurm-26.05")               # the node's version isn't installed
        self.assertEqual(self.find()[0], self.root + "/opt/aws/pcs/scheduler/slurm-25.11/bin")

    def test_path(self):
        a = self.client(self.scratch.path("a"), names=("squeue",))     # half a client
        b = self.client(self.scratch.path("b"))
        self.assertEqual(self.find(path="relative/bin:%s:%s" % (a, b)), (b, "PATH"))
        self.tree("25.11")
        self.tree("26.05")
        self.assertEqual(self.find(path=b), (b, "PATH"))                # after the PCS candidates
        os.chmod(os.path.join(b, "scontrol"), 0o644)                    # no execute bit: not a client
        self.assertEqual(self.find(path=b), (None, None))

    def test_override(self):
        d = self.client(self.scratch.path("override"))
        self.client("/opt/slurm/bin")
        self.assertEqual(self.find("parallelcluster", override=d), (d, "CLUSTER_COST_SLURM_BIN_DIR"))
        empty = self.scratch.path("empty")
        os.mkdir(empty)
        self.assertEqual(self.find("parallelcluster", override=empty), (None, None))   # no fallback
        self.assertEqual(self.find("parallelcluster", override="relative/dir"), (None, None))

    def test_harness_override_end_to_end(self):
        h = fakes.Harness("parallelcluster")
        try:
            del h.environ["CLUSTER_COST_SLURM_BIN_DIR"]
            self.assertEqual(h.run(), 0)
            self.assertEqual(h.metrics().stage_errors(), {"slurm": "SlurmNotFound"})
            os.makedirs(h.scratch.path("opt", "slurm", "bin"))
            fakes.write_slurm_stubs(h.scratch.path("opt", "slurm", "bin"), fakes.SLURM_NODES["parallelcluster"])
            h.clock.advance(60)
            self.assertEqual(h.run(), 0)
            self.assertEqual(h.metrics().stages_ok()["slurm"], 1)
            self.assertEqual(h.report()["slurm"]["found_by"], "/opt/slurm/bin")
        finally:
            h.close()


class ReadSlurmTest(unittest.TestCase):
    def test_mapping_check_failure_keeps_the_data(self):
        run = make_run("parallelcluster", slurm_bin_dir=None)
        run.deps.runner.slurm = ("nodes-v0.0.44.json", "jobs-v0.0.44.json")
        scratch = fakes.Scratch()
        try:
            fakes.write_slurm_stubs(scratch.bin, "nodes-v0.0.44.json")
            run.config.slurm_bin_dir = scratch.bin
            fleet = [i for i in instances("parallelcluster") if i.instance_id != "i-0a00000000000a003"]
            stage = run.health.stage("slurm")
            with self.assertLogs(cc.LOG, "WARNING") as logs:
                slurm = cc.read_slurm(run, stage, fleet)
        finally:
            scratch.cleanup()
        self.assertEqual(logs.output, ["WARNING:cluster_cost:slurm failed: NodeMappingIncomplete: busy Slurm nodes "
                                       "map to no running cluster instance: spot-dy-spot-cr-1"])
        self.assertEqual(stage.code, "NodeMappingIncomplete")
        self.assertEqual(slurm.busy_unmapped, ["spot-dy-spot-cr-1"])
        self.assertEqual(set(slurm.mapping), {"od-st-od-cr-1", "gpu-dy-gpu-cr-1"})
        self.assertEqual(json.loads(json.dumps(slurm.data_parser))["squeue"], "data_parser/v0.0.44")


if __name__ == "__main__":
    unittest.main()
