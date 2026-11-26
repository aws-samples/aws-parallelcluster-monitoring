"""The price cache (design "Price cache"): the file, the due rules, stage
reporting while a source fails (AC 13-15, U), retry and backoff (AC 32) and
the Spot schedule (AC 40)."""
import copy
import json
import os
import stat
import unittest

import cluster_cost as cc

from . import fakes

OD_KEY = "od|c6i.large|RunInstances|Shared"


def pc_runner_two_nodes(h):
    """A ParallelCluster fleet of the head node and the od node, without volumes."""
    recs = fakes.fixture("aws", "pc-describe-instances.json")[:2]
    h.runner.on(("describe-instances",), fakes.ok(recs))
    h.runner.on(("describe-volumes",), fakes.ok({"Volumes": []}))
    return recs


class CacheFileTest(unittest.TestCase):
    def setUp(self):
        self.h = fakes.Harness("pcs")

    def tearDown(self):
        self.h.close()

    def path(self):
        return os.path.join(self.h.scratch.state, cc.PRICES_FILE)

    def test_round_trip_and_mode(self):
        self.h.run()
        with open(self.path()) as f:
            data = json.load(f)
        self.assertEqual(data["schema"], 1)
        loaded = cc.PriceCache.load(self.path())
        self.assertEqual(loaded.to_json(), data)
        self.assertEqual(stat.S_IMODE(os.stat(self.path()).st_mode), 0o640)
        for key in (OD_KEY, "spot|refresh", "ebs|storage|gp3", "pcs|controller|SMALL", "pcs|node|std",
                    "pcs|cluster", "pcs|node_groups", "volumes"):
            self.assertIn(key, data["entries"])
            entry = data["entries"][key]
            self.assertEqual(sorted(k for k in entry if k != "detail"),
                             ["fetched_at", "last_attempt_at", "last_error", "unit", "value"])
        self.assertEqual(data["entries"]["pcs|cluster"]["value"],
                         {"size": "SMALL", "status": "ACTIVE", "accounting_mode": "STANDARD",
                          "scheduler_version": "25.11"})
        self.assertEqual(data["entries"]["pcs|node_groups"]["value"]["pcs_ng00000od1"], "od")
        self.assertEqual(data["entries"][OD_KEY]["detail"], {"sku": "6SJA63RYV6RFTYZ9"})
        # a second run with the cache makes no Pricing call
        self.h.runner.reset()
        self.h.clock.advance(60)
        self.h.run()
        self.assertEqual(self.h.runner.find("pricing"), [])

    def assert_discarded(self, content, why):
        self.h.run()
        good = self.h.prices()
        with open(self.path(), "w") as f:
            f.write(content(good) if callable(content) else content)
        self.h.runner.reset()
        self.h.clock.advance(60)
        self.h.stderr.truncate(0)
        self.h.stderr.seek(0)
        self.h.run()
        self.assertIn("WARNING price cache %s %s" % (self.path(), why), self.h.log())
        # everything is fetched again: 3 On-Demand keys, EBS Storage and AWSPCS
        self.assertEqual(len(self.h.runner.find("pricing")), 5)

    def test_corrupt_file_discarded(self):
        self.assert_discarded("{not json", "doesn't parse: discarded")

    def test_other_schema_discarded(self):
        self.assert_discarded(lambda good: json.dumps(dict(good, schema=2)), "has another schema: discarded")
        self.assert_discarded(lambda good: json.dumps(dict(good, schema=True)), "has another schema: discarded")

    def test_wrong_types_discarded(self):
        def mutate(change):
            def content(good):
                g = copy.deepcopy(good)
                change(g)
                return json.dumps(g)
            return content
        changes = [
            lambda g: g["entries"][OD_KEY].update(value="0.1"),
            lambda g: g["entries"][OD_KEY].update(value=-1),
            lambda g: g["entries"][OD_KEY].update(value=True),
            lambda g: g["entries"][OD_KEY].update(fetched_at="yesterday"),
            lambda g: g["entries"][OD_KEY].update(last_error="Some text from the CLI"),
            lambda g: g["entries"]["pcs|cluster"].update(value=["SMALL"]),
            lambda g: g["entries"]["volumes"]["value"].update({"i-0b00000000000a001": {"fetched_at": 1, "volumes": [
                {"VolumeId": "vol-1", "VolumeType": "gp3", "Size": 0}]}}),
            lambda g: g["entries"].update({"unknown|key": dict(g["entries"][OD_KEY])}),
            lambda g: g["backoff"].update({"pricing": {"until": "soon", "code": "Throttled", "since": 1}}),
            lambda g: g["backoff"].update({"elsewhere": {"until": 1.0, "code": "Throttled", "since": 1}}),
            lambda g: g.update(entries=[]),
        ]
        for change in changes:
            self.assert_discarded(mutate(change), "holds a value of the wrong type: discarded")

    def test_nan_discarded(self):
        self.assert_discarded(lambda good: json.dumps(good).replace('"value": 0.11655', '"value": NaN'),
                              "holds a value of the wrong type: discarded")

    def test_missing_file_is_a_first_run(self):
        self.h.run()
        self.assertNotIn("price cache", self.h.log())


class DueRulesTest(unittest.TestCase):
    def entry(self, cache, key, last_error, at=0.0):
        cache.entries[key] = {"value": None if last_error else 1.0, "unit": "USD/h", "fetched_at": at,
                              "last_attempt_at": at, "last_error": last_error}

    def check(self, intervals, success, not_found, failed):
        for err, interval in ((None, success), ("not_in_price_list", not_found), ("Throttled", failed),
                              ("AwsApiError", failed)):
            cache = cc.PriceCache()
            self.entry(cache, "k", err)
            self.assertFalse(cache.due("k", intervals, interval - 1), (intervals, err))
            self.assertTrue(cache.due("k", intervals, interval), (intervals, err))
        self.assertTrue(cc.PriceCache().due("missing", intervals, 0.0))

    def test_intervals(self):
        self.check(cc.OD_INTERVALS, 86400, 21600, 900)
        self.check(cc.PCS_FEE_INTERVALS, 86400, 21600, 900)
        self.check(cc.EBS_INTERVALS, 86400, 86400, 900)
        self.check(cc.PCS_CLUSTER_INTERVALS, 600, 600, 600)
        self.check(cc.PCS_NODE_GROUPS_INTERVALS, 3600, 3600, 600)

    def test_backoff(self):
        cache = cc.PriceCache()
        cache.set_backoff("pricing", "Throttled", 1000.0)
        self.assertEqual(cache.backoff["pricing"], {"until": 1900.0, "code": "Throttled", "since": 1000.0})
        self.assertTrue(cache.backed_off("pricing", 1899.0))
        self.assertFalse(cache.backed_off("pricing", 1900.0))
        cache.set_backoff("spot", "AccessDenied", 0.0)
        cache.set_backoff("pcs", "Timeout", 0.0)
        self.assertEqual((cache.backoff["spot"]["until"], cache.backoff["pcs"]["until"]), (300.0, 600.0))

    def test_timestamps_are_run_start_times(self):
        h = fakes.Harness("pcs")
        try:
            # Every call takes 10 s of wall time; the cache still records the run's start.
            h.runner.on(("pricing",), fakes.THROTTLED, seconds=10)
            h.runner.on(("get-cluster",), lambda call: fakes.ok(fakes.fixture_text("aws", "pcs-get-cluster.json")),
                        seconds=10)
            h.run()
            data = h.prices()
            stamps = {e["last_attempt_at"] for e in data["entries"].values()}
            self.assertEqual(stamps, {fakes.START})
            self.assertEqual(data["backoff"]["pricing"], {"until": fakes.START + 900, "code": "Throttled",
                                                          "since": fakes.START})
            self.assertEqual(data["entries"]["pcs|cluster"]["fetched_at"], fakes.START)
        finally:
            h.close()

    def test_volumes_every_run(self):
        h = fakes.Harness("parallelcluster")
        try:
            for _ in h.runs(3):
                pass
            self.assertEqual(len(h.runner.find("describe-volumes")), 3)
        finally:
            h.close()


class StageReportingTest(unittest.TestCase):
    """AC 13-15 (U): a failing source keeps its stage failed in every run."""

    def setUp(self):
        self.h = fakes.Harness("parallelcluster")
        pc_runner_two_nodes(self.h)

    def tearDown(self):
        self.h.close()

    def test_denied_nothing_cached(self):
        self.h.runner.on(("pricing",), fakes.DENIED)
        for rc, m in self.h.runs(10):
            self.assertEqual(rc, 0)
            self.assertEqual(m.value("cluster_cost_collector_stage_error", stage="on_demand_prices",
                                     code="AccessDenied"), 1)
            self.assertIn('cluster_cost_collector_stage_error{code="AccessDenied",stage="on_demand_prices"} 1',
                          m.text)
            self.assertEqual(m.unpriced(), {("instance", "price_api_error"): 2})
            self.assertEqual(m.series("cluster_cost_usd_per_hour"), [])      # absent, never 0
        self.assertEqual(len(self.h.runner.find("pricing")), 1)

    def test_valid_cache_then_due_while_denied(self):
        self.h.run()
        self.h.runner.on(("pricing",), fakes.DENIED)
        self.h.runner.reset()
        for minute in (1, 2, 60 * 24 - 1):
            self.h.clock._wall = fakes.START + 60 * minute
            self.h.run()
            self.assertEqual(self.h.metrics().stage_errors(), {}, minute)    # not due: OK
        self.assertEqual(self.h.runner.find("pricing"), [])
        calls = []
        for minute in range(60 * 24, 60 * 24 + 40):
            self.h.clock._wall = fakes.START + 60 * minute
            before = len(self.h.runner.find("pricing"))
            self.h.run()
            if len(self.h.runner.find("pricing")) > before:
                calls.append(minute)
            m = self.h.metrics()
            self.assertEqual(m.stage_errors()["on_demand_prices"], "AccessDenied", minute)
            self.assertEqual(m.unpriced(), {}, minute)                     # cached values stay in use
        self.assertEqual(calls, [1440, 1455, 1470])
        self.assertIn("source pricing backed off until", self.h.log())

    def test_api_error_entry_waits_alone(self):
        recs = fakes.fixture("aws", "pc-describe-instances.json")[:2]
        extra = copy.deepcopy(recs[1])
        extra.update(InstanceId="i-0a00000000000c009", InstanceType="x9z.huge")
        self.h.runner.on(("describe-instances",), fakes.ok(recs + [extra]))
        self.h.runner.on(fakes.is_pricing_for("instanceType=x9z.huge"), fakes.aws_error("InvalidParameterValue"))
        for rc, m in self.h.runs(20):
            self.assertEqual(m.stage_errors(), {"on_demand_prices": "AwsApiError"})
            self.assertEqual(m.unpriced(), {("instance", "price_api_error"): 1})
        x9 = [c for c in self.h.runner.find("pricing") if "Type=TERM_MATCH,Field=instanceType,Value=x9z.huge" in c.argv]
        self.assertEqual(len(x9), 2)                                       # minutes 0 and 15
        self.assertNotIn("pricing", self.h.prices()["backoff"])

    def test_budget_deferral_leaves_the_stage_ok(self):
        recs = fakes.fixture("aws", "pc-describe-instances.json")[:2]
        for i in range(30):
            rec = copy.deepcopy(recs[1])
            rec.update(InstanceId="i-0a00000000000e%03d" % i, InstanceType="zz%d.large" % i)
            recs.append(rec)
        self.h.runner.on(("describe-instances",), fakes.ok(recs))
        self.h.run()
        m = self.h.metrics()
        self.assertNotIn("on_demand_prices", m.stage_errors())
        self.assertEqual(m.unpriced()[("instance", "price_lookup_pending")], 12)

    def test_stored_code_in_key_order(self):
        recs = fakes.fixture("aws", "pc-describe-instances.json")[:2]
        for iid, itype in (("i-0a00000000000c010", "x9z.huge"), ("i-0a00000000000c011", "a1.weird")):
            rec = copy.deepcopy(recs[1])
            rec.update(InstanceId=iid, InstanceType=itype)
            recs.append(rec)
        self.h.runner.on(("describe-instances",), fakes.ok(recs))
        self.h.runner.on(fakes.is_pricing_for("instanceType=a1.weird"), fakes.ok({"FormatVersion": None}))
        self.h.runner.on(fakes.is_pricing_for("instanceType=x9z.huge"), fakes.aws_error("InvalidParameterValue"))
        self.h.run()
        self.assertEqual(self.h.metrics().stage_errors()["on_demand_prices"], "InvalidResponse")  # first call
        self.h.clock.advance(60)
        self.h.run()                                                       # no call: both entries wait
        self.assertEqual(self.h.metrics().stage_errors()["on_demand_prices"], "InvalidResponse")  # a1 < x9
        why = self.h.report()["stages"]["on_demand_prices"]["why"]
        self.assertEqual(why, "entry waiting after a failed call: od|a1.weird|RunInstances|Shared")


class BackoffTest(unittest.TestCase):
    """AC 32."""

    def test_ten_runs_with_pricing_spot_and_pcs_throttled(self):
        h = fakes.Harness("pcs")
        try:
            first = fakes.fixture("aws", "pcs-describe-instances-cluster-id.json")
            fleet = [first[0], first[1], first[2]]           # login node, one On-Demand, one Spot node
            h.runner.on(fakes.has_arg("tag:aws:pcs:cluster-id"), fakes.ok(fleet))
            h.runner.on(fakes.has_arg("tag:pcs-cluster-id"), fakes.ok([]))
            h.runner.on(("pricing",), fakes.THROTTLED)
            h.runner.on(("describe-spot-price-history",), fakes.aws_error("RequestLimitExceeded",
                                                                           "DescribeSpotPriceHistory"))
            h.runner.on(("pcs",), fakes.aws_error("ThrottlingException", "GetCluster"))
            spot_minutes = []
            for minute, (rc, m) in enumerate(h.runs(10)):
                self.assertEqual(rc, 0)
                errors = m.stage_errors()
                for stage in ("on_demand_prices", "spot_prices", "ebs", "pcs"):
                    self.assertEqual(errors.get(stage), "Throttled", (minute, stage, errors))
                if len(h.runner.find("describe-spot-price-history")) > len(spot_minutes):
                    spot_minutes.append(minute)
                self.assertNotIn("discovery", errors)
            self.assertEqual(len(h.runner.find("pricing")), 1)
            self.assertEqual(spot_minutes, [0, 5])
            self.assertEqual(len(h.runner.find("pcs", "get-cluster")), 1)
            self.assertEqual(len(h.runner.find("pcs", "list-compute-node-groups")), 0)
            self.assertEqual(len(h.runner.find("describe-instances")), 20)
            self.assertEqual(len(h.runner.find("describe-volumes")), 10)
        finally:
            h.close()


class SpotRefreshTest(unittest.TestCase):
    """AC 40."""

    def setUp(self):
        self.h = fakes.Harness("parallelcluster")
        self.base = fakes.fixture("aws", "pc-describe-instances.json")[:3]   # head, od, spot (6a)
        self.fleet = list(self.base)
        self.h.runner.on(("describe-instances",), lambda call: fakes.ok(self.fleet))
        self.h.runner.on(("describe-volumes",), fakes.ok({"Volumes": []}))
        self.minutes = []

    def tearDown(self):
        self.h.close()

    def run_at(self, minute):
        self.h.clock._wall = fakes.START + 60 * minute
        before = len(self.h.runner.find("describe-spot-price-history"))
        self.h.run()
        calls = self.h.runner.find("describe-spot-price-history")
        self.assertLessEqual(len(calls) - before, 1)                         # at most one per run
        if len(calls) > before:
            self.minutes.append(minute)
        return self.h.metrics()

    def add_spot(self, iid, itype="c6i.large", az="ap-southeast-6b"):
        rec = copy.deepcopy(self.base[2])
        rec.update(InstanceId=iid, InstanceType=itype, Az=az)
        self.fleet.append(rec)

    def test_scale_up_between_refreshes(self):
        self.run_at(0)
        self.run_at(1)
        self.add_spot("i-0a00000000000f001")                                 # a new pair at minute 2
        m = self.run_at(2)
        self.assertEqual(m.unpriced(), {})
        last = self.h.runner.find("describe-spot-price-history")[-1].argv
        self.assertIn("Name=availability-zone,Values=ap-southeast-6a,ap-southeast-6b", last)
        self.assertEqual(last[last.index("--instance-types") + 1:last.index("--product-descriptions")], ["c6i.large"])
        self.assertEqual(last[last.index("--start-time") + 1], last[last.index("--end-time") + 1])
        for minute in range(3, 13):
            self.run_at(minute)
        self.assertEqual(self.minutes, [0, 2, 7, 12])
        entry = self.h.prices()["entries"]["spot|c6i.large|ap-southeast-6b|Linux/UNIX"]
        self.assertEqual(entry["value"], 0.0388)
        self.assertEqual(entry["detail"], {"price_timestamp": "2026-11-25T21:05:00+00:00",
                                           "query_time": cc.utc_iso(fakes.START + 60 * 12)})

    def test_missing_pair_is_cached_as_unavailable(self):
        self.add_spot("i-0a00000000000f002", itype="m6i.large", az="ap-southeast-6a")
        for minute in range(0, 7):
            m = self.run_at(minute)
            self.assertEqual(m.unpriced(), {("instance", "spot_price_unavailable"): 1}, minute)
            self.assertNotIn("spot_prices", m.stage_errors())
        self.assertEqual(self.minutes, [0, 5])
        self.assertEqual(self.h.prices()["entries"]["spot|m6i.large|ap-southeast-6a|Linux/UNIX"]["last_error"],
                         "spot_price_unavailable")

    def test_no_call_during_a_backoff_new_pairs_included(self):
        def spot(call):
            if self.h.clock.wall() < fakes.START + 240:
                return fakes.aws_error("RequestLimitExceeded", "DescribeSpotPriceHistory")
            return fakes.ok(fakes.stub_aws.spot_answer(call.argv[1:]))
        self.h.runner.on(("describe-spot-price-history",), spot)
        m = self.run_at(0)
        self.assertEqual(m.stage_errors()["spot_prices"], "Throttled")
        self.add_spot("i-0a00000000000f003")
        m = self.run_at(2)
        self.assertEqual(m.stage_errors()["spot_prices"], "Throttled")
        self.assertEqual(m.unpriced(), {("instance", "spot_price_unavailable"): 2})
        m = self.run_at(5)
        self.assertNotIn("spot_prices", m.stage_errors())
        self.assertEqual(m.unpriced(), {})
        self.assertEqual(self.minutes, [0, 5])

    def test_stale_spot_price_not_used(self):
        self.run_at(0)
        self.h.runner.on(("describe-spot-price-history",), fakes.aws_error("UnauthorizedOperation",
                                                                            "DescribeSpotPriceHistory"))
        m = self.run_at(5)
        self.assertEqual(m.unpriced(), {})                                   # cached, under 6 h old
        self.assertEqual(m.stage_errors()["spot_prices"], "AccessDenied")
        m = self.run_at(6 * 60)
        self.assertEqual(m.unpriced(), {("instance", "spot_price_unavailable"): 1})
        self.assertEqual(m.stage_errors()["spot_prices"], "AccessDenied")


if __name__ == "__main__":
    unittest.main()
