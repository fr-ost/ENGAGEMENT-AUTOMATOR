"""The workflow's save script, run against throwaway git repositories."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / ".github" / "scripts" / "persist-state.sh"

pytestmark = pytest.mark.skipif(
    not (SCRIPT.is_file() and shutil.which("git") and shutil.which("bash")),
    reason="needs git, bash and the repository's .github/scripts",
)

GIT_ENV = {
    **os.environ,
    "GIT_CONFIG_GLOBAL": os.devnull,     # no signing, hooks or identity from the machine running the tests
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.test",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.test",
}


def git(cwd, *args):
    done = subprocess.run(["git", *args], cwd=cwd, env=GIT_ENV, capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


def clone(remote, target):
    git(remote.parent, "clone", "--quiet", str(remote), str(target))
    return target


@pytest.fixture
def repo(tmp_path):
    """A remote with one commit, the bot's checkout of it, and that commit's SHA."""
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "--quiet", "--bare", "--initial-branch=main", str(remote))
    work = clone(remote, tmp_path / "bot")
    (work / ".github" / "scripts").mkdir(parents=True)
    shutil.copy(SCRIPT, work / ".github" / "scripts" / SCRIPT.name)
    bot_dir = work / "x-daily-automation"
    (bot_dir / "data").mkdir(parents=True)
    (bot_dir / "reports").mkdir()
    (bot_dir / "data" / "state.json").write_text('{"n": 0}\n')
    (bot_dir / "reports" / "2026-11-15.md").write_text("report 0\n")
    (bot_dir / "main.py").write_text("print('v1')\n")
    git(work, "checkout", "--quiet", "-B", "main")
    git(work, "add", "--all")
    git(work, "commit", "--quiet", "-m", "initial")
    git(work, "push", "--quiet", "origin", "HEAD:main")
    return remote, work, git(work, "rev-parse", "HEAD")


def save(work, start_sha=None):
    env = {**GIT_ENV, "BRANCH": "main"}
    if start_sha:
        env["START_SHA"] = start_sha
    script = work / ".github" / "scripts" / SCRIPT.name
    return subprocess.run(["bash", str(script)], cwd=work, env=env, capture_output=True, text=True)


def remote_file(remote, path):
    return git(remote, "show", f"main:{path}")


def test_commits_and_pushes_the_bots_files_only(repo):
    remote, work, start = repo
    (work / "x-daily-automation" / "data" / "state.json").write_text('{"n": 1}\n')
    (work / "x-daily-automation" / "reports" / "2026-11-15.md").write_text("report 1\n")
    (work / "x-daily-automation" / "scratch.txt").write_text("not the bot's data\n")

    done = save(work, start)

    assert done.returncode == 0, done.stdout + done.stderr
    assert remote_file(remote, "x-daily-automation/data/state.json") == '{"n": 1}'
    assert remote_file(remote, "x-daily-automation/reports/2026-11-15.md") == "report 1"
    assert git(remote, "log", "-1", "--format=%s", "main").startswith("chore(bot): state and report ")
    assert "scratch.txt" not in git(remote, "ls-tree", "-r", "--name-only", "main")


def test_nothing_to_save_is_fine(repo):
    remote, work, start = repo
    assert save(work, start).returncode == 0
    assert git(remote, "rev-parse", "main") == start


def test_rebases_onto_a_code_change_and_asks_for_a_fresh_run(repo, tmp_path):
    remote, work, start = repo
    other = clone(remote, tmp_path / "developer")
    (other / "x-daily-automation" / "main.py").write_text("print('v2')\n")
    git(other, "commit", "--quiet", "-am", "new code")
    git(other, "push", "--quiet", "origin", "HEAD:main")
    (work / "x-daily-automation" / "data" / "state.json").write_text('{"n": 2}\n')

    done = save(work, start)

    assert done.returncode == 10, done.stdout + done.stderr           # saved, but the code changed
    assert remote_file(remote, "x-daily-automation/data/state.json") == '{"n": 2}'
    assert remote_file(remote, "x-daily-automation/main.py") == "print('v2')"
    assert git(remote, "log", "--format=%s", "main").splitlines()[1] == "new code"   # bot commit sits on top
    assert save(work).returncode == 0                                 # without START_SHA it never asks


def test_code_change_is_noticed_even_with_nothing_to_save(repo, tmp_path):
    remote, work, start = repo
    other = clone(remote, tmp_path / "developer")
    (other / ".github" / "workflows").mkdir(parents=True)
    (other / ".github" / "workflows" / "daily.yml").write_text("name: changed\n")
    git(other, "add", "--all")
    git(other, "commit", "--quiet", "-m", "workflow change")
    git(other, "push", "--quiet", "origin", "HEAD:main")
    assert save(work, start).returncode == 10


def test_the_bots_own_earlier_commits_do_not_count_as_a_code_change(repo):
    remote, work, start = repo
    for n in (1, 2):
        (work / "x-daily-automation" / "data" / "state.json").write_text(f'{{"n": {n}}}\n')
        assert save(work, start).returncode == 0
    assert git(remote, "rev-list", "--count", "main") == "3"


def test_conflicting_edit_is_refused_without_forcing(repo, tmp_path):
    remote, work, start = repo
    other = clone(remote, tmp_path / "someone")
    (other / "x-daily-automation" / "data" / "state.json").write_text('{"n": "edited by hand"}\n')
    git(other, "commit", "--quiet", "-am", "manual edit")
    git(other, "push", "--quiet", "origin", "HEAD:main")
    theirs = git(remote, "rev-parse", "main")
    (work / "x-daily-automation" / "data" / "state.json").write_text('{"n": 3}\n')

    done = save(work, start)

    assert done.returncode == 1
    assert git(remote, "rev-parse", "main") == theirs                 # nothing overwritten
    assert not (work / ".git" / "rebase-merge").exists() and not (work / ".git" / "rebase-apply").exists()
    assert (work / "x-daily-automation" / "data" / "state.json").read_text() == '{"n": 3}\n'   # bot's copy intact
