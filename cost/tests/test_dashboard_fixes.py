"""The four dashboard fixes of design "Dashboard fixes (F9, F10, R14, EFS "IA")",
AC 47-50 (U): the variables, links and expressions in the dashboard JSON.
promtool runs the R14 and F10 expressions (promtool/dashboard_fixes_test.yml)."""
import json
import os
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
DASHBOARDS = os.path.normpath(os.path.join(HERE, os.pardir, os.pardir, "grafana", "dashboards"))

F9_QUERY = 'label_values(node_uname_info{job="ec2_instances",instance_name="HeadNode"}, instance_id)'
# Review finding 61: only the node's own node_uname_info series, one step every 15 s.
F10_QUERY = ('query_result(sort_desc(max by (instance_id) (max_over_time('
             'timestamp(node_uname_info{nodename="$node"})[$__range:15s]))))')
F10_REGEX = '/instance_id="([^"]+)"/'
LIST_LINK = ('/grafana/d/compute-node-details/?var-node=${__data.fields["Node Name"]}'
             '&var-node_instance_id=${__data.fields["Instance ID"]}')
R14 = {
    "Idle node-hours (last 24h)": (
        "Idle nodes",
        'avg_over_time((sum(slurm_node_count_per_state{state=~"idle.*", state!~".*~.*"}) or vector(0))'
        '[24h:5m]) * 24'),
    "Idle CPU-hours (last 24h)": (
        "Idle CPUs",
        'avg_over_time((sum(slurm_partition_idle_cpus) - (sum(slurm_cpus_per_state{state=~"idle.*", '
        'state=~".*~.*"}) or vector(0)) or vector(0))[24h:5m]) * 24'),
}
R14_DESCRIPTION = (" summed over the 5-minute steps of the last 24 hours. Steps without data count as 0, "
                   "so on a head or login node with less than 24 hours of data the value covers only that time.")


def read(*parts):
    with open(os.path.join(DASHBOARDS, *parts)) as f:
        return f.read()


def load(*parts):
    return json.loads(read(*parts))


def variable(dashboard, name):
    found = [v for v in dashboard["templating"]["list"] if v["name"] == name]
    assert len(found) == 1, name
    return found[0]


def panels(dashboard):
    def walk(items):
        for p in items:
            yield p
            for nested in walk(p.get("panels") or []):
                yield nested
    return list(walk(dashboard["panels"]))


def panel(dashboard, title):
    found = [p for p in panels(dashboard) if p.get("title") == title]
    assert len(found) == 1, title
    return found[0]


class HeadNodeDetailsTest(unittest.TestCase):
    """F9 (AC 47, U): the hidden instance_id variable lists only the head node."""

    def test_instance_id_selects_the_head_node(self):
        v = variable(load("pcluster", "head-node-details.json"), "instance_id")
        self.assertEqual((v["query"], v["definition"]), (F9_QUERY, F9_QUERY))
        self.assertIn('instance_name="HeadNode"', v["query"])
        self.assertEqual((v["type"], v["hide"], v["regex"]), ("query", 2, ""))


class ComputeNodeDetailsTest(unittest.TestCase):
    """F10 (AC 48, U): the node's instances, newest first, in the query's order."""

    def test_node_instance_id_lists_the_newest_instance_first(self):
        v = variable(load("compute-node-details.json"), "node_instance_id")
        self.assertEqual((v["query"], v["definition"], v["regex"]), (F10_QUERY, F10_QUERY, F10_REGEX))
        self.assertEqual((v["sort"], v["refresh"], v["type"], v["hide"]), (0, 2, "query", 2))


class ListLinksTest(unittest.TestCase):
    """F10 (AC 48, U): the row links of Compute Node List and Login Node List
    open the clicked row's instance."""

    def test_links_pass_the_instance_id(self):
        for parts in (("compute-node-list.json",), ("pcs", "login-node-list.json")):
            with self.subTest(dashboard="/".join(parts)):
                tables = [p for p in panels(load(*parts)) if p["type"] == "table"]
                self.assertEqual(len(tables), 1)
                table = tables[0]
                self.assertEqual([l["url"] for l in table["fieldConfig"]["defaults"]["links"]], [LIST_LINK])
                # The link reads the "Instance ID" field: the table must keep it, visible.
                organize = [t for t in table["transformations"] if t["id"] == "organize"]
                self.assertEqual(len(organize), 1)
                options = organize[0]["options"]
                self.assertEqual(options["renameByName"]["instance_id"], "Instance ID")
                self.assertFalse(options["excludeByName"].get("instance_id", False))
                for override in table["fieldConfig"].get("overrides", []):
                    if override["matcher"].get("options") in ("Instance ID", "instance_id"):
                        for prop in override["properties"]:
                            self.assertFalse(prop["id"] == "custom.hidden" and prop["value"], override)


class ClusterSummaryTest(unittest.TestCase):
    """R14 (AC 49): the idle-waste stats zero-fill inside the 24-hour subquery."""

    def test_idle_stats(self):
        dashboard = load("ParallelCluster.json")
        for title, (what, expr) in sorted(R14.items()):
            with self.subTest(panel=title):
                p = panel(dashboard, title)
                self.assertEqual([t["expr"] for t in p["targets"]], [expr])
                self.assertEqual(p["description"], what + R14_DESCRIPTION)


class StorageTest(unittest.TestCase):
    """EFS "IA" (AC 50): series C asks for the storage class CloudWatch reports."""

    def test_infrequent_access_line(self):
        self.assertNotIn("InfrequentAccess", read("storage.json"))
        targets = {t["refId"]: t for t in panel(load("storage.json"), "EFS Storage Bytes")["targets"]}
        self.assertEqual(targets["C"]["expr"], 'aws_efs_storage_bytes_average{storage_class="IA"}')
        self.assertEqual(targets["C"]["legendFormat"], "IA {{file_system_id}}")


if __name__ == "__main__":
    unittest.main()
