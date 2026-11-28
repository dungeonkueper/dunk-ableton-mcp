"""End-to-end tests for install.py against a local git repository.

Everything runs in tmp_path: a fake HOME with an Ableton User Library, a
local "remote" repo with two branches and a PR ref, and no network.
"""

import importlib.util
import json
import os
import socket
import subprocess
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("dunk_install", ROOT / "install.py")
inst = importlib.util.module_from_spec(spec)
spec.loader.exec_module(inst)


def git(cwd, *args):
    subprocess.run(["git", *args], cwd=str(cwd), check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def commit_version(repo, version):
    (repo / "AbletonMCP_Remote_Script").mkdir(exist_ok=True)
    (repo / "MCP_Server").mkdir(exist_ok=True)
    (repo / "AbletonMCP_Remote_Script" / "__init__.py").write_text(f'SCRIPT_VERSION = "{version}"\n')
    (repo / "MCP_Server" / "remote_script_install.py").write_text(f'EXPECTED_REMOTE_SCRIPT_VERSION = "{version}"\n')
    (repo / "pyproject.toml").write_text(f'[project]\nname = "x"\nversion = "{version}"\n')
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", version)


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    library = home / "Music" / "Ableton" / "User Library"
    library.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("DUNK_MCP_HOME", str(home / ".dunk-ableton-mcp"))
    monkeypatch.setenv("GIT_AUTHOR_NAME", "t")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "t@example.invalid")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "t")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "t@example.invalid")
    # never talk to a real Live instance
    monkeypatch.setattr(inst, "ABLETON_PORT", _free_port())

    repo = tmp_path / "remote"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    commit_version(repo, "1.8.0")
    git(repo, "checkout", "-q", "-b", "v2.0")
    commit_version(repo, "2.0.0")
    git(repo, "update-ref", "refs/pull/3/head", "v2.0")
    git(repo, "checkout", "-q", "main")
    git(repo, "tag", "-a", "v1.8.0", "-m", "release")

    def run(*args):
        return inst.main(["--repo", str(repo), "--yes", "--no-sync", *args])

    return type("Env", (), {
        "home": home,
        "dunk": home / ".dunk-ableton-mcp",
        "rs": library / "Remote Scripts",
        "repo": repo,
        "run": staticmethod(run),
    })


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def script_version(env):
    text = (env.rs / "AbletonMCP" / "__init__.py").read_text()
    return text.split('"')[1]


def state(env):
    return json.loads((env.dunk / "state.json").read_text())


def test_install_switch_and_rollback(env):
    assert env.run("--ref", "main") == 0
    assert script_version(env) == "1.8.0"
    assert inst.is_link(env.rs / "AbletonMCP")

    assert env.run("--ref", "pr/3") == 0
    assert script_version(env) == "2.0.0"
    assert state(env)["current"] == "pr-3"
    assert state(env)["previous"] == "main"

    assert env.run("--rollback") == 0
    assert script_version(env) == "1.8.0"
    assert state(env)["previous"] == "pr-3"

    assert env.run("--switch", "pr-3") == 0
    assert script_version(env) == "2.0.0"
    assert env.run("--list") == 0
    assert env.run("--status") == 0


def test_tag_and_update_of_branch(env):
    assert env.run("--ref", "v1.8.0") == 0
    assert state(env)["current"] == "v1.8.0"
    assert env.run("--ref", "v2.0") == 0
    git(env.repo, "checkout", "-q", "v2.0")
    commit_version(env.repo, "2.0.1")
    assert env.run("--ref", "v2.0") == 0  # same ref again = update
    assert script_version(env) == "2.0.1"


def test_dry_run_changes_nothing(env):
    assert env.run("--ref", "main", "--dry-run") == 0
    assert not env.dunk.exists()
    assert not (env.rs / "AbletonMCP").exists()


def test_existing_folder_is_backed_up(env):
    old = env.rs / "AbletonMCP"
    old.mkdir(parents=True)
    (old / "__init__.py").write_text('SCRIPT_VERSION = "1.7.0"\n')
    assert env.run("--ref", "main") == 0
    backups = list((env.dunk / "backups").iterdir())
    assert len(backups) == 1
    assert "1.7.0" in (backups[0] / "__init__.py").read_text()


def test_copy_mode_and_back_to_link(env):
    assert env.run("--ref", "main", "--copy") == 0
    entry = env.rs / "AbletonMCP"
    assert not inst.is_link(entry) and (entry / inst.MANAGED_MARKER).is_file()
    assert env.run("--ref", "v2.0") == 0  # copy mode is remembered
    assert not inst.is_link(entry) and script_version(env) == "2.0.0"
    assert env.run("--switch", "main", "--link") == 0
    assert inst.is_link(entry) and script_version(env) == "1.8.0"
    assert not (env.dunk / "backups").exists() or all(
        "AbletonMCP" in p.name for p in (env.dunk / "backups").iterdir())


def test_link_failure_falls_back_to_copy(env, monkeypatch):
    real_point_link = inst.point_link

    def flaky(link, target):
        if link.name == "AbletonMCP":
            raise inst.InstallError("no symlink rights")
        return real_point_link(link, target)

    monkeypatch.setattr(inst, "point_link", flaky)
    assert env.run("--ref", "main") == 0
    assert script_version(env) == "1.8.0"
    assert state(env)["script_mode"] == "copy"


def test_local_changes_block_update(env):
    assert env.run("--ref", "main") == 0
    d = env.dunk / "versions" / "main"
    (d / "uv.lock").write_text("lock\n")  # uv may rewrite the lock: must not block
    git(d, "add", "uv.lock")
    git(d, "commit", "-q", "-m", "lock")
    (d / "uv.lock").write_text("rewritten by uv\n")
    assert env.run("--ref", "main") == 0
    (d / "pyproject.toml").write_text("changed\n")
    assert env.run("--ref", "main") == 2  # real edits do block


def test_unknown_ref_and_remove(env):
    assert env.run("--ref", "does-not-exist") == 2
    assert env.run("--ref", "main") == 0
    assert env.run("--ref", "v2.0") == 0
    assert env.run("--remove", "v2.0") == 2  # active
    assert env.run("--remove", "main") == 0
    assert not (env.dunk / "versions" / "main").exists()
    assert "previous" not in state(env)


def test_status_reports_live_mismatch(env, monkeypatch, capsys):
    assert env.run("--ref", "v2.0") == 0
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    monkeypatch.setattr(inst, "ABLETON_PORT", srv.getsockname()[1])

    def serve():
        conn, _ = srv.accept()
        conn.recv(4096)
        conn.sendall(json.dumps({"status": "success", "result": {"script_version": "1.8.0"}}).encode())
        conn.close()

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    assert env.run("--status") == 1
    assert "restart Live" in capsys.readouterr().out
    srv.close()


def test_configure_claude_desktop_keeps_env(env, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    cfg = env.home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(json.dumps({"mcpServers": {
        "AbletonMCP": {"command": "uvx", "args": ["ableton-mcp"], "env": {"ABLETON_MCP_DISABLE_TELEMETRY": "true"}},
        "Other": {"command": "x"},
    }}))
    assert env.run("--configure-claude-desktop") == 0
    data = json.loads(cfg.read_text())
    entry = data["mcpServers"]["AbletonMCP"]
    assert entry["args"][-1] == "ableton-mcp" and str(env.dunk / "current") in entry["args"]
    assert entry["env"] == {"ABLETON_MCP_DISABLE_TELEMETRY": "true"}
    assert data["mcpServers"]["Other"] == {"command": "x"}
    assert cfg.with_suffix(".json.bak").is_file()


@pytest.mark.skipif(os.name == "nt", reason="POSIX slug check")
def test_slug():
    assert inst.slug_for("pr/3") == "pr-3"
    assert inst.slug_for("feature/foo bar") == "feature-foo-bar"


def test_github_slug():
    assert inst.github_slug("https://github.com/dungeonkueper/dunk-ableton-mcp.git") == ("dungeonkueper", "dunk-ableton-mcp")
    assert inst.github_slug("git@github.com:a/b.git") == ("a", "b")
    assert inst.github_slug("https://github.com/a/b") == ("a", "b")
    assert inst.github_slug("/tmp/local/repo") is None
    assert inst.github_slug("https://example.com/a/b.git") is None


def test_pr_warning_only_for_pull_requests(monkeypatch):
    monkeypatch.setattr(inst, "pr_origin", lambda repo, n: None)
    assert inst.pr_warning("x", "refs/heads/main") == []
    assert inst.pr_warning("x", "refs/tags/v1.8.0") == []
    lines = inst.pr_warning("x", "refs/pull/7/head")
    assert "Pull request #7" in lines[0]
    assert "Author unknown" in lines[-1]


def test_pr_warning_names_fork_and_author(monkeypatch):
    info = {"author": "mallory", "head_repo": "mallory/dunk-ableton-mcp",
            "base_repo": "dungeonkueper/dunk-ableton-mcp", "state": "open", "merged": False}
    monkeypatch.setattr(inst, "pr_origin", lambda repo, n: info)
    text = "\n".join(inst.pr_warning("x", "refs/pull/7/head"))
    assert "@mallory" in text and "FORK mallory/dunk-ableton-mcp" in text and "open" in text

    info = dict(info, author="dungeonkueper", head_repo="dungeonkueper/dunk-ableton-mcp")
    text = "\n".join(inst.pr_warning("x", "refs/pull/7/head"))
    assert "a branch of dungeonkueper/dunk-ableton-mcp" in text and "FORK" not in text

    info = dict(info, head_repo=None)
    assert "no longer exists" in "\n".join(inst.pr_warning("x", "refs/pull/7/head"))


def test_plan_shows_pr_warning(env, monkeypatch, capsys):
    monkeypatch.setattr(inst, "pr_origin", lambda repo, n: None)
    assert env.run("--ref", "pr/3", "--dry-run") == 0
    out = capsys.readouterr().out
    assert "Pull request #3: installing it runs" in out
    assert env.run("--ref", "main", "--dry-run") == 0
    assert "Pull request #" not in capsys.readouterr().out
