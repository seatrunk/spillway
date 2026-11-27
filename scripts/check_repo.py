"""Offline repository checks. These do not exercise the external runtime prototype."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tokenize
from typing import Dict, List, Optional, Set, Tuple

from validate_benchmark import PRIVATE, validate_report

ROOT = Path(__file__).resolve().parents[1]
REQUIRED = ["README.md", "CHANGELOG.md", "pyproject.toml", "LICENSE", "SKILL.md", "CONTRIBUTING.md",
            "SECURITY.md", "CODE_OF_CONDUCT.md", "docs/design.md", "docs/benchmarking.md",
            "docs/project-status.md", "docs/rollout.md", "docs/repository-governance.md",
            ".github/CODEOWNERS", ".github/PULL_REQUEST_TEMPLATE.md",
            ".github/ISSUE_TEMPLATE/config.yml", ".github/ISSUE_TEMPLATE/bug.yml",
            ".github/ISSUE_TEMPLATE/feature.yml", ".github/ISSUE_TEMPLATE/benchmark.yml",
            ".github/workflows/ci.yml", ".github/dependabot.yml"]
OLD_NAME = "-".join(("context", "budget", "guard"))
OLD_NAME_RE = re.compile(OLD_NAME.encode("ascii"), re.IGNORECASE)
ALLOWED_COMMENT_RE = re.compile(r"#\s*noqa(?::\s*[A-Z]{1,4}[0-9]{1,4}(?:\s*,\s*[A-Z]{1,4}[0-9]{1,4})*)?")
HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
SKIP_DIRS = frozenset({".git", "__pycache__", ".venv", "node_modules", "build", "dist"})
ZERO_SHA = "0" * 40


def _git(root: Path, *args: str, text: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=text)


def _repo_files(root: Path) -> Optional[List[str]]:
    if (root / ".git").exists():
        res = _git(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard", text=False)
        if res.returncode != 0:
            return None
        return sorted(p.decode("utf-8", "surrogateescape") for p in res.stdout.split(b"\x00") if p)
    return sorted(
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if not any(part in SKIP_DIRS for part in path.relative_to(root).parts)
        and not path.is_symlink() and path.is_file()
    )


def _version_errors(root: Path = ROOT) -> List[str]:
    """The package, pyproject and CHANGELOG must agree on one version."""
    found = {}
    sources = {
        "pyproject.toml": (root / "pyproject.toml", r'^version\s*=\s*"([^"]+)"'),
        "spillway/__init__.py": (root / "spillway/__init__.py", r'^__version__\s*=\s*"([^"]+)"'),
        "CHANGELOG.md": (root / "CHANGELOG.md", r"^## \[?(\d+\.\d+\.\d+)\]?"),
    }
    for label, (path, pattern) in sources.items():
        if path.is_file():
            match = re.search(pattern, path.read_text(encoding="utf-8"), re.M)
            found[label] = match.group(1) if match else None
    if len(set(found.values())) > 1:
        return ["version mismatch: " + ", ".join("%s=%s" % kv for kv in sorted(found.items()))]
    return []


def _old_name_errors(root: Path = ROOT) -> List[str]:
    files = _repo_files(root)
    if files is None:
        return ["old-name check: git ls-files failed"]
    errors = []
    for rel in files:
        if rel == "CHANGELOG.md":
            continue
        path = root / rel
        if path.is_symlink() or not path.is_file():
            continue
        try:
            data = path.read_bytes()
        except OSError:
            errors.append(rel + ": could not read file for old-name check")
            continue
        if OLD_NAME_RE.search(data):
            errors.append(rel + ": old repository name must appear only in CHANGELOG.md")
    return errors


def _resolve_base_commit(root: Path, base_ref: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    in_ci = os.environ.get("GITHUB_ACTIONS", "").lower() == "true"
    head = _git(root, "rev-parse", "--verify", "HEAD^{commit}")
    if head.returncode != 0:
        return None, None
    head_sha = head.stdout.strip()
    if base_ref is not None:
        resolved: Optional[str] = None
        res = _git(root, "merge-base", base_ref, "HEAD")
        if res.returncode == 0 and res.stdout.strip():
            resolved = res.stdout.strip()
        else:
            res = _git(root, "rev-parse", "--verify", base_ref + "^{commit}")
            if res.returncode == 0 and res.stdout.strip():
                resolved = res.stdout.strip()
        if resolved is None:
            return None, "python comment check: could not resolve base ref " + base_ref
        if in_ci and resolved == head_sha:
            return None, "python comment check: resolved base ref equals HEAD (shallow clone or missing base?)"
        return resolved, None
    candidates: List[str] = []
    gh_base = os.environ.get("GITHUB_BASE_REF", "").strip()
    if gh_base:
        candidates.extend(["origin/" + gh_base, gh_base])
    gh_event = os.environ.get("GITHUB_EVENT_NAME", "").strip()
    if gh_event == "pull_request":
        candidates.append("HEAD^1")
    gh_before = os.environ.get("GITHUB_EVENT_BEFORE", "").strip()
    if gh_before and gh_before != ZERO_SHA:
        candidates.append(gh_before)
    candidates.extend(["origin/main", "main"])
    if gh_event == "workflow_dispatch":
        candidates.append("HEAD~1")
    for ref in candidates:
        res = _git(root, "merge-base", ref, "HEAD")
        if res.returncode == 0 and res.stdout.strip():
            resolved = res.stdout.strip()
            if resolved != head_sha:
                return resolved, None
    if in_ci:
        return None, "python comment check: base ref not found (shallow clone?)"
    return head_sha, None


def _tracked_real_replay_results_errors(root: Path = ROOT, base_ref: Optional[str] = None) -> List[str]:
    if not (root / ".git").exists():
        return []
    res = _git(root, "ls-files", "-z", "--", "benchmarks/real/results", text=False)
    if res.returncode != 0:
        return ["benchmarks/real/results/: could not inspect git index"]
    tracked = sorted(p.decode("utf-8", "surrogateescape") for p in res.stdout.split(b"\x00") if p)
    errors = [rel + ": tracked file under benchmarks/real/results/ is forbidden" for rel in tracked]
    seen = set(tracked)
    base_commit, _ = _resolve_base_commit(root, base_ref)
    if base_commit is not None:
        hist = _git(
            root,
            "log", "--name-only", "-z", "--format=", base_commit + "..HEAD", "--", "benchmarks/real/results",
            text=False,
        )
        if hist.returncode != 0:
            errors.append("benchmarks/real/results/: could not inspect git history")
        else:
            hist_paths = sorted(
                {p.decode("utf-8", "surrogateescape").strip("\n") for p in hist.stdout.split(b"\x00") if p.strip(b"\n")}
            )
            for rel in hist_paths:
                if rel not in seen:
                    errors.append(rel + ": file under benchmarks/real/results/ in branch history is forbidden")
    return errors


def _added_python_lines(root: Path, base_ref: Optional[str]) -> Tuple[Dict[str, Optional[Set[int]]], List[str]]:
    base_commit, err = _resolve_base_commit(root, base_ref)
    if err is not None:
        return {}, [err]
    added: Dict[str, Optional[Set[int]]] = {}
    if base_commit is None:
        files = _repo_files(root)
        if files is None:
            return {}, ["python comment check: git ls-files failed"]
        return {rel: None for rel in files if rel.endswith(".py")}, []
    names = _git(root, "diff", "--name-status", "-z", "-M", "--diff-filter=ACMR", base_commit, text=False)
    if names.returncode != 0:
        return {}, ["python comment check: git diff --name-status failed"]
    tokens = [p.decode("utf-8", "surrogateescape") for p in names.stdout.split(b"\x00") if p]
    idx = 0
    while idx < len(tokens):
        status = tokens[idx]
        idx += 1
        if status.startswith(("R", "C")):
            if idx + 1 >= len(tokens):
                return {}, ["python comment check: malformed git diff --name-status output"]
            old_rel, rel = tokens[idx], tokens[idx + 1]
            idx += 2
            if not rel.endswith(".py"):
                continue
            if status == "R100":
                continue
            diff_args = ["-M", "--no-ext-diff", "--no-color", "-U0", base_commit, "--"]
            diff_args.extend([old_rel, rel] if status.startswith("R") else [rel])
        else:
            if idx >= len(tokens):
                return {}, ["python comment check: malformed git diff --name-status output"]
            rel = tokens[idx]
            idx += 1
            if not rel.endswith(".py"):
                continue
            diff_args = ["--no-ext-diff", "--no-color", "-U0", base_commit, "--", rel]
        diff = _git(
            root,
            "-c", "core.quotePath=false",
            "-c", "diff.mnemonicPrefix=false",
            "-c", "diff.noprefix=false",
            "diff", *diff_args,
        )
        if diff.returncode != 0:
            return {}, [rel + ": python comment check git diff failed"]
        for line in diff.stdout.splitlines():
            match = HUNK_RE.match(line)
            if match:
                start = int(match.group(1))
                count = 1 if match.group(2) is None else int(match.group(2))
                if count > 0:
                    added.setdefault(rel, set()).update(range(start, start + count))
    untracked = _git(root, "ls-files", "-z", "--others", "--exclude-standard", "--", "*.py", text=False)
    if untracked.returncode != 0:
        return added, ["python comment check: git ls-files failed"]
    for raw in untracked.stdout.split(b"\x00"):
        if raw:
            added[raw.decode("utf-8", "surrogateescape")] = None
    return added, []


def _new_python_comment_errors(root: Path = ROOT, base_ref: Optional[str] = None) -> List[str]:
    if not (root / ".git").exists():
        return []
    added, errors = _added_python_lines(root, base_ref)
    for rel, lines in sorted(added.items()):
        path = root / rel
        if path.is_symlink() or not path.is_file():
            errors.append(rel + ": changed Python file is missing or not a regular file")
            continue
        try:
            with path.open("rb") as fh:
                for tok in tokenize.tokenize(fh.readline):
                    if tok.type != tokenize.COMMENT:
                        continue
                    line_no = tok.start[0]
                    if lines is not None and line_no not in lines:
                        continue
                    if line_no == 1 and tok.string.startswith("#!"):
                        continue
                    if ALLOWED_COMMENT_RE.fullmatch(tok.string):
                        continue
                    errors.append(f"{rel}:{line_no}: new Python comment is not allowed")
        except (tokenize.TokenError, IndentationError, SyntaxError, OSError):
            errors.append(rel + ": could not tokenize Python file for comment check")
    return errors


def check(root: Path = ROOT, base_ref: Optional[str] = None) -> List[str]:
    errors = []
    for relative in REQUIRED:
        if not (root / relative).is_file():
            errors.append("missing " + relative)
    markdown = [path for path in root.rglob("*.md") if ".git" not in path.parts]
    for path in markdown:
        text = path.read_text(encoding="utf-8")
        label = str(path.relative_to(root))
        if PRIVATE.search(text):
            errors.append(label + ": private-path or credential-shaped text")
        for match in re.finditer(r"!?\[[^\]]*\]\(([^\s)]+)\)", text):
            target = match.group(1).split("#", 1)[0].split("?", 1)[0]
            if not target or re.match(r"[a-zA-Z][a-zA-Z0-9+.-]*:", target):
                continue
            resolved = (path.parent / target).resolve()
            if not resolved.is_relative_to(root) or not resolved.exists():
                errors.append(label + ": unresolved local link " + target)
    workflows = root / ".github/workflows"
    if workflows.is_dir():
        for path in workflows.glob("*.yml"):
            text = path.read_text(encoding="utf-8")
            if re.search(r"^\s*pull_request_target\s*:", text, flags=re.M):
                errors.append("privileged pull_request_target workflow is not allowed")
            for reference in re.findall(r"\buses:\s*([^\s#]+)", text):
                if not re.fullmatch(r"[^@]+@[0-9a-f]{40}", reference):
                    errors.append("action reference is not pinned to a full commit SHA")
    results_dir = root / "benchmarks/results"
    if results_dir.is_dir():
        for path in results_dir.glob("*.json"):
            try:
                validate_report(json.loads(path.read_text(encoding="utf-8")))
            except (ValueError, TypeError, OverflowError, RecursionError):
                errors.append(str(path.relative_to(root)) + ": invalid or unsanitized report")
    errors.extend(_version_errors(root))
    if (root / "adapters/hermes").exists():
        errors.append("adapters/hermes: the Hermes adapter was retired in 0.5.0 and must not come back")
    spillway_dir = root / "spillway"
    if spillway_dir.is_dir():
        for path in spillway_dir.glob("*.py"):
            if re.search(r"^\s*(?:from|import)\s+(?:cbg|adapters)\b", path.read_text(encoding="utf-8"), re.M):
                errors.append(str(path.relative_to(root)) + ": imports legacy/top-level adapters package")
    skill = root / "SKILL.md"
    if skill.exists():
        text = skill.read_text(encoding="utf-8")
        if not text.startswith("---\n") or "\n---\n" not in text[4:]:
            errors.append("SKILL.md: missing YAML frontmatter")
    errors.extend(_old_name_errors(root))
    errors.extend(_tracked_real_replay_results_errors(root, base_ref=base_ref))
    errors.extend(_new_python_comment_errors(root, base_ref=base_ref))
    return errors


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default=None, help="base git ref for changed-Python comment checks")
    args = parser.parse_args(argv)
    problems = check(ROOT, base_ref=args.base)
    for problem in problems:
        print(problem, file=sys.stderr)
    if problems:
        return 1
    print("Repository files, local link targets, action pins and sanitized benchmark reports: OK")
    print("External URLs, link anchors, and full YAML schemas are not checked by this script.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
