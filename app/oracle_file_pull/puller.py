"""
app.oracle_file_pull.puller
==============================
Pulls the aging report and GL Daily Rates files from the Oracle Cloud
file-transfer VM down into this app's LOCAL watch folders
(AGING_WATCH_FOLDER / GL_RATES_WATCH_FOLDER — see db/settings.py), over
the confirmed two-hop SSH jump chain:

    App VM -> ssh {ORACLE_FILE_JUMP_USER}@{ORACLE_FILE_JUMP_HOST}       (DMZ)
           -> ssh {ORACLE_FILE_REMOTE_USER}@{ORACLE_FILE_REMOTE_HOST}   (Oracle Cloud VM)

Both hops are already key-based/passwordless — this opens that exact
chain natively in paramiko (no shelling out to `ssh -J`), per the
confirmed-working reference this was built from.

SCOPE, DELIBERATELY: only the aging report and GL rates files are pulled
right now. The receipt-methods file
(xxzen_ar_receipt_methods_extract.txt, confirmed to exist in the same
remote folder) is NOT pulled yet — that's a separate, not-yet-scoped
task. Adding it later is a two-line change: add its remote filename +
target local folder as a third entry in _build_pull_specs() below; no
other part of this module needs to change.

WHAT THIS DOES NOT DO: it does not parse, upsert, or otherwise touch the
DOWNLOADED files' contents at all. Once a file lands in AGING_WATCH_FOLDER
or GL_RATES_WATCH_FOLDER, the EXISTING watchers (app.aging.watcher /
app.gl_rates.watcher) pick it up exactly as if someone had dropped it
there by hand — this module's only job is "get the bytes from the remote
VM into the right local folder, only when the remote file actually
changed."

USAGE
-----
Run once (e.g. from cron, 4x/day):
    python -m app.oracle_file_pull.puller --once

Run as a long-lived loop (polls every ORACLE_FILE_PULL_INTERVAL_SECONDS):
    python -m app.oracle_file_pull.puller
"""
from __future__ import annotations

import argparse
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path

import paramiko

from ..db.settings import get_settings

logger = logging.getLogger("cashapply.oracle_file_pull")


@dataclass
class PullSpec:
    remote_filename: str
    local_folder: str
    label: str  # human-readable, for logging only


def _build_pull_specs(settings) -> list[PullSpec]:
    """
    What to pull, and where each one lands locally. Deliberately just
    these two right now — see module docstring for how to add the
    receipt-methods file later without touching anything else.
    """
    return [
        PullSpec(
            remote_filename=settings.ORACLE_AGING_REMOTE_FILENAME,
            local_folder=settings.AGING_WATCH_FOLDER,
            label="aging report",
        ),
        PullSpec(
            remote_filename=settings.ORACLE_GL_RATES_REMOTE_FILENAME,
            local_folder=settings.GL_RATES_WATCH_FOLDER,
            label="GL daily rates",
        ),
    ]


# ── SSH jump chain ────────────────────────────────────────────────────────────

class _JumpChain:
    """
    Opens the two-hop SSH chain and hands back a ready-to-use SFTP client
    on the FAR end (the Oracle Cloud VM). Context manager so both legs
    always get closed, in the right order, even on error.

        with _JumpChain(settings) as sftp:
            sftp.stat(remote_path)
            sftp.get(remote_path, local_path)
    """

    def __init__(self, settings):
        self._settings = settings
        self._jump_client: paramiko.SSHClient | None = None
        self._target_client: paramiko.SSHClient | None = None
        self._sftp: paramiko.SFTPClient | None = None

    def __enter__(self) -> paramiko.SFTPClient:
        s = self._settings

        # Hop 1: App VM -> DMZ server. look_for_keys/allow_agent=True mirrors
        # plain `ssh` behavior (uses whatever key/agent already makes the
        # confirmed passwordless `ssh cauatadmin@192.168.7.30` work) --
        # deliberately NOT hardcoding a key file path, since none was given
        # and the whole point is this already works with default SSH auth.
        self._jump_client = paramiko.SSHClient()
        self._jump_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self._jump_client.connect(
            s.ORACLE_FILE_JUMP_HOST, username=s.ORACLE_FILE_JUMP_USER,
            look_for_keys=True, allow_agent=True, timeout=30,
        )

        # Hop 2: tunnel a second SSH connection to the Oracle Cloud VM
        # THROUGH the first hop's transport -- this is the paramiko-native
        # equivalent of `ssh -J`, per the confirmed pattern this was built
        # from. Never shells out to the system `ssh` binary.
        jump_transport = self._jump_client.get_transport()
        channel = jump_transport.open_channel(
            "direct-tcpip",
            dest_addr=(s.ORACLE_FILE_REMOTE_HOST, 22),
            src_addr=(s.ORACLE_FILE_JUMP_HOST, 22),
        )

        self._target_client = paramiko.SSHClient()
        self._target_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self._target_client.connect(
            s.ORACLE_FILE_REMOTE_HOST, username=s.ORACLE_FILE_REMOTE_USER,
            sock=channel, look_for_keys=True, allow_agent=True, timeout=30,
        )

        self._sftp = self._target_client.open_sftp()
        return self._sftp

    def __exit__(self, exc_type, exc, tb):
        # Close in reverse order: SFTP -> target SSH -> jump SSH. Each
        # wrapped individually so a failure closing one hop doesn't skip
        # closing the others (would otherwise leak a connection on the
        # DMZ server every time this runs).
        for closer in (self._sftp, self._target_client, self._jump_client):
            if closer is None:
                continue
            try:
                closer.close()
            except Exception:
                logger.warning("[oracle_file_pull] error closing a connection in the jump chain", exc_info=True)
        return False  # never swallow an exception from inside the `with` block


# ── Local state (last-seen mtime per remote filename) ────────────────────────

def _load_state(state_path: str) -> dict:
    p = Path(state_path)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except Exception:
        logger.warning("[oracle_file_pull] state file '%s' unreadable/corrupt -- starting fresh.", state_path)
        return {}


def _save_state(state_path: str, state: dict) -> None:
    p = Path(state_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, indent=2))


# ── Core pull logic ──────────────────────────────────────────────────────────

def run_once(settings=None) -> dict:
    """
    Connects ONCE, checks every configured file's remote mtime against
    what was last seen, and downloads only the ones that changed.

    Returns a summary dict, e.g.:
        {
          "checked": 2,
          "downloaded": 1,
          "unchanged": 1,
          "errors": [],
          "details": [
            {"label": "aging report", "remote_filename": "...", "action": "downloaded", ...},
            {"label": "GL daily rates", "remote_filename": "...", "action": "unchanged", ...},
          ],
        }
    Never raises for an individual file's problem (missing remote file,
    local folder issue, etc.) -- that file's error is recorded in
    "errors"/its own detail entry and the run continues with the rest.
    A connection-level failure (can't open the jump chain at all) DOES
    raise, since nothing else in this run could possibly succeed either.
    """
    settings = settings or get_settings()
    specs = _build_pull_specs(settings)
    state = _load_state(settings.ORACLE_FILE_PULL_STATE_PATH)

    details: list[dict] = []
    errors: list[str] = []
    downloaded = 0
    unchanged = 0

    with _JumpChain(settings) as sftp:
        for spec in specs:
            remote_path = f"{settings.ORACLE_FILE_REMOTE_PATH.rstrip('/')}/{spec.remote_filename}"
            try:
                remote_stat = sftp.stat(remote_path)
            except FileNotFoundError:
                msg = f"remote file not found: {remote_path}"
                logger.error("[oracle_file_pull] %s (%s)", msg, spec.label)
                errors.append(msg)
                details.append({"label": spec.label, "remote_filename": spec.remote_filename, "action": "error", "error": msg})
                continue

            remote_mtime = remote_stat.st_mtime
            last_seen_mtime = state.get(spec.remote_filename, {}).get("mtime")

            if last_seen_mtime is not None and remote_mtime <= last_seen_mtime:
                logger.info(
                    "[oracle_file_pull] %s ('%s') unchanged since last pull (mtime=%s) -- skipping.",
                    spec.label, spec.remote_filename, remote_mtime,
                )
                unchanged += 1
                details.append({
                    "label": spec.label, "remote_filename": spec.remote_filename,
                    "action": "unchanged", "remote_mtime": remote_mtime,
                })
                continue

            try:
                local_folder = Path(spec.local_folder)
                local_folder.mkdir(parents=True, exist_ok=True)
                local_path = local_folder / spec.remote_filename
                # Download to a temp name first, then atomic rename -- so
                # the local watcher (which polls this same folder
                # independently on its own interval) never sees a
                # partially-written file mid-download.
                tmp_path = local_folder / f".{spec.remote_filename}.downloading"
                sftp.get(remote_path, str(tmp_path))
                tmp_path.replace(local_path)
            except Exception as exc:
                msg = f"download failed for {spec.remote_filename}: {exc}"
                logger.error("[oracle_file_pull] %s", msg, exc_info=True)
                errors.append(msg)
                details.append({"label": spec.label, "remote_filename": spec.remote_filename, "action": "error", "error": msg})
                continue

            state[spec.remote_filename] = {"mtime": remote_mtime, "last_pulled_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
            downloaded += 1
            logger.info(
                "[oracle_file_pull] %s ('%s') changed (mtime=%s) -- downloaded to %s",
                spec.label, spec.remote_filename, remote_mtime, local_path,
            )
            details.append({
                "label": spec.label, "remote_filename": spec.remote_filename,
                "action": "downloaded", "remote_mtime": remote_mtime, "local_path": str(local_path),
            })

    _save_state(settings.ORACLE_FILE_PULL_STATE_PATH, state)

    return {
        "checked": len(specs),
        "downloaded": downloaded,
        "unchanged": unchanged,
        "errors": errors,
        "details": details,
    }


def run_loop() -> None:
    """Long-lived loop, polling every ORACLE_FILE_PULL_INTERVAL_SECONDS.
    Prefer `--once` from cron if specific clock times are needed instead
    of even spacing -- see module docstring."""
    settings = get_settings()
    interval = int(settings.ORACLE_FILE_PULL_INTERVAL_SECONDS)
    logger.info("[oracle_file_pull] Starting loop, polling every %ds", interval)
    while True:
        try:
            result = run_once(settings)
            logger.info("[oracle_file_pull] Run complete: %s", result)
        except Exception:
            # A connection-level failure (jump chain itself couldn't open)
            # shouldn't kill the whole process -- log it and try again
            # next interval, same resilience as the local watchers.
            logger.error("[oracle_file_pull] Run failed", exc_info=True)
        time.sleep(interval)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="Run a single pull and exit (for cron). Default: loop forever.")
    args = parser.parse_args()

    if args.once:
        result = run_once()
        print(json.dumps(result, indent=2))
    else:
        run_loop()