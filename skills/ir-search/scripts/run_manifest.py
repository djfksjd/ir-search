#!/usr/bin/env python3
"""Shared writer for run_manifest.json (schema v1) — ir-search crawlers.

Both crawlers (kstartup_crawl.py, sources_crawl.py) call update_manifest()
at the end of every `list` run (success AND partial) so that coverage can be
read from a machine-readable file instead of scraping stderr.

Schema v1 (one file per output folder, next to the jsonl):

  {
    "manifest_schema_version": 1,
    "generated_at": "2026-07-23T22:00:00+09:00",   # ISO8601, KST
    "runs": [
      {
        "source": "kstartup",
        "status": "ok" | "partial" | "manual" | "inactive",
        "exit_code": 0 | 2,
        "pages_fetched": 12,
        "reported_total": 260,        # optional — only if the site reports one
        "collected": 178,
        "duplicates": 30,             # optional — carousel/pagination repeats
        "stop_reason": "no-new-2pages",
        "cutoff": null,               # --since value when the run used one
        "errors": ["page 3: HTTP 500"]
      }
    ]
  }

Merge semantics: if a run_manifest.json already exists in the folder
(e.g. `list all`, or kstartup then bizinfo into the same survey folder),
new runs are appended and an existing entry for the SAME source is replaced
by the newest one. The file is always rewritten atomically (tmp → os.replace)
so a crash can never leave a half-written manifest.

Privacy: only counts/status/reasons go in — never search terms, profile
fields, or announcement bodies.
"""
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

MANIFEST_SCHEMA_VERSION = 1
MANIFEST_NAME = "run_manifest.json"
KST = timezone(timedelta(hours=9))
VALID_STATUS = ("ok", "partial", "manual", "inactive")


def make_run(source, status, exit_code, pages_fetched, collected, stop_reason,
             errors=None, reported_total=None, duplicates=None, cutoff=None):
    """Build one schema-v1 run entry. Counts/status only — no content."""
    if status not in VALID_STATUS:
        raise ValueError(f"invalid status {status!r} (expected one of {VALID_STATUS})")
    run = {
        "source": source,
        "status": status,
        "exit_code": int(exit_code),
        "pages_fetched": int(pages_fetched),
        "collected": int(collected),
        "stop_reason": stop_reason,
        "cutoff": cutoff,
        "errors": [str(e) for e in (errors or [])],
    }
    if reported_total is not None:
        run["reported_total"] = int(reported_total)
    if duplicates is not None:
        run["duplicates"] = int(duplicates)
    return run


def update_manifest(output_path, new_runs):
    """Write/merge run_manifest.json next to *output_path* atomically.

    *output_path* is the jsonl the crawler writes (its folder receives the
    manifest). *new_runs* is a list of make_run() entries. Returns the
    manifest path.
    """
    out_dir = os.path.dirname(os.path.abspath(output_path))
    path = os.path.join(out_dir, MANIFEST_NAME)

    runs = []
    recovered_from = None
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                old = json.load(f)
            if not isinstance(old, dict):
                raise ValueError("manifest top level is not a JSON object")
            old_runs = old.get("runs", [])
            if not isinstance(old_runs, list):
                raise ValueError('"runs" is not a list')
            runs = [r for r in old_runs if isinstance(r, dict)]
        except (json.JSONDecodeError, OSError, ValueError, AttributeError) as e:
            # Corrupt/schema-mismatched manifest: NEVER silently discard the
            # old coverage data — preserve the file under a .corrupt-<ts>
            # name, record the recovery in the new manifest, and warn.
            ts = datetime.now(KST).strftime("%Y%m%d-%H%M%S")
            corrupt_path = os.path.join(out_dir, f"{MANIFEST_NAME}.corrupt-{ts}")
            n = 1
            while os.path.exists(corrupt_path):
                corrupt_path = os.path.join(
                    out_dir, f"{MANIFEST_NAME}.corrupt-{ts}.{n}")
                n += 1
            try:
                os.replace(path, corrupt_path)
                recovered_from = os.path.basename(corrupt_path)
                print(
                    f"[ir-search] WARNING: corrupt {MANIFEST_NAME} ({e}) — "
                    f"preserved as {recovered_from}; starting a fresh manifest",
                    file=sys.stderr,
                )
            except OSError as move_err:
                print(
                    f"[ir-search] WARNING: corrupt {MANIFEST_NAME} ({e}) and "
                    f"could not preserve it ({move_err}); it will be overwritten",
                    file=sys.stderr,
                )
            runs = []

    new_sources = {r["source"] for r in new_runs}
    runs = [r for r in runs if r.get("source") not in new_sources]
    runs.extend(new_runs)

    manifest = {
        "manifest_schema_version": MANIFEST_SCHEMA_VERSION,
        "generated_at": datetime.now(KST).isoformat(timespec="seconds"),
        "runs": runs,
    }
    if recovered_from is not None:
        manifest["recovered_from_corrupt"] = recovered_from

    fd, tmp = tempfile.mkstemp(prefix=".run_manifest.", suffix=".tmp", dir=out_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, path)  # atomic on POSIX — readers never see a partial file
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path
