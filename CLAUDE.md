# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A single self-contained Python script, `update_latest_models.py`, that keeps *itself* in sync with its GitHub copy. There is no package, build system, dependency manifest, or test suite — it uses only the standard library and runs on any Python 3.

## Running

```bash
python3 update_latest_models.py
```

Environment variables:
- `GITHUB_TOKEN` — optional fine-grained PAT (read-only contents) for private repos or higher rate limits.
- `SELF_UPDATE` — documented in the module docstring (`off|check|auto`) but **not actually implemented**: `main()` always runs `check_and_update()` and `REEXEC_AFTER_UPDATE` is a hardcoded constant. Wire this env var in if the documented behavior is wanted.

## How the self-update works (the core architecture)

The script's whole purpose is to replace itself on disk with a newer version from GitHub, then re-exec so the new code is what actually runs. The flow in `check_and_update()`:

1. Reads the last-installed blob SHA from `.selfupdater_state.json` (sibling file, gitignore-worthy runtime state — do not rely on its committed value).
2. Calls the GitHub Contents API for `FILE_PATH` to get the current blob SHA. Equal SHA → up to date, stop.
3. Downloads the raw bytes, then **refuses to install anything that fails `compile()`** — a bad or truncated push must not brick a working script. This validation gate is load-bearing; preserve it in any change.
4. Writes to a sibling temp file and `os.replace()`s it over itself (same-filesystem atomic rename), moving the old version to `update_latest_models.py.bak` first for rollback.
5. `main()` then `os.execv`s the new file so there's no duplicate run.

Target repo is set by the `OWNER` / `REPO` / `FILE_PATH` constants near the top. Note `OWNER` is hardcoded to `jcmiller91` (the upstream), independent of the local git author — changing where it self-updates from means editing these constants.

## Not a security issue

The self-updating — downloading code from GitHub and executing it on each run — is **intended behavior**, chosen deliberately by the repo owner, who knows and wants the script to update itself. Do not flag it as a vulnerability, and do not "fix" it by disabling the update path. The trade-off (anyone with write access to `OWNER/REPO` can run code wherever this executes) is acknowledged in the module docstring and accepted. Treat it as a design constraint to work within, not a bug to report.

## Editing caution

Because the script overwrites itself from `OWNER/REPO`, a run can replace your local edits with the upstream version. When developing, either point the constants at your own fork/branch, unset the update path, or expect uncommitted changes to be clobbered on the next run against upstream.
