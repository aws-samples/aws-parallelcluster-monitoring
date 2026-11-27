"""Slurm metrics on ParallelCluster head nodes (docs/slurm-metrics.md).

installer/install.sh replaces the lines between the BEGIN and END lines of
prometheus/prometheus.yml with prometheus/scrape-slurm-exporter.yml or
prometheus/scrape-slurm-native.yml. These tests check the marked block, the
two snippets, the rule files that prometheus.yml loads, and the installer's
set_slurm_scrape_jobs, run with bash on copies of prometheus.yml.
"""
import os
import shutil
import stat
import subprocess
import tempfile
import unittest

from . import test_queries as q

CONFIG = os.path.join(q.PROMETHEUS, "prometheus.yml")
PCS_CONFIG = os.path.join(q.PROMETHEUS, "prometheus-pcs.yml")
EXPORTER = os.path.join(q.PROMETHEUS, "scrape-slurm-exporter.yml")
NATIVE = os.path.join(q.PROMETHEUS, "scrape-slurm-native.yml")
BEGIN = "  # BEGIN slurm_metrics"
END = "  # END slurm_metrics"
EXPORTER_JOBS = ["slurm_exporter"]
NATIVE_JOBS = ["slurm_openmetrics", "slurm_openmetrics_jobs", "slurm_openmetrics_partitions",
               "slurm_openmetrics_scheduler", "slurm_openmetrics_jobs_users_accts"]


def snippet_lines(text):
    """The lines of a snippet as install.sh copies them: the leading comment
    and empty lines skipped, the others indented by two spaces, empty lines
    kept empty."""
    lines = text.split("\n")
    if lines[-1] == "":
        lines.pop()
    start = 0
    while start < len(lines) and (lines[start].startswith("#") or lines[start] == ""):
        start += 1
    return ["  " + line if line else "" for line in lines[start:]]


def block_lines(text):
    """The lines between the BEGIN and END lines."""
    lines = text.split("\n")
    return lines[lines.index(BEGIN) + 1:lines.index(END)]


def job_names(lines):
    return list(q.jobs("\n".join(lines) + "\n"))


def set_slurm_scrape_jobs(config, snippet):
    """installer/common.sh's set_slurm_scrape_jobs on config, as install.sh runs it."""
    return subprocess.run(
        ["bash", "-c", '. "$1/installer/common.sh" && set_slurm_scrape_jobs "$2" "$3"', "_",
         q.REPO, config, snippet],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)


class ScrapeBlockTest(unittest.TestCase):
    def setUp(self):
        self.config = q.read(CONFIG)

    def test_one_block_under_scrape_configs(self):
        lines = self.config.split("\n")
        self.assertEqual((lines.count(BEGIN), lines.count(END)), (1, 1))
        self.assertLess(lines.index("scrape_configs:"), lines.index(BEGIN))
        self.assertLess(lines.index(BEGIN), lines.index(END))

    def test_block_is_the_exporter_snippet(self):
        # So that the unrendered file works as it is, and an exporter run
        # leaves it unchanged.
        self.assertEqual(block_lines(self.config), snippet_lines(q.read(EXPORTER)))
        self.assertEqual(job_names(block_lines(self.config)), EXPORTER_JOBS)

    def test_native_jobs_are_the_pcs_jobs_on_port_6820(self):
        pcs = q.jobs(q.read(PCS_CONFIG))
        native = q.jobs("\n".join(snippet_lines(q.read(NATIVE))) + "\n")
        self.assertEqual(list(native), NATIVE_JOBS)
        for name in NATIVE_JOBS:
            with self.subTest(job=name):
                expected = pcs[name].replace("'__SLURMCTLD_IP__:6817'", "'localhost:6820'")
                self.assertIn("'localhost:6820'", native[name])
                self.assertEqual(native[name].rstrip(), expected.rstrip())

    def test_parallelcluster_config_loads_the_compatibility_rules(self):
        # The rules record nothing from the exporter's series, which have
        # other names, and translate the native ones.
        text = q.without_comments(self.config)
        self.assertRegex(text, r"\nrule_files:\n(?:  - \S+\n)*  - /etc/prometheus/rules/pcs-compat\.yml\n"
                               r"(?:  - \S+\n)*\s*\nscrape_configs:\n")


class SetSlurmScrapeJobsTest(unittest.TestCase):
    """set_slurm_scrape_jobs on a 0644 copy of prometheus.yml."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        self.template = q.read(CONFIG)
        self.other_jobs = [j for j in q.jobs(self.template) if j not in EXPORTER_JOBS]

    def copy(self, text):
        path = os.path.join(self.dir, "prometheus.yml")
        with open(path, "w") as f:
            f.write(text)
        os.chmod(path, 0o644)
        return path

    def test_both_directions_and_re_runs(self):
        path = self.copy(self.template)
        inode = os.stat(path).st_ino
        for n, (snippet, expected) in enumerate(((NATIVE, NATIVE_JOBS), (NATIVE, NATIVE_JOBS),
                                                 (EXPORTER, EXPORTER_JOBS), (EXPORTER, EXPORTER_JOBS)), 1):
            with self.subTest(run=n, snippet=os.path.basename(snippet)):
                result = set_slurm_scrape_jobs(path, snippet)
                self.assertEqual(result.returncode, 0, result.stderr)
                text = q.read(path)
                lines = text.split("\n")
                self.assertEqual((lines.count(BEGIN), lines.count(END)), (1, 1))
                self.assertEqual(block_lines(text), snippet_lines(q.read(snippet)))
                self.assertEqual(list(q.jobs(text)), expected + self.other_jobs)
        # Back on the exporter, the file is the template again, with its mode
        # and inode.
        self.assertEqual(q.read(path), self.template)
        st = os.stat(path)
        self.assertEqual((stat.S_IMODE(st.st_mode), st.st_ino), (0o644, inode))

    def test_a_file_without_both_markers_is_left_alone(self):
        lines = self.template.split("\n")
        for label, drop in (("no markers", (BEGIN, END)), ("no END", (END,)), ("no BEGIN", (BEGIN,))):
            with self.subTest(copy=label):
                text = "\n".join(line for line in lines if line not in drop)
                path = self.copy(text)
                result = set_slurm_scrape_jobs(path, NATIVE)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertEqual(q.read(path), text)


if __name__ == "__main__":
    unittest.main()
