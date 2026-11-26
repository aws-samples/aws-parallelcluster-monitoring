"""systemd units of the collector (AC 53 U, the unit-file half)."""
import os
import unittest

from . import fakes

SYSTEMD = os.path.join(fakes.HERE, os.pardir, os.pardir, "systemd")


def read_unit(name):
    """Return {section: [(key, value), ...]} for a systemd unit file."""
    sections = {}
    current = None
    with open(os.path.join(SYSTEMD, name)) as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith(("#", ";")):
                continue
            if line.startswith("[") and line.endswith("]"):
                current = sections.setdefault(line[1:-1], [])
                continue
            key, sep, value = line.partition("=")
            if not sep or current is None:
                raise AssertionError("bad line in %s: %r" % (name, raw))
            current.append((key, value))
    return sections


def values(section, key):
    return [v for k, v in section if k == key]


def environment(section):
    env = {}
    for value in values(section, "Environment"):
        name, _, val = value.partition("=")
        env[name] = val
    return env


class ServiceUnitTest(unittest.TestCase):
    def setUp(self):
        self.unit = read_unit("cluster-cost.service")
        self.service = self.unit["Service"]

    def assertSetting(self, key, value):
        self.assertEqual(values(self.service, key), [value], key)

    def test_credentials_only_from_the_instance_profile(self):
        env = environment(self.service)
        self.assertEqual(env.get("AWS_CONFIG_FILE"), "/nonexistent")
        self.assertEqual(env.get("AWS_SHARED_CREDENTIALS_FILE"), "/nonexistent")

    def test_retry_and_pager(self):
        env = environment(self.service)
        self.assertEqual(env.get("AWS_RETRY_MODE"), "standard")
        self.assertEqual(env.get("AWS_MAX_ATTEMPTS"), "2")
        self.assertIn("AWS_PAGER", env)
        self.assertEqual(env["AWS_PAGER"], "")

    def test_path_holds_both_aws_cli_locations(self):
        path = environment(self.service).get("PATH", "").split(":")
        self.assertIn("/usr/local/bin", path)
        self.assertIn("/usr/bin", path)

    def test_service_settings(self):
        self.assertSetting("EnvironmentFile", "/etc/aws-parallelcluster-monitoring/cluster-cost.env")
        self.assertSetting("Type", "oneshot")
        self.assertSetting("User", "root")
        self.assertSetting("ExecStart", "/usr/local/bin/cluster-cost-collector")
        self.assertSetting("TimeoutStartSec", "150")

    def test_hardening(self):
        self.assertSetting("NoNewPrivileges", "yes")
        self.assertSetting("PrivateTmp", "yes")
        self.assertSetting("ProtectSystem", "full")


class TimerUnitTest(unittest.TestCase):
    def test_timer_settings(self):
        unit = read_unit("cluster-cost.timer")
        timer = unit["Timer"]
        self.assertEqual(values(timer, "OnBootSec"), ["60s"])
        self.assertEqual(values(timer, "OnUnitActiveSec"), ["60s"])
        self.assertEqual(values(timer, "AccuracySec"), ["5s"])
        self.assertEqual(values(unit["Install"], "WantedBy"), ["timers.target"])


if __name__ == "__main__":
    unittest.main()
