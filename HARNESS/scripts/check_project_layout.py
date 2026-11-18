"""Check the maintained HARNESS document routes. Reads the working tree only."""

import argparse
import json
import re
from collections import Counter
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
OWNERS = {
    "architecture.md": "docs/ARCHITECTURE.md",
    "status.md": "agent/STATUS.md",
    "todo.md": "agent/TODO.md",
}
LINK = re.compile(r"\]\((?:<([^>\n]+)>|([^()\s]+))(?:\s+\"[^\"\n]*\")?\)")


def prose_lines(body):
    """Ignore fenced examples when inspecting links, headings and task lists."""
    fence = None
    for number, line in enumerate(body.splitlines(), 1):
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        if marker:
            chars = marker.group(1)
            if fence is None:
                fence = chars
            elif chars.startswith(fence) and not line[marker.end():].strip():
                fence = None
        elif fence is None:
            yield number, line


def anchors(body):
    result = set()
    for _, line in prose_lines(body):
        heading = re.match(r"^#{1,6}\s+(.+)$", line)
        if heading:
            title = re.sub(r"[^\w\s-]", "", heading.group(1).lower())
            result.add(re.sub(r"\s+", "-", title.strip()))
        result.update(re.findall(r'<a\s+(?:name|id)="([^"]+)"', line))
    return result


def check(root, with_evidence):
    problems = []
    paths = {p for p in (root / "README.md", root / "AGENTS.md") if p.is_file()}
    for directory in ("docs", "agent", "skills"):
        paths.update((root / directory).rglob("*.md"))
    for p in sorted(paths.copy()):
        if p.resolve() != p or p.name.lower() == "api.md":
            problems.append(f"{p.relative_to(root)}: private files or symlinks do not belong in these docs")
            paths.remove(p)
    bodies = {p: p.read_text(encoding="utf-8") for p in sorted(paths)}
    canonical = {root / relative for relative in OWNERS.values()}
    index = root / "agent/experiments/README.md"
    required = canonical | {index, root / "AGENTS.md", root / "README.md"}
    for p in sorted(required - bodies.keys()):
        problems.append(f"Missing required document: {p.relative_to(root)}")
    if problems:
        return problems

    status = root / OWNERS["status.md"]
    todo = root / OWNERS["todo.md"]
    architecture = root / OWNERS["architecture.md"]
    registry = json.loads((root / "releases/registry.json").read_text(encoding="utf-8"))
    active = [r for r in registry["releases"] if r["version"] == registry["active_version"]]
    first_paragraph = bodies[status].strip().split("\n\n", 2)[1:2]
    if len(active) != 1 or not first_paragraph or active[0]["name"] not in first_paragraph[0]:
        problems.append("agent/STATUS.md: first paragraph must identify the registered active release")

    indexed = Counter()
    archive = root / "agent/history"
    check_fragments = canonical | {root / "AGENTS.md", index} | set(archive.glob("*.md"))
    available = {p: anchors(bodies[p]) for p in check_fragments if p in bodies}
    report_dir = index.parent
    reports = set(report_dir.rglob("*.md")) - {index}
    workspace = (root.parent, root.parent.parent / "Artifacts", root.parent.parent / "Tools")
    for p in sorted(report_dir.rglob("*")):
        if p.is_file() and p.suffix != ".md":
            problems.append(f"{p.relative_to(root)}: keep experiment payloads in their artifact directory")
    linked = set()
    for p, body in bodies.items():
        rel = p.relative_to(root)
        owner = OWNERS.get(p.name.lower())
        if owner and p != root / owner:
            problems.append(f"{rel}: duplicate document role, canonical path is {owner}")
        if p in reports and not re.fullmatch(r"\d{8}-.+\.md", p.name):
            problems.append(f"{rel}: experiment filename needs the date and question")
        for number, line in prose_lines(body):
            location = f"{rel}:{number}"
            if p == todo and re.match(r"^\s*[-*] \[[xX]\]", line):
                problems.append(f"{location}: move the completed result to its report")
            for match in LINK.finditer(line):
                href = match.group(1) or match.group(2)
                if href.startswith(("//", "/")) or re.match(r"^[\w+.-]+:", href):
                    continue
                path, _, fragment = unquote(href).partition("#")
                target = (p.parent / path).resolve() if path else p
                linked.add((p, target))
                if p == index and target in reports:
                    indexed[target] += 1
                if not target.is_relative_to(root):
                    if with_evidence and any(target.is_relative_to(base) for base in workspace) and not target.exists():
                        problems.append(f"{location}: missing workspace reference {href}")
                    continue
                local = target.relative_to(root)
                if not local.parts:
                    continue
                if local.parts[0] in ("artifacts", "runs", "experience") and not with_evidence:
                    continue
                if local.parts[0] in ("third_party", ".venv", ".overlay"):
                    continue  # Installed separately from maintained source.
                if not target.exists():
                    problems.append(f"{location}: missing {href}")
                elif fragment and target in available and fragment not in available[target]:
                    problems.append(f"{location}: missing section {href}")
                if p == architecture and local.parts[0] == "agent":
                    problems.append(f"{location}: Why&How cannot open agent-only links")
    for p in sorted(reports):
        if indexed[p] != 1:
            problems.append(f"{index.relative_to(root)}: expected one entry for {p.name}, got {indexed[p]}")
    for p in (root / "README.md", root / "AGENTS.md"):
        if (p, architecture) not in linked:
            problems.append(f"{p.name}: link directly to {architecture.relative_to(root)}")
    return problems


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--with-evidence", action="store_true",
                        help="also check linked files in local artifacts/, runs/ and experience/")
    args = parser.parse_args()
    problems = check(ROOT, args.with_evidence)
    for problem in problems:
        print(problem)
    if problems:
        return 1
    print("HARNESS document roles, local links, active release and experiment index are consistent.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
