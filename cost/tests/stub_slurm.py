"""A stand-in for scontrol and squeue that answers from the Slurm fixtures.

route() answers `scontrol show nodes --json` with a nodes fixture and
`squeue --json --states=RUNNING,CONFIGURING,COMPLETING` with a jobs fixture,
or with an empty job list when there is none. FakeRunner uses it in-process,
and the stub executables of fakes.write_slurm_stubs() run main(), which also
appends each call's argv to $COST_SLURM_STUB_LOG.
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(HERE, "fixtures", "slurm")
SCONTROL_ARGS = ["show", "nodes", "--json"]
SQUEUE_ARGS = ["--json", "--states=RUNNING,CONFIGURING,COMPLETING"]


def read(name):
    with open(os.path.join(FIXTURES, name)) as f:
        return f.read()


def load(name):
    return json.loads(read(name))


def _text(doc):
    """A fixture name's text, or a document as JSON."""
    return read(doc) if isinstance(doc, str) else json.dumps(doc)


def route(argv, nodes, jobs=None):
    """(rc, stdout, stderr) for one Slurm argv (argv[0] is the executable).
    nodes and jobs are fixture names or documents. Without jobs, squeue
    answers an empty job list with the meta of the nodes."""
    name, args = os.path.basename(argv[0]), list(argv[1:])
    if nodes is not None and name == "scontrol" and args == SCONTROL_ARGS:
        return 0, _text(nodes), ""
    if nodes is not None and name == "squeue" and args == SQUEUE_ARGS:
        if jobs is None:
            meta = (load(nodes) if isinstance(nodes, str) else nodes).get("meta", {})
            return 0, json.dumps({"jobs": [], "meta": meta, "errors": [], "warnings": []}), ""
        return 0, _text(jobs), ""
    return 1, "", "%s: error: unexpected arguments: %s\n" % (name, " ".join(args))


def main(nodes, jobs=None):
    log = os.environ.get("COST_SLURM_STUB_LOG")
    if log:
        with open(log, "a") as f:
            f.write(json.dumps({"argv": [os.path.basename(sys.argv[0])] + sys.argv[1:]}) + "\n")
    rc, out, err = route(sys.argv, nodes, jobs)
    sys.stdout.write(out)
    sys.stderr.write(err)
    return rc
