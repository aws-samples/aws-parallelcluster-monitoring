"""systemd units of the collector (AC 53 U, the unit-file half), and the
installed paths that the installer, the units and the collector share."""
import os
import re
import unittest

import cluster_cost as cc

from . import fakes

SYSTEMD = os.path.join(fakes.HERE, os.pardir, os.pardir, "systemd")
REPO = os.path.join(fakes.HERE, os.pardir, os.pardir)


def read(*parts):
    with open(os.path.join(REPO, *parts)) as f:
        return f.read()


def shell_function(text, name):
    """The body of a shell function written as `name() {` ... `}` at column 0."""
    m = re.search(r"^%s\(\) \{\n(.*?)^\}$" % re.escape(name), text, re.M | re.S)
    if m is None:
        raise AssertionError("no function %s" % name)
    return m.group(1)


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


class InstalledPathsTest(unittest.TestCase):
    """What install_cost_collector (installer/common.sh) installs is what the
    service runs and what the collector reads by default."""

    def setUp(self):
        self.common = read("installer", "common.sh")
        self.install = shell_function(self.common, "install_cost_collector")
        self.service = read_unit("cluster-cost.service")["Service"]

    def test_executable(self):
        m = re.search(r'^\s*install -m 0755 -o root -g root "\$\{MONITORING_HOME\}/cost/cluster_cost\.py" (\S+)$',
                      self.install, re.M)
        self.assertIsNotNone(m)
        self.assertEqual(values(self.service, "ExecStart"), [m.group(1)])

    def test_environment_file(self):
        envdir = re.search(r"^\s*local envdir=(\S+)$", self.install, re.M).group(1)
        self.assertIn('mv -f "${envdir}/cluster-cost.env.new" "${envdir}/cluster-cost.env"', self.install)
        self.assertEqual(values(self.service, "EnvironmentFile"), [envdir + "/cluster-cost.env"])

    def test_environment_variables(self):
        fmt = re.search(r"printf '([^']*)'", self.install).group(1)
        names = [line.partition("=")[0] for line in fmt.split("\\n") if line]
        self.assertEqual(names, ["PLATFORM", "CLUSTER_ID", "REGION", "LOGIN_NODE_GROUP_ID"])
        # LOGIN_NODE_GROUP_ID is empty on ParallelCluster and holds the login
        # node's own node group on PCS.
        for module, cluster, login_group, want in (("parallelcluster.sh", fakes.PC_CLUSTER, "", None),
                                                    ("pcs.sh", fakes.PCS_CLUSTER, "pcs_ng0login01", "pcs_ng0login01")):
            platform = re.search(r'export PLATFORM="([a-z]+)"', read("installer", "platform", module)).group(1)
            config = cc.load_config(dict(zip(names, (platform, cluster, "us-east-1", login_group))))
            self.assertEqual((config.platform, config.login_node_group_id, config.state_dir, config.textfile_dir),
                             (platform, want, cc.STATE_DIR_DEFAULT, cc.TEXTFILE_DIR_DEFAULT))

    def test_directories(self):
        """Modes that check_private_dir accepts, and a root:root textfile
        directory on every install (review finding 64)."""
        found = {}
        for mode, owner, paths in re.findall(r"^\s*install -d -m ([0-7]{4})( -o root -g root)? (.+)$",
                                             self.install, re.M):
            for path in paths.split():
                found[path] = (int(mode, 8), bool(owner))
        self.assertIn(cc.STATE_DIR_DEFAULT, found)
        self.assertEqual(found[cc.STATE_DIR_DEFAULT][0], 0o750)
        self.assertEqual(found.get(cc.TEXTFILE_DIR_DEFAULT), (0o755, True))

    def test_textfile_directory_is_node_exporters(self):
        head = read("compose", "head.yml")
        self.assertIn("'%s:/textfile:ro'" % cc.TEXTFILE_DIR_DEFAULT, head)
        self.assertIn("'--collector.textfile.directory=/textfile'", head)

    def test_interpreter(self):
        with open(cc.__file__) as f:
            self.assertEqual(f.readline(), "#!/usr/bin/python3 -I\n")
        check = shell_function(self.common, "require_python3")
        self.assertIn("sys.version_info >= (3, 7)", check)
        self.assertIn('/usr/bin/python3 -c "${check}"', check)


if __name__ == "__main__":
    unittest.main()
