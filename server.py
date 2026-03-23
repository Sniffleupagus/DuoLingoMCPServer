"""
DuoLingo MCP Server
Exposes Duolingo streak/XP status as MCP tools.
Part of Brian's personal life-tasks gate system.
"""

import secrets
import yaml
from datetime import datetime
from pathlib import Path
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send
from mcp.server.fastmcp import FastMCP

from duolingo_client import DuolingoClient


def load_config() -> dict:
    config_path = Path(__file__).parent / "config.yaml"
    with open(config_path) as f:
        return yaml.safe_load(f)


CONFIG = load_config()
duo_config = CONFIG["duolingo"]
enforcement = CONFIG["enforcement"]
server_config = CONFIG.get("server", {})


class BearerAuthMiddleware:
    """Reject requests without a valid Bearer token."""

    def __init__(self, app: ASGIApp, api_key: str):
        self.app = app
        self.api_key = api_key

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] == "http":
            request = Request(scope)
            auth = request.headers.get("authorization", "")
            if not auth.startswith("Bearer ") or not secrets.compare_digest(
                auth[7:], self.api_key
            ):
                response = JSONResponse(
                    {"error": "unauthorized"}, status_code=401
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


mcp = FastMCP(
    "duolingo-state",
    instructions="Duolingo streak and XP status for life-tasks gating",
)

# Lazy-initialized client
_client: DuolingoClient | None = None


def get_client() -> DuolingoClient:
    global _client
    if _client is None:
        _client = DuolingoClient(
            jwt_token=duo_config["jwt_token"],
            username=duo_config["username"],
        )
    return _client


def get_alert_level() -> int:
    """Calculate alert level based on enforcement window and time.

    Before start_hour: 0 (no enforcement)
    start_hour to end_hour: escalates 1-5 based on interval
    After end_hour until midnight: 5 (maximum urgency — streak at risk)
    """
    now = datetime.now()
    hour = now.hour

    start = enforcement["start_hour"]
    end = enforcement["end_hour"]

    if hour < start:
        return 0

    if hour >= end:
        return 5  # past deadline, maximum urgency

    minutes_past = (hour - start) * 60 + now.minute
    interval = enforcement["escalation_interval_minutes"]
    return min(minutes_past // interval + 1, 5)


@mcp.tool()
def check_duolingo_status() -> dict:
    """Check Brian's current Duolingo status.

    Returns streak info, daily XP progress, whether the streak
    has been extended today, and whether the daily goal is met.
    Includes a checked_at timestamp for caching (skip re-fetch
    if checked_at is less than 10 minutes ago in context).
    """
    client = get_client()
    status = client.get_full_status()
    status["alert_level"] = get_alert_level()
    return status


@mcp.tool()
def check_life_tasks_gate() -> dict:
    """Check if Brian's life tasks are satisfied before allowing work.

    This is the enforcement gate. Returns whether obligations are met
    and the current alert level. Claude should follow the escalation
    ladder based on alert_level:
      - Level 0: no enforcement (outside window or tasks done)
      - Level 1: gentle mention
      - Level 3: refuse to continue mid-response
      - Level 5: maximum escalation

    If all tasks are satisfied, returns near-zero payload.
    """
    client = get_client()
    status = client.get_full_status()
    alert_level = get_alert_level()

    tasks_ok = status["daily_goal_met"]

    if tasks_ok or alert_level == 0:
        return {
            "gate": "open",
            "checked_at": status["checked_at"],
        }

    return {
        "gate": "blocked",
        "alert_level": alert_level,
        "obligations": {
            "duolingo": {
                "done": status["streak_extended_today"],
                "xp_today": status["xp_progress"]["xp_today"],
                "xp_goal": status["xp_progress"]["xp_goal"],
            },
        },
        "checked_at": status["checked_at"],
    }


if __name__ == "__main__":
    api_key = server_config.get("api_key")
    if api_key:
        app = mcp.sse_app()
        app.add_middleware(BearerAuthMiddleware, api_key=api_key)

        import uvicorn
        uvicorn.run(
            app,
            host=server_config.get("host", "0.0.0.0"),
            port=server_config.get("port", 8000),
        )
    else:
        # No auth — local dev only
        mcp.run(transport="sse")
