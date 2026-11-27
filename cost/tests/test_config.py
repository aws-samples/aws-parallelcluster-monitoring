"""Configuration and its validation (design "Configuration file"), and the
refusal of unsafe directories (AC 37, U)."""
import os
import unittest
from unittest import mock

import cluster_cost as cc

from . import fakes

BASE = {"PLATFORM": "pcs", "CLUSTER_ID": "pcs_5jo3rv4lr8", "REGION": "us-east-1"}


def env(**overrides):
    e = dict(BASE)
    for k, v in overrides.items():
        if v is None:
            e.pop(k, None)
        else:
            e[k] = v
    return e


class LoadConfigTest(unittest.TestCase):
    def test_valid_with_defaults(self):
        c = cc.load_config(env())
        self.assertEqual((c.platform, c.cluster_id, c.region), ("pcs", "pcs_5jo3rv4lr8", "us-east-1"))
        self.assertEqual(c.state_dir, "/var/lib/aws-parallelcluster-monitoring/cost")
        self.assertEqual(c.textfile_dir, "/var/lib/prometheus/node-exporter")
        self.assertIsNone(c.slurm_bin_dir)
        self.assertFalse(c.debug)

    def test_optional_variables(self):
        c = cc.load_config(env(CLUSTER_COST_STATE_DIR="/s", CLUSTER_COST_TEXTFILE_DIR="/t",
                               CLUSTER_COST_SLURM_BIN_DIR="/opt/slurm/bin", CLUSTER_COST_DEBUG="1"))
        self.assertEqual((c.state_dir, c.textfile_dir, c.slurm_bin_dir, c.debug), ("/s", "/t", "/opt/slurm/bin", True))

    def test_debug_other_values_ignored(self):
        for value in ("0", "yes", "true", ""):
            self.assertFalse(cc.load_config(env(CLUSTER_COST_DEBUG=value)).debug, value)

    def test_platform(self):
        self.assertEqual(cc.load_config(env(PLATFORM="parallelcluster")).platform, "parallelcluster")
        for bad in (None, "", "PCS", "slurm", "res"):
            with self.assertRaises(cc.ConfigError, msg=bad):
                cc.load_config(env(PLATFORM=bad))

    def test_cluster_id(self):
        for good in ("mycluster", "pcs_5jo3rv4lr8", "a", "A-b_c", "x" * 128):
            self.assertEqual(cc.load_config(env(CLUSTER_ID=good)).cluster_id, good)
        for bad in (None, "", "-leading", "_leading", "has space", "x" * 129, "a,b", "a=b", "a/b"):
            with self.assertRaises(cc.ConfigError, msg=bad):
                cc.load_config(env(CLUSTER_ID=bad))

    def test_region(self):
        for good in ("us-east-1", "ap-southeast-6", "us-gov-west-1", "eusc-de-east-1", "il-central-1"):
            self.assertEqual(cc.load_config(env(REGION=good)).region, good)
        for bad in (None, "", "us-east", "US-EAST-1", "useast1", "us-east-1a", "us_east_1"):
            with self.assertRaises(cc.ConfigError, msg=bad):
                cc.load_config(env(REGION=bad))

    def test_login_node_group_id(self):
        self.assertIsNone(cc.load_config(env()).login_node_group_id)
        self.assertIsNone(cc.load_config(env(LOGIN_NODE_GROUP_ID="")).login_node_group_id)
        self.assertEqual(cc.load_config(env(LOGIN_NODE_GROUP_ID="pcs_ng0login02")).login_node_group_id,
                         "pcs_ng0login02")
        for bad in ("has space", "a,b", "x" * 129):
            with self.assertRaises(cc.ConfigError, msg=bad):
                cc.load_config(env(LOGIN_NODE_GROUP_ID=bad))

    def test_china_region_message(self):
        for region in ("cn-north-1", "cn-northwest-1"):
            with self.assertRaises(cc.ConfigError) as ctx:
                cc.load_config(env(REGION=region))
            self.assertEqual(str(ctx.exception), "AWS China regions are not supported: their price lists are in CNY")


class CheckPrivateDirTest(unittest.TestCase):
    def setUp(self):
        self.scratch = fakes.Scratch()
        self.uid = os.getuid()

    def tearDown(self):
        self.scratch.cleanup()

    def test_accepts_owned_0755_and_0750(self):
        cc.check_private_dir(self.scratch.textfile, self.uid)
        cc.check_private_dir(self.scratch.state, self.uid)

    def test_refuses_relative_missing_file_and_symlink(self):
        link = self.scratch.path("link")
        os.symlink(self.scratch.state, link)
        afile = self.scratch.path("file")
        open(afile, "w").close()
        for path in ("relative/dir", self.scratch.path("missing"), afile, link):
            with self.assertRaises(cc.ConfigError, msg=path):
                cc.check_private_dir(path, self.uid)

    def test_refuses_other_owner(self):
        # Tests run as root: the refusal is simulated by expecting another owner.
        with self.assertRaises(cc.ConfigError) as ctx:
            cc.check_private_dir(self.scratch.textfile, self.uid + 1)
        self.assertIn("owned by uid", str(ctx.exception))

    @staticmethod
    def _with_mode(real, mode):
        fields = list(real)
        fields[0] = (real.st_mode & ~0o7777) | mode
        return os.stat_result(fields)

    def test_refuses_group_or_other_writable(self):
        # The collector checks the mode bits, so a patched lstat reports them.
        real_lstat = os.lstat
        for mode in (0o775, 0o757, 0o777, 0o770):
            def fake_lstat(path, *a, **kw):
                st = real_lstat(path, *a, **kw)
                return self._with_mode(st, mode) if path == self.scratch.textfile else st
            with mock.patch.object(cc.os, "lstat", fake_lstat):
                with self.assertRaises(cc.ConfigError, msg=oct(mode)) as ctx:
                    cc.check_private_dir(self.scratch.textfile, self.uid)
            self.assertIn("writable by group or others", str(ctx.exception))


class MainConfigExitTest(unittest.TestCase):
    """Invalid configuration: exit 2, an ERROR log, nothing written."""

    def setUp(self):
        self.h = fakes.Harness("parallelcluster")

    def tearDown(self):
        self.h.close()

    def assertNothingWritten(self):
        self.assertEqual(os.listdir(self.h.scratch.textfile), [])
        self.assertEqual(os.listdir(self.h.scratch.state), [])
        self.assertEqual(self.h.runner.calls, [])

    def test_china_region_exits_2(self):
        self.h.environ["REGION"] = "cn-north-1"
        self.assertEqual(self.h.run(), 2)
        self.assertIn("ERROR invalid configuration: AWS China regions are not supported: their price lists are in CNY",
                      self.h.log())
        self.assertNothingWritten()

    def test_missing_platform_exits_2(self):
        del self.h.environ["PLATFORM"]
        self.assertEqual(self.h.run(), 2)
        self.assertNothingWritten()

    def test_unsafe_textfile_dir_exits_2(self):
        self.h.owner_uid = os.getuid() + 1
        self.assertEqual(self.h.run(), 2)
        self.assertIn("owned by uid", self.h.log())
        self.assertNothingWritten()

    def test_symlinked_state_dir_exits_2(self):
        link = self.h.scratch.path("state-link")
        os.symlink(self.h.scratch.state, link)
        self.h.environ["CLUSTER_COST_STATE_DIR"] = link
        self.assertEqual(self.h.run(), 2)
        self.assertIn("is a symlink", self.h.log())
        self.assertNothingWritten()

    def test_unknown_option_exits_2(self):
        with mock.patch("sys.stderr"):
            self.assertEqual(self.h.run("--bogus"), 2)
        self.assertNothingWritten()


if __name__ == "__main__":
    unittest.main()
