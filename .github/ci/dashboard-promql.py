#!/usr/bin/env python3
"""Write the dashboards' Prometheus queries as a rules file for promtool.

Collects the expression of every Prometheus panel target in
grafana/dashboards/**/*.json (CloudWatch targets have none) and the selector
inside each template variable's label_values(...) or query_result(...),
replaces Grafana's variables with fixed values (built-in intervals with
durations, template variables with a value of their type), and writes each
one as a recording rule. `promtool check rules` on the output then parses
every query.

Usage: dashboard-promql.py OUTPUT   (run from the repository root)
Prints the number of rules written. Exits 1 when a variable is left over.
"""
import glob
import json
import re
import sys

BUILTINS = {
    "__rate_interval": "2m", "__interval_ms": "60000", "__interval": "1m",
    "__range_ms": "3600000", "__range_s": "3600", "__range": "1h",
}
QUERY_VALUE = "lint-value"  # stands in for a label_values() or query_result() result


def substitute(text, values):
    for name in sorted(values, key=len, reverse=True):
        v = values[name]
        text = re.sub(r"\$\{" + re.escape(name) + r"(:[^}]*)?\}", lambda _m: v, text)
        text = text.replace("[[" + name + "]]", v)
        text = re.sub(r"\$" + re.escape(name) + r"(?![A-Za-z0-9_])", lambda _m: v, text)
    return text


def var_value(v):
    t = v.get("type")
    if t == "interval":
        return "1m"
    if t == "custom":
        opts = v.get("options") or []
        if opts:
            return str(opts[0].get("value", ""))
        return (v.get("query") or "").split(",")[0].strip()
    if t in ("constant", "textbox"):
        cur = v.get("current") or {}
        return str(cur.get("value", v.get("query", "")) or "") or QUERY_VALUE
    if v.get("includeAll"):
        return ".*"
    return QUERY_VALUE


def walk(panels):
    for p in panels or []:
        yield p
        yield from walk(p.get("panels"))


def is_cloudwatch(target, panel):
    ds = target.get("datasource") or panel.get("datasource")
    if isinstance(ds, dict):
        ds = ds.get("type") or ds.get("uid")
    return isinstance(ds, str) and "cloudwatch" in ds.lower()


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    exprs, problems = [], []  # (dashboard, where, expression)
    for f in sorted(glob.glob("grafana/dashboards/**/*.json", recursive=True)):
        with open(f, encoding="utf-8") as fh:
            d = json.load(fh)
        values = dict(BUILTINS)
        tvars = (d.get("templating") or {}).get("list") or []
        for v in tvars:
            values[v["name"]] = var_value(v)
        for v in tvars:
            if v.get("type") != "query":
                continue
            q = v.get("query")
            if isinstance(q, dict):
                q = q.get("query", "")
            m = (re.match(r"^\s*label_values\((.*),\s*[A-Za-z_][A-Za-z0-9_]*\s*\)\s*$", q or "", re.S)
                 or re.match(r"^\s*query_result\((.*)\)\s*$", q or "", re.S))
            if m:
                exprs.append((f, "variable %s" % v["name"], substitute(m.group(1), values)))
        for p in walk(d.get("panels")):
            for t in p.get("targets") or []:
                if is_cloudwatch(t, p) or not t.get("expr"):
                    continue
                exprs.append((f, "panel %r %s" % (p.get("title", ""), t.get("refId", "")),
                              substitute(t["expr"], values)))
    lines = ["groups:"]
    groups = {}
    for n, (f, where, e) in enumerate(exprs):
        if re.search(r"\$\{?[A-Za-z_]|\[\[[A-Za-z_]", e):
            problems.append("%s %s: variable left in %r" % (f, where, e))
        groups.setdefault(f, []).append(("dashboard:%d" % n, where, e))
    for f, rules in groups.items():
        lines.append("  - name: %s" % json.dumps(f))
        lines.append("    rules:")
        for name, where, e in rules:
            lines.append("      # %s" % where.replace("\n", " "))
            lines.append("      - record: %s" % json.dumps(name))
            lines.append("        expr: %s" % json.dumps(e, ensure_ascii=False))
    with open(sys.argv[1], "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    for p in problems:
        print("ERROR " + p, file=sys.stderr)
    print(len(exprs))
    return 1 if problems or not exprs else 0


if __name__ == "__main__":
    sys.exit(main())
