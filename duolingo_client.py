"""
Direct Duolingo API client using JWT authentication.
No dependency on the abandoned duolingo-api library.
"""

import json
import base64
import httpx
from datetime import datetime, timedelta


class DuolingoClient:
    """Thin client for the unofficial Duolingo API."""

    BASE_URL = "https://www.duolingo.com"
    USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"

    @staticmethod
    def _extract_user_id_from_jwt(token: str) -> str:
        """Pull the user ID (sub claim) from the JWT without verifying signature."""
        payload = token.split(".")[1]
        # Fix base64 padding
        payload += "=" * (4 - len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
        return str(data["sub"])

    def __init__(self, jwt_token: str, username: str | None = None, user_id: str | None = None):
        self.jwt_token = jwt_token
        self.username = username
        self.user_id = user_id or self._extract_user_id_from_jwt(jwt_token)
        self._user_data: dict | None = None
        self._client = httpx.Client(
            headers={
                "Authorization": f"Bearer {jwt_token}",
                "User-Agent": self.USER_AGENT,
            },
            timeout=15.0,
        )

    def _get_user_data(self, force_refresh: bool = False) -> dict:
        """Fetch the main user data blob (cached per instance unless forced)."""
        if self._user_data is not None and not force_refresh:
            return self._user_data

        resp = self._client.get(f"{self.BASE_URL}/users/{self.username}")
        resp.raise_for_status()
        self._user_data = resp.json()
        self.user_id = str(self._user_data.get("id", ""))
        return self._user_data

    def get_raw_user_data(self) -> dict:
        """Dump the full raw user data blob for debugging."""
        return self._get_user_data(force_refresh=True)

    def get_raw_user_data_v2(self, fields: list[str] | None = None) -> dict:
        """Dump the v2 endpoint response. If no fields specified, tries common ones."""
        if fields is None:
            fields = [
                "xpGoal", "xpGains", "streakData",
                "achievements", "currentCourse", "courses",
                "trackingProperties", "globalAmbassadorStatus",
                "weeklyXp", "monthlyXp", "totalXp",
                "practiceReminderSettings", "health",
                "gemsConfig", "lingots", "gems",
                "currentCourseId", "streak",
                "challengeStatus", "questStatus",
                "dailyChallenge", "shopItems",
            ]
        return self._get_user_data_v2(fields)

    def _get_user_data_v2(self, fields: list[str]) -> dict:
        """Fetch from the versioned endpoint with specific fields."""
        if not self.user_id:
            self._get_user_data()

        params = {"fields": ",".join(fields)}
        resp = self._client.get(
            f"{self.BASE_URL}/2017-06-30/users/{self.user_id}",
            params=params,
        )
        resp.raise_for_status()
        return resp.json()

    def get_streak_info(self) -> dict:
        """Get streak status: daily_goal, site_streak, streak_extended_today."""
        data = self._get_user_data()
        return {
            "daily_goal": data.get("daily_goal", 0),
            "site_streak": data.get("site_streak", 0),
            "streak_extended_today": data.get("streak_extended_today", False),
        }

    def get_daily_xp_progress(self) -> dict:
        """Get today's XP progress: xp_goal, xp_today, lessons_today count."""
        data = self._get_user_data_v2(["xpGoal", "xpGains", "streakData"])

        # Only count lessons from after midnight today (local time).
        # The container must have TZ set correctly for this to work.
        midnight = datetime.combine(datetime.today().date(), datetime.min.time())
        cutoff_ts = round(midnight.timestamp())

        xp_gains = data.get("xpGains", [])
        lessons_today = [l for l in xp_gains if l.get("time", 0) >= cutoff_ts]

        return {
            "xp_goal": data.get("xpGoal", 0),
            "xp_today": sum(l.get("xp", 0) for l in lessons_today),
            "lessons_today": len(lessons_today),
        }

    def get_full_status(self) -> dict:
        """Combined status check — one call for the MCP tool."""
        streak = self.get_streak_info()
        xp = self.get_daily_xp_progress()
        return {
            "streak": streak,
            "xp_progress": xp,
            "streak_extended_today": streak["streak_extended_today"],
            "daily_goal_met": xp["xp_today"] >= xp["xp_goal"] if xp["xp_goal"] else streak["streak_extended_today"],
            "checked_at": datetime.now().isoformat(),
        }

    def close(self):
        self._client.close()
