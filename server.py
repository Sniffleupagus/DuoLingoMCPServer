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


# ---------------------------------------------------------------------------
# Docker Swarm Secrets Integration
# ---------------------------------------------------------------------------
# When running under Docker Swarm (`docker stack deploy`), secrets created
# with `docker secret create <name> -` are encrypted in the Swarm Raft log
# and mounted into the container as plain-text files *in a tmpfs* (RAM-only)
# at /run/secrets/<name>.  They never touch disk inside the container.
#
# To adapt this pattern to another project:
#   1. Identify every secret value your app needs.
#   2. Create each one:  echo "value" | docker secret create my_secret -
#   3. Reference them in your compose file under the top-level `secrets:` key
#      and grant access to the service under `services.<svc>.secrets:`.
#   4. In your app, read from /run/secrets/<name> (see read_secret() below).
#   5. Deploy with: docker stack deploy -c docker-compose.swarm.yml <stack>
#
# For local development (plain `docker compose up` or running outside Docker
# entirely), the secrets files won't exist, so we fall back to config.yaml.
# ---------------------------------------------------------------------------

# The directory where Docker Swarm mounts decrypted secrets (in-memory tmpfs).
SECRETS_DIR = Path("/run/secrets")


def read_secret(name: str) -> str | None:
    """Read a single Docker Swarm secret by name.

    Docker Swarm mounts each secret as a file at /run/secrets/<name>.
    The file contains the raw secret value (often with a trailing newline,
    so we strip() it).  Returns None if the file doesn't exist — which
    means we're not running under Swarm and should fall back to config.yaml.

    To use this in another project, just call:
        value = read_secret("my_secret_name")
    The name must match what you passed to `docker secret create`.
    """
    secret_path = SECRETS_DIR / name
    if secret_path.exists():
        return secret_path.read_text().strip()
    return None


def load_config() -> dict:
    """Load configuration, preferring Docker Swarm secrets over config.yaml.

    Resolution order for each secret value:
      1. /run/secrets/<name>  — used when deployed via `docker stack deploy`
         (encrypted at rest in Swarm's Raft log, decrypted into RAM only)
      2. config.yaml          — used for local dev / plain docker-compose

    The non-secret settings (enforcement windows, server host/port) always
    come from config.yaml since they're not sensitive.
    """
    # --- Load the base config file (always needed for non-secret settings) ---
    config_path = Path(__file__).parent / "config.yaml"
    with open(config_path) as f:
        config = yaml.safe_load(f)

    # --- Override secret values with Docker Swarm secrets if available ---
    # Each read_secret() call checks /run/secrets/<name>.
    # If we're running under Swarm, these files exist and take priority.
    # If not (local dev), they return None and we keep the config.yaml values.

    jwt = read_secret("duolingo_jwt")          # docker secret create duolingo_jwt -
    if jwt:
        config["duolingo"]["jwt_token"] = jwt

    username = read_secret("duolingo_username") # docker secret create duolingo_username -
    if username:
        config["duolingo"]["username"] = username

    api_key = read_secret("duolingo_api_key")   # docker secret create duolingo_api_key -
    if api_key:
        config.setdefault("server", {})["api_key"] = api_key

    return config


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
            # Allow SSE GET without auth — EventSource can't send custom headers.
            # Auth is enforced on POST /messages/ where tool calls actually happen.
            path = request.url.path
            method = request.method
            if not (method == "GET" and path == "/sse"):
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
    host="0.0.0.0",
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
    """This tool gets Brian's current Duolingo status.

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
    """This tool tells if Brian's life tasks are satisfied before allowing work.

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


@mcp.tool()
def debug_raw_duolingo_data() -> dict:
    """Do not use unless asked. Dump raw Duolingo API responses for debugging.

    Returns both the v1 user data and v2 endpoint data
    with all available fields. Use this to discover what
    data is available (quests, challenges, etc).
    """
    client = get_client()
    v1_data = client.get_raw_user_data()
    v2_data = client.get_raw_user_data_v2()

    # Return just the top-level keys from v1 (it's huge),
    # and full v2 data
    return {
        "v1_keys": sorted(v1_data.keys()),
        "v1_sample_fields": {
            k: v1_data.get(k)
            for k in [
                "daily_goal", "site_streak", "streak_extended_today",
                "practiceReminderSettings", "achievements",
                "challengeStatus", "questStatus", "dailyChallenge",
            ]
            if v1_data.get(k) is not None
        },
        "v2_data": v2_data,
        "checked_at": datetime.now().isoformat(),
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
            forwarded_allow_ips="*",
        )
    else:
        # No auth — local dev only
        mcp.run(transport="sse")
