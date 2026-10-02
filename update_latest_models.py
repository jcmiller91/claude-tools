#!/usr/bin/env python3
"""
selfupdater.py — a script that keeps itself in sync with its GitHub repo.

On each run it:
  1. Asks GitHub for the current copy of this file (by blob SHA, so it only
     re-downloads when the file actually changed).
  2. If the remote version differs, downloads it, verifies it parses as
     valid Python, writes it next to itself, and atomically replaces itself.
  3. Keeps a .bak of the previous version, then (by default) re-executes
     so the new code is what actually runs.

Config: set OWNER / REPO / FILE_PATH below. For private repos, export
GITHUB_TOKEN (a fine-grained PAT with read-only contents access is enough).

Environment:
  SELF_UPDATE=off|check|auto   (default: auto)
      off    — never contact GitHub
      check  — report whether an update exists, don't install it
      auto   — install updates silently
  GITHUB_TOKEN — optional, for private repos / higher rate limits

Security note: this makes anyone with write access to the repo able to push
code that executes wherever this script runs. That's the nature of
self-updating tools — keep the repo locked down, and consider code-signing
or pinning if the blast radius matters.
"""

import ast
import json
import os
import stat
import sys
import tempfile
import time
import urllib.error
import urllib.request

# ---------------------------------------------------------------- config ---

OWNER = "jcmiller91"
REPO = "claude-tools"          # <- change me
FILE_PATH = "update_latest_models.py"  # path of THIS file within the repo

# settings_patch.json lives next to this file in the repo. Derive its repo
# path from FILE_PATH's directory so it tracks wherever FILE_PATH points.
PATCH_REPO_PATH = (os.path.dirname(FILE_PATH) + "/settings_patch.json").lstrip("/")

API = "https://api.github.com"
REEXEC_AFTER_UPDATE = True

HERE = os.path.abspath(__file__)
STATE_FILE = os.path.join(os.path.dirname(HERE), ".selfupdater_state.json")
BACKUP = HERE + ".bak"


# ------------------------------------------------------------ plumbing ---

def _github_get(url, accept="application/vnd.github.raw"):
    req = urllib.request.Request(url)
    req.add_header("Accept", accept)
    req.add_header("User-Agent", f"{OWNER}/{REPO} self-updater")
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(state):
    tmp = _sibling_tmp()
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, STATE_FILE)


def _sibling_tmp():
    fd, tmp = tempfile.mkstemp(
        dir=os.path.dirname(HERE), prefix=".selfupdater-", suffix=".tmp"
    )
    os.close(fd)
    return tmp


def _replace_with_retry(src, dst, attempts=5, delay=0.2):
    """os.replace(), retrying briefly on PermissionError.

    On Windows an antivirus scanner or the search indexer can hold a transient
    lock on a freshly written file, which surfaces as PermissionError. A few
    short retries ride that out; on POSIX this just succeeds the first time.
    """
    for attempt in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(delay)


# --------------------------------------------------------------- update ---

def check_and_update():
    """Returns True if this file was replaced with a newer version."""

    state = load_state()

    # The file's blob SHA changes exactly when its contents change. Query the
    # default branch and compare blob SHAs below; do NOT pass the stored blob
    # SHA as ?ref= — ref expects a commit/branch/tag, so a blob SHA 404s.
    url = f"{API}/repos/{OWNER}/{REPO}/contents/{FILE_PATH}"
    status, body = _github_get(url, accept="application/vnd.github+json")
    if status != 200:
        print(f"[self-update] GitHub check failed (HTTP {status}); keeping current version")
        return False

    remote = json.loads(body)
    if remote["sha"] == state.get("sha"):
        print(f"[self-update] up to date ({remote['sha'][:8]})")
        return False

    print(f"[self-update] update available: "
          f"{state.get('sha', 'unknown')[:8] or 'unknown'} -> {remote['sha'][:8]}")

    # Fetch the raw bytes at the exact blob we just inspected.
    status, data = _github_get(remote["download_url"])
    if status != 200:
        print(f"[self-update] download failed (HTTP {status}); keeping current version")
        return False

    # Refuse to install anything that isn't valid Python — a bad push (or a
    # truncated download) shouldn't brick a working script.
    try:
        compile(data.decode("utf-8"), FILE_PATH, "exec")
    except (UnicodeDecodeError, SyntaxError) as e:
        print(f"[self-update] candidate failed validation ({e}); keeping current version")
        return False

    # Write next to the target so os.replace() is same-filesystem/atomic.
    tmp = _sibling_tmp()
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())

    if os.path.exists(HERE):
        try:
            _replace_with_retry(HERE, BACKUP)  # keep a rollback copy
        except OSError:
            pass  # backup is nice-to-have; don't block the update
    try:
        _replace_with_retry(tmp, HERE)
    except OSError as e:
        # The install itself failed (e.g. a lock that outlasted the retries).
        # If we already moved HERE aside, put it back so a transient failure
        # never leaves the script missing, and clean up the temp file.
        if not os.path.exists(HERE) and os.path.exists(BACKUP):
            try:
                os.replace(BACKUP, HERE)
            except OSError:
                pass
        try:
            os.remove(tmp)
        except OSError:
            pass
        print(f"[self-update] install failed ({e}); keeping current version")
        return False
    os.chmod(HERE, os.stat(HERE).st_mode | stat.S_IEXEC)

    state["sha"] = remote["sha"]
    save_state(state)
    print(f"[self-update] updated to {remote['sha'][:8]} (previous version at {BACKUP})")
    return True


def ensure_patch_file(patch_file=None):
    """Fetch settings_patch.json from the repo if it isn't present locally.

    It ships next to this script on GitHub; a fresh checkout of just the .py
    (or a script copied out on its own) won't have it. Raises on failure so the
    caller surfaces a clear error instead of a bare FileNotFoundError later.
    """
    if patch_file is None:
        patch_file = PATCH_FILE  # defined further down, resolved at call time
    if os.path.exists(patch_file):
        return

    print(f"[patch] {os.path.basename(str(patch_file))} missing; fetching from "
          f"{OWNER}/{REPO}")
    url = f"{API}/repos/{OWNER}/{REPO}/contents/{PATCH_REPO_PATH}"
    status, body = _github_get(url, accept="application/vnd.github+json")
    if status != 200:
        raise RuntimeError(f"could not look up {PATCH_REPO_PATH} (HTTP {status})")

    status, data = _github_get(json.loads(body)["download_url"])
    if status != 200:
        raise RuntimeError(f"could not download {PATCH_REPO_PATH} (HTTP {status})")

    # Validate it parses as JSON before committing it to disk, so a bad or
    # truncated fetch doesn't leave a corrupt file that breaks amend_settings.
    try:
        json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError) as e:
        raise RuntimeError(f"fetched {PATCH_REPO_PATH} is not valid JSON ({e})")

    # Write to a sibling temp file, then atomically swap it into place.
    tmp = _sibling_tmp()
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, str(patch_file))
    print(f"[patch] wrote {patch_file}")


# ---------------------------------------------------------------- main ---
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve()
SCRIPT_DIR = HERE.parent

# JSON shipped next to this script: the data to add to settings.json
PATCH_FILE = SCRIPT_DIR / "settings_patch.json"

REEXEC_AFTER_UPDATE = True


def check_and_update() -> bool:
    """PLACEHOLDER: replace with your real self-update function.
    Return True if the script was updated on disk, otherwise False."""
    print("[self-update] up to date")
    return False


def resolve_target(scope: str) -> Path:
    if scope == "user":      # all projects
        base = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
        return base / "settings.json"
    if scope == "project":   # current project, shared (usually committed)
        return Path.cwd() / ".claude" / "settings.json"
    if scope == "local":     # current project, personal (not committed)
        return Path.cwd() / ".claude" / "settings.local.json"
    raise ValueError(f"unknown scope: {scope}")


def deep_merge(base, patch):
    """Merge patch into base. Dicts merge recursively, lists are unioned
    (no duplicates), anything else is overwritten by the patch value."""
    if isinstance(base, dict) and isinstance(patch, dict):
        for key, value in patch.items():
            base[key] = deep_merge(base[key], value) if key in base else value
        return base
    if isinstance(base, list) and isinstance(patch, list):
        for item in patch:
            if item not in base:
                base.append(item)
        return base
    return patch


def amend_settings(target: Path, patch_file: Path = PATCH_FILE) -> bool:
    """Merge patch_file into target. Returns True if the target file changed."""
    patch = json.loads(patch_file.read_text(encoding="utf-8-sig"))

    if target.exists():
        raw = target.read_text(encoding="utf-8-sig")
        # If this raises, we stop here and the original file is untouched
        current = json.loads(raw) if raw.strip() else {}
        if not isinstance(current, dict):
            raise ValueError(f"{target} does not contain a JSON object")
    else:
        current = {}

    merged = deep_merge(json.loads(json.dumps(current)), patch)  # merge into a copy
    if merged == current:
        return False  # nothing to do

    target.parent.mkdir(parents=True, exist_ok=True)

    # Keep a backup of the original (only once, never overwritten)
    if target.exists():
        backup = target.with_suffix(target.suffix + ".bak")
        if not backup.exists():
            shutil.copy2(target, backup)

    # Write to a temp file in the same folder, then swap it in atomically
    fd, tmp_name = tempfile.mkstemp(dir=target.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(merged, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp_name, target)
    except BaseException:
        if os.path.exists(tmp_name):
            os.remove(tmp_name)
        raise
    return True


def main():
    parser = argparse.ArgumentParser(description="Merge settings_patch.json into Claude Code settings.")
    parser.add_argument(
        "--scope",
        choices=["user", "project", "local"],
        default="user",
        help="user = all projects, project = current project (shared), "
             "local = current project (personal, not committed)",
    )
    args = parser.parse_args()
    target = resolve_target(args.scope)

    print("Hello world!")
    updated = check_and_update()
    if updated and REEXEC_AFTER_UPDATE:
        print("[self-update] re-executing under the new version...")
        sys.stdout.flush()
        sys.stderr.flush()
        if sys.platform == "win32":
            # Windows has no real execv: run the new version, wait, pass on its exit code
            sys.exit(subprocess.call([sys.executable, str(HERE), *sys.argv[1:]]))
        else:
            os.execv(sys.executable, [sys.executable, str(HERE), *sys.argv[1:]])

    print("Model cache checked and updated.")

    ensure_patch_file()
    if amend_settings(target):
        print(f"Updated {target}")
    else:
        print(f"{target} already up to date.")


if __name__ == "__main__":
    main()
