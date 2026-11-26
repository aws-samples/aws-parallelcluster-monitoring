"""State files (AC 20): state.json, its backup and the corrupt copy, the
save and load orders, a crash between the renames, StateUnreadable and
StateWriteFailed, the cluster identity, and --dry-run."""
import json
import os
import stat
import unittest
from unittest import mock

import cluster_cost as cc

from . import fakes

START = fakes.START


class StateFileTest(unittest.TestCase):
    def setUp(self):
        self.h = fakes.Harness("parallelcluster")
        self.h.use_slurm("nodes-v0.0.44.json", "jobs-v0.0.44.json")

    def tearDown(self):
        self.h.close()

    def path(self, name):
        return os.path.join(self.h.scratch.state, name)

    def read(self, name):
        with open(self.path(name), "rb") as f:
            return f.read()

    def write(self, name, data):
        with open(self.path(name), "wb") as f:
            f.write(data if isinstance(data, bytes) else json.dumps(data).encode())

    def files(self):
        return sorted(os.listdir(self.h.scratch.state))

    def run_at(self, seconds, *argv):
        self.h.clock._wall = START + seconds
        return self.h.run(*argv)

    def month_total(self):
        return self.h.metrics().sum("cluster_cost_accumulated_usd", period="month")

    def test_first_and_second_run(self):
        self.assertEqual(self.run_at(0), 0)
        self.assertIn("INFO no state.json yet: first run", self.h.log())
        self.assertEqual(self.files(), [".lock", "last-run.json", "prices.json", "state.json"])
        self.assertEqual(stat.S_IMODE(os.stat(self.path("state.json")).st_mode), 0o640)
        st = self.h.state()
        self.assertEqual((st["schema"], st["cluster"], st["started_at"], st["month"], st["month_started_at"]),
                         (1, "parallelcluster/mycluster/ap-southeast-6", START, "2026-11", START))
        self.assertEqual(set(st["families"]), {"lines", "alloc"})
        self.assertEqual(set(st["accumulated"]["month"]), {"lines", "users", "unallocated", "unpriced"})
        first = self.read("state.json")
        self.run_at(60)
        self.assertEqual(self.read("state.json.bak"), first)              # state.json -> state.json.bak
        self.assertEqual(self.h.state()["families"]["lines"]["last_at"], START + 60)
        self.assertNotIn("state.json.tmp", self.files())
        self.assertEqual(self.h.metrics().stages_ok()["state"], 1)

    def test_corrupt_state_recovered_from_the_backup(self):
        """AC 20: the backup is used, the next save keeps it, renames the bad
        file to state.json.corrupt and writes a valid state.json."""
        self.run_at(0)
        self.run_at(60)
        rate = sum(v for _, v in self.h.metrics().series("cluster_cost_usd_per_hour"))
        backup = self.read("state.json.bak")                              # the state of the first run
        self.write("state.json", b'{"schema": 1, "cluster": "parallel')
        self.assertEqual(self.run_at(120), 0)
        log = self.h.log()
        self.assertIn("ERROR state.json unreadable (doesn't parse): trying state.json.bak", log)
        self.assertIn("ERROR accumulators loaded from state.json.bak (state.json unreadable)", log)
        m = self.h.metrics()
        self.assertEqual(m.stages_ok()["state"], 1)                       # not StateUnreadable
        self.assertEqual(self.read("state.json.bak"), backup)            # kept
        self.assertEqual(self.read("state.json.corrupt"), b'{"schema": 1, "cluster": "parallel')
        st = self.h.state()
        self.assertEqual(st["families"]["lines"]["last_at"], START + 120)
        self.assertAlmostEqual(self.month_total(), rate * 120 / 3600, delta=1e-8)   # from the backup's last run
        self.run_at(180)                                                  # a valid state.json: normal rotation
        self.assertEqual(json.loads(self.read("state.json.bak").decode())["families"]["lines"]["last_at"],
                         START + 120)
        self.assertEqual(self.read("state.json.corrupt"), b'{"schema": 1, "cluster": "parallel')

    def test_crash_between_the_renames_leaves_a_loadable_file(self):
        self.run_at(0)
        self.run_at(60)
        second = self.read("state.json")
        real = os.replace

        def crash(src, dst):
            if src.endswith(cc.STATE_TMP):
                raise OSError(5, "Input/output error")
            return real(src, dst)
        self.h.clock._wall = START + 120
        with mock.patch.object(cc.os, "replace", crash):
            self.assertEqual(self.h.run(), 1)                             # StateWriteFailed: exit 1...
        m = self.h.metrics()                                              # ...after writing the textfile
        self.assertEqual(m.stage_errors(), {"state": "StateWriteFailed"})
        self.assertEqual(m.value("cluster_cost_collector_up"), 0)
        self.assertIn("ERROR couldn't save state.json", self.h.log())
        self.assertNotIn("state.json", self.files())                      # between the two renames
        self.assertNotIn("state.json.tmp", self.files())
        self.assertEqual(self.read("state.json.bak"), second)
        self.assertEqual(self.h.report()["exit_code"], 1)
        self.assertEqual(self.run_at(180), 0)
        self.assertIn("ERROR accumulators loaded from state.json.bak (state.json missing)", self.h.log())
        self.assertEqual(self.h.metrics().stages_ok()["state"], 1)
        st = self.h.state()
        self.assertEqual(st["started_at"], START)
        self.assertEqual(st["families"]["lines"]["last_at"], START + 180)
        self.assertEqual(self.read("state.json.bak"), second)             # not rotated: state.json was missing

    def test_both_files_unusable(self):
        """AC 20: accumulation restarts, StateUnreadable for that run, logged."""
        self.run_at(0)
        self.run_at(60)
        self.write("state.json", b"\x00\xff garbage")
        self.write("state.json.bak", {"schema": 2})
        self.assertEqual(self.run_at(120), 0)
        m = self.h.metrics()
        self.assertEqual(m.stage_errors(), {"state": "StateUnreadable"})
        self.assertEqual(m.value("cluster_cost_collector_up"), 0)
        self.assertIn("ERROR state.json (doesn't parse) and state.json.bak (invalid schema) are unusable: "
                      "accumulation restarts (StateUnreadable)", self.h.log())
        st = self.h.state()
        self.assertEqual(st["started_at"], START + 120)                  # tracking restarts
        self.assertEqual(self.month_total(), 0)
        self.assertEqual(self.read("state.json.corrupt"), b"\x00\xff garbage")
        self.assertEqual(json.loads(self.read("state.json.bak").decode()), {"schema": 2})   # left alone
        self.run_at(180)
        self.assertEqual(self.h.metrics().stages_ok()["state"], 1)
        self.assertEqual(json.loads(self.read("state.json.bak").decode())["started_at"], START + 120)

    def test_backup_unusable_and_state_missing(self):
        self.write("state.json.bak", b"[]")
        self.run_at(0)
        self.assertEqual(self.h.metrics().stage_errors(), {"state": "StateUnreadable"})
        self.assertEqual(self.files(), [".lock", "last-run.json", "prices.json", "state.json", "state.json.bak"])

    def test_invalid_values_are_corrupt(self):
        self.run_at(0)
        self.run_at(60)
        good = json.loads(self.read("state.json").decode())

        def variant(change):
            doc = json.loads(json.dumps(good))
            change(doc)
            return doc
        line = next(iter(good["accumulated"]["month"]["lines"]))
        variants = {
            "schema": variant(lambda d: d.update(schema=2)),
            "accumulated.month.lines": variant(lambda d: d["accumulated"]["month"]["lines"].update({line: -1.0})),
            "accumulated.since_start.users": variant(
                lambda d: d["accumulated"]["since_start"]["users"].update({"not a key": 1.0})),
            "families.alloc.last_at": variant(lambda d: d["families"]["alloc"].update(last_at="yesterday")),
            "month": variant(lambda d: d.update(month="Nov 2026")),
            "unaccounted_seconds.month.alloc": variant(lambda d: d["unaccounted_seconds"]["month"].update(alloc=None)),
            "families.lines.last_unpriced": variant(lambda d: d["families"]["lines"].pop("last_unpriced")),
        }
        for what, doc in variants.items():
            self.write("state.json", doc)
            self.h.stderr.truncate(0)
            self.h.stderr.seek(0)
            self.h.clock.advance(60)
            self.h.run()
            self.assertIn("ERROR state.json unreadable (invalid %s): trying state.json.bak" % what, self.h.log())
            self.assertEqual(self.h.metrics().stages_ok()["state"], 1, what)
        text = self.read("state.json").decode()
        self.assertIn('"started_at":%s' % json.dumps(START), text)
        self.write("state.json", text.replace('"started_at":%s' % json.dumps(START), '"started_at":NaN').encode())
        self.h.clock.advance(60)
        self.h.run()
        self.assertIn("ERROR state.json unreadable (invalid started_at)", self.h.log())

    def test_cluster_identity_mismatch(self):
        self.run_at(0)
        doc = self.h.state()
        doc["cluster"] = "parallelcluster/othercluster/ap-southeast-6"
        self.write("state.json", doc)
        self.assertEqual(self.run_at(60), 0)
        self.assertIn("WARNING state.json holds the accumulators of parallelcluster/othercluster/ap-southeast-6, "
                      "not parallelcluster/mycluster/ap-southeast-6: starting a fresh state", self.h.log())
        m = self.h.metrics()
        self.assertEqual(m.stages_ok()["state"], 1)
        st = self.h.state()
        self.assertEqual((st["cluster"], st["started_at"]), ("parallelcluster/mycluster/ap-southeast-6", START + 60))
        self.assertEqual(json.loads(self.read("state.json.bak").decode())["cluster"],
                         "parallelcluster/othercluster/ap-southeast-6")

    def test_state_write_failed(self):
        with mock.patch.object(cc, "write_state_file", side_effect=OSError(28, "No space left on device")):
            self.assertEqual(self.run_at(0), 1)
        m = self.h.metrics()
        self.assertEqual(m.stage_errors(), {"state": "StateWriteFailed"})
        self.assertIn("ERROR couldn't save state.json: [Errno 28] No space left on device", self.h.log())
        self.assertIn("prices.json", self.files())                         # still saved
        self.assertNotIn("state.json", self.files())

    def test_write_failure_replaces_state_unreadable(self):
        self.write("state.json", b"garbage")
        with mock.patch.object(cc, "write_state_file", side_effect=OSError(30, "Read-only file system")):
            self.assertEqual(self.run_at(0), 1)
        self.assertEqual(self.h.metrics().stage_errors(), {"state": "StateWriteFailed"})

    def test_dry_run_writes_no_state(self):
        self.run_at(0)
        before = {name: self.read(name) for name in self.files() if name != ".lock"}
        self.assertEqual(self.run_at(60, "--dry-run"), 0)
        out = fakes.Exposition(self.h.stdout.getvalue())
        rate = sum(v for _, v in out.series("cluster_cost_usd_per_hour"))
        self.assertAlmostEqual(out.sum("cluster_cost_accumulated_usd", period="month"), rate / 60, delta=1e-8)
        self.assertEqual({name: self.read(name) for name in self.files() if name != ".lock"}, before)
        self.assertNotIn("state.json.tmp", self.files())
        self.assertNotIn("state", out.stages_ok())


class StateUnitTest(unittest.TestCase):
    def test_round_trip(self):
        st = cc.State("pcs/pcs_abc123def4/ap-southeast-6")
        st.started_at, st.month, st.month_started_at = START, "2026-11", START
        st.accumulated["month"]["users"][cc.state_key(cc.label_key({"username": "u", "account": "a"}))] = 1.5
        st.unaccounted["since_start"]["alloc"] = 60.0
        back = cc.State.from_json(json.loads(json.dumps(st.to_json())))
        self.assertEqual(back.to_json(), st.to_json())

    def test_keys(self):
        key = cc.label_key({"username": "u\"1", "account": "a"})
        self.assertEqual(cc.key_labels(cc.state_key(key)), key)
        self.assertTrue(cc._valid_state_key(cc.state_key(key)))
        for bad in ('[["b","1"],["a","2"]]', '[["a","1"],["a","2"]]', '[["1a","x"]]', '{"a":1}', "[[1, 2]]",
                    '[["a","%s"]]' % ("x" * 129), "nope"):
            self.assertFalse(cc._valid_state_key(bad), bad)


if __name__ == "__main__":
    unittest.main()
