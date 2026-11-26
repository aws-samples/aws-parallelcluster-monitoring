"""AWS error codes (AC 33): the recorded CLI samples of CLI 2.38.0, 2.33.0
and 2.31.35 map to the codes of design "AWS error codes"; the first failed
call of a stage wins; no CLI text becomes a label."""
import glob
import json
import os
import re
import unittest

import cluster_cost as cc

from . import fakes

SAMPLES = os.path.join(fakes.FIXTURES, "aws-cli-errors")
LINE = re.compile(r"^(\S+)\s+rc=(\d+)\s+[\d.]+s\s*(.*)$")
EXPECTED = {
    "pricing-throttled": "Throttled",
    "pricing-access-denied": "AccessDenied",
    "ec2-unauthorized": "AccessDenied",
    "ec2-request-limit": "Throttled",
    "ec2-auth-failure": "CredentialsError",
    "pricing-not-json": "InvalidResponse",
    "pricing-read-timeout-3s": "Timeout",
    "silent-endpoint-cap-30s": "Timeout",
    "conn-refused": "EndpointUnreachable",
    "connect-timeout": "EndpointUnreachable",
    "proxy-unroutable": "EndpointUnreachable",
    "proxy-silent": None,               # differs by CLI version, see PROXY_SILENT
    "no-credentials": "CredentialsError",
    "endpoint-env-override": "Throttled",
    "cli-missing": "AwsCliNotFound",
}
# A proxy that accepts the connection and never answers.
PROXY_SILENT = {"2.38.0": "EndpointUnreachable", "2.33.0": "EndpointUnreachable", "2.31.35": "Timeout"}
NEEDS_PRICE_LIST = lambda data: isinstance(data, dict) and isinstance(data.get("PriceList"), list)  # noqa: E731


def load_samples():
    """[(version, name, CommandResult)] from every results-*.txt file."""
    out = []
    for path in sorted(glob.glob(os.path.join(SAMPLES, "results-*.txt"))):
        with open(path) as f:
            lines = f.read().splitlines()
        version = lines[0].split("aws-cli/")[1].split()[0]
        stdout = {}
        for line in lines[1:]:
            if line.startswith("pricing-not-json stdout:"):
                stdout["pricing-not-json"] = line.split(":", 1)[1].strip()
        for line in lines[1:]:
            m = LINE.match(line)
            if not m:
                continue
            name, rc, err = m.group(1), int(m.group(2)), m.group(3)
            result = cc.CommandResult(
                rc=rc, stderr=err, stdout=stdout.get(name, ""),
                timed_out=(name == "silent-endpoint-cap-30s"),   # the collector's subprocess timeout
                not_found=(name == "cli-missing"))               # FileNotFoundError in the collector
            out.append((version, name, result))
    return out


class SampleTest(unittest.TestCase):
    def test_all_45_samples(self):
        samples = load_samples()
        self.assertEqual(len(samples), 45)
        self.assertEqual(sorted({v for v, _, _ in samples}), ["2.31.35", "2.33.0", "2.38.0"])
        for version, name, result in samples:
            want = PROXY_SILENT[version] if name == "proxy-silent" else EXPECTED[name]
            got = cc.classify_aws_error(result, NEEDS_PRICE_LIST)
            self.assertEqual(got, want, "%s %s" % (version, name))

    def test_version_differences(self):
        by = {(v, n): r for v, n, r in load_samples()}
        # 2.38.0 reports missing credentials with a code, the older two without.
        self.assertIn("(NoCredentials)", by[("2.38.0", "no-credentials")].stderr)
        self.assertNotIn("An error occurred", by[("2.33.0", "no-credentials")].stderr)
        self.assertTrue(by[("2.38.0", "pricing-throttled")].stderr.startswith("aws: [ERROR]: "))
        self.assertFalse(by[("2.31.35", "pricing-throttled")].stderr.startswith("aws: [ERROR]: "))


class ClassifyTableTest(unittest.TestCase):
    def r(self, stderr="", rc=254, stdout=""):
        return cc.CommandResult(rc=rc, stderr=stderr, stdout=stdout)

    def test_codes_in_order(self):
        cases = [
            ("An error occurred (ExpiredToken) when calling", "CredentialsError"),
            ("An error occurred (UnrecognizedClientException) when calling", "CredentialsError"),
            ("Error when retrieving credentials from Ec2InstanceMetadata", "CredentialsError"),
            ("An error occurred (UnauthorizedException) when calling", "AccessDenied"),
            ("An error occurred (SlowDown) when calling", "Throttled"),
            ("An error occurred (TooManyRequestsException) when calling", "Throttled"),
            ("An error occurred (InvalidParameterValue) when calling", "AwsApiError"),
            ("Connection was closed before we received a valid response from endpoint URL", "EndpointUnreachable"),
            # an endpoint failure wins over an API code in the same text
            ("Read timeout on endpoint URL ... An error occurred (Throttling)", "Timeout"),
            ("Something unexpected happened", "AwsCliError"),
            ("", "AwsCliError"),
        ]
        for stderr, code in cases:
            self.assertEqual(cc.classify_aws_error(self.r(stderr)), code, stderr)

    def test_exit_zero(self):
        self.assertIsNone(cc.classify_aws_error(self.r(rc=0, stdout='{"PriceList": []}'), NEEDS_PRICE_LIST))
        self.assertEqual(cc.classify_aws_error(self.r(rc=0, stdout="not json"), NEEDS_PRICE_LIST), "InvalidResponse")
        self.assertEqual(cc.classify_aws_error(self.r(rc=0, stdout="[]"), NEEDS_PRICE_LIST), "InvalidResponse")
        self.assertIsNone(cc.classify_aws_error(self.r(rc=0, stdout="[]"), cc._is_list))

    def test_not_found_and_timeout_first(self):
        self.assertEqual(cc.classify_aws_error(cc.CommandResult(rc=127, not_found=True)), "AwsCliNotFound")
        self.assertEqual(cc.classify_aws_error(cc.CommandResult(rc=-9, timed_out=True,
                                                                stderr="An error occurred (AccessDenied)")), "Timeout")

    def test_codes_are_closed_set(self):
        for stderr in ('An error occurred (Bad"Code\n) x', "An error occurred (New.Error-1) x", "\x00\xff"):
            self.assertIn(cc.classify_aws_error(self.r(stderr)), cc.AWS_CODES)


class FirstFailedCallTest(unittest.TestCase):
    def test_on_demand_first_failed_call_wins(self):
        # Entry-level failures don't back off the source, so both keys are called.
        # Keys are called missing first, in key order: c6i.large before t3.medium.
        for first, second, want in (
                (fakes.aws_error("InvalidParameterValue"), fakes.ok({"FormatVersion": None}), "AwsApiError"),
                (fakes.ok({"FormatVersion": None}), fakes.aws_error("InvalidParameterValue"), "InvalidResponse")):
            h = fakes.Harness("parallelcluster")
            try:
                h.runner.on(fakes.is_pricing_for("instanceType=c6i.large"), first)
                h.runner.on(fakes.is_pricing_for("instanceType=t3.medium"), second)
                self.assertEqual(h.run(), 0)
                m = h.metrics()
                self.assertEqual(m.stage_errors()["on_demand_prices"], want)
                self.assertEqual(len(m.series("cluster_cost_collector_stage_error")), 1)
                ops = [c.argv for c in h.runner.find("pricing") if any("instanceType" in a for a in c.argv)]
                self.assertEqual(len(ops), 3)   # c6i.large, g4dn.xlarge, t3.medium
            finally:
                h.close()

    def test_volume_chunks_first_failed_call_wins(self):
        h = fakes.Harness("parallelcluster")
        try:
            many = fakes.fixture("aws", "pc-describe-instances.json")
            for i in range(120):
                rec = dict(many[1])
                rec["InstanceId"] = "i-0c%015x" % i
                many.append(rec)
            h.runner.on(("describe-instances",), fakes.ok(many))
            calls = []

            def volumes(call):
                calls.append(call)
                if len(calls) == 1:
                    return fakes.aws_error("UnauthorizedOperation", "DescribeVolumes")
                return fakes.THROTTLED
            h.runner.on(("describe-volumes",), volumes)
            h.run()
            self.assertEqual(len(calls), 2)
            self.assertEqual(h.metrics().stage_errors()["ebs"], "AccessDenied")
        finally:
            h.close()

    def test_no_cli_text_in_labels(self):
        h = fakes.Harness("parallelcluster")
        try:
            h.runner.on(("describe-instances",), fakes.aws_error("SomethingBrandNew", "DescribeInstances",
                                                                 message='bad "quote" arn:aws:iam::123456789012:x'))
            h.run()
            text = h.textfile()
            self.assertIn('cluster_cost_collector_stage_error{code="AwsApiError",stage="discovery"} 1', text)
            for leaked in ("SomethingBrandNew", "arn:aws", "quote"):
                self.assertNotIn(leaked, text)
        finally:
            h.close()


if __name__ == "__main__":
    unittest.main()
