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
CI_ENV_VARS = ("GITHUB_ACTIONS", "GITHUB_BASE_REF", "GITHUB_EVENT_NAME", "GITHUB_EVENT_BEFORE")


def _load_check_repo():
    scripts = str(ROOT / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    import check_repo
    return check_repo


def _clean_env(**overrides):
    env = {k: v for k, v in os.environ.items() if k not in CI_ENV_VARS}
    env.update(overrides)
    return mock.patch.dict(os.environ, env, clear=True)


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


def _make_shallow_clone_with_comment(tmp: str) -> Path:
    origin = _make_clean_repo(tmp)
    _git(origin, "checkout", "-b", "feat/pr")
    bad = origin / "spillway" / "shallow_bad.py"
    bad.write_text("x = 1  " + chr(35) + " committed comment\n", encoding="utf-8")
    _commit(origin, "commit comment on branch")
    shallow = Path(tmp) / "shallow"
    res = subprocess.run(
        ["git", "clone", "--depth", "1", "--no-local", "--branch", "feat/pr", str(origin), str(shallow)],
        capture_output=True,
        text=True,
    )
    if res.returncode != 0:
        raise AssertionError(f"git clone failed: {res.stderr}")
    return shallow


class CheckRepoGuards(unittest.TestCase):
    def test_clean_fixture_repo_passes(self):
        cr = _load_check_repo()
        with _clean_env(), tempfile.TemporaryDirectory() as tmp:
            repo = _make_clean_repo(tmp)
            self.assertEqual(cr.check(repo, base_ref="main"), [])

    def test_old_name_allowed_in_changelog_but_rejected_elsewhere(self):
        cr = _load_check_repo()
        with _clean_env(), tempfile.TemporaryDirectory() as tmp:
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
        with _clean_env(), tempfile.TemporaryDirectory() as tmp:
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

    def test_committed_then_removed_real_replay_result_in_branch_history_rejected(self):
        cr = _load_check_repo()
        with _clean_env(), tempfile.TemporaryDirectory() as tmp:
            repo = _make_clean_repo(tmp)
            _git(repo, "checkout", "-b", "feat/leak")
            result = repo / "benchmarks" / "real" / "results" / "leak.json"
            result.parent.mkdir(parents=True, exist_ok=True)
            result.write_text('{"aggregate_only": true}\n', encoding="utf-8")
            _git(repo, "add", "-f", "benchmarks/real/results/leak.json")
            _commit(repo, "commit forbidden result")
            _git(repo, "rm", "benchmarks/real/results/leak.json")
            _commit(repo, "remove forbidden result")
            errors = cr.check(repo, base_ref="main")
            self.assertTrue(
                any("benchmarks/real/results/leak.json" in err and "forbidden" in err for err in errors),
                errors,
            )

    def test_new_python_comment_in_changed_file_rejected(self):
        cr = _load_check_repo()
        with _clean_env(), tempfile.TemporaryDirectory() as tmp:
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

    def test_paths_with_spaces_or_quotes_are_checked_not_skipped(self):
        cr = _load_check_repo()
        with _clean_env(), tempfile.TemporaryDirectory() as tmp:
            repo = _make_clean_repo(tmp)
            _git(repo, "checkout", "-b", "feat/special-paths")
            spaced = repo / "spillway" / "has space.py"
            spaced.write_text("x = 1  " + chr(35) + " spaced comment\n", encoding="utf-8")
            quoted = repo / "spillway" / 'q"x.py'
            quoted.write_text("y = 2  " + chr(35) + " quoted comment\n", encoding="utf-8")
            _commit(repo, "add special paths")
            errors = cr.check(repo, base_ref="main")
            self.assertTrue(any("spillway/has space.py:1" in err for err in errors), errors)
            self.assertTrue(any('spillway/q"x.py:1' in err for err in errors), errors)

    def test_pure_rename_with_legacy_comment_allowed_but_new_comment_on_rename_rejected(self):
        cr = _load_check_repo()
        with _clean_env(), tempfile.TemporaryDirectory() as tmp:
            repo = _make_clean_repo(tmp)
            _git(repo, "checkout", "-b", "feat/rename")
            _git(repo, "mv", "spillway/legacy.py", "spillway/renamed.py")
            _commit(repo, "pure rename")
            self.assertEqual(cr.check(repo, base_ref="main"), [])

            renamed = repo / "spillway" / "renamed.py"
            renamed.write_text(
                renamed.read_text(encoding="utf-8") + "\nz = 3  " + chr(35) + " added after rename\n",
                encoding="utf-8",
            )
            _commit(repo, "add comment after rename")
            errors = cr.check(repo, base_ref="main")
            self.assertTrue(any("spillway/renamed.py:5" in err and "comment" in err for err in errors), errors)
            self.assertFalse(any("spillway/renamed.py:2" in err for err in errors), errors)

    def test_inline_comment_untracked_file_and_noqa_prose_rejected(self):
        cr = _load_check_repo()
        with _clean_env(), tempfile.TemporaryDirectory() as tmp:
            repo = _make_clean_repo(tmp)
            _git(repo, "checkout", "-b", "feat/pr")
            new_file = repo / "spillway" / "fresh.py"
            new_file.write_text("x = 1  " + chr(35) + " inline note\n", encoding="utf-8")
            errors = cr.check(repo, base_ref="main")
            self.assertTrue(any("spillway/fresh.py:1" in err and "comment" in err for err in errors), errors)

            new_file.write_text("import sys  " + chr(35) + " noqa: E402 sneaky prose\n", encoding="utf-8")
            errors = cr.check(repo, base_ref="main")
            self.assertTrue(any("spillway/fresh.py:1" in err and "comment" in err for err in errors), errors)

            new_file.write_text("import sys  " + chr(35) + " noqa: THIS, IS, A, FREE, COMMENT\n", encoding="utf-8")
            errors = cr.check(repo, base_ref="main")
            self.assertTrue(any("spillway/fresh.py:1" in err and "comment" in err for err in errors), errors)

    def test_untouched_legacy_comment_strings_shebang_and_strict_noqa_allowed(self):
        cr = _load_check_repo()
        with _clean_env(), tempfile.TemporaryDirectory() as tmp:
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
                "import sys  " + chr(35) + " noqa\n"
                "VALUE = os.name + str(bool(sys.version))\n",
                encoding="utf-8",
            )
            _commit(repo, "clean changes")
            self.assertEqual(cr.check(repo, base_ref="main"), [])
            with _clean_env(GITHUB_ACTIONS="true", GITHUB_BASE_REF="main", GITHUB_EVENT_NAME="pull_request"):
                self.assertEqual(cr.check(repo), [])

    def test_ci_shallow_clone_fails_closed_in_pr_and_push_modes(self):
        cr = _load_check_repo()
        with tempfile.TemporaryDirectory() as tmp:
            shallow = _make_shallow_clone_with_comment(tmp)
            with _clean_env(GITHUB_ACTIONS="true", GITHUB_BASE_REF="main", GITHUB_EVENT_NAME="pull_request"):
                errors = cr.check(shallow)
                self.assertTrue(any("base ref not found" in err for err in errors), errors)
            with _clean_env(GITHUB_ACTIONS="true", GITHUB_EVENT_NAME="push"):
                errors = cr.check(shallow)
                self.assertTrue(any("base ref not found" in err for err in errors), errors)

    def test_ci_direct_push_to_main_checks_event_before_and_fails_without_base(self):
        cr = _load_check_repo()
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_clean_repo(tmp)
            before = _git(repo, "rev-parse", "HEAD").stdout.strip()
            pushed = repo / "spillway" / "pushed.py"
            pushed.write_text("x = 1  " + chr(35) + " direct push comment\n", encoding="utf-8")
            _commit(repo, "push directly to main")
            with _clean_env(GITHUB_ACTIONS="true", GITHUB_EVENT_NAME="push", GITHUB_EVENT_BEFORE=before):
                errors = cr.check(repo)
                self.assertTrue(any("spillway/pushed.py:1" in err and "comment" in err for err in errors), errors)
            with _clean_env(GITHUB_ACTIONS="true", GITHUB_EVENT_NAME="push"):
                errors = cr.check(repo, base_ref="main")
                self.assertTrue(any("equals HEAD" in err for err in errors), errors)

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
