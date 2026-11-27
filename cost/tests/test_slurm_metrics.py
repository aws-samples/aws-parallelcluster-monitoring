"""Slurm metrics on ParallelCluster head nodes (docs/slurm-metrics.md).

installer/install.sh replaces the lines between the BEGIN and END lines of
prometheus/prometheus.yml with prometheus/scrape-slurm-exporter.yml or
prometheus/scrape-slurm-native.yml. These tests check the marked block, the
two snippets and the rule files that prometheus.yml loads.
"""
import os
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


if __name__ == "__main__":
    unittest.main()
