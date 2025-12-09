#!/usr/bin/env python3
"""
training_dashboard.py

Single-file Flask app:
 - Current conditions + 7-day forecast in tabs
 - WBGT estimate, wind chill, heat category (TRADOC-like)
 - Option A uniform recommendations (Army-style cold-weather guidance)
 - PT uniform recommendations (Option A mapping)
 - Precipitation handling (rain/snow/freezing/thunder)
 - Local time using WeatherAPI timezone (falls back to system local)
 - Printable 7-day weekly slide via /weekly (and linked from main page)
"""

from flask import Flask, render_template_string, request, redirect, url_for
import requests
import math
import os
from datetime import datetime
try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None

# -----------------------
# CONFIG
# -----------------------
API_KEY = "6177d93754bf4766adc190645250812"  # replace if needed
DEFAULT_LOCATION = "Brookings, SD"
WBGT_CUTOFF_F = 50
FORECAST_DAYS = 7
WEEKLY_DAYS = 7

app = Flask(__name__)

# -----------------------
# Helpers
# -----------------------
def f_to_c(Tf):
    return (Tf - 32.0) * 5.0 / 9.0

def c_to_f(Tc):
    return Tc * 9.0 / 5.0 + 32.0

def wind_chill_f(Tf, wind_mph):
    if Tf > 50 or wind_mph <= 3:
        return None
    return 35.74 + 0.6215 * Tf - 35.75 * (wind_mph ** 0.16) + 0.4275 * Tf * (wind_mph ** 0.16)

def approx_natural_wet_bulb(Tc, rh):
    rh = max(0.0, min(100.0, rh))
    return (Tc * math.atan(0.151977 * math.sqrt(rh + 8.313659)) +
            math.atan(Tc + rh) - math.atan(rh - 1.676331) +
            0.00391838 * (rh ** 1.5) * math.atan(0.023101 * rh) -
            4.686035)

def approx_wbgt(Tc, rh, sunny=False, globe_offset_c=3.0):
    Tw = approx_natural_wet_bulb(Tc, rh)
    Tg = Tc + globe_offset_c if sunny else Tc
    wbgt_c = 0.7 * Tw + 0.3 * Tg
    return wbgt_c, Tw, Tg

def heat_category_from_wbgt_f(wbgt_f):
    if wbgt_f < 78:
        return "Below White", 1
    if 78 <= wbgt_f <= 81.9:
        return "White (Cat 1)", 1
    if 82 <= wbgt_f <= 84.9:
        return "Green (Cat 2)", 2
    if 85 <= wbgt_f <= 87.9:
        return "Yellow (Cat 3)", 3
    if 88 <= wbgt_f <= 89.9:
        return "Red (Cat 4)", 4
    return "Black (Cat 5)", 5

# Precipitation / condition interpreter
def interpret_condition(cond_text):
    c = (cond_text or "").lower()
    if "thunder" in c or "storm" in c:
        return "extreme", "Thunderstorm / lightning risk", "NO OUTDOOR TRAINING"
    if "freezing rain" in c or ("freezing" in c and "rain" in c):
        return "extreme", "Freezing rain / ice risk", "NO OUTDOOR TRAINING"
    if "sleet" in c or "ice" in c or "icy" in c:
        return "extreme", "Icy conditions", "NO OUTDOOR TRAINING"
    if "blizzard" in c:
        return "extreme", "Blizzard / near-zero visibility", "NO OUTDOOR_TRAINING"
    if "heavy snow" in c:
        return "high", "Heavy snow — visibility & slip risk", None
    if "snow" in c or "flurr" in c:
        return "moderate", "Snow present — traction/visibility caution", None
    if "heavy rain" in c or "torrential" in c:
        return "high", "Heavy rain — hypothermia & slip risk", None
    if "rain" in c or "shower" in c:
        return "moderate", "Rain — wet/hypothermia risk", None
    if "drizzle" in c or "light rain" in c:
        return "low", "Light rain / drizzle", None
    if "fog" in c or "mist" in c:
        return "moderate", "Fog / reduced visibility", None
    return "low", "No precipitation hazards", None

# Option A uniform recommendations
def recommend_uniform_option_a(temp_f, wind_chill_f, heat_cat_num, wbgt_applicable, precip_level="low"):
    if precip_level == "extreme":
        return ("Suspend outdoor training due to dangerous precipitation (lightning/ice).", 3)
    if wbgt_applicable and heat_cat_num is not None:
        if heat_cat_num >= 5:
            return ("Light clothing only; no armor; full hydration and move indoors", 3)
        if heat_cat_num == 4:
            return ("Light OCP/PT, reduce load, hydrate frequently", 2)
        if heat_cat_num == 3:
            return ("OCP, consider modified load and frequent water breaks", 1)
        return ("Standard OCP/PT uniform", 0)
    if wind_chill_f is not None:
        if wind_chill_f <= -20:
            return ("Arctic clothing / extreme cold gear. No exposed skin. No outdoor training.", 3)
        if wind_chill_f <= 0:
            return ("Parka + layered clothing + gloves + balaclava. Move indoors for prolonged training.", 2)
        if wind_chill_f <= 20:
            return ("OCP + parka + gloves + warm layers. Limit prolonged exposed activities.", 2)
        if wind_chill_f <= 32:
            return ("OCP + fleece + gloves recommended.", 1)
    if temp_f <= 50 and temp_f > 33:
        return ("OCP + fleece optional; monitor wind and wetness.", 1)
    return ("Standard OCP/PT uniform", 0)

# -----------------------
# NEW: PT uniform recommendations (Option A mapping)
# -----------------------
def recommend_pt_uniform(temp_f):
    """
    Option A PT uniform mapping (temperature-based):
      > 80°F : short-sleeve shirt + shorts
      60–80°F: short-sleeve shirt + shorts (light jacket optional)
      40–59°F: long-sleeve shirt + pants
      20–39°F: sweats + jacket + hat optional
      < 20°F: full cold-weather PT gear (sweats, gloves, hat)
    Returns a short descriptive string.
    """
    try:
        t = float(temp_f)
    except Exception:
        return "Standard PT uniform"
    if t > 80:
        return "Short-sleeve shirt + shorts"
    if 60 <= t <= 80:
        return "Short-sleeve shirt + shorts (light jacket optional)"
    if 40 <= t <= 59:
        return "Long-sleeve shirt + pants"
    if 20 <= t <= 39:
        return "Sweats + jacket (+ hat optional)"
    return "Full cold-weather PT gear (sweats, gloves, hat)"

# Final decision logic
def final_training_decision(temp_f, wind_chill_f, heat_cat_num, wbgt_applicable, precip_override=None, precip_level="low"):
    if precip_override:
        return precip_override
    if wind_chill_f is not None and wind_chill_f <= -20:
        return "NO OUTDOOR TRAINING — EXTREME COLD"
    if wind_chill_f is not None and wind_chill_f <= 0:
        return "MOVE TRAINING INDOORS / HIGH COLD RISK"
    if wind_chill_f is not None and wind_chill_f <= 20:
        return "LIMIT OUTDOOR TRAINING / USE INDOORS WHEN POSSIBLE (COLD CAUTION)"
    if precip_level == "high":
        return "LIMIT OUTDOOR TRAINING / USE INDOORS WHEN POSSIBLE (PRECIPITATION)"
    if wbgt_applicable and heat_cat_num is not None:
        if heat_cat_num >= 5:
            return "NO OUTDOOR TRAINING — EXTREME HEAT (BLACK FLAG)"
        if heat_cat_num == 4:
            return "LIMIT OUTDOOR TRAINING / USE INDOORS WHEN POSSIBLE (HEAT)"
        if heat_cat_num == 3:
            return "TRAIN OUTDOORS WITH CAUTION (HEAT)"
        return "TRAIN OUTDOORS (NO RESTRICTIONS)"
    return "TRAIN OUTDOORS (NO RESTRICTIONS)"

# -----------------------
# WeatherAPI fetchers
# -----------------------
def fetch_current_weather(location):
    url = "http://api.weatherapi.com/v1/current.json"
    params = {"key": API_KEY, "q": location, "aqi": "no"}
    resp = requests.get(url, params=params, timeout=10)
    resp.raise_for_status()
    return resp.json()

def fetch_forecast(location, days=7):
    url = "http://api.weatherapi.com/v1/forecast.json"
    params = {"key": API_KEY, "q": location, "days": days, "aqi": "no", "alerts": "no"}
    resp = requests.get(url, params=params, timeout=10)
    resp.raise_for_status()
    return resp.json()

# -----------------------
# Templates (combined page + weekly printable)
# -----------------------
PAGE_HTML = r"""
<!doctype html>
<html>
<head><meta charset="utf-8"><title>Training Dashboard</title>
<style>
body{font-family:system-ui; background:#f7fafc; margin:18px; color:#0f172a;}
.tabs{display:flex; gap:10px; margin-bottom:12px;}
.tab{cursor:pointer; padding:8px 12px; border-radius:6px; background:#e2e8f0;}
.tab.active{background:#0ea5a4; color:white;}
.section{display:none;}
.section.active{display:block;}
.card{background:white; padding:14px; border-radius:8px; box-shadow:0 6px 18px rgba(15,23,42,0.06); margin-bottom:12px;}
.green{color:#0f5132; background:#ecfdf5; padding:4px 8px; border-radius:6px;}
.yellow{color:#664d03; background:#fff7ed; padding:4px 8px; border-radius:6px;}
.red{color:#7f1d1d; background:#fee2e2; padding:4px 8px; border-radius:6px;}
.black{color:#111827; background:#e5e7eb; padding:4px 8px; border-radius:6px;}
.btn { display:inline-block; padding:8px 12px; border-radius:6px; background:#0ea5a4; color:white; text-decoration:none; margin-right:8px;}
.small { font-size:0.9rem; color:#475569; }
</style>
</head>
<body>
<h1>Training Dashboard</h1>
<div class="small">Local time: {{ local_time }} • Location: {{ location_name }}</div>

<div style="margin-top:10px;" class="tabs">
  <div class="tab active" onclick="showTab('current')">Current</div>
  <div class="tab" onclick="showTab('forecast')">7-Day Forecast</div>
  <a href="{{ url_for('weekly') }}" class="btn" style="margin-left:auto;">Print Weekly Analysis</a>
</div>

<div id="current" class="section active">
  <div class="card">
    <div><strong>Temp:</strong> {{ temp_f }}°F / {{ temp_c }}°C &nbsp; <strong>RH:</strong> {{ rh }}% &nbsp; <strong>Wind:</strong> {{ wind_mph }} mph &nbsp; <strong>Clouds:</strong> {{ clouds }}%</div>
    <div style="margin-top:6px;"><strong>WBGT (est.):</strong> {{ wbgt_f }}°F ({{ wbgt_c }}°C) • Twb: {{ twb_c }}°C • Tg: {{ tg_c }}°C</div>
    <div style="margin-top:6px;"><strong>Heat Category:</strong> {{ heat_label }}</div>
    <div style="margin-top:6px;"><strong>Condition:</strong> {{ weather_text }} ({{ cond_note }})</div>
    <div style="margin-top:6px;"><strong>Wind Chill:</strong> {{ wc_text }}</div>
    <div style="margin-top:10px;"><strong>Uniform:</strong> <span class="{% if uniform_level==0 %}green{% elif uniform_level==1 %}yellow{% elif uniform_level==2 %}red{% else %}black{% endif %}">{{ uniform }}</span></div>
    <div style="margin-top:6px;"><strong>PT Uniform:</strong> <span class="green">{{ pt_uniform }}</span></div>
    <div style="margin-top:10px;"><strong>Decision:</strong> <span class="{% if 'NO OUTDOOR' in final_decision or 'MOVE' in final_decision %}red{% elif 'LIMIT' in final_decision %}yellow{% else %}green{% endif %}">{{ final_decision }}</span></div>
  </div>
</div>

<div id="forecast" class="section">
  {% for d in forecast %}
  <div class="card">
    <div><strong>{{ d.date }}</strong> • {{ d.condition }} • Precip: {{ d.precip_type }}</div>
    <div style="margin-top:6px;">Temp: {{ d.temp_f }}°F / {{ d.temp_c }}°C • RH: {{ d.rh }}% • Wind: {{ d.wind_mph }} mph</div>
    <div style="margin-top:6px;">WBGT est: {{ d.wbgt_f }}°F ({{ d.wbgt_c }}°C) • Wind Chill: {{ d.wc_text }}</div>
    <div style="margin-top:8px;">Uniform: <span class="{% if d.uniform_level==0 %}green{% elif d.uniform_level==1 %}yellow{% elif d.uniform_level==2 %}red{% else %}black{% endif %}">{{ d.uniform }}</span></div>
    <div style="margin-top:6px;"><strong>PT Uniform:</strong> <span class="green">{{ d.pt_uniform }}</span></div>
    <div style="margin-top:8px;">Decision: <span class="{% if 'NO OUTDOOR' in d.final or 'MOVE' in d.final %}red{% elif 'LIMIT' in d.final %}yellow{% else %}green{% endif %}">{{ d.final }}</span></div>
  </div>
  {% endfor %}
</div>

<script>
function showTab(id){
  document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
  document.querySelectorAll('.section').forEach(s=>s.classList.remove('active'));
  Array.from(document.querySelectorAll('.tab')).find(t=>t.textContent.trim().toLowerCase()===(id==='current'?'current':'5-day forecast'))?.classList.add('active');
  document.getElementById(id).classList.add('active');
}
// ensure first tab active
document.querySelectorAll('.tab')[0].classList.add('active');
</script>
</body>
</html>
"""

WEEKLY_HTML = r"""
<!doctype html>
<html>
<head><meta charset="utf-8"><title>Weekly Training Analysis</title>
<style>
body{font-family:Arial,Helvetica,sans-serif; margin:18px;}
h1{margin-bottom:4px;}
table{width:100%; border-collapse:collapse; margin-top:12px;}
th,td{border:1px solid #ddd; padding:8px; text-align:left;}
th{background:#f1f5f9;}
.btn{display:inline-block; padding:8px 12px; background:#0ea5a4; color:white; border-radius:6px; text-decoration:none;}
.small{font-size:0.9rem; color:#475569;}
</style>
</head>
<body>
<h1>Weekly Training Environment Analysis</h1>
<div class="small">Location: {{ location_name }}</div>
<p><a href="#" onclick="window.print()" class="btn">Print / Save Slide</a> <a href="{{ url_for('dashboard') }}" class="btn" style="background:#64748b;">Back</a></p>

<table>
<tr><th>Date</th><th>Avg Temp (°F)</th><th>RH%</th><th>Wind (mph)</th><th>WBGT (est.)</th><th>Heat Cat</th><th>Wind Chill</th><th>Decision</th><th>Uniform</th><th>PT Uniform</th></tr>
{% for r in rows %}
<tr>
  <td>{{ r.date }}</td>
  <td>{{ r.avg_f }}</td>
  <td>{{ r.rh }}</td>
  <td>{{ r.wind }}</td>
  <td>{{ r.wbgt_f }}</td>
  <td>{{ r.heat }}</td>
  <td>{{ r.wc }}</td>
  <td>{{ r.final }}</td>
  <td>{{ r.uniform }}</td>
  <td>{{ r.pt_uniform }}</td>
</tr>
{% endfor %}
</table>
</body>
</html>
"""

# -----------------------
# Routes
# -----------------------
@app.route("/", methods=["GET"])
def dashboard():
    q_location = request.args.get("location", "").strip()
    q_temp_f = request.args.get("temp_f", "").strip()
    q_rh = request.args.get("rh", "").strip()
    q_wind = request.args.get("wind_mph", "").strip()
    location = q_location or DEFAULT_LOCATION

    # Manual override if temp+rh provided
    if q_temp_f and q_rh:
        try:
            temp_f = float(q_temp_f); rh = float(q_rh)
            wind_mph = float(q_wind) if q_wind else 3.0
            weather_text = "Manual"
            clouds = 0
            tz_name = None
        except ValueError:
            return "Invalid manual input (temp/rh/wind must be numbers)", 400
    else:
        # live fetch
        try:
            j = fetch_current_weather(location)
            loc = j.get("location", {})
            tz_name = loc.get("tz_id")
            weather = j["current"]
            temp_f = weather["temp_f"]
            rh = weather.get("humidity", 50)
            wind_mph = weather.get("wind_mph", 0.0)
            clouds = weather.get("cloud", 0)
            weather_text = weather.get("condition", {}).get("text", "")
        except Exception as e:
            return f"Weather fetch failed: {e}", 500

    # local time using tz from WeatherAPI if available, else system local UTC fallback
    try:
        if 'tz_name' in locals() and tz_name and ZoneInfo:
            local_time = datetime.now(ZoneInfo(tz_name)).strftime("%Y-%m-%d %H:%M:%S %Z")
        else:
            local_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        local_time = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")

    sunny = clouds < 30
    temp_c = round(f_to_c(temp_f), 2)
    wbgt_c, twb_c, tg_c = approx_wbgt(temp_c, rh, sunny)
    wbgt_f = round(c_to_f(wbgt_c), 2)
    twb_c = round(twb_c, 2)
    tg_c = round(tg_c, 2)

    wc = wind_chill_f(temp_f, wind_mph)
    wc_text = f"{wc:.1f} °F" if wc is not None else "N/A"

    wbgt_applicable = temp_f > WBGT_CUTOFF_F
    heat_label, heat_num = heat_category_from_wbgt_f(wbgt_f) if wbgt_applicable else ("N/A (cold)", None)

    # precipitation note
    precip_level, cond_note, precip_override = interpret_condition(weather_text)

    uniform, uniform_level = recommend_uniform_option_a(temp_f, wc, heat_num, wbgt_applicable, precip_level)
    final_dec = final_training_decision(temp_f, wc, heat_num, wbgt_applicable, precip_override, precip_level)

    # NEW: PT uniform for current conditions
    pt_uniform = recommend_pt_uniform(temp_f)

    # Forecast block
    try:
        fdata = fetch_forecast(location, days=FORECAST_DAYS)
        forecast_days = fdata.get("forecast", {}).get("forecastday", [])
    except Exception as e:
        forecast_days = []

    forecast_out = []
    for day in forecast_days:
        dday = day["day"]
        cond = dday["condition"]["text"]
        cond_l = cond.lower()
        if "snow" in cond_l or "flurr" in cond_l or dday.get("daily_chance_of_snow",0) > 20:
            precip = "Snow/Flurries"
        elif "rain" in cond_l or dday.get("daily_chance_of_rain",0) > 20:
            precip = "Rain"
        else:
            precip = "None"

        avg_f = dday["avgtemp_f"]
        avg_c = dday["avgtemp_c"]
        rh_d = dday.get("avghumidity", 50)
        wind_max = dday.get("maxwind_mph", 0)
        clouds_pct = max(dday.get("daily_chance_of_rain",0), dday.get("daily_chance_of_snow",0))

        sunny_d = clouds_pct < 30
        wbgt_c_d, twb_d, tg_d = approx_wbgt(avg_c, rh_d, sunny_d)
        wbgt_f_d = round(c_to_f(wbgt_c_d), 1)
        wc_d = wind_chill_f(avg_f, wind_max)
        wc_text_d = f"{wc_d:.1f} °F" if wc_d is not None else "N/A"

        wbgt_app_d = avg_f > WBGT_CUTOFF_F
        heat_label_d, heat_num_d = heat_category_from_wbgt_f(wbgt_f_d) if wbgt_app_d else ("Cold", None)

        precip_level_d, precip_note_d, precip_override_d = interpret_condition(cond)
        uniform_d, lvl_d = recommend_uniform_option_a(avg_f, wc_d, heat_num_d, wbgt_app_d, precip_level_d)
        final_d = final_training_decision(avg_f, wc_d, heat_num_d, wbgt_app_d, precip_override_d, precip_level_d)
        pt_uniform_d = recommend_pt_uniform(avg_f)

        forecast_out.append({
            "date": day["date"],
            "temp_f": round(avg_f,1),
            "temp_c": round(avg_c,1),
            "rh": int(rh_d),
            "wind_mph": round(wind_max,1),
            "condition": cond,
            "precip_type": precip,
            "clouds": clouds_pct,
            "wbgt_f": wbgt_f_d,
            "wbgt_c": round(wbgt_c_d,1),
            "wc_text": wc_text_d,
            "uniform": uniform_d,
            "uniform_level": lvl_d,
            "final": final_d,
            "pt_uniform": pt_uniform_d
        })

    return render_template_string(PAGE_HTML,
                                  local_time=local_time,
                                  location_name=location,
                                  temp_f=f"{temp_f:.1f}",
                                  temp_c=f"{temp_c:.1f}",
                                  rh=int(rh),
                                  wind_mph=round(wind_mph,1),
                                  clouds=clouds,
                                  wbgt_f=wbgt_f,
                                  wbgt_c=round(wbgt_c,2),
                                  twb_c=twb_c,
                                  tg_c=tg_c,
                                  heat_label=heat_label,
                                  weather_text=weather_text,
                                  cond_note=cond_note,
                                  wc_text=wc_text,
                                  uniform=uniform,
                                  uniform_level=uniform_level,
                                  final_decision=final_dec,
                                  pt_uniform=pt_uniform,
                                  forecast=forecast_out)

@app.route("/weekly")
def weekly():
    location = request.args.get("location", DEFAULT_LOCATION)
    try:
        raw = fetch_forecast(location, days=WEEKLY_DAYS)
    except Exception as e:
        return f"Forecast fetch failed: {e}", 500

    loc = raw.get("location", {})
    location_name = f"{loc.get('name','')}, {loc.get('region','') or loc.get('country','')}"
    days = raw.get("forecast", {}).get("forecastday", [])

    rows = []
    for day in days:
        d = day["day"]
        date = day["date"]
        avg_f = d["avgtemp_f"]
        rh = int(d.get("avghumidity", 50))
        wind = round(d.get("maxwind_mph", 0),1)
        clouds_pct = max(d.get("daily_chance_of_rain",0), d.get("daily_chance_of_snow",0))
        temp_c = f_to_c(avg_f)
        sunny = clouds_pct < 30
        wbgt_c, twb, tg = approx_wbgt(temp_c, rh, sunny)
        wbgt_f = round(c_to_f(wbgt_c),1)
        wc = wind_chill_f(avg_f, wind)
        wc_str = "N/A" if wc is None else f"{wc:.1f}"
        wbgt_app = avg_f > WBGT_CUTOFF_F
        if wbgt_app:
            heat_label, heat_num = heat_category_from_wbgt_f(wbgt_f)
        else:
            heat_label, heat_num = "N/A", None
        precip_level, precip_note, precip_override = interpret_condition(d.get("condition",{}).get("text",""))
        uniform, _ = recommend_uniform_option_a(avg_f, wc, heat_num, wbgt_app, precip_level)
        final = final_training_decision(avg_f, wc, heat_num, wbgt_app, precip_override, precip_level)
        pt = recommend_pt_uniform(avg_f)

        rows.append({
            "date": date,
            "avg_f": round(avg_f,1),
            "rh": rh,
            "wind": wind,
            "wbgt_f": wbgt_f,
            "heat": heat_label,
            "wc": wc_str,
            "final": final,
            "uniform": uniform,
            "pt_uniform": pt
        })

    return render_template_string(WEEKLY_HTML, location_name=location_name, rows=rows)

# -----------------------
# Run app
# -----------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

