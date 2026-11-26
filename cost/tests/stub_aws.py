"""A stand-in for the AWS CLI that answers from the fixture files.

route() maps an aws argv to the fixture a real call would return, filtered
the way the real API filters it. FakeRunner uses it in-process, and the stub
`aws` executable of the end-to-end tests runs main(), which also appends each
call's argv and AWS_* environment to $COST_STUB_LOG.
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(HERE, "fixtures")

OD_FIXTURES = {
    ("c6i.large", "RunInstances", "Shared"): "od-c6i.large-linux-shared.json",
    ("t3.medium", "RunInstances", "Shared"): "od-t3.medium-linux-shared.json",
    ("c6i.large", "RunInstances:0010", "Shared"): "od-c6i.large-rhel-shared.json",
    ("c6i.large", "RunInstances", "Dedicated"): "od-c6i.large-linux-dedicated.json",
}
EBS_FIXTURES = {
    "Storage": "ebs-storage.json",
    "System Operation": "ebs-system-operation.json",
    "Provisioned Throughput": "ebs-provisioned-throughput.json",
}


def read(*parts):
    with open(os.path.join(FIXTURES, *parts)) as f:
        return f.read()


def load(*parts):
    return json.loads(read(*parts))


def arg_after(argv, flag):
    if flag in argv:
        i = argv.index(flag)
        if i + 1 < len(argv):
            return argv[i + 1]
    return None


def args_after(argv, flag):
    """The values of a list option, up to the next --option."""
    if flag not in argv:
        return []
    out = []
    for a in argv[argv.index(flag) + 1:]:
        if a.startswith("--"):
            break
        out.append(a)
    return out


def pricing_filters(argv):
    out = {}
    for a in args_after(argv, "--filters"):
        parts = dict(p.split("=", 1) for p in a.split(",", 2))
        out[parts["Field"]] = parts["Value"]
    return out


def describe_instances_fixture(argv, platform):
    filters = " ".join(args_after(argv, "--filters"))
    if "tag:parallelcluster:cluster-name" in filters:
        return "pc-describe-instances.json"
    if "tag:aws:pcs:cluster-id" in filters:
        return "pcs-describe-instances-cluster-id.json"
    return "pcs-describe-instances-pcs-cluster-id.json"


def volumes_answer(argv, fixture):
    """describe-volumes filtered on the attachment instance IDs of the call."""
    ids = set()
    for a in args_after(argv, "--filters"):
        if a.startswith("Name=attachment.instance-id,Values="):
            ids.update(a.split("Values=", 1)[1].split(","))
    vols = [v for v in load("aws", fixture)["Volumes"]
            if any(att.get("InstanceId") in ids for att in v.get("Attachments", []))]
    return json.dumps({"Volumes": vols})


def spot_answer(argv):
    types = set(args_after(argv, "--instance-types"))
    descs = set(args_after(argv, "--product-descriptions"))
    azs = set()
    for a in args_after(argv, "--filters"):
        if a.startswith("Name=availability-zone,Values="):
            azs.update(a.split("Values=", 1)[1].split(","))
    hist = [e for e in load("aws", "describe-spot-price-history.json")["SpotPriceHistory"]
            if e["InstanceType"] in types and e["ProductDescription"] in descs and e["AvailabilityZone"] in azs]
    return json.dumps({"SpotPriceHistory": hist})


def pricing_fixture(argv):
    service = arg_after(argv, "--service-code")
    f = pricing_filters(argv)
    if service == "AWSPCS":
        return "awspcs-us-east-1.json"
    if "productFamily" in f:
        return EBS_FIXTURES.get(f["productFamily"], "empty.json")
    return OD_FIXTURES.get((f.get("instanceType"), f.get("operation"), f.get("tenancy")), "empty.json")


def route(argv):
    """(rc, stdout, stderr) for one aws argv (argv[0] is the executable)."""
    args = argv[1:]
    if args[:2] == ["ec2", "describe-instances"]:
        return 0, read("aws", describe_instances_fixture(args, None)), ""
    if args[:2] == ["ec2", "describe-volumes"]:
        filters = " ".join(args_after(args, "--filters"))
        fixture = "pc-describe-volumes.json" if "i-0a" in filters else "pcs-describe-volumes.json"
        return 0, volumes_answer(args, fixture), ""
    if args[:2] == ["ec2", "describe-spot-price-history"]:
        return 0, spot_answer(args), ""
    if args[:2] == ["pricing", "get-products"]:
        return 0, read("pricing", pricing_fixture(args)), ""
    if args[:2] == ["pcs", "get-cluster"]:
        return 0, read("aws", "pcs-get-cluster.json"), ""
    if args[:2] == ["pcs", "list-compute-node-groups"]:
        return 0, read("aws", "pcs-list-compute-node-groups.json"), ""
    return 252, "", "aws: [ERROR]: argument operation: Invalid choice: %s" % " ".join(args[:2])


def main():
    log = os.environ.get("COST_STUB_LOG")
    if log:
        env = {k: v for k, v in os.environ.items() if k.startswith("AWS_") or k == "HOME"}
        with open(log, "a") as f:
            f.write(json.dumps({"argv": sys.argv[1:], "env": env}) + "\n")
    rc, out, err = route(sys.argv)
    sys.stdout.write(out)
    sys.stderr.write(err)
    return rc


if __name__ == "__main__":
    sys.exit(main())
