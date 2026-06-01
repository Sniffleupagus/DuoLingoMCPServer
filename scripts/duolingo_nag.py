"""Cron-driven Duolingo nag.

Calls the local MCP server's check_duolingo_status tool. If the streak
isn't extended yet, sends a Robocop-styled ntfy notification with an
owl icon and a countdown to midnight (America/Los_Angeles).

If the MCP call fails, sends a priority-max "MCP unreachable" ntfy
instead — so the cron run doubles as an uptime check on the server.

Config: ~/.config/duolingo-nag/config (KEY=value, mode 600).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from mcp import ClientSession
from mcp.client.sse import sse_client


CONFIG_PATH = Path.home() / ".config" / "duolingo-nag" / "config"
ERROR_LOG = Path.home() / ".local" / "duolingo-nag" / "last_error.log"
TZ = ZoneInfo("America/Los_Angeles")
OWL_ICON = "https://www.duolingo.com/images/facebook/duo200.png"
CLICK_URL = "https://www.duolingo.com"
MCP_TIMEOUT_SEC = 20


def load_config() -> dict[str, str]:
    cfg: dict[str, str] = {}
    with open(CONFIG_PATH) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            k, _, v = line.partition("=")
            cfg[k.strip()] = v.strip()
    for key in ("MCP_URL", "MCP_API_KEY", "NTFY_URL", "NTFY_TOPIC"):
        if not cfg.get(key):
            sys.exit(f"missing {key} in {CONFIG_PATH}")
    return cfg


def format_countdown(now: datetime) -> str:
    """Robocop-style 'You have X hours Y minutes to comply'."""
    midnight = (now + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    remaining = midnight - now
    total_min = max(0, int(remaining.total_seconds() // 60))
    hours, minutes = divmod(total_min, 60)

    parts = []
    if hours:
        parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
    if minutes or not hours:
        parts.append(f"{minutes} minute{'s' if minutes != 1 else ''}")
    return f"You have {' '.join(parts)} to comply"


def build_message(now: datetime) -> tuple[str, str, str]:
    """Returns (title, body, priority) for the current hour."""
    hour = now.hour
    minute = now.minute
    countdown = format_countdown(now)

    if hour <= 20:
        return ("Duolingo streak check", f"{countdown}.", "default")
    if hour == 21:
        return ("Streak reminder", f"{countdown}. Citizen, attend to your duty.", "high")
    if hour == 22:
        return ("Comply", f"{countdown}. Failure to maintain streak will be logged.", "high")
    # hour == 23 or beyond
    if minute < 30:
        return ("Comply now", f"{countdown}. This is your final warning.", "urgent")
    return ("FINAL WARNING", f"{countdown}. Streak loss imminent.", "urgent")


def post_ntfy(
    cfg: dict[str, str],
    title: str,
    body: str,
    priority: str,
    click: str | None = CLICK_URL,
) -> None:
    headers = {
        "Content-Type": "text/plain; charset=utf-8",
        "X-Title": title,
        "X-Priority": priority,
        "X-Tags": "owl",
        "X-Icon": OWL_ICON,
    }
    if click:
        headers["X-Click"] = click
    url = f"{cfg['NTFY_URL'].rstrip('/')}/{cfg['NTFY_TOPIC']}"
    resp = httpx.post(url, headers=headers, content=body.encode("utf-8"), timeout=10)
    resp.raise_for_status()


async def fetch_status(cfg: dict[str, str]) -> dict:
    headers = {"Authorization": f"Bearer {cfg['MCP_API_KEY']}"}
    sse_url = f"{cfg['MCP_URL'].rstrip('/')}/sse"
    async with sse_client(sse_url, headers=headers) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("check_duolingo_status")
            if result.isError:
                raise RuntimeError(
                    f"tool returned error: {result.content[0].text if result.content else 'no detail'}"
                )
            if not result.content:
                raise RuntimeError("tool returned no content")
            return json.loads(result.content[0].text)


def log_error(message: str) -> None:
    ERROR_LOG.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(TZ).isoformat()
    with open(ERROR_LOG, "a") as f:
        f.write(f"[{ts}] {message}\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="don't POST to ntfy; print what would be sent",
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="force the negative path even if streak is extended (real ntfy POST)",
    )
    args = parser.parse_args()

    cfg = load_config()
    now = datetime.now(TZ)

    try:
        status = asyncio.run(asyncio.wait_for(fetch_status(cfg), MCP_TIMEOUT_SEC))
    except Exception as exc:
        exc_type = type(exc).__name__
        exc_msg = str(exc)[:120] or "(no detail)"
        title = "Duolingo MCP unreachable"
        body = f"check failed: {exc_type}: {exc_msg}"
        print(f"MCP error: {exc_type}: {exc_msg}", file=sys.stderr)
        if args.dry_run:
            print(f"would send (MCP-down): title={title!r} body={body!r} priority=urgent")
            return 1
        try:
            post_ntfy(cfg, title, body, "urgent", click=None)
        except Exception as post_exc:
            log_error(f"MCP down AND ntfy POST failed: {post_exc}\n{traceback.format_exc()}")
            return 1
        return 1

    if status.get("streak_extended_today") and not args.test:
        if args.dry_run:
            print(f"streak extended today, would not send (xp_today={status.get('xp_progress', {}).get('xp_today')})")
        return 0

    title, body, priority = build_message(now)

    if args.dry_run:
        print(f"would send: title={title!r} body={body!r} priority={priority}")
        return 0

    try:
        post_ntfy(cfg, title, body, priority)
    except Exception as exc:
        log_error(f"ntfy POST failed: {exc}\n{traceback.format_exc()}")
        print(f"ntfy POST failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
