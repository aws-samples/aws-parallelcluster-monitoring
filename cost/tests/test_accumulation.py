"""Accumulation (design "Accumulation and state", D8): the trapezoid, the
one-hour cap and the unaccounted seconds, a clock that moves backwards, the
month rollover and split, the first good run of each family, family
independence, the key cap, and unpriced resource-seconds (AC 18, 19, 34,
44, 45, finding 60)."""
import datetime
import unittest
from unittest import mock

import cluster_cost as cc

from . import fakes

B = datetime.datetime(2026, 12, 1, tzinfo=datetime.timezone.utc).timestamp()   # a UTC month boundary
START = fakes.START
LINE = cc.label_key({"component": "instance", "node_role": "compute", "queue": "od",
                     "ec2_instance_type": "c6i.large", "purchase_option": "on_demand"})
LINE2 = cc.label_key({"component": "ebs", "node_role": "compute", "queue": "od",
                      "ec2_instance_type": "c6i.large", "purchase_option": "on_demand"})
LINE3 = cc.label_key({"component": "instance", "node_role": "head", "ec2_instance_type": "t3.medium",
                      "purchase_option": "on_demand"})
USER = cc.label_key({"username": "user1", "account": "proj-a"})
IDLE = cc.label_key({"reason": "idle_node", "queue": "od", "ec2_instance_type": "c6i.large",
                     "purchase_option": "on_demand"})
SPOT_UNPRICED = ("instance", "spot_price_unavailable")
VOLUMES_UNAVAILABLE = ("volume", "volume_list_unavailable")


class Sim:
    """Runs step 8 (accumulate) on one State at chosen times."""

    def __init__(self):
        self.state = cc.State("parallelcluster/mycluster/ap-southeast-6")

    def step(self, now, lines=None, unpriced=None, users=None, unallocated=None, discovery=True, slurm=True):
        config = cc.Config(platform="parallelcluster", cluster_id=fakes.PC_CLUSTER, region=fakes.REGION,
                           state_dir="/nonexistent", textfile_dir="/nonexistent")
        clock = fakes.FakeClock(wall=now)
        run = cc.Run(config, cc.Deps(runner=fakes.FakeRunner(clock), clock=clock, environ={}))
        run.state = self.state
        cost = alloc = None
        if discovery:
            cost = cc.CostResult()
            cost.lines = dict(lines if lines is not None else {LINE: 3.6})
            cost.unpriced = dict(unpriced or {})
            if slurm:
                alloc = cc.Allocation()
                alloc.users = dict(users if users is not None else {USER: 3.6})
                alloc.unallocated = dict(unallocated or {})
        cc.accumulate(run, cost, alloc)
        return self

    def acc(self, bucket, key, period="month"):
        if isinstance(key, tuple) and key and isinstance(key[0], str):     # (kind, reason)
            key = cc.label_key({"kind": key[0], "reason": key[1]})
        return self.state.accumulated[period][bucket].get(cc.state_key(key))

    def unaccounted(self, family, period="month"):
        return self.state.unaccounted[period][family]


class IntegrationTest(unittest.TestCase):
    def test_first_run_integrates_nothing(self):
        s = Sim().step(START)
        st = s.state
        self.assertEqual((st.started_at, st.month, st.month_started_at), (START, "2026-11", START))
        for period in cc.PERIODS:
            self.assertEqual(s.acc("lines", LINE, period), 0.0)          # a known 0, exported
            self.assertEqual(s.acc("users", USER, period), 0.0)
            self.assertEqual(s.unaccounted("lines", period), 0.0)
            self.assertEqual(s.unaccounted("alloc", period), 0.0)
        self.assertEqual(st.families["lines"]["last_at"], START)
        self.assertEqual(st.families["alloc"]["last_at"], START)

    def test_steady_rate(self):
        s = Sim()
        for i in range(11):
            s.step(START + 60 * i)
        for period in cc.PERIODS:
            self.assertAlmostEqual(s.acc("lines", LINE, period), 0.6, delta=1e-12)
            self.assertAlmostEqual(s.acc("users", USER, period), 0.6, delta=1e-12)

    def test_trapezoid_with_changing_keys(self):
        s = Sim().step(START, lines={LINE: 1.0, LINE3: 2.0})
        s.step(START + 60, lines={LINE: 3.0, LINE2: 2.0})
        self.assertAlmostEqual(s.acc("lines", LINE), 2.0 / 60, delta=1e-12)      # (1 + 3) / 2 for a minute
        self.assertAlmostEqual(s.acc("lines", LINE2), 1.0 / 60, delta=1e-12)     # new key: 0 on the other side
        self.assertAlmostEqual(s.acc("lines", LINE3), 1.0 / 60, delta=1e-12)     # gone key: 0 on this side
        s.step(START + 120, lines={LINE: 3.0})
        self.assertAlmostEqual(s.acc("lines", LINE3), 1.0 / 60, delta=1e-12)     # keeps its total
        self.assertAlmostEqual(s.acc("lines", LINE2), 2.0 / 60, delta=1e-12)

    def test_gap_up_to_one_hour_is_integrated(self):
        s = Sim().step(START).step(START + 3600)
        self.assertAlmostEqual(s.acc("lines", LINE), 3.6, delta=1e-12)
        self.assertEqual(s.unaccounted("lines"), 0.0)

    def test_gap_over_one_hour(self):
        """AC 18: one hour at the last known rates; the rest is unaccounted."""
        s = Sim().step(START).step(START + 7200, lines={LINE: 7.2}, users={USER: 7.2})
        for period in cc.PERIODS:
            self.assertAlmostEqual(s.acc("lines", LINE, period), 3.6, delta=1e-12)
            self.assertAlmostEqual(s.acc("users", USER, period), 3.6, delta=1e-12)
            self.assertEqual(s.unaccounted("lines", period), 3600.0)
            self.assertEqual(s.unaccounted("alloc", period), 3600.0)
        s.step(START + 7260, lines={LINE: 7.2})                         # integrates from the new last_at
        self.assertAlmostEqual(s.acc("lines", LINE), 3.6 + 0.12, delta=1e-12)

    def test_clock_moving_backwards_adds_nothing(self):
        """AC 18."""
        s = Sim().step(START).step(START + 60)
        with self.assertLogs(cc.LOG, "WARNING") as logs:
            s.step(START + 30)
        self.assertAlmostEqual(s.acc("lines", LINE), 0.06, delta=1e-12)
        self.assertTrue(any("the clock moved backwards" in line for line in logs.output))
        self.assertEqual(s.state.families["lines"]["last_at"], START + 30)
        s.step(START + 90)
        self.assertAlmostEqual(s.acc("lines", LINE), 0.12, delta=1e-12)


class MonthTest(unittest.TestCase):
    def test_boundary_inside_a_window(self):
        """AC 19: split at the boundary, the rate interpolated there; the new
        month holds only the part after it."""
        s = Sim().step(B - 90, lines={LINE: 1.0}).step(B - 30, lines={LINE: 1.0})
        s.step(B + 30, lines={LINE: 3.0})
        st = s.state
        self.assertEqual((st.month, st.month_started_at), ("2026-12", B))
        self.assertAlmostEqual(s.acc("lines", LINE, "month"), 2.5 * 30 / 3600, delta=1e-12)   # rate 2 -> 3
        self.assertAlmostEqual(s.acc("lines", LINE, "since_start"), (60 + 1.5 * 30 + 2.5 * 30) / 3600, delta=1e-12)

    def test_boundary_inside_a_gap(self):
        s = Sim().step(B - 1800).step(B + 7200)
        self.assertAlmostEqual(s.acc("lines", LINE, "since_start"), 3.6, delta=1e-12)   # [B - 30 min, B + 30 min]
        self.assertAlmostEqual(s.acc("lines", LINE, "month"), 1.8, delta=1e-12)
        self.assertEqual(s.unaccounted("lines", "since_start"), 5400.0)
        self.assertEqual(s.unaccounted("lines", "month"), 5400.0)
        s = Sim().step(B - 7200).step(B + 600)
        self.assertAlmostEqual(s.acc("lines", LINE, "since_start"), 3.6, delta=1e-12)   # all in November
        self.assertEqual(s.acc("lines", LINE, "month"), 0.0)                            # a known 0
        self.assertEqual(s.unaccounted("lines", "since_start"), 4200.0)                 # 3600 s + 600 s
        self.assertEqual(s.unaccounted("lines", "month"), 600.0)

    def test_gap_over_whole_months(self):
        jan = datetime.datetime(2027, 1, 15, tzinfo=datetime.timezone.utc).timestamp()
        s = Sim().step(B - 1800).step(jan)
        self.assertEqual(s.state.month, "2027-01")
        self.assertEqual(s.acc("lines", LINE, "month"), 0.0)
        self.assertEqual(s.unaccounted("lines", "month"), jan - datetime.datetime(
            2027, 1, 1, tzinfo=datetime.timezone.utc).timestamp())
        self.assertEqual(s.unaccounted("lines", "since_start"), jan - B - 1800)

    def test_both_families_cross_in_one_run(self):
        """AC 34, first case."""
        s = Sim().step(B - 90).step(B - 30).step(B + 30)
        for bucket, key in (("lines", LINE), ("users", USER)):
            self.assertAlmostEqual(s.acc(bucket, key, "month"), 0.03, delta=1e-12)
            self.assertAlmostEqual(s.acc(bucket, key, "since_start"), 0.12, delta=1e-12)

    def test_alloc_50_minutes_behind_lines(self):
        """AC 34, second case: Slurm fails across the boundary."""
        s = Sim().step(B - 120).step(B - 60)
        s.step(B + 60, slurm=False)                     # the lines family rolls the month over
        s.step(B + 49 * 60, slurm=False)
        s.step(B + 50 * 60)
        for bucket, key in (("lines", LINE), ("users", USER)):
            self.assertAlmostEqual(s.acc(bucket, key, "month"), 3.0, delta=1e-9)          # 50 minutes in December
            self.assertAlmostEqual(s.acc(bucket, key, "since_start"), 3.12, delta=1e-9)   # and all of it
        self.assertEqual(s.unaccounted("alloc", "month"), 0.0)

    def test_rollover_in_a_run_whose_discovery_failed(self):
        s = Sim().step(B - 120).step(B - 60)
        since = s.acc("lines", LINE, "since_start")
        s.step(B + 60, discovery=False)
        st = s.state
        self.assertEqual((st.month, st.month_started_at), ("2026-12", B))
        self.assertEqual(st.accumulated["month"], {b: {} for b in cc.ACC_BUCKETS})
        self.assertEqual(st.unaccounted["month"], {"lines": 0.0, "alloc": 0.0})
        self.assertEqual(s.acc("lines", LINE, "since_start"), since)
        self.assertEqual(st.families["lines"]["last_at"], B - 60)
        s.step(B + 120)
        self.assertAlmostEqual(s.acc("lines", LINE, "month"), 0.12, delta=1e-12)
        self.assertAlmostEqual(s.acc("lines", LINE, "since_start"), since + 0.18, delta=1e-12)

    def test_rollover_without_tracking_does_nothing(self):
        s = Sim().step(B + 60, discovery=False)
        self.assertEqual((s.state.started_at, s.state.month), (None, None))


class FamilyTest(unittest.TestCase):
    def test_first_good_alloc_run_after_a_failed_start(self):
        """AC 44: no integration, and the time since tracking started is unaccounted."""
        s = Sim().step(START, slurm=False)
        self.assertIsNone(s.state.families["alloc"]["last_at"])
        s.step(START + 600)
        for period in cc.PERIODS:
            self.assertEqual(s.acc("users", USER, period), 0.0)
            self.assertEqual(s.unaccounted("alloc", period), 600.0)
            self.assertEqual(s.unaccounted("lines", period), 0.0)
            self.assertAlmostEqual(s.acc("lines", LINE, period), 0.6, delta=1e-12)
        s = Sim().step(B - 600, slurm=False).step(B + 600)
        self.assertEqual(s.unaccounted("alloc", "since_start"), 1200.0)
        self.assertEqual(s.unaccounted("alloc", "month"), 600.0)

    def test_families_are_independent(self):
        s = Sim().step(START)
        s.step(START + 60, slurm=False).step(START + 120, slurm=False)
        self.assertEqual(s.state.families["alloc"]["last_at"], START)
        s.step(START + 180)
        self.assertAlmostEqual(s.acc("users", USER), 0.18, delta=1e-12)     # the whole window, one trapezoid
        self.assertAlmostEqual(s.acc("lines", LINE), 0.18, delta=1e-12)
        t = START + 180
        for _ in range(12):                              # Slurm fails for two hours
            t += 600
            s.step(t, slurm=False)
        s.step(t + 60)
        self.assertAlmostEqual(s.acc("users", USER), 0.18 + 3.6, delta=1e-9)          # one hour, then unaccounted
        self.assertEqual(s.unaccounted("alloc"), 7260.0 - 3600.0)
        self.assertEqual(s.unaccounted("lines"), 0.0)
        self.assertAlmostEqual(s.acc("lines", LINE), 0.18 + 3.6 * 7260 / 3600, delta=1e-9)

    def test_unallocated_and_users_share_the_alloc_family(self):
        s = Sim().step(START, unallocated={IDLE: 1.0}).step(START + 60, unallocated={IDLE: 2.0})
        self.assertAlmostEqual(s.acc("unallocated", IDLE), 1.5 / 60, delta=1e-12)
        self.assertAlmostEqual(s.acc("users", USER), 3.6 / 60, delta=1e-12)
        self.assertEqual(set(s.state.families["alloc"]["last_rates"]), {cc.state_key(USER), cc.state_key(IDLE)})


class KeyCapTest(unittest.TestCase):
    def users(self, *names):
        return {cc.label_key({"username": n, "account": "acct-" + n}): 1.0 for n in names}

    def test_cap_per_period(self):
        other = cc.label_key({"username": "(other)", "account": "(other)"})
        with mock.patch.object(cc, "USER_KEY_CAP", 3):
            s = Sim().step(B - 120, users=self.users("u1", "u2", "u3"))
            with self.assertLogs(cc.LOG, "WARNING") as logs:
                s.step(B - 60, users=self.users("u1", "u2", "u3", "u4", "u5"))
            for period in cc.PERIODS:
                acc = s.state.accumulated[period]["users"]
                self.assertEqual(len(acc), 4)
                self.assertAlmostEqual(acc[cc.state_key(other)], 2 * 0.5 / 60, delta=1e-12)   # u4 and u5
            self.assertIn("2 new (username, account) keys over the cap of 3 for month: added to (other)",
                          "\n".join(logs.output))
            u4 = cc.state_key(self.users("u4").popitem()[0])
            with self.assertLogs(cc.LOG, "WARNING"):
                s.step(B - 30, users=self.users("u4"))
                s.step(B + 60, users=self.users("u4"))   # a new month: u4 gets its own key there
            self.assertIn(u4, s.state.accumulated["month"]["users"])
            self.assertNotIn(u4, s.state.accumulated["since_start"]["users"])
            self.assertGreater(s.state.accumulated["month"]["users"][u4], 0)

    def test_cap_is_5000(self):
        self.assertEqual(cc.USER_KEY_CAP, 5000)
        names = ["user%04d" % i for i in range(5001)]
        with self.assertLogs(cc.LOG, "WARNING"):
            s = Sim().step(START, users=self.users(*names))
        acc = s.state.accumulated["since_start"]["users"]
        self.assertEqual(len(acc), 5001)                 # 5,000 keys and (other)
        self.assertIn(cc.OTHER_USER_KEY, acc)
        self.assertNotIn(cc.state_key(self.users("user5000").popitem()[0]), acc)   # the last in key order


class UnpricedSecondsTest(unittest.TestCase):
    """AC 45 (U): resource-seconds integrated with the lines family."""

    def test_one_instance_unpriced_every_minute(self):
        s = Sim()
        for i in range(11):
            s.step(START + 60 * i, lines={}, unpriced={SPOT_UNPRICED: 1})
            if i:
                for period in cc.PERIODS:
                    self.assertAlmostEqual(s.acc("unpriced", SPOT_UNPRICED, period), 60.0 * i, delta=1e-9)
        self.assertEqual(s.state.accumulated["month"]["lines"], {})             # and no cost

    def test_across_a_month_boundary(self):
        s = Sim().step(B - 90, lines={}, unpriced={SPOT_UNPRICED: 1}).step(B - 30, lines={},
                                                                          unpriced={SPOT_UNPRICED: 1})
        s.step(B + 30, lines={}, unpriced={SPOT_UNPRICED: 1})
        self.assertAlmostEqual(s.acc("unpriced", SPOT_UNPRICED, "month"), 30.0, delta=1e-9)
        self.assertAlmostEqual(s.acc("unpriced", SPOT_UNPRICED, "since_start"), 120.0, delta=1e-9)

    def test_two_hour_gap_adds_one_hour(self):
        s = Sim().step(START, unpriced={SPOT_UNPRICED: 2}).step(START + 7200, unpriced={SPOT_UNPRICED: 2})
        self.assertAlmostEqual(s.acc("unpriced", SPOT_UNPRICED), 7200.0, delta=1e-9)   # 2 resources x 1 hour
        self.assertEqual(s.unaccounted("lines"), 3600.0)

    def test_resource_priced_again_adds_half_the_window(self):
        s = Sim().step(START, unpriced={SPOT_UNPRICED: 1}).step(START + 60, unpriced={})
        self.assertAlmostEqual(s.acc("unpriced", SPOT_UNPRICED), 30.0, delta=1e-9)
        s.step(START + 120, unpriced={})
        self.assertAlmostEqual(s.acc("unpriced", SPOT_UNPRICED), 30.0, delta=1e-9)

    def test_volume_list_unavailable_integrates(self):
        """Finding 60: instances without usable volume records count in
        cluster_cost_unpriced_resource_seconds like the other unpriced counts."""
        h = fakes.Harness("parallelcluster")
        try:
            h.runner.on(("describe-volumes",), fakes.THROTTLED)
            for rc, m in h.runs(3):
                self.assertEqual(m.unpriced()[VOLUMES_UNAVAILABLE], 5)
            m = h.metrics()
            for period in cc.PERIODS:
                self.assertAlmostEqual(m.value("cluster_cost_unpriced_resource_seconds", period=period, kind="volume",
                                               reason="volume_list_unavailable"), 5 * 120.0, delta=1e-6)
                self.assertAlmostEqual(m.value("cluster_cost_unpriced_resource_seconds", period=period,
                                               kind="instance", reason="not_in_price_list"), 120.0, delta=1e-6)
        finally:
            h.close()


class ExportTest(unittest.TestCase):
    """What a run exports when its stages fail."""

    def setUp(self):
        self.h = fakes.Harness("parallelcluster")

    def tearDown(self):
        self.h.close()

    def accumulated(self, m):
        return {f: m.series(f) for f in ("cluster_cost_accumulated_usd", "cluster_cost_user_accumulated_usd",
                                         "cluster_cost_unallocated_accumulated_usd",
                                         "cluster_cost_unpriced_resource_seconds")}

    def test_failed_discovery_keeps_the_totals(self):
        self.h.use_slurm("nodes-v0.0.44.json", "jobs-v0.0.44.json")
        self.h.run()
        self.h.clock.advance(60)
        self.h.run()
        good = self.h.metrics()
        self.h.clock.advance(60)
        self.h.runner.on(("describe-instances",), fakes.aws_error("RequestLimitExceeded", "DescribeInstances"))
        self.h.run()
        m = self.h.metrics()
        self.assertEqual(m.stage_errors(), {"discovery": "Throttled"})
        for family in ("cluster_cost_usd_per_hour", "cluster_cost_nodes", "cluster_cost_unpriced_resources",
                       "cluster_cost_user_usd_per_hour", "cluster_cost_unallocated_usd_per_hour",
                       "cluster_cost_idle_nodes"):
            self.assertEqual(m.series(family), [], family)
        self.assertEqual(self.accumulated(m), self.accumulated(good))
        self.assertTrue(all(self.accumulated(m).values()))
        self.assertEqual(m.series("cluster_cost_period_start_timestamp_seconds"),
                         good.series("cluster_cost_period_start_timestamp_seconds"))
        self.assertEqual(len(m.series("cluster_cost_unaccounted_seconds")), 4)

    def test_unaccounted_seconds_always_exported(self):
        self.h.runner.on(("describe-instances",), fakes.aws_error("RequestLimitExceeded", "DescribeInstances"))
        self.h.run()
        m = self.h.metrics()
        self.assertEqual({(l["period"], l["family"]): v for l, v in m.series("cluster_cost_unaccounted_seconds")},
                         {(p, f): 0 for p in cc.PERIODS for f in cc.ACC_FAMILIES})
        self.assertEqual(m.series("cluster_cost_period_start_timestamp_seconds"), [])   # no tracking yet

    def test_month_start_after_a_rollover(self):
        self.h.clock._wall = B - 60
        self.h.run()
        self.h.clock._wall = B + 60
        self.h.run()
        m = self.h.metrics()
        self.assertEqual(m.value("cluster_cost_period_start_timestamp_seconds", period="month"), B)
        self.assertEqual(m.value("cluster_cost_period_start_timestamp_seconds", period="since_start"), B - 60)

    def test_last_success_timestamp(self):
        recs = fakes.fixture("aws", "pc-describe-instances.json")[:3]        # head, od and spot: all priced
        self.h.runner.on(("describe-instances",), fakes.ok(recs))
        self.h.run()
        m = self.h.metrics()
        self.assertEqual(m.value("cluster_cost_collector_up"), 1)
        self.assertEqual(m.value("cluster_cost_collector_last_success_timestamp_seconds"), START)
        self.assertEqual(self.h.state()["last_success_at"], START)
        self.h.clock.advance(60)
        self.h.runner.on(fakes.is_command("squeue"), cc.CommandResult(rc=1, stderr="squeue: error\n"))
        self.h.run()
        m = self.h.metrics()
        self.assertEqual(m.value("cluster_cost_collector_up"), 0)
        self.assertEqual(m.value("cluster_cost_collector_last_success_timestamp_seconds"), START)


if __name__ == "__main__":
    unittest.main()
