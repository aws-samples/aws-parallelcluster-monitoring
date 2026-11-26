"""Exposition and write (AC 41, AC 37 U), --dry-run and the run lock."""
import copy
import fcntl
import os
import stat
import unittest
from unittest import mock

import cluster_cost as cc

from . import fakes


def fleet_with_queues(*queues):
    recs = fakes.fixture("aws", "pc-describe-instances.json")[:1]
    base = fakes.fixture("aws", "pc-describe-instances.json")[1]
    for i, queue in enumerate(queues):
        rec = copy.deepcopy(base)
        rec["InstanceId"] = "i-0a00000000000e%03d" % i
        for t in rec["Tags"]:
            if t["Key"] == "parallelcluster:queue-name":
                t["Value"] = queue
        recs.append(rec)
    return recs


class RenderTest(unittest.TestCase):
    def test_family_order_and_blocks(self):
        self.assertEqual(len(cc.FAMILIES), 19)
        self.assertEqual(len(set(cc.FAMILY_NAMES)), 19)
        fam = {name: {(("x", "1"),): 1} for name in reversed(cc.FAMILY_NAMES)}
        text = cc.render_exposition(fam)
        parsed = fakes.Exposition(text)
        self.assertEqual(parsed.families, list(cc.FAMILY_NAMES))
        self.assertEqual(text.count("# TYPE "), 19)

    def test_series_sorted_values_and_no_timestamps(self):
        fam = {"cluster_cost_usd_per_hour": {(("component", "instance"), ("queue", "b")): 1 / 3,
                                             (("component", "instance"), ("queue", "a")): 2.0},
               "cluster_cost_collector_up": {(): 1},
               "cluster_cost_collector_run_duration_seconds": {(): 1.23456}}
        text = cc.render_exposition(fam)
        self.assertEqual(text.splitlines(), [
            "# HELP cluster_cost_usd_per_hour Estimated cost rate per cost line, in USD per hour.",
            "# TYPE cluster_cost_usd_per_hour gauge",
            'cluster_cost_usd_per_hour{component="instance",queue="a"} 2.000000000',
            'cluster_cost_usd_per_hour{component="instance",queue="b"} 0.333333333',
            "# HELP cluster_cost_collector_up 1 when the run produced a complete estimate, 0 otherwise.",
            "# TYPE cluster_cost_collector_up gauge",
            "cluster_cost_collector_up 1",
            "# HELP cluster_cost_collector_run_duration_seconds Duration of this run.",
            "# TYPE cluster_cost_collector_run_duration_seconds gauge",
            "cluster_cost_collector_run_duration_seconds 1.235",
        ])
        for line in text.splitlines():
            if not line.startswith("#"):
                self.assertEqual(len(line.rsplit("} ", 1)[-1].split()), 1 if "}" in line else 2)

    def test_escaping_and_caps(self):
        key = cc.label_key({"queue": 'a"b\\c\nd', "username": "u" * 200})
        text = cc.render_exposition({"cluster_cost_user_usd_per_hour": {key: 1}})
        self.assertIn('queue="a\\"b\\\\c\\nd"', text)
        self.assertIn('username="%s"' % ("u" * 128), text)
        parsed = fakes.Exposition(text)
        labels = parsed.series("cluster_cost_user_usd_per_hour")[0][0]
        self.assertEqual(labels["queue"], 'a"b\\c\nd')

    def test_absent_labels_and_empty_families_left_out(self):
        text = cc.render_exposition({"cluster_cost_nodes": {cc.label_key({"node_role": "head", "queue": None}): 1},
                                     "cluster_cost_idle_nodes": {}})
        self.assertIn('cluster_cost_nodes{node_role="head"} 1', text)
        self.assertNotIn("idle_nodes", text)


class WriteTest(unittest.TestCase):
    def setUp(self):
        self.h = fakes.Harness("parallelcluster")

    def tearDown(self):
        self.h.close()

    def test_file_mode_name_and_parse(self):
        self.assertEqual(self.h.run(), 0)
        path = self.h.textfile_path()
        self.assertEqual(os.path.basename(path), "cluster_cost.prom")
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o644)
        self.assertEqual(sorted(os.listdir(self.h.scratch.textfile)), ["cluster_cost.prom"])
        self.assertFalse(cc.TEXTFILE_TMP.endswith(".prom"))
        m = self.h.metrics()                                    # raises if a family is in two blocks
        self.assertEqual(m.families, [f for f in cc.FAMILY_NAMES if f in m.families])
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.h.scratch.state, "last-run.json")).st_mode), 0o640)

    def test_every_run_replaces_the_whole_file(self):
        self.h.run()
        first = os.stat(self.h.textfile_path()).st_ino
        self.h.clock.advance(60)
        self.h.runner.on(("describe-instances",), fakes.aws_error("UnauthorizedOperation", "DescribeInstances"))
        self.h.run()
        self.assertNotEqual(os.stat(self.h.textfile_path()).st_ino, first)
        self.assertEqual(self.h.metrics().series("cluster_cost_usd_per_hour"), [])

    def test_leftover_temporary_file_and_symlink(self):
        victim = self.h.scratch.path("victim")
        with open(victim, "w") as f:
            f.write("keep")
        os.symlink(victim, os.path.join(self.h.scratch.textfile, cc.TEXTFILE_TMP))
        self.assertEqual(self.h.run(), 0)
        with open(victim) as f:
            self.assertEqual(f.read(), "keep")
        self.assertEqual(sorted(os.listdir(self.h.scratch.textfile)), ["cluster_cost.prom"])

    def test_rename_failure_exits_1_and_keeps_the_old_file(self):
        self.h.run()
        with open(self.h.textfile_path()) as f:
            old = f.read()
        real = os.replace

        def failing(src, dst):
            if dst.endswith("cluster_cost.prom"):
                raise OSError(28, "No space left on device")
            return real(src, dst)
        self.h.clock.advance(60)
        with mock.patch.object(cc.os, "replace", failing):
            self.assertEqual(self.h.run(), 1)
        with open(self.h.textfile_path()) as f:
            self.assertEqual(f.read(), old)
        self.assertEqual(sorted(os.listdir(self.h.scratch.textfile)), ["cluster_cost.prom"])
        self.assertIn("ERROR couldn't write", self.h.log())

    def test_fsync_failure_exits_1(self):
        with mock.patch.object(cc.os, "fsync", side_effect=OSError(5, "Input/output error")):
            self.assertEqual(self.h.run(), 1)
        self.assertFalse(os.path.exists(self.h.textfile_path()))

    def test_price_cache_save_failure(self):
        real = cc.write_json_atomic

        def failing(directory, name, obj, mode=0o640):
            if name == cc.PRICES_FILE:
                raise OSError(30, "Read-only file system")
            return real(directory, name, obj, mode)
        with mock.patch.object(cc, "write_json_atomic", failing):
            self.assertEqual(self.h.run(), 1)
        m = self.h.metrics()                                       # the textfile is still written
        self.assertEqual(m.stage_errors()["state"], "StateWriteFailed")
        self.assertEqual(m.value("cluster_cost_collector_up"), 0)
        self.assertIn("ERROR couldn't save prices.json", self.h.log())

    def test_report_failure_is_only_a_warning(self):
        real = cc.write_json_atomic

        def failing(directory, name, obj, mode=0o640):
            if name == cc.REPORT_FILE:
                raise OSError(5, "Input/output error")
            return real(directory, name, obj, mode)
        with mock.patch.object(cc, "write_json_atomic", failing):
            self.assertEqual(self.h.run(), 0)
        self.assertIn("WARNING couldn't write last-run.json", self.h.log())

    def test_truncation_before_merging(self):
        long_a, long_b = "q" * 128 + "-a", "q" * 128 + "-b"
        self.h.runner.on(("describe-instances",), fakes.ok(fleet_with_queues(long_a, long_b)))
        self.h.runner.on(("describe-volumes",), fakes.ok({"Volumes": []}))
        self.h.run()
        m = self.h.metrics()
        lines = [(labels, v) for labels, v in m.series("cluster_cost_usd_per_hour") if labels.get("queue")]
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0][0]["queue"], "q" * 128)
        self.assertAlmostEqual(lines[0][1], 2 * 0.11655, places=9)
        self.assertEqual(m.value("cluster_cost_nodes", node_role="compute", queue="q" * 128,
                                 ec2_instance_type="c6i.large", purchase_option="on_demand"), 2)

    def test_utf8_replacement_and_escaping_in_the_file(self):
        recs = fleet_with_queues("bad\ud800q", 'we"ird\\q\nx')
        self.h.runner.on(("describe-instances",), fakes.ok(recs))
        self.h.runner.on(("describe-volumes",), fakes.ok({"Volumes": []}))
        self.assertEqual(self.h.run(), 0)
        with open(self.h.textfile_path(), "rb") as f:
            raw = f.read()
        text = raw.decode("utf-8")                                  # valid UTF-8
        self.assertIn('queue="bad?q"', text)
        self.assertIn('queue="we\\"ird\\\\q\\nx"', text)
        fakes.Exposition(text)

    def test_usd_rounded_to_9_decimals(self):
        self.h.run()
        for line in self.h.textfile().splitlines():
            if line.startswith("cluster_cost_usd_per_hour{"):
                self.assertRegex(line, r" [0-9]+\.[0-9]{9}$")


class RefusalTest(unittest.TestCase):
    """AC 37 (U): exit 2 and nothing written for an unsafe directory."""

    def setUp(self):
        self.h = fakes.Harness("parallelcluster")

    def tearDown(self):
        self.h.close()

    def test_not_owned_by_root(self):
        self.h.owner_uid = os.getuid() + 1            # tests run as root: inject the expected owner
        self.assertEqual(self.h.run(), 2)
        self.assertEqual(os.listdir(self.h.scratch.textfile), [])
        self.assertEqual(os.listdir(self.h.scratch.state), [])

    def test_group_writable_textfile_dir(self):
        real_lstat = os.lstat

        def lstat(path, *a, **kw):
            st = real_lstat(path, *a, **kw)
            if path == self.h.scratch.textfile:
                fields = list(st)
                fields[0] = st.st_mode | stat.S_IWGRP
                return os.stat_result(fields)
            return st
        with mock.patch.object(cc.os, "lstat", lstat):
            self.assertEqual(self.h.run(), 2)
        self.assertIn("writable by group or others", self.h.log())
        self.assertEqual(os.listdir(self.h.scratch.textfile), [])
        self.assertEqual(self.h.runner.calls, [])

    def test_textfile_dir_is_a_symlink(self):
        link = self.h.scratch.path("textfile-link")
        os.symlink(self.h.scratch.textfile, link)
        self.h.environ["CLUSTER_COST_TEXTFILE_DIR"] = link
        self.assertEqual(self.h.run(), 2)
        self.assertEqual(os.listdir(self.h.scratch.textfile), [])


class DryRunAndLockTest(unittest.TestCase):
    def setUp(self):
        self.h = fakes.Harness("pcs")

    def tearDown(self):
        self.h.close()

    def test_dry_run_writes_nothing(self):
        self.assertEqual(self.h.run("--dry-run"), 0)
        self.assertEqual(os.listdir(self.h.scratch.state), [])
        self.assertEqual(os.listdir(self.h.scratch.textfile), [])
        out = fakes.Exposition(self.h.stdout.getvalue())
        self.assertIn("cluster_cost_usd_per_hour", out.families)
        self.assertNotIn("state", out.stages_ok())
        self.assertEqual(set(out.stages_ok()), {"discovery", "ebs", "slurm", "on_demand_prices", "spot_prices", "pcs"})

    def test_dry_run_uses_the_cache_without_changing_it(self):
        self.h.run()
        with open(os.path.join(self.h.scratch.state, cc.PRICES_FILE)) as f:
            before = f.read()
        files = sorted(os.listdir(self.h.scratch.state))
        mtime = os.stat(self.h.textfile_path()).st_mtime_ns
        self.h.runner.reset()
        self.h.clock.advance(cc.DAY)                    # everything due
        self.h.runner.on(("pricing",), fakes.THROTTLED)
        self.assertEqual(self.h.run("--dry-run"), 0)
        self.assertTrue(self.h.runner.find("pricing"))
        with open(os.path.join(self.h.scratch.state, cc.PRICES_FILE)) as f:
            self.assertEqual(f.read(), before)          # no backoff, no cache update
        self.assertEqual(sorted(os.listdir(self.h.scratch.state)), files)
        self.assertEqual(os.stat(self.h.textfile_path()).st_mtime_ns, mtime)

    def test_lock_held_by_another_run(self):
        fd = os.open(os.path.join(self.h.scratch.state, cc.LOCK_FILE), os.O_RDWR | os.O_CREAT, 0o640)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(self.h.run(), 0)
            self.assertEqual(self.h.runner.calls, [])
            self.assertEqual(os.listdir(self.h.scratch.textfile), [])
            self.assertIn("INFO another run holds the lock", self.h.log())
            self.assertEqual(self.h.run("--dry-run"), 0)    # the dry run takes no lock
            self.assertTrue(self.h.runner.calls)
        finally:
            os.close(fd)
        self.assertEqual(self.h.run(), 0)
        self.assertTrue(os.path.exists(self.h.textfile_path()))


if __name__ == "__main__":
    unittest.main()
