"""Tests for scripts/check_repo.py guards (issue #22)."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
OLD_NAME = "-".join(("context", "budget", "guard"))


def _load_check_repo():
    scripts = str(ROOT / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    import check_repo
    return check_repo


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    res = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
    )
    if res.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {res.stderr}")
    return res


def _commit(repo: Path, message: str) -> None:
    _git(repo, "add", "-A")
    _git(
        repo,
        "-c", "user.name=Spillway Test",
        "-c", "user.email=test@example.com",
        "-c", "commit.gpgsign=false",
        "commit", "-m", message,
    )


def _make_clean_repo(tmp: str) -> Path:
    cr = _load_check_repo()
    repo = Path(tmp) / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    for rel in cr.REQUIRED:
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if rel == "pyproject.toml":
            target.write_text('version = "0.5.0"\n', encoding="utf-8")
        elif rel == "CHANGELOG.md":
            target.write_text(f"## [0.5.0]\n\nRenamed from {OLD_NAME}.\n", encoding="utf-8")
        elif rel == "SKILL.md":
            target.write_text("---\nname: spillway\n---\n\nbody\n", encoding="utf-8")
        elif rel == ".github/workflows/ci.yml":
            target.write_text(
                "name: CI\non: [push]\njobs:\n  t:\n    runs-on: ubuntu-24.04\n"
                "    steps:\n      - uses: actions/checkout@" + "a" * 40 + "\n",
                encoding="utf-8",
            )
        else:
            target.write_text("ok\n", encoding="utf-8")
    (repo / ".gitignore").write_text("benchmarks/real/results/\n", encoding="utf-8")
    init_py = repo / "spillway" / "__init__.py"
    init_py.parent.mkdir(parents=True, exist_ok=True)
    init_py.write_text('__version__ = "0.5.0"\n', encoding="utf-8")
    legacy_py = repo / "spillway" / "legacy.py"
    legacy_py.write_text(
        "def existing():\n"
        "    value = 1  " + chr(35) + " pre-existing comment\n"
        "    return value\n",
        encoding="utf-8",
    )
    _commit(repo, "initial")
    return repo


class CheckRepoGuards(unittest.TestCase):
    def test_clean_fixture_repo_and_real_workspace_pass(self):
        cr = _load_check_repo()
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_clean_repo(tmp)
            self.assertEqual(cr.check(repo, base_ref="main"), [])
        self.assertEqual(cr.check(ROOT), [])

    def test_old_name_allowed_in_changelog_but_rejected_elsewhere(self):
        cr = _load_check_repo()
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_clean_repo(tmp)
            self.assertEqual(cr._old_name_errors(repo), [])
            offender = repo / "docs" / "design.md"
            offender.write_text(f"Old URL: https://github.com/seatrunk/{OLD_NAME}\n", encoding="utf-8")
            errors = cr.check(repo, base_ref="main")
            self.assertTrue(any("docs/design.md" in err and "old repository name" in err for err in errors), errors)

            offender.write_text("ok\n", encoding="utf-8")
            mixed = repo / "README.md"
            mixed.write_text(f"Mixed case {OLD_NAME.title()} leftover\n", encoding="utf-8")
            errors = cr.check(repo, base_ref="main")
            self.assertTrue(any("README.md" in err and "old repository name" in err for err in errors), errors)

    def test_tracked_real_replay_results_rejected_while_untracked_allowed(self):
        cr = _load_check_repo()
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_clean_repo(tmp)
            result = repo / "benchmarks" / "real" / "results" / "real-replay.json"
            result.parent.mkdir(parents=True, exist_ok=True)
            result.write_text('{"aggregate_only": true}\n', encoding="utf-8")
            self.assertEqual(cr.check(repo, base_ref="main"), [])

            _git(repo, "add", "-f", "benchmarks/real/results/real-replay.json")
            errors = cr.check(repo, base_ref="main")
            self.assertTrue(
                any("benchmarks/real/results/real-replay.json" in err and "forbidden" in err for err in errors),
                errors,
            )

            _commit(repo, "commit forbidden result")
            errors = cr.check(repo, base_ref="main")
            self.assertTrue(
                any("benchmarks/real/results/real-replay.json" in err and "forbidden" in err for err in errors),
                errors,
            )

    def test_new_python_comment_in_changed_file_rejected(self):
        cr = _load_check_repo()
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_clean_repo(tmp)
            _git(repo, "checkout", "-b", "feat/pr")
            target = repo / "spillway" / "legacy.py"
            target.write_text(
                target.read_text(encoding="utf-8")
                + "\ndef added():\n    " + chr(35) + " forbidden comment\n    return 2\n",
                encoding="utf-8",
            )
            _commit(repo, "add comment")
            for key in ("diff.mnemonicPrefix", "diff.noprefix"):
                _git(repo, "config", key, "true")
            errors = cr.check(repo, base_ref="main")
            self.assertTrue(any("spillway/legacy.py:6" in err and "comment" in err for err in errors), errors)

    def test_inline_comment_and_new_untracked_python_file_comment_rejected(self):
        cr = _load_check_repo()
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_clean_repo(tmp)
            _git(repo, "checkout", "-b", "feat/pr")
            new_file = repo / "spillway" / "fresh.py"
            new_file.write_text("x = 1  " + chr(35) + " inline note\n", encoding="utf-8")
            errors = cr.check(repo, base_ref="main")
            self.assertTrue(any("spillway/fresh.py:1" in err and "comment" in err for err in errors), errors)

            new_file.write_text("import sys  " + chr(35) + " noqa: E402 sneaky prose\n", encoding="utf-8")
            errors = cr.check(repo, base_ref="main")
            self.assertTrue(any("spillway/fresh.py:1" in err and "comment" in err for err in errors), errors)

    def test_untouched_legacy_comment_strings_shebang_and_strict_noqa_allowed(self):
        cr = _load_check_repo()
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_clean_repo(tmp)
            _git(repo, "checkout", "-b", "feat/pr")
            legacy = repo / "spillway" / "legacy.py"
            legacy.write_text(
                legacy.read_text(encoding="utf-8")
                + '\ndef clean():\n    """Docstring with ' + chr(35) + ' hash."""\n    return "' + chr(35) + ' ok"\n',
                encoding="utf-8",
            )
            script = repo / "spillway" / "tool.py"
            script.write_text(
                chr(35) + "!/usr/bin/env python3\n"
                "import os  " + chr(35) + " noqa: E402,F401\n"
                "VALUE = os.name\n",
                encoding="utf-8",
            )
            _commit(repo, "clean changes")
            self.assertEqual(cr.check(repo, base_ref="main"), [])
            with mock.patch.dict(os.environ, {"GITHUB_BASE_REF": "main", "GITHUB_EVENT_NAME": "pull_request"}):
                self.assertEqual(cr.check(repo), [])

    def test_cli_exits_nonzero_on_invalid_base_ref(self):
        res = subprocess.run(
            [sys.executable, str(ROOT / "scripts/check_repo.py"), "--base", "refs/heads/does-not-exist-xyz"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 1)
        self.assertIn("could not resolve base ref", res.stderr)


if __name__ == "__main__":
    unittest.main()
