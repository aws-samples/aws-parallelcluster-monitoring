"""Shared test helpers: a scripted command runner, a fake clock, scratch
directories, stub executables, an exposition parser and a harness that runs
cluster_cost.main() against them."""
import io
import json
import os
import re
import shutil
import sys
import tempfile

import cluster_cost as cc

from . import stub_aws, stub_slurm

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = stub_aws.FIXTURES
# 2026-11-25T12:00:00Z: mid-month, so that no test crosses a month boundary by accident.
START = 1795608000.0
PC_CLUSTER = "mycluster"
PCS_CLUSTER = "pcs_abc123def4"
REGION = "ap-southeast-6"
# The Slurm fixtures of each platform's fleet: nodes, and the jobs of the end-to-end tests.
SLURM_NODES = {"parallelcluster": "nodes-v0.0.44.json", "pcs": "nodes-v0.0.45.json"}
SLURM_JOBS = {"parallelcluster": "jobs-v0.0.44.json", "pcs": "jobs-v0.0.45.json"}


def fixture_text(*parts):
    return stub_aws.read(*parts)


def fixture(*parts):
    return stub_aws.load(*parts)


# --- Clock ------------------------------------------------------------------------
class FakeClock:
    """Wall and monotonic time that move only when told to."""

    def __init__(self, wall=START, mono=1000.0):
        self._wall = float(wall)
        self._mono = float(mono)

    def wall(self):
        return self._wall

    def monotonic(self):
        return self._mono

    def advance(self, seconds):
        self._wall += seconds
        self._mono += seconds


# --- Runner -----------------------------------------------------------------------
def ok(obj):
    text = obj if isinstance(obj, str) else json.dumps(obj)
    return cc.CommandResult(rc=0, stdout=text)


def aws_error(x, operation="GetProducts", message="denied", prefix=True, rc=254):
    text = "An error occurred (%s) when calling the %s operation: %s" % (x, operation, message)
    return cc.CommandResult(rc=rc, stderr=("aws: [ERROR]: " if prefix else "") + text + "\n")


THROTTLED = aws_error("ThrottlingException", message="Rate exceeded")
DENIED = aws_error("AccessDeniedException", message="User is not authorized")


class Call:
    def __init__(self, argv, timeout, env):
        self.argv = list(argv)
        self.timeout = timeout
        self.env = dict(env) if env is not None else None

    @property
    def op(self):
        return " ".join(self.argv[1:3])

    def __repr__(self):
        return "Call(%s)" % self.op


class Rule:
    def __init__(self, match, answer, seconds=0.0, times=None):
        self.match = match
        self.answer = answer
        self.seconds = seconds
        self.times = times

    def matches(self, argv):
        if self.times is not None and self.times <= 0:
            return False
        if callable(self.match):
            return self.match(argv)
        return all(word in argv for word in self.match)


class FakeRunner:
    """A command runner scripted by argv patterns.

    Rules are tried newest first. A match is a tuple of argv tokens that must
    all be present, or a callable. The answer is a CommandResult, or a
    callable(Call) returning one. A rule can advance the fake clock, to
    simulate a slow call. Without a matching rule, the call is answered from
    the fixture files: scontrol and squeue by stub_slurm.route with the
    (nodes, jobs) of self.slurm, everything else by stub_aws.route. Every
    call's argv, timeout and env are recorded.
    """

    def __init__(self, clock=None, slurm=None):
        self.clock = clock
        self.slurm = slurm
        self.rules = []
        self.calls = []

    def on(self, match, answer, seconds=0.0, times=None):
        self.rules.insert(0, Rule(match, answer, seconds, times))
        return self

    def __call__(self, argv, timeout, env):
        call = Call(argv, timeout, env)
        self.calls.append(call)
        for rule in self.rules:
            if rule.matches(argv):
                if rule.times is not None:
                    rule.times -= 1
                if rule.seconds and self.clock is not None:
                    self.clock.advance(rule.seconds)
                answer = rule.answer(call) if callable(rule.answer) else rule.answer
                return answer
        if os.path.basename(argv[0]) in ("scontrol", "squeue"):
            nodes, jobs = self.slurm or (None, None)
            rc, out, err = stub_slurm.route(argv, nodes, jobs)
        else:
            rc, out, err = stub_aws.route(argv)
        return cc.CommandResult(rc=rc, stdout=out, stderr=err)

    def count(self, *words):
        return len(self.find(*words))

    def find(self, *words):
        return [c for c in self.calls if all(w in c.argv for w in words)]

    def reset(self):
        self.calls = []


def is_pricing_for(field_value):
    """A match for a pricing call whose filters contain Field=..,Value=.."""
    field, value = field_value.split("=", 1)
    needle = "Field=%s,Value=%s" % (field, value)
    return lambda argv: "pricing" in argv and any(a.endswith(needle) for a in argv)


def has_arg(text):
    """A match for any call with an argument that contains text."""
    return lambda argv: any(text in a for a in argv)


def is_command(name):
    """A match for the calls of one executable (scontrol, squeue), by basename."""
    return lambda argv: os.path.basename(argv[0]) == name


# --- Files and executables ------------------------------------------------------------
class Scratch:
    """A scratch root with a state directory (0750), a textfile directory
    (0755) and a bin directory for stub executables."""

    def __init__(self):
        self.root = tempfile.mkdtemp(prefix="cost-test-")
        self.state = os.path.join(self.root, "state")
        self.textfile = os.path.join(self.root, "textfile")
        self.bin = os.path.join(self.root, "bin")
        os.mkdir(self.state, 0o750)
        os.mkdir(self.textfile, 0o755)
        os.mkdir(self.bin, 0o755)

    def path(self, *parts):
        return os.path.join(self.root, *parts)

    def cleanup(self):
        shutil.rmtree(self.root, ignore_errors=True)


def write_executable(path, text):
    """Write a stub executable (mode 0755, whatever the umask)."""
    with open(path, "w") as f:
        f.write(text)
    os.chmod(path, 0o755)
    return path


def write_python_stub(directory, name="aws"):
    """A stub `aws` that answers from the fixtures (stub_aws.main)."""
    return write_executable(os.path.join(directory, name), (
        "#!%s -B\nimport sys\nsys.path.insert(0, %r)\nimport stub_aws\nsys.exit(stub_aws.main())\n"
        % (sys.executable, HERE)))


def write_shell_stub(directory, body, name="aws"):
    """A /bin/sh stub. `$@` holds the aws arguments."""
    return write_executable(os.path.join(directory, name), "#!/bin/sh\n" + body)


def write_slurm_stubs(directory, nodes, jobs=None):
    """Stub scontrol and squeue executables that answer with stub_slurm.route.
    nodes and jobs are fixture names or documents (written next to the stubs)."""
    args = []
    for kind, doc in (("nodes", nodes), ("jobs", jobs)):
        if doc is not None and not isinstance(doc, str):
            path = os.path.join(directory, "%s.json" % kind)
            with open(path, "w") as f:
                json.dump(doc, f)
            doc = path
        args.append(doc)
    for name in ("scontrol", "squeue"):
        write_executable(os.path.join(directory, name), (
            "#!%s -B\nimport sys\nsys.path.insert(0, %r)\nimport stub_slurm\nsys.exit(stub_slurm.main(%r, %r))\n"
            % (sys.executable, HERE, args[0], args[1])))


def read_stub_log(path):
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


# --- Exposition ---------------------------------------------------------------------
SAMPLE_RE = re.compile(r'^([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{(.*)\})? (\S+)$')
LABEL_RE = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:[^"\\]|\\.)*)"')


def _unescape(v):
    return re.sub(r'\\(.)', lambda m: {"n": "\n"}.get(m.group(1), m.group(1)), v)


class Exposition:
    """A parsed textfile. Raises AssertionError when a family appears in more
    than one block, a series repeats, a TYPE isn't gauge or a sample has a
    timestamp."""

    def __init__(self, text):
        self.text = text
        self.families = []
        self.samples = {}
        current = None
        state = None
        for line in text.splitlines():
            if line.startswith("# HELP "):
                name = line.split(" ", 3)[2]
                if name in self.families:
                    raise AssertionError("family %s appears in two blocks" % name)
                self.families.append(name)
                current, state = name, "help"
                continue
            if line.startswith("# TYPE "):
                _, _, name, kind = line.split(" ", 3)
                if name != current or state != "help" or kind != "gauge":
                    raise AssertionError("bad TYPE line %r" % line)
                state = "type"
                continue
            m = SAMPLE_RE.match(line)
            if not m:
                raise AssertionError("bad sample line %r" % line)
            name, labels_text, value = m.groups()
            if name != current or state not in ("type", "sample"):
                raise AssertionError("sample %s outside its family block" % name)
            state = "sample"
            labels = {}
            if labels_text:
                consumed = ",".join(x.group(0) for x in LABEL_RE.finditer(labels_text))
                if consumed != labels_text:
                    raise AssertionError("bad labels %r" % labels_text)
                labels = {k: _unescape(v) for k, v in LABEL_RE.findall(labels_text)}
            key = (name, frozenset(labels.items()))
            if key in self.samples:
                raise AssertionError("duplicate series %r" % (key,))
            self.samples[key] = float(value)

    def series(self, name):
        return [(dict(labels), v) for (n, labels), v in sorted(self.samples.items(), key=repr) if n == name]

    def value(self, name, **labels):
        return self.samples.get((name, frozenset(labels.items())))

    def has(self, name, **labels):
        return (name, frozenset(labels.items())) in self.samples

    def sum(self, name, **match):
        return sum(v for labels, v in self.series(name)
                   if all(labels.get(k) == want for k, want in match.items()))

    def stage_errors(self):
        return {labels["stage"]: labels["code"] for labels, _ in self.series("cluster_cost_collector_stage_error")}

    def stages_ok(self):
        return {labels["stage"]: v for labels, v in self.series("cluster_cost_collector_stage_ok")}

    def unpriced(self):
        return {(labels["kind"], labels["reason"]): v
                for labels, v in self.series("cluster_cost_unpriced_resources")}


# --- Harness ------------------------------------------------------------------------
class Harness:
    """Runs cluster_cost.main() with a FakeRunner, a FakeClock and scratch
    directories. Call close() when done."""

    def __init__(self, platform="parallelcluster", cluster=None, region=REGION, runner=None, clock=None):
        self.scratch = Scratch()
        self.clock = clock or FakeClock()
        self.runner = runner if runner is not None else FakeRunner(self.clock)
        if getattr(self.runner, "clock", None) is None:
            self.runner.clock = self.clock
        self.slurm_bin = self.scratch.path("slurm-bin")
        os.mkdir(self.slurm_bin, 0o755)
        self.environ = {
            "PLATFORM": platform,
            "CLUSTER_ID": cluster or (PC_CLUSTER if platform == "parallelcluster" else PCS_CLUSTER),
            "REGION": region,
            "CLUSTER_COST_STATE_DIR": self.scratch.state,
            "CLUSTER_COST_TEXTFILE_DIR": self.scratch.textfile,
            "CLUSTER_COST_SLURM_BIN_DIR": self.slurm_bin,
            "PATH": "/usr/local/bin:/usr/bin:/bin",
        }
        # By default Slurm knows the fleet's nodes and runs no job, so that a
        # test's own fleet never fails the mapping check.
        self.use_slurm(SLURM_NODES[platform], None)
        self.owner_uid = os.getuid()
        self.stdout = io.StringIO()
        self.stderr = io.StringIO()

    def use_slurm(self, nodes, jobs=None):
        """Answer scontrol and squeue with these fixtures or documents, through
        the FakeRunner and through the stub executables of slurm-bin."""
        self.runner.slurm = (nodes, jobs)
        write_slurm_stubs(self.slurm_bin, nodes, jobs)

    def deps(self):
        return cc.Deps(runner=self.runner, clock=self.clock, owner_uid=self.owner_uid,
                       root=self.scratch.root, environ=dict(self.environ), stdout=self.stdout,
                       stderr=self.stderr)

    def run(self, *argv):
        return cc.main(list(argv), dict(self.environ), self.deps())

    def runs(self, n, every=60.0):
        """n runs, `every` seconds apart. Yields (exit code, exposition)."""
        for i in range(n):
            if i:
                self.clock.advance(every)
            rc = self.run()
            yield rc, self.metrics()

    def textfile_path(self):
        return os.path.join(self.scratch.textfile, cc.TEXTFILE_NAME)

    def textfile(self):
        with open(self.textfile_path(), encoding="utf-8") as f:
            return f.read()

    def metrics(self):
        return Exposition(self.textfile())

    def report(self):
        with open(os.path.join(self.scratch.state, cc.REPORT_FILE)) as f:
            return json.load(f)

    def prices(self):
        with open(os.path.join(self.scratch.state, cc.PRICES_FILE)) as f:
            return json.load(f)

    def state(self, name=cc.STATE_FILE):
        with open(os.path.join(self.scratch.state, name)) as f:
            return json.load(f)

    def write_prices(self, data):
        with open(os.path.join(self.scratch.state, cc.PRICES_FILE), "w") as f:
            json.dump(data, f)

    def log(self):
        return self.stderr.getvalue()

    def close(self):
        self.scratch.cleanup()
