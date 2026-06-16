#!/usr/bin/env python3
"""Long-running watchdog for novels12 coordinator with MiniMax quota check."""
from __future__ import annotations

import json
import logging
import subprocess
import sys
import time
from pathlib import Path

try:
    import psutil
except Exception as exc:  # pragma: no cover
    psutil = None
    print(f"WARNING: psutil not available: {exc}", file=sys.stderr)

# Resolve paths relative to this script (repository root).
ROOT = Path(__file__).resolve().parent.parent.parent
PROJECT_DIR = ROOT / "projects" / "novels12"
LOG_DIR = PROJECT_DIR / "logs"
LOG_FILE = LOG_DIR / "auto_restart.log"
COORDINATOR_SCRIPT = ROOT / "scripts" / "pipeline" / "coordinator.py"
COORDINATOR_STDOUT = LOG_DIR / "coordinator_mmx.stdout.log"
COORDINATOR_STDERR = LOG_DIR / "coordinator_mmx.stderr.log"

INTERVAL_SECONDS = 300  # 5 minutes
MIN_INTERVAL_PERCENT = 20
MIN_WEEKLY_PERCENT = 15


def _setup_logging() -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("auto_restart_novels12")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        fmt = logging.Formatter(
            "%(asctime)s [%(levelname)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
        fh.setFormatter(fmt)
        logger.addHandler(fh)
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        logger.addHandler(sh)
    return logger


LOGGER = _setup_logging()


def is_coordinator_running() -> bool:
    """Return True if a python process executing coordinator.py for novels12 exists."""
    if psutil is None:
        LOGGER.warning("psutil unavailable, cannot detect coordinator process")
        return False

    target = "coordinator.py"
    marker = PROJECT_DIR.as_posix().lower()
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            cmdline = proc.info.get("cmdline") or []
            if not cmdline:
                continue
            exe = (cmdline[0] or "").lower()
            if "python" not in exe:
                continue
            cmd_str = (" ".join(str(x) for x in cmdline)).replace("\\", "/").lower()
            if target in cmd_str and marker in cmd_str:
                LOGGER.info(
                    "coordinator process found: pid=%s cmd=%s",
                    proc.pid,
                    cmd_str,
                )
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return False


def get_mmx_quota() -> dict | None:
    """Return the 'general' model quota entry from `mmx quota show`, or None."""
    try:
        result = subprocess.run(
            "mmx quota show",
            shell=True,
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        if result.returncode != 0:
            LOGGER.error(
                "mmx quota show failed: rc=%s stderr=%s",
                result.returncode,
                result.stderr.strip(),
            )
            return None
        data = json.loads(result.stdout)
        for entry in data.get("model_remains", []):
            if entry.get("model_name") == "general":
                return entry
        LOGGER.error("general model quota not found in mmx output")
        return None
    except Exception:
        LOGGER.exception("failed to fetch mmx quota")
        return None


def quota_is_sufficient(quota: dict) -> bool:
    interval = quota.get("current_interval_remaining_percent", 0)
    weekly = quota.get("current_weekly_remaining_percent", 0)
    LOGGER.info("quota general: interval=%s%%, weekly=%s%%", interval, weekly)
    return interval >= MIN_INTERVAL_PERCENT and weekly >= MIN_WEEKLY_PERCENT


def restart_coordinator() -> None:
    cmd = [
        sys.executable,
        str(COORDINATOR_SCRIPT),
        "--project",
        str(PROJECT_DIR),
        "--start",
        "39",
        "--end",
        "500",
        "--skip-planner",
        "--outline-first",
        "--force-outline-book-review",
    ]
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    COORDINATOR_STDOUT.touch(exist_ok=True)
    COORDINATOR_STDERR.touch(exist_ok=True)
    LOGGER.info("restarting coordinator: %s", " ".join(cmd))
    try:
        with open(COORDINATOR_STDOUT, "a", encoding="utf-8") as out, open(
            COORDINATOR_STDERR, "a", encoding="utf-8"
        ) as err:
            kwargs: dict = {}
            if sys.platform == "win32":
                kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            subprocess.Popen(cmd, stdout=out, stderr=err, **kwargs)
        LOGGER.info(
            "coordinator restarted; output appended to %s and %s",
            COORDINATOR_STDOUT,
            COORDINATOR_STDERR,
        )
    except Exception:
        LOGGER.exception("failed to restart coordinator")


def main() -> None:
    LOGGER.info(
        "auto_restart_novels12 watchdog started; checking every %ss",
        INTERVAL_SECONDS,
    )
    try:
        while True:
            LOGGER.info("checking coordinator status...")
            if is_coordinator_running():
                LOGGER.info("coordinator is running; no action needed")
            else:
                LOGGER.info("coordinator is NOT running; checking MiniMax quota")
                quota = get_mmx_quota()
                if quota is None:
                    LOGGER.warning(
                        "quota check failed; will retry in %ss",
                        INTERVAL_SECONDS,
                    )
                elif quota_is_sufficient(quota):
                    restart_coordinator()
                else:
                    LOGGER.warning(
                        "quota insufficient (interval>=20%% and weekly>=15%% required); "
                        "waiting for replenishment"
                    )
            LOGGER.info("sleeping %ss until next check", INTERVAL_SECONDS)
            time.sleep(INTERVAL_SECONDS)
    except KeyboardInterrupt:
        LOGGER.info("watchdog stopped by user (KeyboardInterrupt)")
    except Exception:
        LOGGER.exception("watchdog crashed")
        raise


if __name__ == "__main__":
    main()
