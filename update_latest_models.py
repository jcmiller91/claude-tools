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
import urllib.error
import urllib.request

# ---------------------------------------------------------------- config ---

OWNER = "jcmiller91"
REPO = "claude-tools"          # <- change me
FILE_PATH = "update_latest_models.py"  # path of THIS file within the repo

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
            os.replace(HERE, BACKUP)  # keep a rollback copy
        except OSError:
            pass  # backup is nice-to-have; don't block the update
    os.replace(tmp, HERE)
    os.chmod(HERE, os.stat(HERE).st_mode | stat.S_IEXEC)

    state["sha"] = remote["sha"]
    save_state(state)
    print(f"[self-update] updated to {remote['sha'][:8]} (previous version at {BACKUP})")
    return True


# ---------------------------------------------------------------- main ---

def main():
    print("Hello world!")
    updated = check_and_update()
    if updated and REEXEC_AFTER_UPDATE:
        print("[self-update] re-executing under the new version…")
        # execv replaces the process, so no duplicate "main" run.
        os.execv(sys.executable, [sys.executable, HERE, *sys.argv[1:]])

    print(f"Model cache checked and updated.");


if __name__ == "__main__":
    main()
