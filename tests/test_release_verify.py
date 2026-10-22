from pathlib import Path
import shutil
import subprocess

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "verify_release_tag.sh"
GIT = shutil.which("git")
BASH = shutil.which("bash")


def _git(repo: Path, *args: str) -> str:
    assert GIT is not None
    completed = subprocess.run(  # noqa: S603
        [
            GIT,
            "-c",
            "user.name=release-test",
            "-c",
            "user.email=release-test@example.com",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "core.hooksPath=/dev/null",
            *args,
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout.strip()


def _commit(repo: Path, version: str, message: str) -> str:
    (repo / "pyproject.toml").write_text(f'[project]\nname = "demo"\nversion = "{version}"\n')
    _git(repo, "add", "pyproject.toml")
    _git(repo, "commit", "-q", "--allow-empty", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _verify(repo: Path, tag: str, sha: str) -> subprocess.CompletedProcess[str]:
    assert BASH is not None
    return subprocess.run(  # noqa: S603
        [BASH, str(SCRIPT), tag, sha],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture
def repo(tmp_path):
    """A release commit on master with later work after it, and a side commit off master."""
    _git(tmp_path, "init", "-q", "-b", "master")
    release = _commit(tmp_path, "0.2.0", "release")
    later = _commit(tmp_path, "0.2.0", "later work")
    _git(tmp_path, "update-ref", "refs/remotes/origin/master", later)
    _git(tmp_path, "checkout", "-q", "-b", "side", release)
    side = _commit(tmp_path, "0.2.0", "side work")
    _git(tmp_path, "checkout", "-q", release)
    return tmp_path, release, side


def test_a_tag_matching_the_version_on_a_master_commit_passes(repo):
    path, release, _side = repo

    result = _verify(path, "v0.2.0", release)

    assert result.returncode == 0, result.stderr
    assert "v0.2.0" in result.stdout


def test_a_tag_that_does_not_match_the_version_fails(repo):
    path, release, _side = repo

    result = _verify(path, "v0.2.1", release)

    assert result.returncode == 1
    assert "does not match" in result.stderr


def test_a_tag_without_the_v_prefix_fails(repo):
    path, release, _side = repo

    result = _verify(path, "0.2.0", release)

    assert result.returncode == 1
    assert "does not match" in result.stderr


def test_a_commit_that_is_not_on_master_fails(repo):
    path, _release, side = repo
    _git(path, "checkout", "-q", side)

    result = _verify(path, "v0.2.0", side)

    assert result.returncode == 1
    assert "not on origin/master" in result.stderr
