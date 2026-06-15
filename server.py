#!/usr/bin/env python3
"""
Garmin Connect — Remote MCP Server (streamable HTTP).
Add as a custom connector in Claude (works on phone + web + desktop).

Auth: requests must include the access key, either as
  - URL query param:  https://<host>/mcp?key=YOUR_SECRET
  - or header:        Authorization: Bearer YOUR_SECRET

Garmin session is loaded from the GARTH_TOKEN env var (a garth token dump).
"""

import json
import os
from datetime import date, timedelta

import garth
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, PlainTextResponse

# ── Config from environment ──
SECRET = os.environ.get("MCP_SECRET", "")
GARTH_TOKEN = os.environ.get("GARTH_TOKEN", "")

if GARTH_TOKEN:
    garth.client.loads(GARTH_TOKEN)

_display_name = None


def display_name() -> str:
    global _display_name
    if _display_name is None:
        profile = garth.connectapi("/userprofile-service/socialProfile")
        _display_name = profile["displayName"]
    return _display_name


def _today() -> str:
    return date.today().isoformat()


# ── MCP server + tools ──
# DNS-rebinding protection is disabled because the server runs behind a hosting
# proxy (the public Host header is not localhost). Access is still gated by the
# MCP_SECRET access key in AuthMiddleware below.
mcp = FastMCP(
    "Garmin Connect",
    stateless_http=True,
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


@mcp.tool()
def get_today_stats() -> str:
    """Get today's activity summary: steps, calories, heart rate, stress, etc."""
    data = garth.connectapi(
        f"/usersummary-service/usersummary/daily/{display_name()}?calendarDate={_today()}"
    )
    return json.dumps(data, indent=2)


@mcp.tool()
def get_activities(limit: int = 10) -> str:
    """Get recent Garmin activities (runs, rides, workouts, etc.)."""
    activities = garth.connectapi(
        f"/activitylist-service/activities/search/activities?start=0&limit={limit}"
    )
    simplified = [
        {
            "name": a.get("activityName"),
            "type": a.get("activityType", {}).get("typeKey"),
            "date": a.get("startTimeLocal"),
            "duration_min": round(a.get("duration", 0) / 60, 1),
            "distance_km": round(a.get("distance", 0) / 1000, 2),
            "calories": a.get("calories"),
            "avg_hr": a.get("averageHR"),
            "max_hr": a.get("maxHR"),
        }
        for a in (activities or [])
    ]
    return json.dumps(simplified, indent=2)


@mcp.tool()
def get_sleep(days: int = 7) -> str:
    """Get sleep data for the past N days."""
    results = []
    for i in range(days):
        d = (date.today() - timedelta(days=i)).isoformat()
        try:
            sleep = garth.connectapi(
                f"/wellness-service/wellness/dailySleepData/{display_name()}"
                f"?date={d}&nonSleepBufferMinutes=60"
            )
            daily = (sleep or {}).get("dailySleepDTO", {}) or {}
            results.append({
                "date": d,
                "duration_hours": round((daily.get("sleepTimeSeconds") or 0) / 3600, 2),
                "deep_hours": round((daily.get("deepSleepSeconds") or 0) / 3600, 2),
                "rem_hours": round((daily.get("remSleepSeconds") or 0) / 3600, 2),
                "light_hours": round((daily.get("lightSleepSeconds") or 0) / 3600, 2),
                "score": (daily.get("sleepScores", {}) or {}).get("overall", {}).get("value"),
            })
        except Exception:
            pass
    return json.dumps(results, indent=2)


@mcp.tool()
def get_steps(days: int = 7) -> str:
    """Get daily step counts and calories for the past N days."""
    results = []
    for i in range(days):
        d = (date.today() - timedelta(days=i)).isoformat()
        try:
            stats = garth.connectapi(
                f"/usersummary-service/usersummary/daily/{display_name()}?calendarDate={d}"
            )
            results.append({
                "date": d,
                "steps": stats.get("totalSteps"),
                "goal": stats.get("dailyStepGoal"),
                "calories": stats.get("totalKilocalories"),
                "active_calories": stats.get("activeKilocalories"),
                "distance_km": round((stats.get("totalDistanceMeters") or 0) / 1000, 2),
            })
        except Exception:
            pass
    return json.dumps(results, indent=2)


@mcp.tool()
def get_heart_rate(date_str: str = "") -> str:
    """Get heart rate data for a given date (YYYY-MM-DD). Defaults to today."""
    d = date_str or _today()
    data = garth.connectapi(
        f"/wellness-service/wellness/dailyHeartRate/{display_name()}?date={d}"
    )
    return json.dumps(data, indent=2)


@mcp.tool()
def get_body_composition(days: int = 30) -> str:
    """Get weight and body composition data for the past N days."""
    end = date.today().isoformat()
    start = (date.today() - timedelta(days=days)).isoformat()
    data = garth.connectapi(
        f"/weight-service/weight/dateRange?startDate={start}&endDate={end}"
    )
    return json.dumps(data, indent=2)


# ── ASGI app with access-key auth ──
class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        # Allow health check at "/" without a key
        if request.url.path == "/":
            return await call_next(request)
        key = request.query_params.get("key")
        auth = request.headers.get("authorization", "")
        bearer = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        if not SECRET or (key != SECRET and bearer != SECRET):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)


app = mcp.streamable_http_app()
app.add_middleware(AuthMiddleware)


async def _health(_request):
    return PlainTextResponse("Garmin MCP server is running.")


app.add_route("/", _health, methods=["GET"])


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
