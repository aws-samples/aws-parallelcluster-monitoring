#!/usr/bin/env python3
"""Check the relative links in the repository's Markdown files.

For every link in a tracked *.md file whose target isn't an external URL
(http, https, mailto), check that the target exists relative to the file,
and that a #fragment on a Markdown target, or on the file itself, matches a
heading's GitHub anchor. Inline links and images, reference definitions and
HTML href/src attributes are checked; links in fenced code blocks and inline
code are not. External URLs are not fetched.

Run from the repository root. Exits 1 when a link is broken.
"""
import os
import re
import subprocess
import sys

INLINE = re.compile(r"\]\(\s*<?([^)\s>]+)>?(?:\s+(?:\"[^\"]*\"|'[^']*'))?\s*\)")
REFERENCE = re.compile(r"^\s{0,3}\[[^\]]+\]:\s*<?([^\s>]+)>?")
HTML = re.compile(r"\b(?:href|src)\s*=\s*[\"']([^\"']+)[\"']")
HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
FENCE = re.compile(r"^\s{0,3}(```|~~~)")
CODE_SPAN = re.compile(r"(`+)(?:.+?)\1")
EXTERNAL = re.compile(r"^(?:[A-Za-z][A-Za-z0-9+.-]*:|//)")


def lines_outside_code(path):
    """(line number, text) of the lines outside fenced code blocks."""
    fence = None
    with open(path, encoding="utf-8") as fh:
        for n, line in enumerate(fh, 1):
            m = FENCE.match(line)
            if m:
                if fence is None:
                    fence = m.group(1)
                elif m.group(1) == fence:
                    fence = None
                continue
            if fence is None:
                yield n, line


def slug(text):
    """GitHub's anchor for a heading."""
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", text)  # links and images: their text
    text = re.sub(r"<[^>]+>", "", text)                       # HTML tags
    text = text.replace("`", "")
    return re.sub(r"[^\w\- ]", "", text.lower()).replace(" ", "-")


anchors_cache = {}


def anchors(path):
    if path not in anchors_cache:
        seen, found = {}, set()
        for _, line in lines_outside_code(path):
            m = HEADING.match(line)
            if m:
                s = slug(m.group(2))
                n = seen.get(s, 0)
                seen[s] = n + 1
                found.add(s if n == 0 else "%s-%d" % (s, n))
        anchors_cache[path] = found
    return anchors_cache[path]


def main():
    files = subprocess.run(["git", "ls-files", "-z", "--", "*.md"], check=True,
                           capture_output=True, text=True).stdout.split("\0")
    files = [f for f in files if f]
    broken, checked = [], 0
    for md in files:
        for lineno, line in lines_outside_code(md):
            line = CODE_SPAN.sub("", line)
            targets = INLINE.findall(line) + REFERENCE.findall(line) + HTML.findall(line)
            for target in targets:
                if EXTERNAL.match(target):
                    continue
                checked += 1
                path, _, fragment = target.partition("#")
                dest = os.path.normpath(os.path.join(os.path.dirname(md), path)) if path else md
                if not os.path.exists(dest):
                    broken.append("%s:%d: %s: %s doesn't exist" % (md, lineno, target, dest))
                elif fragment and dest.endswith(".md") and fragment not in anchors(dest):
                    broken.append("%s:%d: %s: %s has no heading with the anchor #%s" % (md, lineno, target, dest, fragment))
    print("checked %d relative links in %d Markdown files; broken: %d" % (checked, len(files), len(broken)))
    for b in broken:
        print("ERROR " + b)
    return 1 if broken or not files else 0


if __name__ == "__main__":
    sys.exit(main())
