#!/usr/bin/env python3
"""Check the Grafana dashboards and the links that point at them.

installer/install.sh deploys the shared dashboards in grafana/dashboards/
together with one platform subdirectory, pcluster/ on ParallelCluster or
pcs/ on AWS PCS, flattened into one directory. Each of these two layouts is
checked on its own:

- every dashboard file is valid JSON and has a valid UID;
- no two dashboards in the layout have the same UID (Grafana then refuses to
  save any provisioned dashboard);
- every /grafana/d/<uid> link in the layout's dashboards and on the landing
  page (www/index.html, served on both platforms) opens a dashboard of the
  layout;
- the home dashboard that compose/head.yml sets is part of the layout.

Run from the repository root. Exits 1 when a check fails.
"""
import json
import os
import re
import sys

DASHBOARDS = "grafana/dashboards"
LAYOUTS = {"ParallelCluster": "pcluster", "AWS PCS": "pcs"}
LANDING_PAGE = "www/index.html"
COMPOSE_HEAD = "compose/head.yml"
PROVISIONED_DIR = "/etc/grafana/provisioning/dashboards/"
UID = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
LINK = re.compile(r"/grafana/d/([^/?#\"'\s]+)")
HOME = re.compile(r"GF_DASHBOARDS_DEFAULT_HOME_DASHBOARD_PATH=([^'\"\s]+)")

errors = []


def json_files(directory):
    return sorted(os.path.join(directory, f) for f in os.listdir(directory) if f.endswith(".json"))


def strings(node):
    """Every string value in a JSON document."""
    if isinstance(node, dict):
        for value in node.values():
            yield from strings(value)
    elif isinstance(node, list):
        for value in node:
            yield from strings(value)
    elif isinstance(node, str):
        yield node


def load(path):
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError) as e:
        errors.append("%s: not valid JSON: %s" % (path, e))
        return None
    uid = doc.get("uid") if isinstance(doc, dict) else None
    if not isinstance(uid, str) or not UID.match(uid):
        errors.append("%s: missing or invalid uid %r (1-40 characters: letters, digits, - and _)" % (path, uid))
        return None
    return doc


shared = {path: load(path) for path in json_files(DASHBOARDS)}
with open(LANDING_PAGE, encoding="utf-8") as fh:
    landing_links = LINK.findall(fh.read())
with open(COMPOSE_HEAD, encoding="utf-8") as fh:
    homes = HOME.findall(fh.read())
if len(homes) != 1:
    errors.append("%s: expected one GF_DASHBOARDS_DEFAULT_HOME_DASHBOARD_PATH, found %d" % (COMPOSE_HEAD, len(homes)))

for platform, subdir in sorted(LAYOUTS.items()):
    layout = dict(shared)
    layout.update((path, load(path)) for path in json_files(os.path.join(DASHBOARDS, subdir)))
    docs = {path: doc for path, doc in layout.items() if doc is not None}
    by_uid = {}
    for path, doc in sorted(docs.items()):
        by_uid.setdefault(doc["uid"], []).append(path)
    for uid, paths in sorted(by_uid.items()):
        if len(paths) > 1:
            errors.append("%s: uid %r is used by %s" % (platform, uid, ", ".join(paths)))
    links = [(LANDING_PAGE, uid) for uid in landing_links]
    for path, doc in sorted(docs.items()):
        # A UID built from a Grafana variable can't be checked here.
        links += [(path, uid) for text in strings(doc) for uid in LINK.findall(text) if "$" not in uid]
    for source, uid in links:
        if uid not in by_uid:
            errors.append("%s: %s links to /grafana/d/%s, which no %s dashboard has" % (platform, source, uid, platform))
    for home in homes:
        name = home[len(PROVISIONED_DIR):] if home.startswith(PROVISIONED_DIR) else None
        if name is None or "/" in name or not any(os.path.basename(p) == name for p in docs):
            errors.append("%s: %s sets the home dashboard %s, which the %s layout doesn't have"
                          % (platform, COMPOSE_HEAD, home, platform))
    print("%s: %d dashboards, %d unique UIDs, %d dashboard links checked (%d on the landing page)"
          % (platform, len(layout), len(by_uid), len(links), len(landing_links)))

for e in errors:
    print("ERROR " + e)
sys.exit(1 if errors else 0)
