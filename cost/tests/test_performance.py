"""NFR-4: 2,000 running jobs on 500 nodes parse and allocate in under 5 s."""
import json
import os
import random
import tempfile
import time
import unittest

import cluster_cost as cc

from . import fakes

NODES = 500
JOBS = 2000


def fleet():
    """500 compute instances and the scontrol and squeue output of their
    Slurm nodes and 2,000 running jobs, with socket and core lists."""
    rng = random.Random(4)
    instances, nodes, jobs = [], [], []
    for i in range(NODES):
        ip = "10.0.%d.%d" % (i // 250, i % 250 + 1)
        gpu = i % 5 == 0
        instances.append(cc.Instance(instance_id="i-%017x" % (i + 1), instance_type="p4d.24xlarge" if gpu else "c6i.24xlarge",
                                     az="ap-southeast-6a", tenancy="default", usage_operation="RunInstances",
                                     purchase_option="on_demand", role="compute", queue="gpu" if gpu else "cpu",
                                     private_ip=ip, private_dns="ip-%s.ap-southeast-6.compute.internal" % ip.replace(".", "-")))
        nodes.append({"name": "node-%03d" % i, "cpus": 96, "effective_cpus": 96, "real_memory": 768000,
                      "specialized_memory": 0, "gres": "gpu:a100:8(S:0-1)" if gpu else "", "address": ip,
                      "hostname": "ip-" + ip.replace(".", "-"), "instance_id": "", "state": ["MIXED"],
                      "boot_time": {"set": True, "infinite": False, "number": 1795600000},
                      "free_mem": {"set": True, "infinite": False, "number": 100000}, "partitions": ["q"]})
    for j in range(JOBS):
        width = rng.choice([1, 1, 1, 2, 4])
        first = rng.randrange(NODES - width)
        entries, details = [], []
        for k in range(width):
            n = first + k
            cpus = rng.choice([1, 2, 4, 8, 16])
            entries.append({"index": k, "name": "node-%03d" % n, "cpus": {"count": cpus, "used": 0},
                            "memory": {"used": 0, "allocated": cpus * 4000},
                            "sockets": [{"index": s, "cores": [{"index": c, "status": ["ALLOCATED"]}
                                                               for c in range(cpus // 2 or 1)]} for s in range(2)]})
            details.append("gpu:a100:1(IDX:%d)" % (j % 8) if n % 5 == 0 else "")
        jobs.append({"job_id": 10000 + j, "user_name": "user%d" % (j % 40), "user_id": 1000 + j % 40,
                     "account": "proj-%d" % (j % 7), "job_state": ["RUNNING"], "gres_detail": details,
                     "tres_alloc_str": "cpu=%d,node=%d" % (sum(e["cpus"]["count"] for e in entries), width),
                     "job_resources": {"select_type": ["CR_CPU_MEMORY"],
                                       "nodes": {"count": width, "whole": False, "allocation": entries}},
                     "name": "job-%d" % j, "partition": "q"})
    meta = {"plugin": {"data_parser": "data_parser/v0.0.45"}}
    return (instances, json.dumps({"nodes": nodes, "meta": meta, "errors": [], "warnings": []}),
            json.dumps({"jobs": jobs, "meta": meta, "errors": [], "warnings": []}))


class PerformanceTest(unittest.TestCase):
    def test_2000_jobs_on_500_nodes(self):
        instances, nodes_text, jobs_text = fleet()
        self.assertGreater(len(jobs_text), 2000000)                   # the JSON of a large cluster
        bin_dir = tempfile.mkdtemp(prefix="cost-perf-")
        try:
            for name in ("scontrol", "squeue"):
                fakes.write_executable(os.path.join(bin_dir, name), "#!/bin/sh\nexit 0\n")
            config = cc.Config(platform="pcs", cluster_id=fakes.PCS_CLUSTER, region=fakes.REGION,
                               state_dir="/nonexistent", textfile_dir="/nonexistent", slurm_bin_dir=bin_dir)
            clock = fakes.FakeClock()
            runner = fakes.FakeRunner(clock)
            runner.on(fakes.is_command("scontrol"), fakes.ok(nodes_text))
            runner.on(fakes.is_command("squeue"), fakes.ok(jobs_text))
            run = cc.Run(config, cc.Deps(runner=runner, clock=clock, environ={}))
            cost = cc.CostResult()
            cost.node_cost = {inst.instance_id: 32.77 if inst.queue == "gpu" else 4.08 for inst in instances}
            started = time.monotonic()
            stage = run.health.stage("slurm")
            slurm = cc.read_slurm(run, stage, instances)
            alloc = cc.allocate(run, stage, slurm, cc.Discovery(instances, 0), cost)
            elapsed = time.monotonic() - started
        finally:
            for name in ("scontrol", "squeue"):
                os.unlink(os.path.join(bin_dir, name))
            os.rmdir(bin_dir)
        self.assertTrue(stage.ok, stage.describe())
        self.assertEqual((len(slurm.nodes), slurm.jobs), (NODES, JOBS))
        self.assertEqual(len(slurm.mapping), NODES)
        self.assertAlmostEqual(sum(alloc.users.values()) + sum(alloc.unallocated.values()),
                               sum(cost.node_cost.values()), delta=1e-6)
        self.assertLess(elapsed, 5.0, "parse and allocate took %.2f s" % elapsed)


if __name__ == "__main__":
    unittest.main()
