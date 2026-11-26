"""run_command(): no shell, its own session, the whole process group killed
on a timeout, and a missing or non-executable CLI reported as not found."""
import os
import time
import unittest

import cluster_cost as cc

from . import fakes

ENV = {"PATH": "/usr/bin:/bin"}


class RunCommandTest(unittest.TestCase):
    def setUp(self):
        self.scratch = fakes.Scratch()

    def tearDown(self):
        self.scratch.cleanup()

    def test_output_and_exit_code(self):
        r = cc.run_command(["/bin/sh", "-c", "echo out; echo err >&2; exit 3"], 10, ENV)
        self.assertEqual((r.rc, r.stdout, r.stderr, r.timed_out, r.not_found), (3, "out\n", "err\n", False, False))

    def test_environment_is_the_given_one(self):
        r = cc.run_command(["/bin/sh", "-c", 'echo "$FOO:$HOME"'], 10, dict(ENV, FOO="bar"))
        self.assertEqual(r.stdout, "bar:\n")

    def test_invalid_utf8_is_replaced(self):
        r = cc.run_command(["/bin/sh", "-c", r"printf 'a\377b'"], 10, ENV)
        self.assertEqual(r.stdout, "a\ufffdb")

    def test_own_session_and_process_group(self):
        r = cc.run_command(["/bin/sh", "-c", "cat /proc/$$/stat"], 10, ENV)
        fields = r.stdout.rsplit(")", 1)[1].split()
        pid = int(r.stdout.split()[0])
        self.assertEqual((int(fields[2]), int(fields[3])), (pid, pid))   # pgrp, session

    def test_timeout_kills_the_process_group(self):
        # The child `sleep`s keep the pipes open unless the whole group is killed.
        start = time.monotonic()
        r = cc.run_command(["/bin/sh", "-c", "(sleep 300 &); sleep 300; echo never"], 1, ENV)
        elapsed = time.monotonic() - start
        self.assertTrue(r.timed_out)
        self.assertLess(elapsed, 10)
        self.assertEqual(cc.classify_aws_error(r), "Timeout")

    def test_missing_executable(self):
        r = cc.run_command([self.scratch.path("missing", "aws"), "--version"], 10, ENV)
        self.assertTrue(r.not_found)
        self.assertEqual(cc.classify_aws_error(r), "AwsCliNotFound")

    def test_missing_on_path(self):
        r = cc.run_command(["aws", "--version"], 10, {"PATH": self.scratch.bin})
        self.assertTrue(r.not_found)
        self.assertEqual(cc.classify_aws_error(r), "AwsCliNotFound")

    def test_not_executable(self):
        path = self.scratch.path("bin", "aws")
        with open(path, "w") as f:       # created 0644: no execute bit for anyone, root included
            f.write("#!/bin/sh\necho hi\n")
        r = cc.run_command([path], 10, ENV)
        self.assertTrue(r.not_found)
        self.assertEqual(cc.classify_aws_error(r), "AwsCliNotFound")

    def test_path_lookup_uses_the_given_environment(self):
        fakes.write_shell_stub(self.scratch.bin, 'echo "stub $*"\n')
        r = cc.run_command(["aws", "ec2", "describe-instances"], 10, {"PATH": self.scratch.bin + ":/usr/bin:/bin"})
        self.assertEqual((r.rc, r.stdout), (0, "stub ec2 describe-instances\n"))
        self.assertTrue(os.path.exists(os.path.join(self.scratch.bin, "aws")))


if __name__ == "__main__":
    unittest.main()
