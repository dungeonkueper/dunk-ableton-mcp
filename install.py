#!/usr/bin/env python3
"""Install and switch versions of dunk-ableton-mcp.

You run this yourself. Nothing happens without your confirmation: the
installer prints a plan (what is active now, what comes next, which paths
change) and waits for "y" (or "j"). Use --dry-run to only see the plan.

Layout (default home ``~/.dunk-ableton-mcp``, override with DUNK_MCP_HOME):

    repo.git/                git cache (bare); your own checkout stays untouched
    versions/<name>/         one git worktree per branch, tag or PR
    current -> versions/<name>
    backups/                 anything the installer moved out of the way
    state.json               which version is current/previous, Remote Scripts path

Ableton sees ``<User Library>/Remote Scripts/AbletonMCP`` as a link to
``current/AbletonMCP_Remote_Script``, and the MCP server is started from
``current`` (``uv run --directory ~/.dunk-ableton-mcp/current ableton-mcp``).
A switch therefore only moves the ``current`` link.

Examples:

    python install.py --ref main            # upstream state (Remote Script 1.9.x)
    python install.py --ref v2.0            # branch
    python install.py --ref pr/3            # pull request #3
    python install.py --ref v2.0 --dry-run  # plan only
    python install.py --list
    python install.py --switch main         # installed version, no fetch
    python install.py --rollback
    python install.py --status
    python install.py --configure-claude-desktop

Only the Python standard library and git are needed (uv for the server).
Works with Python 3.8+ on macOS and Windows.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional, Tuple

DEFAULT_REPO = "https://github.com/dungeonkueper/dunk-ableton-mcp.git"
SCRIPT_FOLDER = "AbletonMCP"  # folder name under "Remote Scripts"
SCRIPT_SOURCE = "AbletonMCP_Remote_Script"  # folder inside the repo
MANAGED_MARKER = ".dunk-ableton-mcp"  # marks a Remote Script copy we own
SERVER_NAME = "AbletonMCP"  # key in the MCP client config
ABLETON_PORT = int(os.environ.get("ABLETON_PORT", "9877"))
IS_WINDOWS = sys.platform == "win32"


class InstallError(Exception):
    pass


# --------------------------------------------------------------------------
# small helpers


def home_dir() -> Path:
    return Path(os.environ.get("DUNK_MCP_HOME", "~/.dunk-ableton-mcp")).expanduser()


def now_iso() -> str:
    return _dt.datetime.now().replace(microsecond=0).isoformat()


def short(commit: Optional[str]) -> str:
    return (commit or "?")[:7]


def slug_for(ref: str) -> str:
    """Folder name for a ref: 'pr/3' -> 'pr-3', 'feature/x' -> 'feature-x'."""
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", ref.strip()).strip("-.")
    if not s:
        raise InstallError(f"Cannot use {ref!r} as a version name")
    return s


def run_git(args: List[str], cwd: Optional[Path] = None, check: bool = True) -> str:
    try:
        proc = subprocess.run(
            ["git"] + args,
            cwd=str(cwd) if cwd else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
        )
    except FileNotFoundError:
        raise InstallError("git not found. Install git first (https://git-scm.com).")
    if check and proc.returncode != 0:
        raise InstallError(f"git {' '.join(args)} failed:\n{proc.stderr.strip()}")
    return proc.stdout.strip()


def ask(question: str) -> bool:
    if not sys.stdin.isatty():
        print(f"{question} -> no interactive terminal; re-run with --yes to confirm.")
        return False
    try:
        answer = input(f"{question} [y/N] ").strip().lower()
    except EOFError:
        return False
    return answer in {"y", "yes", "j", "ja"}


def display(p: Path) -> str:
    """Show paths under the home folder as ~/..."""
    try:
        return "~" + os.sep + str(p.relative_to(Path.home()))
    except ValueError:
        return str(p)


# --------------------------------------------------------------------------
# links: symlink on macOS, symlink or junction on Windows


def is_link(p: Path) -> bool:
    if os.path.islink(str(p)):
        return True
    isjunction = getattr(os.path, "isjunction", None)  # Python 3.12+
    if isjunction is not None:
        return bool(isjunction(str(p)))
    if IS_WINDOWS and p.exists():
        try:
            os.readlink(str(p))  # also reads junctions on Windows (3.8+)
            return True
        except (OSError, ValueError):
            return False
    return False


def link_target(p: Path) -> Optional[Path]:
    try:
        raw = os.readlink(str(p))
    except (OSError, ValueError):
        return None
    if raw.startswith("\\\\?\\"):
        raw = raw[4:]
    target = Path(raw)
    if not target.is_absolute():
        target = p.parent / target
    return target


def remove_link(p: Path) -> None:
    try:
        os.unlink(str(p))
    except (IsADirectoryError, PermissionError, OSError):
        os.rmdir(str(p))  # directory symlinks and junctions on Windows


def make_dir_link(link: Path, target: Path) -> str:
    """Create a directory link; returns 'symlink' or 'junction'."""
    try:
        os.symlink(str(target), str(link), target_is_directory=True)
        return "symlink"
    except (OSError, NotImplementedError) as e:
        if not IS_WINDOWS:
            raise InstallError(f"Cannot create link {link} -> {target}: {e}")
    # Windows without Developer Mode/admin: a junction needs no extra rights.
    proc = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
    )
    if proc.returncode != 0:
        raise InstallError(
            f"Cannot create symlink or junction {link} -> {target}: "
            f"{(proc.stderr or proc.stdout).strip()}"
        )
    return "junction"


def point_link(link: Path, target: Path) -> str:
    """Point `link` at `target`, replacing an existing link (never a real dir)."""
    if is_link(link):
        if not IS_WINDOWS:
            tmp = link.with_name(link.name + ".tmp")
            if is_link(tmp) or tmp.exists():
                remove_link(tmp)
            os.symlink(str(target), str(tmp), target_is_directory=True)
            os.replace(str(tmp), str(link))  # atomic on POSIX
            return "symlink"
        remove_link(link)
    elif link.exists():
        raise InstallError(f"{link} exists and is not a link; refusing to replace it")
    return make_dir_link(link, target)


# --------------------------------------------------------------------------
# state


class State:
    def __init__(self, home: Path):
        self.home = home
        self.path = home / "state.json"
        self.data: Dict = {"versions": {}}
        if self.path.is_file():
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
            except ValueError as e:
                raise InstallError(f"{self.path} is not valid JSON ({e}); fix or delete it")
        self.data.setdefault("versions", {})

    @property
    def versions(self) -> Dict[str, Dict]:
        return self.data["versions"]

    @property
    def current(self) -> Optional[str]:
        return self.data.get("current")

    @property
    def previous(self) -> Optional[str]:
        return self.data.get("previous")

    def save(self) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(str(tmp), str(self.path))


# --------------------------------------------------------------------------
# Ableton User Library discovery (same rules as MCP_Server/remote_script_install.py)


def _live_pref_dirs() -> List[Path]:
    home = Path.home()
    if sys.platform == "darwin":
        bases = [home / "Library" / "Preferences" / "Ableton"]
    elif IS_WINDOWS:
        bases = [Path(os.environ.get("APPDATA", str(home / "AppData" / "Roaming"))) / "Ableton"]
    else:
        bases = [home / ".config" / "ableton", home / ".local" / "share" / "Ableton"]
    dirs: List[Path] = []
    for base in bases:
        if base.is_dir():
            dirs.extend(d for d in base.glob("Live *") if d.is_dir())

    def key(d: Path) -> List[int]:
        return [int(n) for n in re.findall(r"\d+", d.name)]

    return sorted(dirs, key=key, reverse=True)


def _library_from_cfg(cfg: Path) -> Optional[Path]:
    try:
        import xml.etree.ElementTree as ET

        root = ET.parse(str(cfg)).getroot()
    except Exception:
        return None
    for elem in root.iter():
        if "userlibrary" not in elem.tag.lower():
            continue
        project_path = project_name = None
        for sub in elem.iter():
            value = sub.attrib.get("Value")
            if value is None:
                continue
            if "projectpath" in sub.tag.lower():
                project_path = value
            elif "projectname" in sub.tag.lower():
                project_name = value
        if project_path and project_name:
            candidate = Path(project_path).expanduser() / project_name
            if candidate.is_dir():
                return candidate
    return None


def discover_remote_scripts_dirs() -> List[Path]:
    libraries: List[Path] = []
    for pref in _live_pref_dirs():
        for cfg in (pref / "Library.cfg", pref / "Preferences" / "Library.cfg"):
            if cfg.is_file():
                lib = _library_from_cfg(cfg)
                if lib is not None:
                    libraries.append(lib)
    home = Path.home()
    for lib in (home / "Music" / "Ableton" / "User Library",
                home / "Documents" / "Ableton" / "User Library"):
        if lib.is_dir():
            libraries.append(lib)
    seen, out = set(), []
    for lib in libraries:
        target = lib / "Remote Scripts"
        if str(target) not in seen:
            seen.add(str(target))
            out.append(target)
    return out


# --------------------------------------------------------------------------
# version metadata read from a checkout or a cached commit


def _match(pattern: str, text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    m = re.search(pattern, text, re.M)
    return m.group(1) if m else None


def versions_from_texts(script: Optional[str], installer: Optional[str], pyproject: Optional[str]) -> Dict:
    return {
        "script_version": _match(r'^SCRIPT_VERSION\s*=\s*["\']([^"\']+)', script),
        "server_expects": _match(r'^EXPECTED_REMOTE_SCRIPT_VERSION\s*=\s*["\']([^"\']+)', installer),
        "package_version": _match(r'^version\s*=\s*["\']([^"\']+)', pyproject),
    }


FILES = (
    f"{SCRIPT_SOURCE}/__init__.py",
    "MCP_Server/remote_script_install.py",
    "pyproject.toml",
)


def versions_in_dir(d: Path) -> Dict:
    texts = []
    for rel in FILES:
        f = d / rel
        texts.append(f.read_text(encoding="utf-8", errors="replace") if f.is_file() else None)
    return versions_from_texts(*texts)


def versions_in_commit(cache: Path, commit: str) -> Dict:
    texts = [run_git(["show", f"{commit}:{rel}"], cwd=cache, check=False) or None for rel in FILES]
    return versions_from_texts(*texts)


def describe(v: Dict) -> str:
    return (
        f"package {v.get('package_version') or '?'}, "
        f"Remote Script {v.get('script_version') or '?'}, "
        f"server expects {v.get('server_expects') or '?'}"
    )


# --------------------------------------------------------------------------
# resolving and fetching refs


def resolve_remote(repo: str, ref: str) -> Tuple[str, str]:
    """Return (remote ref name or sha, commit) using `git ls-remote` only."""
    m = re.fullmatch(r"(?:pr[/-]|#)(\d+)", ref.strip(), re.I)
    if m:
        name = f"refs/pull/{m.group(1)}/head"
        out = run_git(["ls-remote", repo, name])
        if not out:
            raise InstallError(f"Pull request #{m.group(1)} not found on {repo}")
        return name, out.split()[0]

    out = run_git(["ls-remote", repo, f"refs/heads/{ref}", f"refs/tags/{ref}", f"refs/tags/{ref}^{{}}"])
    found = {}
    for line in out.splitlines():
        sha, name = line.split("\t", 1)
        found[name] = sha
    if f"refs/tags/{ref}^{{}}" in found:
        return f"refs/tags/{ref}", found[f"refs/tags/{ref}^{{}}"]
    for name in (f"refs/tags/{ref}", f"refs/heads/{ref}"):
        if name in found:
            return name, found[name]
    if re.fullmatch(r"[0-9a-fA-F]{7,40}", ref):
        return ref, ref  # a commit sha; fetched and verified later
    raise InstallError(f"No branch, tag or PR named {ref!r} on {repo}")


PR_REF_RE = re.compile(r"refs/pull/(\d+)/head")


def github_slug(repo: str) -> Optional[Tuple[str, str]]:
    """('owner', 'name') for a github.com URL, else None."""
    m = re.fullmatch(r"(?:https://|git@|ssh://git@)github\.com[/:]([\w.-]+)/([\w.-]+?)(?:\.git)?/?", repo.strip())
    return (m.group(1), m.group(2)) if m else None


def pr_origin(repo: str, number: int, timeout: float = 5.0) -> Optional[Dict]:
    """Who opened a pull request and from which repository (GitHub API, best effort).

    Returns None when the repo is not on github.com or the API cannot be
    reached; the caller then warns without the details.
    """
    slug = github_slug(repo)
    if slug is None:
        return None
    url = f"https://api.github.com/repos/{slug[0]}/{slug[1]}/pulls/{number}"
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json",
                                               "User-Agent": "dunk-ableton-mcp-installer"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None
    head_repo = ((data.get("head") or {}).get("repo") or {}).get("full_name")
    return {
        "author": (data.get("user") or {}).get("login"),
        "head_repo": head_repo,  # None when the fork was deleted
        "base_repo": f"{slug[0]}/{slug[1]}",
        "state": data.get("state"),
        "merged": bool(data.get("merged_at")),
    }


def pr_warning(repo: str, remote_ref: str) -> List[str]:
    """Plan lines that warn before code from a pull request is installed."""
    m = PR_REF_RE.fullmatch(remote_ref)
    if not m:
        return []
    number = int(m.group(1))
    lines = [
        f"!! Pull request #{number}: installing it runs the PR's code on this computer:",
        "   uv sync (build step), the MCP server, and the Remote Script inside Live.",
        "   Anyone can open a pull request on a public repository.",
    ]
    info = pr_origin(repo, number)
    if info is None:
        lines.append("   Author unknown (GitHub not reachable). Check the PR on GitHub before you confirm.")
        return lines
    who = f"@{info['author']}" if info.get("author") else "an unknown account"
    head = info.get("head_repo")
    if head is None:
        src = "a fork that no longer exists"
    elif head.lower() == info["base_repo"].lower():
        src = f"a branch of {head}"
    else:
        src = f"the FORK {head} (not {info['base_repo']})"
    state = "merged" if info.get("merged") else (info.get("state") or "unknown state")
    lines.append(f"   Opened by {who} from {src}; {state}.")
    lines.append("   Only confirm if you trust this author and have read the changes.")
    return lines


def ensure_cache(home: Path, repo: str) -> Path:
    cache = home / "repo.git"
    if not (cache / "HEAD").is_file():
        cache.mkdir(parents=True, exist_ok=True)
        run_git(["init", "--bare", "--quiet"], cwd=cache)
        run_git(["remote", "add", "origin", repo], cwd=cache)
    elif run_git(["remote", "get-url", "origin"], cwd=cache, check=False) != repo:
        run_git(["remote", "set-url", "origin", repo], cwd=cache)
    return cache


def fetch(cache: Path, remote_ref: str, slug: str) -> str:
    local = f"refs/dunk/{slug}"
    run_git(["fetch", "--quiet", "--force", "origin", f"+{remote_ref}:{local}"], cwd=cache)
    return run_git(["rev-parse", f"{local}^{{commit}}"], cwd=cache)


def worktree_commit(d: Path) -> Optional[str]:
    if not d.is_dir():
        return None
    return run_git(["rev-parse", "HEAD"], cwd=d, check=False) or None


def worktree_dirty(d: Path) -> bool:
    # uv.lock is ignored: `uv sync`/`uv run` may rewrite it; it is reset on update.
    return bool(run_git(["status", "--porcelain", "--untracked-files=no", "--", ".", ":(exclude)uv.lock"],
                        cwd=d, check=False))


# --------------------------------------------------------------------------
# Live connection (for --status and the "Live is running" hint)


def query_live(timeout: float = 1.5) -> Optional[Dict]:
    """Ask the running Remote Script for its version; None if Live is not reachable."""
    try:
        with socket.create_connection(("127.0.0.1", ABLETON_PORT), timeout=timeout) as s:
            s.settimeout(timeout)
            s.sendall(json.dumps({"type": "get_script_info", "params": {}}).encode("utf-8"))
            buf = b""
            while True:
                chunk = s.recv(65536)
                if not chunk:
                    return {"status": "closed without answer"}
                buf += chunk
                try:
                    reply = json.loads(buf.decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    continue
                if isinstance(reply, dict) and reply.get("status") == "success":
                    return reply.get("result") or {}
                return reply if isinstance(reply, dict) else {"reply": reply}
    except (OSError, socket.timeout):
        return None


# --------------------------------------------------------------------------
# installer


class Installer:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.home = home_dir()
        self.state = State(self.home)
        self.repo = args.repo or self.state.data.get("repo") or DEFAULT_REPO
        self.versions_dir = self.home / "versions"
        self.current_link = self.home / "current"
        self.cache = self.home / "repo.git"

    # -- where the Remote Script goes ----------------------------------------

    def remote_scripts_dir(self) -> Tuple[Optional[Path], List[Path]]:
        if self.args.remote_scripts:
            return Path(self.args.remote_scripts).expanduser(), []
        saved = self.state.data.get("remote_scripts_dir")
        if saved:
            return Path(saved), []
        found = discover_remote_scripts_dirs()
        return (found[0] if found else None), found[1:]

    def script_entry_state(self, rs_dir: Path) -> Tuple[str, Optional[Path]]:
        """What sits at <Remote Scripts>/AbletonMCP right now."""
        entry = rs_dir / SCRIPT_FOLDER
        if is_link(entry):
            return "link", link_target(entry)
        if entry.is_dir() and (entry / MANAGED_MARKER).is_file():
            return "managed-copy", None
        if entry.exists():
            return "foreign", None
        return "missing", None

    # -- plan + apply --------------------------------------------------------

    def install(self, ref: Optional[str], switch_only: Optional[str]) -> int:
        st = self.state
        if switch_only:
            slug = switch_only if switch_only in st.versions else slug_for(switch_only)
            if slug not in st.versions or not (self.versions_dir / slug).is_dir():
                raise InstallError(f"{switch_only!r} is not installed. Installed: {', '.join(st.versions) or 'none'}")
            info = st.versions[slug]
            ref, remote_ref, commit = info["ref"], info["remote_ref"], info["commit"]
        else:
            assert ref
            slug = slug_for(ref)
            print(f"Looking up {ref!r} on {self.repo} ...")
            remote_ref, commit = resolve_remote(self.repo, ref)

        target_dir = self.versions_dir / slug
        have_dir = target_dir.is_dir()
        old_commit = worktree_commit(target_dir) if have_dir else None
        if have_dir and not switch_only and worktree_dirty(target_dir):
            raise InstallError(
                f"{target_dir} has local changes. Commit/stash them there or use --switch {slug} "
                "to activate it without updating."
            )

        # fetch first (cache only) so the plan can show real version numbers
        if not switch_only and not self.args.dry_run:
            print("Fetching into the local cache (nothing is installed yet) ...")
            ensure_cache(self.home, self.repo)
            commit = fetch(self.cache, remote_ref, slug)

        new_versions: Dict = {}
        if have_dir and old_commit == commit:
            new_versions = versions_in_dir(target_dir)
        elif (self.cache / "HEAD").is_file() and len(commit) == 40:
            try:
                new_versions = versions_in_commit(self.cache, commit)
            except InstallError:
                new_versions = {}

        rs_dir, other_rs = self.remote_scripts_dir()
        copy_mode = self.args.copy or (self.state.data.get("script_mode") == "copy" and not self.args.link)
        uv = shutil.which("uv")

        # ---------------- plan ----------------
        cur = st.current
        print()
        print("dunk-ableton-mcp: plan")
        print("-" * 60)
        if cur and cur in st.versions:
            cinfo = st.versions[cur]
            print(f"  Active now : {cur} @ {short(cinfo.get('commit'))} ({describe(versions_in_dir(self.versions_dir / cur))})")
        else:
            print("  Active now : nothing managed by this installer yet")
        print(f"  Next       : {slug} = {ref} @ {short(commit)}"
              + (f" ({describe(new_versions)})" if any(new_versions.values()) else ""))
        print()
        steps: List[str] = []
        if not switch_only:
            if not have_dir:
                steps.append(f"Create {display(target_dir)} (git worktree @ {short(commit)})")
            elif old_commit != commit:
                steps.append(f"Update {display(target_dir)}: {short(old_commit)} -> {short(commit)}")
            else:
                steps.append(f"{display(target_dir)} is already at {short(commit)} (no change)")
        if uv and not self.args.no_sync:
            steps.append(f"uv sync in {display(target_dir)} (server dependencies; aborts before switching if it fails)")
        elif not uv:
            steps.append("SKIP uv sync: uv not found. The server needs uv: https://docs.astral.sh/uv/")
        if cur == slug:
            steps.append(f"{display(self.current_link)} already points to {slug} (no change)")
        else:
            steps.append(f"Point {display(self.current_link)} -> versions/{slug}")

        if rs_dir is None:
            steps.append("Remote Script: NO Ableton User Library found. Pass --remote-scripts \"<User Library>/Remote Scripts\".")
        else:
            entry = rs_dir / SCRIPT_FOLDER
            kind, tgt = self.script_entry_state(rs_dir)
            script_src = self.current_link / SCRIPT_SOURCE
            if copy_mode:
                if kind == "foreign" or kind == "link":
                    steps.append(f"Move existing {entry} to {display(self.home / 'backups')}/")
                steps.append(f"Copy Remote Script of {slug} into {entry}  (copy mode)")
            elif kind == "link" and tgt is not None and os.path.normcase(str(tgt)) == os.path.normcase(str(script_src)):
                steps.append(f"Remote Script link {entry} -> {display(script_src)} is already in place (no change)")
            else:
                if kind in ("foreign", "managed-copy"):
                    steps.append(f"Move existing folder {entry} to {display(self.home / 'backups')}/")
                elif kind == "link":
                    steps.append(f"Replace link {entry} (was -> {tgt})")
                steps.append(f"Link {display(entry)} -> {display(script_src)}")
            if other_rs:
                steps.append("Other User Libraries found (not touched): " + ", ".join(str(p) for p in other_rs))
        self_copy = self.home / "install.py"
        refresh_self = not self_copy.is_file() or self_copy.read_bytes() != Path(__file__).read_bytes()
        if refresh_self:
            steps.append(f"Copy this installer to {display(self_copy)} (so it is always at hand)")

        for i, s in enumerate(steps, 1):
            print(f"  {i}. {s}")
        if not switch_only:
            warning = pr_warning(self.repo, remote_ref)
            if warning:
                print()
                for line in warning:
                    print(f"  {line}")
        live = query_live(timeout=0.5)
        if live is not None:
            print()
            print(f"  Note: Live is running with AbletonMCP {live.get('script_version', '?') if isinstance(live, dict) else '?'} loaded.")
            print("        Restart Live after switching so the new Remote Script is loaded.")
        print("-" * 60)

        if self.args.dry_run:
            print("Dry run: nothing was changed.")
            return 0
        if rs_dir is None:
            raise InstallError("No Remote Scripts folder; pass --remote-scripts and run again.")
        if not self.args.yes and not ask("Proceed?"):
            print("Aborted, nothing was changed.")
            return 1

        # ---------------- apply ----------------
        self.home.mkdir(parents=True, exist_ok=True)
        self.versions_dir.mkdir(parents=True, exist_ok=True)
        if not switch_only:
            if not have_dir:
                run_git(["worktree", "prune"], cwd=self.cache, check=False)
                run_git(["worktree", "add", "--detach", "--force", str(target_dir), commit], cwd=self.cache)
            elif old_commit != commit:
                run_git(["checkout", "--quiet", "--", "uv.lock"], cwd=target_dir, check=False)
                run_git(["checkout", "--quiet", "--detach", commit], cwd=target_dir)
        if not (target_dir / SCRIPT_SOURCE / "__init__.py").is_file():
            raise InstallError(f"{ref} has no {SCRIPT_SOURCE}/__init__.py; not switching.")
        if uv and not self.args.no_sync:
            print("Running uv sync ...")
            cmd = [uv, "sync", "--quiet"]
            if (target_dir / "uv.lock").is_file():
                cmd.append("--frozen")  # install exactly the committed lock, never rewrite it
            proc = subprocess.run(cmd, cwd=str(target_dir))
            if proc.returncode != 0:
                raise InstallError(f"uv sync failed in {target_dir}; still on {cur or 'nothing'}.")

        prev = cur
        point_link(self.current_link, target_dir)
        mode = self._place_remote_script(rs_dir, slug, copy_mode)

        entry = st.versions.get(slug, {})
        entry.update({"ref": ref, "remote_ref": remote_ref, "commit": commit})
        entry.setdefault("installed_at", now_iso())
        entry["activated_at"] = now_iso()
        st.versions[slug] = entry
        if prev and prev != slug:
            st.data["previous"] = prev
        st.data.update({
            "current": slug,
            "repo": self.repo,
            "remote_scripts_dir": str(rs_dir),
            "script_mode": mode,
        })
        st.save()
        if refresh_self:
            shutil.copy2(__file__, str(self_copy))

        v = versions_in_dir(target_dir)
        print()
        print(f"Done. Active: {slug} @ {short(commit)} ({describe(v)})")
        if mode != "symlink":
            print(f"Remote Script placed as: {mode}")
        print()
        print("Next:")
        print("  1. Restart Ableton Live (or re-select AbletonMCP under")
        print("     Settings -> Link, Tempo & MIDI -> Control Surface).")
        print("  2. Restart Claude Desktop / your MCP client so the server restarts.")
        if not self.state.data.get("client_configured"):
            print(f"  3. One-time: let your MCP client start the server from {display(self.current_link)}:")
            print("     python install.py --configure-claude-desktop   (or see --print-config)")
        print("  Check with: python install.py --status")
        return 0

    def _place_remote_script(self, rs_dir: Path, slug: str, copy_mode: bool) -> str:
        rs_dir.mkdir(parents=True, exist_ok=True)
        entry = rs_dir / SCRIPT_FOLDER
        src = self.current_link / SCRIPT_SOURCE
        kind, tgt = self.script_entry_state(rs_dir)

        if not copy_mode:
            if kind == "link" and tgt is not None and os.path.normcase(str(tgt)) == os.path.normcase(str(src)):
                return self.state.data.get("script_mode") or "symlink"
            if kind in ("foreign", "managed-copy", "link"):
                self._backup(entry)  # a foreign link is recorded as a note
            try:
                return point_link(entry, src)
            except (InstallError, OSError) as e:
                print(f"Could not link the Remote Script ({e}); falling back to a copy.")

        # copy mode: refresh a managed copy, back up anything else
        if is_link(entry):
            self._backup(entry)
        elif kind == "foreign":
            self._backup(entry)
        elif entry.exists():
            shutil.rmtree(str(entry))
        shutil.copytree(str(self.versions_dir / slug / SCRIPT_SOURCE), str(entry),
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        (entry / MANAGED_MARKER).write_text(
            json.dumps({"version": slug, "commit": self.state.versions.get(slug, {}).get("commit")}) + "\n",
            encoding="utf-8",
        )
        return "copy"

    def _backup(self, entry: Path) -> None:
        backups = self.home / "backups"
        backups.mkdir(parents=True, exist_ok=True)
        dest = backups / f"{SCRIPT_FOLDER}-{_dt.datetime.now().strftime('%Y%m%d-%H%M%S')}"
        if is_link(entry):
            dest.write_text(f"was a link to {link_target(entry)}\n", encoding="utf-8")
            remove_link(entry)
        else:
            shutil.move(str(entry), str(dest))
        print(f"Moved previous {entry} to {dest}")

    # -- other commands ------------------------------------------------------

    def rollback(self) -> int:
        prev = self.state.previous
        if not prev or prev not in self.state.versions:
            raise InstallError("No previous version recorded.")
        print(f"Rolling back to {prev}.")
        return self.install(None, prev)

    def list(self) -> int:
        st = self.state
        if not st.versions:
            print(f"No versions installed in {display(self.home)} yet. Start with: python install.py --ref main")
            return 0
        print(f"Installed versions in {display(self.versions_dir)}:")
        for slug in sorted(st.versions):
            info = st.versions[slug]
            d = self.versions_dir / slug
            mark = "*" if slug == st.current else ("<" if slug == st.previous else " ")
            v = versions_in_dir(d) if d.is_dir() else {}
            dirty = " [local changes]" if d.is_dir() and worktree_dirty(d) else ""
            missing = " [folder missing]" if not d.is_dir() else ""
            print(f" {mark} {slug:<18} {info.get('ref', '?'):<14} {short(info.get('commit'))}  "
                  f"Remote Script {v.get('script_version') or '?':<7} installed {info.get('installed_at', '?')}{dirty}{missing}")
        print("\n  * active   < previous (--rollback)")
        return 0

    def status(self) -> int:
        st = self.state
        ok = True
        print(f"Home            : {self.home}")
        cur = st.current
        target = link_target(self.current_link) if is_link(self.current_link) else None
        print(f"Active version  : {cur or '-'}" + (f"  (previous: {st.previous})" if st.previous else ""))
        print(f"current link    : {target or 'missing'}")
        if cur and target and os.path.normcase(str(target)) != os.path.normcase(str(self.versions_dir / cur)):
            print("  !! current link does not match state.json")
            ok = False
        installed: Dict = {}
        if target and target.is_dir():
            installed = versions_in_dir(target)
            print(f"Checkout        : {short(worktree_commit(target))}"
                  + (" (local changes)" if worktree_dirty(target) else ""))
            print(f"Installed files : {describe(installed)}")
            if installed.get("script_version") != installed.get("server_expects"):
                print("  !! Remote Script and server in this version disagree")
                ok = False

        rs_dir, _ = self.remote_scripts_dir()
        if rs_dir is None:
            print("Remote Script   : no User Library found")
            ok = False
        else:
            kind, tgt = self.script_entry_state(rs_dir)
            entry = rs_dir / SCRIPT_FOLDER
            if kind == "link":
                good = tgt is not None and os.path.normcase(str(tgt)) == os.path.normcase(str(self.current_link / SCRIPT_SOURCE))
                print(f"Remote Script   : {entry} -> {tgt}" + ("" if good else "  !! not managed by this installer"))
                ok = ok and good
            elif kind == "managed-copy":
                marker = json.loads((entry / MANAGED_MARKER).read_text(encoding="utf-8"))
                good = marker.get("version") == cur
                print(f"Remote Script   : copy of {marker.get('version')} in {entry}"
                      + ("" if good else f"  !! stale, active is {cur}; run --switch {cur}"))
                ok = ok and good
            elif kind == "foreign":
                print(f"Remote Script   : {entry} is a folder not managed by this installer")
                ok = False
            else:
                print(f"Remote Script   : not installed in {rs_dir}")
                ok = False

        live = query_live()
        if live is None:
            print(f"Live            : not reachable on port {ABLETON_PORT} (Live closed or AbletonMCP not selected)")
        elif isinstance(live, dict) and "script_version" in live:
            lv = live.get("script_version")
            print(f"Live reports    : Remote Script {lv}")
            if installed.get("script_version") and lv != installed.get("script_version"):
                print("  !! Live runs a different Remote Script than installed: restart Live")
                ok = False
        else:
            print(f"Live            : answered, but without a version ({live})")

        print()
        print("All consistent." if ok else "Something needs attention (see !! above).")
        return 0 if ok else 1

    def remove(self, name: str) -> int:
        st = self.state
        slug = name if name in st.versions else slug_for(name)
        if slug not in st.versions:
            raise InstallError(f"{name!r} is not installed.")
        if slug == st.current:
            raise InstallError("Cannot remove the active version; switch first.")
        d = self.versions_dir / slug
        print(f"Will delete {d} (git worktree, incl. its .venv).")
        if not self.args.yes and not ask("Proceed?"):
            print("Aborted.")
            return 1
        if d.is_dir():
            run_git(["worktree", "remove", "--force", str(d)], cwd=self.cache, check=False)
            if d.exists():
                shutil.rmtree(str(d))
        run_git(["worktree", "prune"], cwd=self.cache, check=False)
        del st.versions[slug]
        if st.previous == slug:
            st.data.pop("previous", None)
        st.save()
        print(f"Removed {slug}.")
        return 0

    # -- MCP client config ---------------------------------------------------

    def server_entry(self) -> Dict:
        uv = shutil.which("uv") or "uv"
        return {"command": uv, "args": ["run", "--frozen", "--directory", str(self.current_link), "ableton-mcp"]}

    def print_config(self) -> int:
        entry = self.server_entry()
        print("Claude Desktop (claude_desktop_config.json, inside \"mcpServers\"):")
        print(json.dumps({SERVER_NAME: entry}, indent=4))
        print()
        print("Claude Code:")
        print(f"  claude mcp add {SERVER_NAME} -- {entry['command']} " + " ".join(f'"{a}"' if " " in a else a for a in entry["args"]))
        return 0

    def configure_claude_desktop(self) -> int:
        if sys.platform == "darwin":
            cfg = Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
        elif IS_WINDOWS:
            cfg = Path(os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming"))) / "Claude" / "claude_desktop_config.json"
        else:
            cfg = Path.home() / ".config" / "Claude" / "claude_desktop_config.json"
        data: Dict = {}
        if cfg.is_file():
            try:
                data = json.loads(cfg.read_text(encoding="utf-8") or "{}")
            except ValueError as e:
                raise InstallError(f"{cfg} is not valid JSON ({e}); not touching it.")
        servers = data.setdefault("mcpServers", {})
        old = servers.get(SERVER_NAME)
        new = dict(old or {})
        new.update(self.server_entry())  # keeps e.g. "env"
        print(f"Claude Desktop config: {cfg}")
        print(f"  {SERVER_NAME} before: {json.dumps(old) if old else '(none)'}")
        print(f"  {SERVER_NAME} after : {json.dumps(new)}")
        if old == new:
            print("Already configured.")
            return 0
        if self.args.dry_run:
            print("Dry run: nothing was changed.")
            return 0
        if not self.args.yes and not ask("Write this (a .bak copy is kept)?"):
            print("Aborted.")
            return 1
        cfg.parent.mkdir(parents=True, exist_ok=True)
        if cfg.is_file():
            shutil.copy2(str(cfg), str(cfg.with_suffix(".json.bak")))
        servers[SERVER_NAME] = new
        cfg.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        self.state.data["client_configured"] = True
        self.state.save()
        print("Written. Restart Claude Desktop.")
        return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Install and switch dunk-ableton-mcp versions (shows a plan, asks before changing anything).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Examples:\n  python install.py --ref v2.0\n  python install.py --ref pr/3 --dry-run\n"
               "  python install.py --rollback\n  python install.py --status",
    )
    action = p.add_mutually_exclusive_group(required=True)
    action.add_argument("--ref", help="branch, tag, pr/N or commit to install/update and activate")
    action.add_argument("--switch", metavar="NAME", help="activate an installed version without fetching")
    action.add_argument("--rollback", action="store_true", help="go back to the previously active version")
    action.add_argument("--list", action="store_true", help="list installed versions")
    action.add_argument("--status", action="store_true", help="check links, versions and what Live reports")
    action.add_argument("--remove", metavar="NAME", help="delete an installed (inactive) version")
    action.add_argument("--configure-claude-desktop", action="store_true",
                        help="point Claude Desktop's AbletonMCP server at the active version")
    action.add_argument("--print-config", action="store_true", help="print MCP client config snippets")
    p.add_argument("--dry-run", action="store_true", help="show the plan only")
    p.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")
    p.add_argument("--repo", help=f"git URL or path (default: {DEFAULT_REPO})")
    p.add_argument("--remote-scripts", metavar="DIR", help="<User Library>/Remote Scripts folder to use (remembered)")
    p.add_argument("--copy", action="store_true", help="copy the Remote Script instead of linking (remembered)")
    p.add_argument("--link", action="store_true", help="switch back from --copy to linking the Remote Script")
    p.add_argument("--no-sync", action="store_true", help="skip `uv sync`")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        inst = Installer(args)
        if args.ref:
            return inst.install(args.ref, None)
        if args.switch:
            return inst.install(None, args.switch)
        if args.rollback:
            return inst.rollback()
        if args.list:
            return inst.list()
        if args.status:
            return inst.status()
        if args.remove:
            return inst.remove(args.remove)
        if args.configure_claude_desktop:
            return inst.configure_claude_desktop()
        if args.print_config:
            return inst.print_config()
    except InstallError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
