"""Pricing: price parsing (AC 46), instance pricing and the call budget, EBS
(AC 8, AC 52, finding 60) and PCS fees (AC 7, finding 60)."""
import copy
import json
import unittest

import cluster_cost as cc

from . import fakes

H_NOV = 720.0   # 30-day month of fakes.START


def make_run(platform="parallelcluster", now=fakes.START):
    config = cc.Config(platform=platform, cluster_id=fakes.PC_CLUSTER if platform == "parallelcluster"
                       else fakes.PCS_CLUSTER, region=fakes.REGION, state_dir="/nonexistent",
                       textfile_dir="/nonexistent")
    clock = fakes.FakeClock(wall=now)
    return cc.Run(config, cc.Deps(runner=fakes.FakeRunner(clock), clock=clock, environ={}))


def price_list(name):
    return cc.parse_price_list(fakes.fixture("pricing", name))


def with_price(doc, index, new_price, new_sku):
    """A copy of product `index` of a get-products document with another price and SKU."""
    item = json.loads(doc["PriceList"][index])
    old_sku = item["product"]["sku"]
    text = json.dumps(item).replace(old_sku, new_sku)
    item = json.loads(text)
    for term in item["terms"]["OnDemand"].values():
        for dim in term["priceDimensions"].values():
            dim["pricePerUnit"]["USD"] = new_price
    return json.dumps(item)


class PriceParsingTest(unittest.TestCase):
    def test_awspcs_products_by_attributes(self):
        items = price_list("awspcs-us-east-1.json")
        self.assertEqual(len(items), 9)
        fees = {}
        ignored = []
        for it in items:
            key = cc.pcs_fee_key(it.attributes)
            if key is None:
                ignored.append(it)
            else:
                fees.setdefault(key, []).append(it)
        self.assertEqual(len(ignored), 1)
        self.assertEqual(ignored[0].dimensions, [("GB-Mo", 0.81)])          # accounting storage
        units = {k: v[0].dimensions[0][0] for k, v in fees.items()}
        self.assertEqual(units["pcs|controller|SMALL"], "Hours")
        self.assertEqual(units["pcs|accounting|SMALL"], "Hour")
        self.assertEqual(units["pcs|node|std"], "Hour")
        self.assertEqual(units["pcs|node|adv"], "hour")
        want = {"pcs|controller|SMALL": 0.5825, "pcs|controller|MEDIUM": 3.2579, "pcs|controller|LARGE": 6.5426,
                "pcs|accounting|SMALL": 0.17, "pcs|accounting|MEDIUM": 0.98, "pcs|accounting|LARGE": 1.96,
                "pcs|node|std": 0.08, "pcs|node|adv": 0.64}
        for key, price in want.items():
            got, reason, skus = cc.select_price(fees[key], cc.PCS_FEE_UNITS)
            self.assertEqual((got, reason, len(skus)), (price, None, 1), key)

    def test_unit_checks_are_case_insensitive_and_per_item(self):
        adv = [it for it in price_list("awspcs-us-east-1.json") if cc.pcs_fee_key(it.attributes) == "pcs|node|adv"]
        self.assertEqual(cc.select_price(adv, {"hour": 1.0})[0], 0.64)
        self.assertEqual(cc.select_price(adv, cc.OD_UNITS)[1], "not_in_price_list")   # wrong unit: ignored
        storage = price_list("ebs-storage.json")
        io2 = [it for it in storage if it.attributes.get("volumeApiName") == "io2"]
        self.assertEqual(io2[0].dimensions[0][0], "GB-month")
        self.assertEqual(cc.select_price(io2, cc.EBS_UNITS["storage"][0])[0], 0.1449)

    def test_invalid_dimensions_ignored(self):
        doc = fakes.fixture("pricing", "od-c6i.large-linux-shared.json")
        for bad in ("-1", "NaN", "inf", "abc", None, True):
            item = json.loads(doc["PriceList"][0])
            for term in item["terms"]["OnDemand"].values():
                for dim in term["priceDimensions"].values():
                    dim["pricePerUnit"]["USD"] = bad
            items = cc.parse_price_list({"PriceList": [json.dumps(item)]})
            self.assertEqual(items[0].dimensions, [], bad)
            self.assertEqual(cc.select_price(items, cc.OD_UNITS)[1], "not_in_price_list")
        # entries that aren't JSON strings are skipped
        self.assertEqual(cc.parse_price_list({"PriceList": ["{not json", 5, None]}), [])

    def test_one_zero_and_several_prices(self):
        one = price_list("od-c6i.large-linux-shared.json")
        self.assertEqual(cc.select_price(one, cc.OD_UNITS), (0.11655, None, ["6SJA63RYV6RFTYZ9"]))
        self.assertEqual(cc.select_price(price_list("empty.json"), cc.OD_UNITS), (None, "not_in_price_list", []))
        several = price_list("od-c6i.large-ambiguous.json")
        price, reason, skus = cc.select_price(several, cc.OD_UNITS)
        self.assertEqual((price, reason, skus), (None, "ambiguous_price", ["6SJA63RYV6RFTYZ9", "6SJA63RYV6RFTYZX"]))
        # two products with the same price are one distinct price
        doc = fakes.fixture("pricing", "od-c6i.large-linux-shared.json")
        doc["PriceList"].append(with_price(doc, 0, "0.1165500000", "SAMEPRICE0000001"))
        self.assertEqual(cc.select_price(cc.parse_price_list(doc), cc.OD_UNITS)[:2], (0.11655, None))

    def test_ambiguous_on_demand_logs_the_skus(self):
        h = fakes.Harness("parallelcluster")
        try:
            h.runner.on(fakes.is_pricing_for("instanceType=c6i.large"),
                        fakes.ok(fakes.fixture_text("pricing", "od-c6i.large-ambiguous.json")))
            self.assertEqual(h.run(), 0)
            self.assertIn("od|c6i.large|RunInstances|Shared: several distinct prices in the price list "
                          "(SKUs 6SJA63RYV6RFTYZ9, 6SJA63RYV6RFTYZX): unpriced (ambiguous_price)", h.log())
            m = h.metrics()
            self.assertEqual(m.unpriced()[("instance", "ambiguous_price")], 1)
            self.assertNotIn("on_demand_prices", m.stage_errors())    # a successful call
            self.assertEqual(m.value("cluster_cost_collector_up"), 0)
            self.assertEqual(h.prices()["entries"]["od|c6i.large|RunInstances|Shared"]["last_error"], "ambiguous_price")
        finally:
            h.close()

    def test_ambiguous_ebs_item_and_pcs_fee(self):
        storage = fakes.fixture("pricing", "ebs-storage.json")
        gp3 = [i for i, s in enumerate(storage["PriceList"]) if '"volumeApiName": "gp3"' in s][0]
        storage["PriceList"].append(with_price(storage, gp3, "0.2000000000", "EBSCOPY000000001"))
        pcs = fakes.fixture("pricing", "awspcs-us-east-1.json")
        std = [i for i, s in enumerate(pcs["PriceList"]) if "SlurmStd" in s][0]
        pcs["PriceList"].append(with_price(pcs, std, "0.0900000000", "PCSCOPY000000001"))
        h = fakes.Harness("pcs")
        try:
            h.runner.on(fakes.is_pricing_for("productFamily=Storage"), fakes.ok(storage))
            h.runner.on(("pricing", "AWSPCS"), fakes.ok(pcs))
            h.run()
            log = h.log()
            self.assertIn("ebs|storage|gp3: several distinct prices", log)
            self.assertIn("EBSCOPY000000001", log)
            self.assertIn("pcs|node|std: several distinct prices", log)
            self.assertIn("PCSCOPY000000001", log)
            m = h.metrics()
            self.assertEqual(m.unpriced()[("volume", "ambiguous_price")], 8)
            self.assertEqual(m.unpriced()[("fee", "ambiguous_price")], 6)   # 6 standard-tier instances
            self.assertNotIn("ebs", m.stage_errors())
            self.assertNotIn("pcs", m.stage_errors())
        finally:
            h.close()


class TimestampTest(unittest.TestCase):
    def test_parse_iso8601(self):
        want = 1795636800.0                                   # 2026-11-25T20:00:00Z
        for text in ("2026-11-25T20:00:00Z", "2026-11-25T20:00:00.000Z", "2026-11-25T20:00:00+00:00",
                     "2026-11-25T20:00:00.000000+00:00", "2026-11-25T21:00:00+01:00", "2026-11-25T20:00:00"):
            self.assertEqual(cc.parse_iso8601(text), want, text)
        for bad in ("", "yesterday", "2026-13-01T00:00:00Z", None, 1795636800):
            self.assertIsNone(cc.parse_iso8601(bad), bad)
        self.assertEqual(cc.utc_iso(want), "2026-11-25T20:00:00Z")

    def test_latest_spot_timestamp_wins_in_every_format(self):
        run = make_run()
        history = {"SpotPriceHistory": [
            {"AvailabilityZone": "ap-southeast-6a", "InstanceType": "c6i.large", "ProductDescription": "Linux/UNIX",
             "SpotPrice": "0.050000", "Timestamp": "2026-11-25T23:00:00.000Z"},
            {"AvailabilityZone": "ap-southeast-6a", "InstanceType": "c6i.large", "ProductDescription": "Linux/UNIX",
             "SpotPrice": "0.040000", "Timestamp": "2026-11-25T20:00:00+00:00"},
            {"AvailabilityZone": "ap-southeast-6a", "InstanceType": "c6i.large", "ProductDescription": "Linux/UNIX",
             "SpotPrice": "0.090000", "Timestamp": "not a time"},
            {"AvailabilityZone": "ap-southeast-6a", "InstanceType": "c6i.large", "ProductDescription": "Linux/UNIX",
             "SpotPrice": "-1", "Timestamp": "2026-11-26T00:00:00Z"},
        ]}
        run.deps.runner.on(("describe-spot-price-history",), fakes.ok(history))
        recs = fakes.fixture("aws", "pc-describe-instances.json")
        inst, _ = cc.parse_instance(recs[2], cc.parse_tags(recs[2]["Tags"]), "parallelcluster")
        stage = run.health.stage("spot_prices")
        cc.refresh_spot(run, stage, [inst])
        self.assertTrue(stage.ok)
        self.assertEqual(cc.spot_price(run, cc.spot_key(inst)), (0.05, None))


class InstancePricingTest(unittest.TestCase):
    def setUp(self):
        self.h = fakes.Harness("parallelcluster")
        self.recs = fakes.fixture("aws", "pc-describe-instances.json")

    def tearDown(self):
        self.h.close()

    def fleet(self, *changes):
        recs = copy.deepcopy(self.recs[:2])           # head node and the od node
        for change in changes:
            rec = copy.deepcopy(self.recs[1])
            rec.update(change)
            recs.append(rec)
        self.h.runner.on(("describe-instances",), fakes.ok(recs))

    def rates(self):
        return {r["instance_id"]: (r["usd_per_hour"], r["unpriced_reason"]) for r in self.h.report()["instances"]}

    def test_on_demand_rhel_dedicated_host_capacity_block_spot(self):
        self.fleet({"InstanceId": "i-0a00000000000c001", "UsageOperation": "RunInstances:0010"},
                   {"InstanceId": "i-0a00000000000c002", "Tenancy": "dedicated"},
                   {"InstanceId": "i-0a00000000000c003", "Tenancy": "host"},
                   {"InstanceId": "i-0a00000000000c004", "Lifecycle": "capacity-block"},
                   {"InstanceId": "i-0a00000000000c005", "Lifecycle": "spot"},
                   {"InstanceId": "i-0a00000000000c006", "Lifecycle": "spot", "Az": "ap-southeast-6b"},
                   {"InstanceId": "i-0a00000000000c007", "Lifecycle": "spot", "UsageOperation": "RunInstances:0g00"})
        self.h.run()
        rates = self.rates()
        self.assertEqual(rates["i-0a00000000000a001"], (0.0554, None))
        self.assertEqual(rates["i-0a00000000000a002"], (0.11655, None))
        self.assertEqual(rates["i-0a00000000000c001"], (0.14535, None))           # RHEL
        self.assertEqual(rates["i-0a00000000000c002"], (0.12821, None))           # Dedicated
        self.assertEqual(rates["i-0a00000000000c003"], (None, "dedicated_host"))
        self.assertEqual(rates["i-0a00000000000c004"], (None, "capacity_block"))
        self.assertEqual(rates["i-0a00000000000c005"], (0.0412, None))            # latest of two timestamps
        self.assertEqual(rates["i-0a00000000000c006"], (0.0388, None))            # latest, the other AZ
        self.assertEqual(rates["i-0a00000000000c007"], (None, "spot_platform_unsupported"))
        od_calls = [c.argv for c in self.h.runner.find("pricing") if any("instanceType" in a for a in c.argv)]
        filters = sorted(tuple(a for a in argv if a.startswith("Type=TERM_MATCH")) for argv in od_calls)
        self.assertIn(("Type=TERM_MATCH,Field=regionCode,Value=ap-southeast-6",
                       "Type=TERM_MATCH,Field=instanceType,Value=c6i.large",
                       "Type=TERM_MATCH,Field=operation,Value=RunInstances:0010",
                       "Type=TERM_MATCH,Field=tenancy,Value=Shared",
                       "Type=TERM_MATCH,Field=capacitystatus,Value=Used"), filters)
        self.assertEqual(len(od_calls), 4)     # c6i Linux Shared, RHEL, Dedicated; t3.medium. No host or CB call.
        for argv in od_calls:
            self.assertEqual(argv[argv.index("--region") + 1], "us-east-1")
        spot = self.h.runner.find("describe-spot-price-history")
        self.assertEqual(len(spot), 1)
        m = self.h.metrics()
        self.assertEqual(m.unpriced(), {("instance", "dedicated_host"): 1, ("instance", "capacity_block"): 1,
                                        ("instance", "spot_platform_unsupported"): 1})

    def test_capacity_block_and_host_alone_dont_degrade(self):
        self.fleet({"InstanceId": "i-0a00000000000c003", "Tenancy": "host"},
                   {"InstanceId": "i-0a00000000000c004", "Lifecycle": "capacity-block"})
        self.h.runner.on(("describe-volumes",), fakes.ok({"Volumes": []}))
        self.h.run()
        self.assertEqual(self.h.metrics().value("cluster_cost_collector_up"), 1)

    def test_cache_hit_miss_expiry_and_stale_use(self):
        self.fleet()
        self.h.run()
        self.assertEqual(len(self.h.runner.find("pricing")), 5)     # 2 On-Demand keys, 3 EBS families
        self.h.runner.reset()
        self.h.clock.advance(60)
        self.h.run()
        self.assertEqual(self.h.runner.find("pricing"), [])         # hit: nothing due
        self.h.runner.reset()
        self.h.clock.advance(cc.DAY)
        self.h.runner.on(("pricing",), fakes.DENIED)
        self.h.run()                                                # expired: due, and the call fails
        self.assertEqual(len(self.h.runner.find("pricing")), 1)
        self.assertEqual(self.rates()["i-0a00000000000a002"], (0.11655, None))   # stale value still used
        self.assertEqual(self.h.metrics().stage_errors()["on_demand_prices"], "AccessDenied")
        self.h.runner.reset()
        self.h.clock.advance(60)
        # a new type during the backoff has no value: price_api_error
        self.fleet({"InstanceId": "i-0a00000000000c008", "InstanceType": "m6i.large"})
        self.h.run()
        self.assertEqual(self.h.runner.find("pricing"), [])
        self.assertEqual(self.rates()["i-0a00000000000c008"], (None, "price_api_error"))

    def test_budget_of_20_calls_and_price_lookup_pending(self):
        self.fleet(*[{"InstanceId": "i-0a00000000000d%03d" % i, "InstanceType": "zz%d.large" % i}
                     for i in range(25)])
        self.h.runner.on(("describe-volumes",), fakes.ok({"Volumes": []}))
        self.h.run()
        self.assertEqual(len(self.h.runner.find("pricing")), 20)
        reasons = [r for _, r in self.rates().values()]
        self.assertEqual(reasons.count("price_lookup_pending"), 7)          # 27 keys, 20 calls
        m = self.h.metrics()
        self.assertNotIn("on_demand_prices", m.stage_errors())
        self.assertIn("deferred: Pricing call budget used up", self.h.log())
        self.h.runner.reset()
        self.h.clock.advance(60)
        self.h.run()
        self.assertEqual(len(self.h.runner.find("pricing")), 7)
        self.assertNotIn("price_lookup_pending", [r for _, r in self.rates().values()])

    def test_failed_calls_count_against_the_budget(self):
        self.fleet(*[{"InstanceId": "i-0a00000000000d%03d" % i, "InstanceType": "zz%d.large" % i}
                     for i in range(25)])
        self.h.runner.on(("describe-volumes",), fakes.ok({"Volumes": []}))
        self.h.runner.on(("pricing",), fakes.aws_error("InvalidParameterException"))
        self.h.run()
        self.assertEqual(len(self.h.runner.find("pricing")), 20)
        reasons = [r for _, r in self.rates().values()]
        self.assertEqual((reasons.count("price_api_error"), reasons.count("price_lookup_pending")), (20, 7))
        self.assertEqual(self.h.metrics().stage_errors()["on_demand_prices"], "AwsApiError")


def gp(vid, vtype, size, iops=None, throughput=None):
    return {"VolumeId": vid, "VolumeType": vtype, "Size": size, "Iops": iops, "Throughput": throughput}


class EbsTest(unittest.TestCase):
    def priced_run(self, now=fakes.START):
        run = make_run(now=now)
        stage = run.health.stage("ebs")
        all_types = [gp("vol-1", t, 10, 70000, 300) for t in cc.EBS_STORAGE_TYPES]
        cc.refresh_ebs_prices(run, stage, all_types)
        self.assertTrue(stage.ok)
        self.assertEqual(len(run.deps.runner.find("pricing")), 3)
        return run

    def rate(self, run, v, hours=H_NOV):
        return cc.volume_rate(run, v, hours)

    def test_prices_from_the_three_families(self):
        run = self.priced_run()
        e = run.cache.entries
        self.assertEqual({t: e["ebs|storage|" + t]["value"] for t in cc.EBS_STORAGE_TYPES}, {
            "gp2": 0.126, "gp3": 0.1008, "io1": 0.1449, "io2": 0.1449, "st1": 0.0567, "sc1": 0.0189, "standard": 0.084})
        self.assertEqual(e["ebs|iops|gp3"]["value"], 0.0063)
        self.assertEqual(e["ebs|iops|io1"]["value"], 0.0756)
        self.assertEqual([e["ebs|iops|io2|%d" % t]["value"] for t in (1, 2, 3)], [0.0756, 0.0529, 0.0367])
        self.assertAlmostEqual(e["ebs|throughput|gp3"]["value"], 51.6096 / 1024, places=12)
        calls = run.deps.runner.find("pricing")
        self.assertEqual(len(calls), 3)
        families = sorted(a for c in calls for a in c.argv if "productFamily" in a)
        self.assertEqual(families, ["Type=TERM_MATCH,Field=productFamily,Value=Provisioned Throughput",
                                    "Type=TERM_MATCH,Field=productFamily,Value=Storage",
                                    "Type=TERM_MATCH,Field=productFamily,Value=System Operation"])

    def test_gp3_baseline_free_and_above_charged(self):
        run = self.priced_run()
        self.assertAlmostEqual(self.rate(run, gp("v", "gp3", 40, 3000, 125))[0], 40 * 0.1008 / H_NOV, places=12)
        self.assertEqual(cc.volume_items(gp("v", "gp3", 40, 3000, 125)), ["ebs|storage|gp3"])
        want = (35 * 0.1008 + 3000 * 0.0063 + 125 * 51.6096 / 1024) / H_NOV
        self.assertAlmostEqual(self.rate(run, gp("v", "gp3", 35, 6000, 250))[0], want, places=12)

    def test_io1_and_io2_tiers(self):
        run = self.priced_run()
        self.assertAlmostEqual(self.rate(run, gp("v", "io1", 100, 5000))[0], (14.49 + 5000 * 0.0756) / H_NOV, places=12)
        cases = {
            20000: 20000 * 0.0756,
            40000: 32000 * 0.0756 + 8000 * 0.0529,
            80000: 32000 * 0.0756 + 32000 * 0.0529 + 16000 * 0.0367,
        }
        for iops, iops_cost in cases.items():
            got = self.rate(run, gp("v", "io2", 200, iops))[0]
            self.assertAlmostEqual(got, (200 * 0.1449 + iops_cost) / H_NOV, places=12, msg=iops)

    def test_storage_only_types(self):
        run = self.priced_run()
        for vtype, price in (("gp2", 0.126), ("st1", 0.0567), ("sc1", 0.0189), ("standard", 0.084)):
            self.assertAlmostEqual(self.rate(run, gp("v", vtype, 500, 1500, 250))[0], 500 * price / H_NOV,
                                   places=12, msg=vtype)

    def test_unknown_type_and_missing_item(self):
        run = make_run()
        stage = run.health.stage("ebs")
        cc.refresh_ebs_prices(run, stage, [gp("vol-1", "gp9", 10), gp("vol-2", "gp3", 10, 3000, 125)])
        self.assertTrue(stage.ok)
        self.assertEqual(self.rate(run, gp("v", "gp9", 10)), (None, "unknown_volume_type"))
        self.assertEqual(len(run.deps.runner.find("pricing")), 1)    # only the Storage family was needed
        # an io2 tier that wasn't fetched yet, with the source healthy: pending
        self.assertEqual(self.rate(run, gp("v", "io2", 10, 70000)), (None, "price_lookup_pending"))

    def test_hours_per_month(self):
        import datetime as dt
        for (y, mth), hours in (((2027, 2), 672.0), ((2028, 2), 696.0), ((2026, 11), 720.0), ((2026, 12), 744.0)):
            t = dt.datetime(y, mth, 15, tzinfo=dt.timezone.utc).timestamp()
            self.assertEqual(cc.hours_in_month(t), hours)
        run = self.priced_run()
        v = gp("v", "gp3", 100, 3000, 125)
        self.assertAlmostEqual(self.rate(run, v, 744.0)[0], 100 * 0.1008 / 744.0, places=12)

    def test_multi_attach_counted_once(self):
        h = fakes.Harness("parallelcluster")
        try:
            h.run()
            vols = [v for v in h.report()["volumes"] if v["volume_id"] == "vol-0a00000000000b006"]
            self.assertEqual(len(vols), 1)
            self.assertEqual(vols[0]["instance_id"], "i-0a00000000000a002")     # first discovered attachment
            self.assertEqual(h.metrics().unpriced()[("volume", "invalid_record")], 1)
        finally:
            h.close()


def many_instances(n):
    base = fakes.fixture("aws", "pc-describe-instances.json")
    recs = [base[0]]
    for i in range(n):
        rec = copy.deepcopy(base[1])
        rec["InstanceId"] = "i-0e%015x" % i
        recs.append(rec)
    return recs


def volumes_per_instance(call):
    """One gp3 volume per instance of the call's chunk."""
    ids = []
    for a in call.argv:
        if a.startswith("Name=attachment.instance-id,Values="):
            ids = a.split("Values=", 1)[1].split(",")
    vols = [{"VolumeId": "vol-%s" % iid[2:], "VolumeType": "gp3", "Size": 10, "Iops": 3000, "Throughput": 125,
             "State": "in-use", "Attachments": [{"InstanceId": iid, "State": "attached"}]} for iid in ids]
    return fakes.ok({"Volumes": vols})


class VolumeRecordsTest(unittest.TestCase):
    """AC 52 and finding 60: chunks of 100 and per-instance records."""

    def setUp(self):
        self.h = fakes.Harness("parallelcluster")
        self.recs = many_instances(149)                   # 150 instances: chunks of 100 and 50
        self.h.runner.on(("describe-instances",), lambda call: fakes.ok(self.recs))
        self.calls = []

        def volumes(call):
            self.calls.append(call)
            if self.fail_second and len(self.calls) % 2 == 0:
                return fakes.THROTTLED
            return volumes_per_instance(call)
        self.fail_second = False
        self.h.runner.on(("describe-volumes",), volumes)
        self.ids = sorted(r["InstanceId"] for r in self.recs)

    def tearDown(self):
        self.h.close()

    def ebs_instances(self):
        return {v["instance_id"] for v in self.h.report()["volumes"]}

    def test_chunks_cache_and_age(self):
        self.h.run()
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(len(self.ebs_instances()), 150)
        first_chunk, second_chunk = set(self.ids[:100]), set(self.ids[100:])

        # 10 minutes later the second chunk fails: its instances use their cached records.
        self.fail_second = True
        self.calls.clear()
        gone = self.recs.pop()                              # an instance that isn't running any more
        self.h.clock.advance(600)
        self.assertEqual(self.h.run(), 0)
        m = self.h.metrics()
        self.assertEqual(m.stage_errors()["ebs"], "Throttled")
        self.assertEqual(self.ebs_instances(), (first_chunk | second_chunk) - {gone["InstanceId"]})
        self.assertEqual(m.value("cluster_cost_price_age_seconds", source="volumes"), 600)
        cached = self.h.prices()["entries"]["volumes"]["value"]
        self.assertNotIn(gone["InstanceId"], cached)
        self.assertEqual({cached[i]["fetched_at"] for i in first_chunk}, {fakes.START + 600})
        self.assertEqual({cached[i]["fetched_at"] for i in second_chunk - {gone["InstanceId"]}}, {fakes.START})
        self.assertNotIn(("volume", "volume_list_unavailable"), m.unpriced())

        # More than one hour after the second chunk's last records: they're no longer used.
        self.calls.clear()
        self.h.clock.advance(3001)
        self.h.run()
        m = self.h.metrics()
        self.assertEqual(self.ebs_instances(), first_chunk)
        self.assertEqual(m.unpriced()[("volume", "volume_list_unavailable")], 49)   # once per instance
        self.assertEqual(m.value("cluster_cost_collector_up"), 0)
        self.assertEqual(m.value("cluster_cost_price_age_seconds", source="volumes"), 0)

    def test_failed_chunk_without_cache(self):
        self.fail_second = True
        self.h.run()
        m = self.h.metrics()
        self.assertEqual(m.unpriced(), {("volume", "volume_list_unavailable"): 50})
        self.assertEqual(m.stage_errors(), {"ebs": "Throttled"})
        self.assertEqual(m.value("cluster_cost_collector_up"), 0)
        self.assertEqual(len(self.ebs_instances()), 100)
        # the instance lines are unaffected
        self.assertEqual(m.sum("cluster_cost_nodes"), 150)

    def test_volume_record_validation(self):
        bad = [
            {"VolumeId": "vol-XYZ", "VolumeType": "gp3", "Size": 10, "State": "in-use"},
            {"VolumeId": "vol-0a1", "VolumeType": "gp3", "Size": 0, "State": "in-use"},
            {"VolumeId": "vol-0a2", "VolumeType": "gp3", "Size": 10, "Iops": -1, "State": "in-use"},
            {"VolumeId": "vol-0a3", "VolumeType": "GP3", "Size": 10, "State": "in-use"},
            {"VolumeId": "vol-0a4", "VolumeType": "gp3", "Size": 10, "State": "deleting"},
            {"VolumeId": "vol-0a5", "VolumeType": "gp3", "Size": 1.5, "State": "in-use"},
            "not an object",
        ]
        for v in bad:
            if isinstance(v, dict):
                v["Attachments"] = [{"InstanceId": self.ids[0]}]
        self.h.runner.on(("describe-volumes",), fakes.ok({"Volumes": bad}))
        self.h.run()
        self.assertEqual(self.h.metrics().unpriced()[("volume", "invalid_record")], 7 * 2)   # both chunks


class PcsFeeTest(unittest.TestCase):
    def setUp(self):
        self.h = fakes.Harness("pcs")

    def tearDown(self):
        self.h.close()

    def fee_lines(self):
        m = self.h.metrics()
        return {labels["component"]: v for labels, v in m.series("cluster_cost_usd_per_hour")
                if labels["component"] in ("pcs_controller", "pcs_accounting")}

    def cluster(self, **changes):
        doc = fakes.fixture("aws", "pcs-get-cluster.json")
        doc["cluster"].update(changes)
        self.h.runner.on(("get-cluster",), fakes.ok(doc))

    def test_tiers(self):
        for itype in ("p5.48xlarge", "p6-b200.48xlarge", "trn2.48xlarge", "p4d.24xlarge", "trn1n.32xlarge"):
            self.assertEqual(cc.node_tier(itype), "adv", itype)
        for itype in ("g4dn.xlarge", "c6i.large", "inf2.xlarge", "m7i.large", "hpc7g.16xlarge"):
            self.assertEqual(cc.node_tier(itype), "std", itype)

    def test_fees_for_the_fleet(self):
        self.h.run()
        self.assertEqual(self.fee_lines(), {"pcs_controller": 0.5825, "pcs_accounting": 0.17})
        m = self.h.metrics()
        self.assertAlmostEqual(m.sum("cluster_cost_usd_per_hour", component="pcs_node_management"),
                               6 * 0.08 + 0.64, places=9)
        self.assertEqual(m.value("cluster_cost_usd_per_hour", component="pcs_node_management", node_role="compute",
                                 ec2_instance_type="p5.48xlarge", purchase_option="capacity_block",
                                 queue="pcs_ng00000cb1"), 0.64)
        self.assertEqual(m.value("cluster_cost_usd_per_hour", component="pcs_node_management", node_role="login",
                                 ec2_instance_type="t3.medium", purchase_option="on_demand"), 0.08)
        self.assertEqual(m.stages_ok()["pcs"], 1)
        call = self.h.runner.find("get-cluster")[0].argv
        self.assertEqual(call[call.index("--cluster-identifier") + 1], fakes.PCS_CLUSTER)

    def test_size_mapping(self):
        for size, controller, accounting in (("MEDIUM", 3.2579, 0.98), ("large", 6.5426, 1.96)):
            self.h.close()
            self.h = fakes.Harness("pcs")
            self.cluster(size=size)
            self.h.run()
            self.assertEqual(self.fee_lines(), {"pcs_controller": controller, "pcs_accounting": accounting})

    def test_accounting_off_or_absent(self):
        self.cluster(slurmConfiguration={"accounting": {"mode": "NONE"}})
        self.h.run()
        self.assertEqual(self.fee_lines(), {"pcs_controller": 0.5825})
        self.h.close()
        self.h = fakes.Harness("pcs")
        self.cluster(slurmConfiguration={"scaleDownIdleTimeInSeconds": 600})
        self.h.run()
        self.assertEqual(self.fee_lines(), {"pcs_controller": 0.5825})

    def test_status_gating(self):
        for status, lines in (("UPDATING", 2), ("UPDATE_FAILED", 2), ("SUSPENDED", 0), ("CREATING", 0)):
            self.h.close()
            self.h = fakes.Harness("pcs")
            self.cluster(status=status)
            self.h.run()
            self.assertEqual(len(self.fee_lines()), lines, status)
        self.assertIn("PCS cluster status CREATING: no controller or accounting fee", self.h.log())

    def test_invalid_cluster_response(self):
        self.cluster(size="HUGE")
        self.h.run()
        m = self.h.metrics()
        self.assertEqual(m.stage_errors()["pcs"], "InvalidResponse")
        self.assertEqual(self.fee_lines(), {})
        self.assertEqual(m.unpriced()[("fee", "price_api_error")], 1)     # controller; no accounting line

    def test_no_cluster_details_at_all(self):
        """Finding 60: without any pcs|cluster value, the controller line is
        unpriced and the accounting line is left out."""
        self.h.runner.on(("get-cluster",), fakes.aws_error("AccessDeniedException", "GetCluster"))
        self.h.run()
        m = self.h.metrics()
        self.assertEqual(self.fee_lines(), {})
        self.assertEqual(m.unpriced()[("fee", "price_api_error")], 1)
        fees = {f["component"]: f for f in self.h.report()["fees"] if f["component"] != "pcs_node_management"}
        self.assertEqual(sorted(fees), ["pcs_controller"])
        self.assertEqual(fees["pcs_controller"]["unpriced_reason"], "price_api_error")
        self.assertEqual(m.stage_errors()["pcs"], "AccessDenied")

    def test_cluster_details_older_than_24h(self):
        """Finding 60: pcs|cluster older than 24 h with accounting STANDARD:
        the controller and accounting lines are both unpriced
        (kind="fee", price_api_error)."""
        self.h.run()
        self.assertEqual(self.fee_lines(), {"pcs_controller": 0.5825, "pcs_accounting": 0.17})
        self.h.runner.on(("get-cluster",), fakes.aws_error("AccessDeniedException", "GetCluster"))
        self.h.clock.advance(23 * cc.HOUR)
        self.h.run()
        self.assertEqual(self.fee_lines(), {"pcs_controller": 0.5825, "pcs_accounting": 0.17})   # cached, < 24 h
        self.assertEqual(self.h.metrics().stage_errors()["pcs"], "AccessDenied")
        self.h.clock.advance(2 * cc.HOUR)
        self.h.run()
        m = self.h.metrics()
        self.assertEqual(self.fee_lines(), {})
        self.assertEqual(m.unpriced()[("fee", "price_api_error")], 2)
        fees = {f["component"]: f["unpriced_reason"] for f in self.h.report()["fees"]
                if f["component"] != "pcs_node_management"}
        self.assertEqual(fees, {"pcs_controller": "price_api_error", "pcs_accounting": "price_api_error"})
        self.assertEqual(m.value("cluster_cost_collector_up"), 0)

    def test_old_details_without_accounting(self):
        self.cluster(slurmConfiguration={"accounting": {"mode": "NONE"}})
        self.h.run()
        self.h.runner.on(("get-cluster",), fakes.aws_error("AccessDeniedException", "GetCluster"))
        self.h.clock.advance(25 * cc.HOUR)
        self.h.run()
        fees = {f["component"] for f in self.h.report()["fees"] if f["component"] != "pcs_node_management"}
        self.assertEqual(fees, {"pcs_controller"})

    def test_node_group_names_and_fallback(self):
        self.h.run()
        m = self.h.metrics()
        queues = {labels.get("queue") for labels, _ in m.series("cluster_cost_nodes")}
        self.assertEqual(queues, {None, "od", "spot", "gpu", "login-extra", "pcs_ng00000cb1"})
        self.assertEqual(len(self.h.runner.find("list-compute-node-groups")), 1)
        # A group ID the cache doesn't name: refreshed 5 minutes after the last attempt, not every run.
        self.h.runner.reset()
        for _ in range(5):
            self.h.clock.advance(60)
            self.h.run()
        self.assertEqual(len(self.h.runner.find("list-compute-node-groups")), 1)   # at minute 5
        self.assertEqual(len(self.h.runner.find("get-cluster")), 0)                # every 10 minutes


if __name__ == "__main__":
    unittest.main()
