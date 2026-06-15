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


def _days_back(i: int) -> str:
    return (date.today() - timedelta(days=i)).isoformat()


def _fmt_secs(s):
    if not s:
        return None
    s = int(s)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def _j(obj) -> str:
    return json.dumps(obj, indent=2)


# ── MCP server + tools ──
# DNS-rebinding protection is disabled because the server runs behind a hosting
# proxy (the public Host header is not localhost). Access is still gated by the
# MCP_SECRET access key in AuthMiddleware below.
mcp = FastMCP(
    "Garmin Connect",
    stateless_http=True,
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


# ─────────────────────────── Daily summary / activity ───────────────────────────
@mcp.tool()
def get_today_stats() -> str:
    """Today's full daily summary: steps, calories, stress, body battery, intensity
    minutes, floors, resting HR, and more (raw Garmin daily summary)."""
    data = garth.connectapi(
        f"/usersummary-service/usersummary/daily/{display_name()}?calendarDate={_today()}"
    )
    return _j(data)


@mcp.tool()
def get_activities(limit: int = 10) -> str:
    """Recent Garmin activities (runs, rides, workouts, etc.)."""
    activities = garth.connectapi(
        f"/activitylist-service/activities/search/activities?start=0&limit={limit}"
    )
    out = [
        {
            "name": a.get("activityName"),
            "type": a.get("activityType", {}).get("typeKey"),
            "date": a.get("startTimeLocal"),
            "duration_min": round(a.get("duration", 0) / 60, 1),
            "distance_km": round(a.get("distance", 0) / 1000, 2),
            "calories": a.get("calories"),
            "avg_hr": a.get("averageHR"),
            "max_hr": a.get("maxHR"),
            "elevation_gain_m": a.get("elevationGain"),
            "avg_speed_kmh": round(a.get("averageSpeed", 0) * 3.6, 2) if a.get("averageSpeed") else None,
            "training_effect": a.get("aerobicTrainingEffect"),
        }
        for a in (activities or [])
    ]
    return _j(out)


@mcp.tool()
def get_steps(days: int = 7) -> str:
    """Daily step counts, distance and calories for the past N days."""
    out = []
    for i in range(days):
        d = _days_back(i)
        try:
            s = garth.connectapi(
                f"/usersummary-service/usersummary/daily/{display_name()}?calendarDate={d}"
            )
            out.append({
                "date": d,
                "steps": s.get("totalSteps"),
                "goal": s.get("dailyStepGoal"),
                "calories": s.get("totalKilocalories"),
                "active_calories": s.get("activeKilocalories"),
                "distance_km": round((s.get("totalDistanceMeters") or 0) / 1000, 2),
            })
        except Exception:
            pass
    return _j(out)


# ─────────────────────────── Sleep / HR ───────────────────────────
@mcp.tool()
def get_sleep(days: int = 7) -> str:
    """Sleep duration and stages (deep/REM/light) plus sleep score for past N days."""
    out = []
    for i in range(days):
        d = _days_back(i)
        try:
            sleep = garth.connectapi(
                f"/wellness-service/wellness/dailySleepData/{display_name()}"
                f"?date={d}&nonSleepBufferMinutes=60"
            )
            dto = (sleep or {}).get("dailySleepDTO", {}) or {}
            out.append({
                "date": d,
                "duration_hours": round((dto.get("sleepTimeSeconds") or 0) / 3600, 2),
                "deep_hours": round((dto.get("deepSleepSeconds") or 0) / 3600, 2),
                "rem_hours": round((dto.get("remSleepSeconds") or 0) / 3600, 2),
                "light_hours": round((dto.get("lightSleepSeconds") or 0) / 3600, 2),
                "awake_hours": round((dto.get("awakeSleepSeconds") or 0) / 3600, 2),
                "score": (dto.get("sleepScores", {}) or {}).get("overall", {}).get("value"),
            })
        except Exception:
            pass
    return _j(out)


@mcp.tool()
def get_heart_rate(date_str: str = "") -> str:
    """Heart rate detail for a date (YYYY-MM-DD, defaults to today): resting, min, max,
    and intraday values."""
    d = date_str or _today()
    return _j(garth.connectapi(
        f"/wellness-service/wellness/dailyHeartRate/{display_name()}?date={d}"
    ))


@mcp.tool()
def get_resting_heart_rate(days: int = 7) -> str:
    """Resting heart rate for the past N days."""
    out = []
    for i in range(days):
        d = _days_back(i)
        try:
            s = garth.connectapi(
                f"/usersummary-service/usersummary/daily/{display_name()}?calendarDate={d}"
            )
            out.append({"date": d, "resting_hr": s.get("restingHeartRate"),
                        "min_hr": s.get("minHeartRate"), "max_hr": s.get("maxHeartRate")})
        except Exception:
            pass
    return _j(out)


# ─────────────────────────── Body Battery / Stress ───────────────────────────
@mcp.tool()
def get_body_battery(days: int = 1) -> str:
    """Body Battery for the past N days: charged, drained, highest, lowest, most recent."""
    out = []
    for i in range(days):
        d = _days_back(i)
        try:
            rep = garth.connectapi(
                f"/wellness-service/wellness/bodyBattery/reports/daily?startDate={d}&endDate={d}"
            )
            item = (rep or [{}])[0] if rep else {}
            summ = garth.connectapi(
                f"/usersummary-service/usersummary/daily/{display_name()}?calendarDate={d}"
            )
            out.append({
                "date": d,
                "charged": item.get("charged"),
                "drained": item.get("drained"),
                "highest": summ.get("bodyBatteryHighestValue"),
                "lowest": summ.get("bodyBatteryLowestValue"),
                "most_recent": summ.get("bodyBatteryMostRecentValue"),
            })
        except Exception:
            pass
    return _j(out)


@mcp.tool()
def get_stress(days: int = 7) -> str:
    """Stress levels for the past N days: average and max stress (0-100)."""
    out = []
    for i in range(days):
        d = _days_back(i)
        try:
            s = garth.connectapi(f"/wellness-service/wellness/dailyStress/{d}")
            out.append({
                "date": d,
                "avg_stress": s.get("avgStressLevel"),
                "max_stress": s.get("maxStressLevel"),
            })
        except Exception:
            pass
    return _j(out)


# ─────────────────────────── SpO2 / Respiration / HRV ───────────────────────────
@mcp.tool()
def get_spo2(date_str: str = "") -> str:
    """Blood oxygen (SpO2 / Pulse Ox) for a date: average, lowest, latest, sleep average."""
    d = date_str or _today()
    s = garth.connectapi(f"/wellness-service/wellness/daily/spo2/{d}") or {}
    return _j({
        "date": d,
        "average": s.get("averageSpO2"),
        "lowest": s.get("lowestSpO2"),
        "latest": s.get("latestSpO2"),
        "avg_sleep": s.get("avgSleepSpO2"),
        "last_7_days_avg": s.get("lastSevenDaysAvgSpO2"),
    })


@mcp.tool()
def get_respiration(date_str: str = "") -> str:
    """Breathing rate (respiration) for a date: lowest, highest, waking avg, sleep avg."""
    d = date_str or _today()
    s = garth.connectapi(f"/wellness-service/wellness/daily/respiration/{d}") or {}
    return _j({
        "date": d,
        "lowest": s.get("lowestRespirationValue"),
        "highest": s.get("highestRespirationValue"),
        "avg_waking": s.get("avgWakingRespirationValue"),
        "avg_sleep": s.get("avgSleepRespirationValue"),
    })


@mcp.tool()
def get_hrv(days: int = 1) -> str:
    """Heart Rate Variability (HRV) summary for the past N days: last-night average,
    weekly average, status, and baseline range."""
    out = []
    for i in range(days):
        d = _days_back(i)
        try:
            r = garth.connectapi(f"/hrv-service/hrv/{d}") or {}
            out.append({"date": d, "hrvSummary": r.get("hrvSummary")})
        except Exception:
            pass
    return _j(out)


# ─────────────────────────── Performance metrics ───────────────────────────
@mcp.tool()
def get_vo2max() -> str:
    """Latest VO2 max (running & cycling) and fitness age."""
    end = _today()
    start = _days_back(60)
    data = garth.connectapi(f"/metrics-service/metrics/maxmet/daily/{start}/{end}") or []
    latest_run = latest_cycle = fitness_age = None
    for item in data:
        g = item.get("generic") or {}
        c = item.get("cycling") or {}
        if g.get("vo2MaxValue") is not None:
            latest_run = g.get("vo2MaxValue")
            fitness_age = g.get("fitnessAge")
        if c.get("vo2MaxValue") is not None:
            latest_cycle = c.get("vo2MaxValue")
    return _j({"vo2max_running": latest_run, "vo2max_cycling": latest_cycle,
               "fitness_age": fitness_age})


@mcp.tool()
def get_training_readiness(date_str: str = "") -> str:
    """Training Readiness for a date: overall score, level, and contributing factors
    (sleep, recovery, HRV, stress, load)."""
    d = date_str or _today()
    data = garth.connectapi(f"/metrics-service/metrics/trainingreadiness/{d}") or []
    item = data[0] if data else {}
    return _j({
        "date": d,
        "score": item.get("score"),
        "level": item.get("level"),
        "feedback": item.get("feedbackLong") or item.get("feedbackShort"),
        "sleep_score": item.get("sleepScore"),
        "recovery_time_hours": item.get("recoveryTime"),
        "hrv_weekly_avg": item.get("hrvWeeklyAverage"),
        "acute_load": item.get("acuteLoad"),
    })


@mcp.tool()
def get_training_status() -> str:
    """Current training status, training load balance, and VO2 max (raw)."""
    return _j(garth.connectapi(
        f"/metrics-service/metrics/trainingstatus/aggregated/{_today()}"
    ))


@mcp.tool()
def get_race_predictions() -> str:
    """Predicted race times for 5K, 10K, half marathon, and marathon."""
    dn = display_name()
    r = garth.connectapi(f"/metrics-service/metrics/racepredictions/latest/{dn}") or {}
    return _j({
        "5K": _fmt_secs(r.get("time5K")),
        "10K": _fmt_secs(r.get("time10K")),
        "half_marathon": _fmt_secs(r.get("timeHalfMarathon")),
        "marathon": _fmt_secs(r.get("timeMarathon")),
    })


# ─────────────────────────── Activity minutes / floors / hydration ───────────────────────────
@mcp.tool()
def get_intensity_minutes(days: int = 7) -> str:
    """Moderate and vigorous intensity minutes vs. weekly goal, for the past N days."""
    out = []
    for i in range(days):
        d = _days_back(i)
        try:
            s = garth.connectapi(
                f"/usersummary-service/usersummary/daily/{display_name()}?calendarDate={d}"
            )
            out.append({
                "date": d,
                "moderate": s.get("moderateIntensityMinutes"),
                "vigorous": s.get("vigorousIntensityMinutes"),
                "goal": s.get("intensityMinutesGoal"),
            })
        except Exception:
            pass
    return _j(out)


@mcp.tool()
def get_floors(days: int = 7) -> str:
    """Floors climbed and descended for the past N days."""
    out = []
    for i in range(days):
        d = _days_back(i)
        try:
            s = garth.connectapi(
                f"/usersummary-service/usersummary/daily/{display_name()}?calendarDate={d}"
            )
            out.append({
                "date": d,
                "floors_up": round(s.get("floorsAscended") or 0, 1),
                "floors_down": round(s.get("floorsDescended") or 0, 1),
                "goal": s.get("userFloorsAscendedGoal"),
            })
        except Exception:
            pass
    return _j(out)


@mcp.tool()
def get_hydration(date_str: str = "") -> str:
    """Hydration for a date: intake, goal, daily average, sweat loss (in mL)."""
    d = date_str or _today()
    s = garth.connectapi(f"/usersummary-service/usersummary/hydration/daily/{d}") or {}
    return _j({
        "date": d,
        "intake_ml": s.get("valueInML"),
        "goal_ml": s.get("goalInML"),
        "daily_average_ml": s.get("dailyAverageinML"),
        "sweat_loss_ml": s.get("sweatLossInML"),
    })


# ─────────────────────────── Body composition / records / devices ───────────────────────────
@mcp.tool()
def get_body_composition(days: int = 30) -> str:
    """Weight and body composition over the past N days."""
    return _j(garth.connectapi(
        f"/weight-service/weight/dateRange?startDate={_days_back(days)}&endDate={_today()}"
    ))


@mcp.tool()
def get_personal_records() -> str:
    """Personal records (best times/distances) across activities."""
    return _j(garth.connectapi(f"/personalrecord-service/personalrecord/prs/{display_name()}"))


@mcp.tool()
def get_devices() -> str:
    """Registered Garmin devices on the account."""
    devs = garth.connectapi("/device-service/deviceregistration/devices") or []
    out = [{
        "name": d.get("productDisplayName") or d.get("displayName"),
        "part_number": d.get("partNumber"),
        "serial": d.get("serialNumber"),
        "last_sync": d.get("lastSyncTime"),
    } for d in devs] if isinstance(devs, list) else devs
    return _j(out)


# ─────────────────────────── Generic passthrough (access to EVERYTHING) ───────────────────────────
@mcp.tool()
def garmin_get(path: str) -> str:
    """Call ANY Garmin Connect API endpoint directly (read-only GET) and return the raw
    JSON. Use this for data not covered by the other tools.

    `path` must start with '/'. Use `{displayName}` as a placeholder for the user's
    profile id and it will be substituted automatically.

    Examples:
      /usersummary-service/usersummary/daily/{displayName}?calendarDate=2026-06-14
      /wellness-service/wellness/dailyStress/2026-06-14
      /fitnessage-service/fitnessage/{displayName}
      /metrics-service/metrics/trainingstatus/aggregated/2026-06-14
    """
    if not path.startswith("/"):
        return _j({"error": "path must start with '/'"})
    path = path.replace("{displayName}", display_name())
    try:
        result = garth.connectapi(path)
    except Exception as e:
        return _j({"error": str(e)})
    text = _j(result)
    if len(text) > 60000:
        text = text[:60000] + "\n...[truncated]"
    return text


# ── ASGI app with access-key auth ──
class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
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
