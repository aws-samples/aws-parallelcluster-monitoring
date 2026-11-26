"""Queries and Prometheus configs (AC 42): the recording rules and the configs.

Cost series are trusted only from the monitoring node's own node_exporter
(design D13). Raw cluster_cost_* and node_textfile_* series must be selected
with S, recording-rule outputs with {job=""}, the pushgateway job must drop
cost look-alikes, and it must stay the only job with honor_labels: true.
Standard-library regex scans are enough, because the expressions and the
configs are ours.
"""
import os
import re
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.normpath(os.path.join(HERE, os.pardir, os.pardir))
PROMETHEUS = os.path.join(REPO, "prometheus")
RULES = os.path.join(PROMETHEUS, "rules", "cost.yml")
CONFIGS = ("prometheus.yml", "prometheus-pcs.yml")
ALL_CONFIGS = CONFIGS + ("prometheus-res.yml",)

S = 'job=~"ec2_instances|login_node",instance_name="HeadNode"'
RULE_OUTPUTS = ("cluster_cost_collector:fresh",
                "cluster:cluster_cost_usd_per_hour:sum",
                "cluster:cluster_cost_projected_month_usd:avg_24h")
RULE_SELECTOR = '{job=""}'
DROP_REGEX = "cluster_cost_.+|cluster:cluster_cost_.+|node_textfile_.+"

# PromQL tokens: a string literal, a label-matcher block, a name, or any other
# character. Our matcher blocks hold no "}" inside a string.
TOKEN_RE = re.compile(r'"(?:[^"\\]|\\.)*"|\{[^}]*\}|[a-zA-Z_:][a-zA-Z0-9_:]*|\s+|.', re.S)
RAW_RE = re.compile(r"^(cluster_cost_|node_textfile_)[a-zA-Z0-9_]*$")
EXPR_RE = re.compile(r"^(\s*)(?:- )?expr:\s*(.*)$")
JOB_RE = re.compile(r"^\s*- job_name:\s*'?([A-Za-z0-9_]+)'?\s*$")
DROP_RULE_RE = re.compile(
    r"^    metric_relabel_configs:\n"
    r"      - source_labels: \[__name__\]\n"
    r"        regex: '" + re.escape(DROP_REGEX) + r"'\n"
    r"        action: drop\n", re.M)
HONOR_RE = re.compile(r"^\s*honor_labels:\s*true\s*$", re.M)


def read(path):
    with open(path) as f:
        return f.read()


def rule_expressions(text):
    """The expr values of a rules file: block scalars (expr: |) and inline ones."""
    exprs = []
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        m = EXPR_RE.match(lines[i])
        i += 1
        if not m:
            continue
        value = m.group(2).strip()
        if value not in ("|", "|-", ">", ">-"):
            exprs.append(value)
            continue
        indent = len(m.group(1))
        body = []
        while i < len(lines) and (not lines[i].strip()
                                  or len(lines[i]) - len(lines[i].lstrip()) > indent):
            body.append(lines[i].strip())
            i += 1
        exprs.append("\n".join(body))
    return exprs


def selectors(expr):
    """(name, matcher block or None) for every name in a PromQL expression."""
    tokens = [t for t in TOKEN_RE.findall(expr) if not t.isspace()]
    out = []
    for i, tok in enumerate(tokens):
        if re.match(r"^[a-zA-Z_:]", tok):
            nxt = tokens[i + 1] if i + 1 < len(tokens) else ""
            out.append((tok, nxt if nxt.startswith("{") else None))
    return out


def raw_selector_problems(exprs):
    """Raw cost or textfile series selected without S. Returns (problems, count)."""
    problems, count = [], 0
    for expr in exprs:
        for name, block in selectors(expr):
            if ":" in name or not RAW_RE.match(name):
                continue
            count += 1
            if block is None or S not in block:
                problems.append("%s%s without S in: %s" % (name, block or "", expr))
    return problems, count


def rule_output_problems(exprs):
    """References to the recording-rule outputs not followed by {job=""}."""
    problems, count = [], 0
    for expr in exprs:
        for name, block in selectors(expr):
            if name not in RULE_OUTPUTS:
                continue
            count += 1
            if block != RULE_SELECTOR:
                problems.append("%s%s without {job=\"\"} in: %s" % (name, block or "", expr))
    return problems, count


def without_comments(text):
    return "\n".join(l for l in text.split("\n") if not l.lstrip().startswith("#")) + "\n"


def jobs(text):
    """{job_name: text of its block} for a Prometheus config or the RES fragment."""
    found = {}
    name, block = None, []
    for line in without_comments(text).split("\n"):
        m = JOB_RE.match(line)
        if m or (line and not line[0].isspace()):
            if name is not None:
                found[name] = "\n".join(block) + "\n"
            name, block = (m.group(1), [line]) if m else (None, [])
            continue
        if name is not None:
            block.append(line)
    if name is not None:
        found[name] = "\n".join(block) + "\n"
    return found


def drop_rule_problems(name, text):
    job = jobs(text).get("pushgateway")
    if job is None:
        return ["%s: no pushgateway job" % name]
    if not DROP_RULE_RE.search(job):
        return ["%s: the pushgateway job has no drop rule %r with action: drop" % (name, DROP_REGEX)]
    return []


def honor_labels_problems(name, text):
    problems = []
    blocks = jobs(text)
    for job, block in sorted(blocks.items()):
        if job != "pushgateway" and HONOR_RE.search(block):
            problems.append("%s: job %s has honor_labels: true" % (name, job))
    inside = sum(len(HONOR_RE.findall(b)) for b in blocks.values())
    if len(HONOR_RE.findall(without_comments(text))) != inside:
        problems.append("%s: honor_labels: true outside a job" % name)
    return problems


class RulesTest(unittest.TestCase):
    def setUp(self):
        self.text = read(RULES)
        self.exprs = rule_expressions(self.text)

    def test_three_rules(self):
        self.assertEqual(len(self.exprs), 3)
        self.assertEqual(re.findall(r"^\s*- record:\s*(\S+)\s*$", self.text, re.M), list(RULE_OUTPUTS))

    def test_raw_series_selected_with_s(self):
        problems, count = raw_selector_problems(self.exprs)
        self.assertEqual(problems, [])
        self.assertEqual(count, 3)

    def test_rule_outputs_selected_with_empty_job(self):
        problems, count = rule_output_problems(self.exprs)
        self.assertEqual(problems, [])
        self.assertEqual(count, 3)


class ConfigsTest(unittest.TestCase):
    def test_pushgateway_drops_cost_look_alikes(self):
        for name in CONFIGS:
            with self.subTest(config=name):
                text = read(os.path.join(PROMETHEUS, name))
                self.assertEqual(drop_rule_problems(name, text), [])
                self.assertTrue(HONOR_RE.search(jobs(text)["pushgateway"]))

    def test_pushgateway_is_the_only_honor_labels_job(self):
        for name in ALL_CONFIGS:
            with self.subTest(config=name):
                text = read(os.path.join(PROMETHEUS, name))
                self.assertGreater(len(jobs(text)), 0)
                self.assertEqual(honor_labels_problems(name, text), [])

    def test_parallelcluster_config_loads_cost_rules(self):
        text = without_comments(read(os.path.join(PROMETHEUS, "prometheus.yml")))
        self.assertRegex(text, r"\nrule_files:\n  - /etc/prometheus/rules/cost\.yml\n\s*\nscrape_configs:\n")


class SelfCheckTest(unittest.TestCase):
    """Each scan fails on a copy with the guard removed."""

    def test_raw_scan_catches_each_missing_s(self):
        text = read(RULES)
        starts = [m.start() for m in re.finditer(re.escape(S), text)]
        self.assertEqual(len(starts), 3)
        for start in starts:
            copy = text[:start] + text[start + len(S):]
            copy = copy.replace("{,", "{")
            with self.subTest(offset=start):
                problems, _ = raw_selector_problems(rule_expressions(copy))
                self.assertEqual(len(problems), 1)
        partial = text.replace(S, 'job=~"ec2_instances|login_node"', 1)
        self.assertEqual(len(raw_selector_problems(rule_expressions(partial))[0]), 1)

    def test_rule_output_scan_catches_each_missing_empty_job(self):
        text = read(RULES)
        starts = [m.start() for m in re.finditer(re.escape(RULE_SELECTOR), text)]
        # The comment names {job=""} too; only the expressions count.
        exprs = [m for m in starts if not text[:m].rsplit("\n", 1)[-1].lstrip().startswith("#")]
        self.assertEqual(len(exprs), 3)
        for start in exprs:
            copy = text[:start] + text[start + len(RULE_SELECTOR):]
            with self.subTest(offset=start):
                problems, _ = rule_output_problems(rule_expressions(copy))
                self.assertEqual(len(problems), 1)
        other = text.replace('fresh{job=""}', 'fresh{job="x"}', 1)
        self.assertEqual(len(rule_output_problems(rule_expressions(other))[0]), 1)

    def test_drop_scan_catches_a_missing_or_weakened_rule(self):
        for name in CONFIGS:
            text = read(os.path.join(PROMETHEUS, name))
            block = DROP_RULE_RE.search(text).group(0)
            for label, copy in (
                    ("removed", text.replace(block, "")),
                    ("keep", text.replace("action: drop", "action: keep")),
                    ("regex", text.replace(DROP_REGEX, "cluster_cost_.+|node_textfile_.+")),
                    ("other job", text.replace(block, "").replace(
                        "  - job_name: 'cloudwatch_exporter'\n",
                        "  - job_name: 'cloudwatch_exporter'\n" + block))):
                with self.subTest(config=name, mutation=label):
                    self.assertNotEqual(drop_rule_problems(name, copy), [])

    def test_honor_labels_scan_catches_another_job(self):
        for name in ALL_CONFIGS:
            text = read(os.path.join(PROMETHEUS, name))
            job = [j for j in jobs(text) if j != "pushgateway"][0]
            line = re.search(r"^  - job_name: '?%s'?\n" % job, text, re.M).group(0)
            copy = text.replace(line, line + "    honor_labels: true\n", 1)
            with self.subTest(config=name, job=job):
                self.assertEqual(honor_labels_problems(name, copy),
                                 ["%s: job %s has honor_labels: true" % (name, job)])


if __name__ == "__main__":
    unittest.main()
