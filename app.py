import os
from pathlib import Path

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS


BASE_DIR = Path(__file__).resolve().parent
load_dotenv()

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

# In-memory latest sensor values (simple start; replace with DB later if needed)
sensor_data = {
    "moisture": 72,
    "ph": 6.8,
    "temp": 24,
    "nitrogen": 55,
}


def _system_prompt(lang_name: str, telugu_style_instruction: str) -> str:
    return (
        "You are AgroBot, an intelligent and friendly farming assistant. "
        "Your goal is to help farmers with crop advice, soil health, weather interpretation, and market trends. "
        "Keep your answers concise, practical, and encouraging. "
        "When location is provided, use it to make local recommendations for weather timing, crops, and farming practices. "
        f"The user is asking in {lang_name}. Please reply in {lang_name}. "
        f"{telugu_style_instruction}"
    ).strip()


def _format_location_context(raw_location: object) -> str:
    if not isinstance(raw_location, dict):
        return ""

    city = str(raw_location.get("city") or "").strip()
    region = str(raw_location.get("region") or "").strip()
    country = str(raw_location.get("country") or "").strip()
    latitude = raw_location.get("latitude")
    longitude = raw_location.get("longitude")

    area = ", ".join(part for part in [city, region, country] if part)
    coords = ""
    if isinstance(latitude, (int, float)) and isinstance(longitude, (int, float)):
        coords = f"{latitude:.4f}, {longitude:.4f}"

    if area and coords:
        return f"User location: {area} (coordinates: {coords})."
    if area:
        return f"User location: {area}."
    if coords:
        return f"User coordinates: {coords}."
    return ""


@app.get("/")
def index():
    return send_from_directory(BASE_DIR, "index.html")


@app.route("/update", methods=["GET", "POST"])
def update_data():
    global sensor_data

    expected_token = os.getenv("SENSOR_API_TOKEN", "").strip()
    if expected_token:
        provided_token = request.headers.get("X-Sensor-Token", "").strip()
        if provided_token != expected_token:
            return jsonify({"error": "Unauthorized sensor client"}), 401

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "Invalid payload. Expected JSON object."}), 400

    sensor_data = {
        "moisture": payload.get("moisture", sensor_data.get("moisture", 0)),
        "ph": payload.get("ph", sensor_data.get("ph", 7)),
        "temp": payload.get("temp", sensor_data.get("temp", 25)),
        "nitrogen": payload.get("nitrogen", sensor_data.get("nitrogen", 50)),
    }
    return jsonify({"status": "Data received successfully", "data": sensor_data})


@app.get("/data")
def get_data():
    return jsonify(sensor_data)


def _extract_gemini_text(data: dict) -> str:
    candidates = data.get("candidates") or []
    if not candidates:
        return ""

    content = candidates[0].get("content") or {}
    parts = content.get("parts") or []
    text_chunks = [p.get("text", "") for p in parts if isinstance(p, dict)]
    return "\n".join(chunk for chunk in text_chunks if chunk).strip()


def _normalize_model_name(model_name: str) -> str:
    if model_name.startswith("models/"):
        return model_name.split("/", 1)[1]
    return model_name


def _list_generate_content_models(api_key: str) -> list[str]:
    try:
        response = requests.get(
            "https://generativelanguage.googleapis.com/v1beta/models",
            params={"key": api_key, "pageSize": 1000},
            timeout=20,
        )
        if not response.ok:
            return []
        payload = response.json()
    except Exception:  # noqa: BLE001
        return []

    models = payload.get("models") or []
    names: list[str] = []
    for model in models:
        methods = model.get("supportedGenerationMethods") or []
        if not any(str(m).lower() == "generatecontent" for m in methods):
            continue
        name = _normalize_model_name(str(model.get("name") or "").strip())
        if name:
            names.append(name)
    return names


def _to_float(value: object) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _openweather_condition_to_text(entry: dict) -> str:
    weather = entry.get("weather") or []
    if isinstance(weather, list) and weather:
        first = weather[0] if isinstance(weather[0], dict) else {}
        desc = str(first.get("description") or "").strip()
        main = str(first.get("main") or "").strip()
        return desc.title() if desc else (main or "Weather update")
    return "Weather update"


def _fetch_weather_openweather(lat: float, lon: float, api_key: str) -> dict:
    current_resp = requests.get(
        "https://api.openweathermap.org/data/2.5/weather",
        params={"lat": lat, "lon": lon, "appid": api_key, "units": "metric"},
        timeout=20,
    )
    if not current_resp.ok:
        raise RuntimeError(f"OpenWeather current failed: {current_resp.status_code}")
    current_data = current_resp.json()

    forecast_resp = requests.get(
        "https://api.openweathermap.org/data/2.5/forecast",
        params={"lat": lat, "lon": lon, "appid": api_key, "units": "metric"},
        timeout=20,
    )
    if not forecast_resp.ok:
        raise RuntimeError(f"OpenWeather forecast failed: {forecast_resp.status_code}")
    forecast_data = forecast_resp.json()

    current_main = current_data.get("main") or {}
    current_wind = current_data.get("wind") or {}
    current = {
        "temperature_c": _to_float(current_main.get("temp")),
        "humidity_pct": _to_float(current_main.get("humidity")),
        "wind_kmh": (_to_float(current_wind.get("speed")) or 0.0) * 3.6,
        "condition": _openweather_condition_to_text(current_data),
    }

    grouped: dict[str, list[dict]] = {}
    for entry in forecast_data.get("list") or []:
        if not isinstance(entry, dict):
            continue
        dt_txt = str(entry.get("dt_txt") or "")
        day = dt_txt.split(" ", 1)[0]
        if not day:
            continue
        grouped.setdefault(day, []).append(entry)

    daily: list[dict] = []
    for day in sorted(grouped.keys())[:5]:
        entries = grouped[day]
        temps = [_to_float((e.get("main") or {}).get("temp_max")) for e in entries]
        temps = [t for t in temps if t is not None]
        pops = [_to_float(e.get("pop")) for e in entries]
        pops = [p for p in pops if p is not None]

        noon_entry = next((e for e in entries if "12:00:00" in str(e.get("dt_txt") or "")), entries[0])
        daily.append(
            {
                "date": day,
                "temp_max_c": max(temps) if temps else None,
                "rain_chance_pct": (max(pops) * 100.0) if pops else 0.0,
                "condition": _openweather_condition_to_text(noon_entry),
            }
        )

    return {"source": "openweather", "current": current, "daily": daily}


def _open_meteo_label(code: int) -> str:
    return {
        0: "Clear",
        1: "Mostly clear",
        2: "Partly cloudy",
        3: "Cloudy",
        45: "Fog",
        48: "Rime fog",
        51: "Light drizzle",
        53: "Drizzle",
        55: "Dense drizzle",
        61: "Light rain",
        63: "Rain",
        65: "Heavy rain",
        71: "Light snow",
        73: "Snow",
        75: "Heavy snow",
        80: "Rain showers",
        81: "Rain showers",
        82: "Heavy showers",
        95: "Thunderstorm",
    }.get(int(code), "Weather update")


def _fetch_weather_open_meteo(lat: float, lon: float) -> dict:
    response = requests.get(
        "https://api.open-meteo.com/v1/forecast",
        params={
            "latitude": lat,
            "longitude": lon,
            "current": "temperature_2m,relative_humidity_2m,wind_speed_10m,weather_code",
            "daily": "weather_code,temperature_2m_max,precipitation_probability_max",
            "timezone": "auto",
            "forecast_days": 5,
        },
        timeout=20,
    )
    if not response.ok:
        raise RuntimeError(f"Open-Meteo failed: {response.status_code}")
    weather = response.json()

    current_raw = weather.get("current") or {}
    daily_raw = weather.get("daily") or {}
    code = int(current_raw.get("weather_code") or 0)
    current = {
        "temperature_c": _to_float(current_raw.get("temperature_2m")),
        "humidity_pct": _to_float(current_raw.get("relative_humidity_2m")),
        "wind_kmh": _to_float(current_raw.get("wind_speed_10m")),
        "condition": _open_meteo_label(code),
    }

    days = daily_raw.get("time") or []
    codes = daily_raw.get("weather_code") or []
    max_temps = daily_raw.get("temperature_2m_max") or []
    rains = daily_raw.get("precipitation_probability_max") or []
    daily: list[dict] = []
    for i, day in enumerate(days[:5]):
        day_code = int(codes[i] or 0) if i < len(codes) else 0
        daily.append(
            {
                "date": str(day),
                "temp_max_c": _to_float(max_temps[i]) if i < len(max_temps) else None,
                "rain_chance_pct": _to_float(rains[i]) if i < len(rains) else 0.0,
                "condition": _open_meteo_label(day_code),
            }
        )

    return {"source": "open-meteo", "current": current, "daily": daily}


@app.get("/api/weather")
def weather():
    lat = _to_float(request.args.get("lat"))
    lon = _to_float(request.args.get("lon"))
    if lat is None or lon is None:
        return jsonify({"error": "lat and lon query params are required"}), 400

    openweather_key = os.getenv("OPENWEATHER_API_KEY", "").strip()
    if openweather_key:
        try:
            return jsonify(_fetch_weather_openweather(lat, lon, openweather_key))
        except Exception as exc:  # noqa: BLE001
            try:
                fallback = _fetch_weather_open_meteo(lat, lon)
                fallback["fallback_reason"] = str(exc)
                return jsonify(fallback)
            except Exception as fallback_exc:  # noqa: BLE001
                return jsonify({"error": str(fallback_exc)}), 502

    try:
        return jsonify(_fetch_weather_open_meteo(lat, lon))
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 502


@app.post("/api/chat")
def chat():
    google_ai_key = os.getenv("GOOGLE_AI_API_KEY", "").strip()
    if not google_ai_key:
        return jsonify({"error": "GOOGLE_AI_API_KEY is not set on server."}), 500

    body = request.get_json(silent=True) or {}
    question = (body.get("question") or "").strip()
    lang_name = (body.get("langName") or "English").strip()
    telugu_style_instruction = (body.get("teluguStyleInstruction") or "").strip()
    location_context = _format_location_context(body.get("location"))

    if not question:
        return jsonify({"error": "Question is required."}), 400

    prompt = (
        f"{_system_prompt(lang_name, telugu_style_instruction)}\n"
        f"{location_context}\n\n"
        f"User question: {question}"
    )

    preferred_models = [
        "gemini-2.5-flash",
        "gemini-2.5-flash-lite",
        "gemini-2.0-flash",
        "gemini-1.5-flash",
    ]
    listed_models = _list_generate_content_models(google_ai_key)
    models = [m for m in preferred_models if m in listed_models]
    models.extend(m for m in listed_models if m not in models)
    if not models:
        models = preferred_models

    last_error = "Unknown API error."
    for model in models:
        try:
            response = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                params={"key": google_ai_key},
                headers={"Content-Type": "application/json"},
                json={"contents": [{"parts": [{"text": prompt}]}]},
                timeout=30,
            )
            data = response.json()
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
            continue

        if response.ok:
            reply = _extract_gemini_text(data)
            if reply:
                return jsonify({"reply": reply, "model": model})
            last_error = "Gemini returned an empty response."
            continue

        if isinstance(data, dict) and isinstance(data.get("error"), dict):
            api_error = data["error"].get("message", "")
            if api_error:
                last_error = f"{model}: {api_error}"
        else:
            generic_error = data.get("error", "") if isinstance(data, dict) else ""
            if generic_error:
                last_error = f"{model}: {generic_error}"

    return jsonify({"error": f"Google AI unavailable. Last error: {last_error}"}), 502


@app.get("/health")
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=True)
