import os
import json
import base64
import importlib
import time
import threading
from io import BytesIO
from datetime import datetime, timedelta
from pathlib import Path

import requests
from dotenv import load_dotenv
from flask import Flask, Response, jsonify, request, send_file, send_from_directory
from flask_cors import CORS

try:
    firebase_admin = importlib.import_module("firebase_admin")
    credentials = importlib.import_module("firebase_admin.credentials")
    messaging = importlib.import_module("firebase_admin.messaging")
    firestore = importlib.import_module("firebase_admin.firestore")
    firebase_db = importlib.import_module("firebase_admin.db")
except Exception:  # noqa: BLE001
    firebase_admin = None
    credentials = None
    messaging = None
    firestore = None
    firebase_db = None


BASE_DIR = Path(__file__).resolve().parent
load_dotenv()

app = Flask(__name__)
raw_allowed_origins = os.getenv("ALLOWED_ORIGINS", "").strip()
allowed_origins = [o.strip() for o in raw_allowed_origins.split(",") if o.strip()]
if not allowed_origins:
    allowed_origins = ["http://127.0.0.1:5000", "http://localhost:5000", "https://agrisens.truvgo.me"]
CORS(app, resources={r"/*": {"origins": allowed_origins}})
PUSH_TOKENS_PATH = BASE_DIR / "push_tokens.json"
FARM_RECORDS_PATH = BASE_DIR / "data" / "farm_records.json"
FARM_PROFILES_PATH = BASE_DIR / "data" / "farm_profiles.json"
ALERT_THRESHOLDS_PATH = BASE_DIR / "data" / "crop_thresholds.json"
ALERT_COOLDOWN_MINUTES = int(os.getenv("ALERT_COOLDOWN_MINUTES", "30").strip() or "30")
ALERT_ENGINE_INTERVAL_SECONDS = int(os.getenv("ALERT_ENGINE_INTERVAL_SECONDS", "300").strip() or "300")

# In-memory latest sensor values (simple start; replace with DB later if needed)
sensor_data = {
    "moisture": 72,
    "ph": 6.8,
    "temp": 24,
    "nitrogen": 55,
    "phosphorus": 28,
    "potassium": 35,
}
sensor_readings_window: list[dict] = []
_alert_engine_started = False
_alert_engine_lock = threading.Lock()
_alert_engine_last_run_at: datetime | None = None

try:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas
except Exception:  # noqa: BLE001
    A4 = None
    canvas = None


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


def _push_config() -> dict:
    return {
        "apiKey": os.getenv("FIREBASE_API_KEY", "AIzaSyCPZvb3f97CaoUt2tiy1znw76-29EVtU08").strip(),
        "authDomain": os.getenv("FIREBASE_AUTH_DOMAIN", "agrisens-fca57.firebaseapp.com").strip(),
        "projectId": os.getenv("FIREBASE_PROJECT_ID", "agrisens-fca57").strip(),
        "storageBucket": os.getenv("FIREBASE_STORAGE_BUCKET", "agrisens-fca57.firebasestorage.app").strip(),
        "messagingSenderId": os.getenv("FIREBASE_MESSAGING_SENDER_ID", "643022334969").strip(),
        "appId": os.getenv("FIREBASE_APP_ID", "1:643022334969:web:7e842a6b4a0a5125fd6b28").strip(),
        "databaseURL": os.getenv("FIREBASE_DATABASE_URL", "https://agrisens-fca57-default-rtdb.firebaseio.com").strip(),
        "vapidKey": os.getenv("FIREBASE_VAPID_KEY", "").strip(),
    }


def _load_push_tokens() -> dict:
    if not PUSH_TOKENS_PATH.exists():
        return {}
    try:
        return json.loads(PUSH_TOKENS_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _save_push_tokens(tokens: dict) -> None:
    PUSH_TOKENS_PATH.write_text(json.dumps(tokens, indent=2), encoding="utf-8")


def _default_crop_thresholds() -> dict:
    return {
        "default": {
            "moisture": {"critical": 30.0, "warning": 45.0},
            "npk_min_ppm": {"nitrogen": 50.0, "phosphorus": 25.0, "potassium": 30.0},
            "ph_range": {"min": 6.0, "max": 7.5},
        },
        "paddy": {
            "npk_min_ppm": {"nitrogen": 60.0, "phosphorus": 30.0, "potassium": 35.0},
            "ph_range": {"min": 5.5, "max": 7.0},
        },
        "maize": {
            "npk_min_ppm": {"nitrogen": 55.0, "phosphorus": 28.0, "potassium": 32.0},
            "ph_range": {"min": 5.8, "max": 7.2},
        },
        "cotton": {
            "npk_min_ppm": {"nitrogen": 52.0, "phosphorus": 26.0, "potassium": 34.0},
            "ph_range": {"min": 6.0, "max": 7.8},
        },
        "groundnut": {
            "npk_min_ppm": {"nitrogen": 40.0, "phosphorus": 24.0, "potassium": 28.0},
            "ph_range": {"min": 6.0, "max": 7.4},
        },
        "wheat": {
            "npk_min_ppm": {"nitrogen": 50.0, "phosphorus": 24.0, "potassium": 30.0},
            "ph_range": {"min": 6.0, "max": 7.5},
        },
    }


def _load_crop_thresholds() -> dict:
    default = _default_crop_thresholds()
    if not ALERT_THRESHOLDS_PATH.exists():
        return default
    try:
        payload = json.loads(ALERT_THRESHOLDS_PATH.read_text(encoding="utf-8"))
        if isinstance(payload, dict) and "default" in payload:
            return payload
    except Exception:  # noqa: BLE001
        pass
    return default


def _save_crop_thresholds(payload: dict) -> None:
    ALERT_THRESHOLDS_PATH.parent.mkdir(parents=True, exist_ok=True)
    ALERT_THRESHOLDS_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _thresholds_for_crop(crop_type: str) -> dict:
    all_thresholds = _load_crop_thresholds()
    base = all_thresholds.get("default") if isinstance(all_thresholds, dict) else None
    base = base if isinstance(base, dict) else _default_crop_thresholds()["default"]
    crop_key = str(crop_type or "default").strip().lower()
    override = all_thresholds.get(crop_key) if isinstance(all_thresholds, dict) else None
    override = override if isinstance(override, dict) else {}

    moisture_base = base.get("moisture") if isinstance(base.get("moisture"), dict) else {}
    npk_base = base.get("npk_min_ppm") if isinstance(base.get("npk_min_ppm"), dict) else {}
    ph_base = base.get("ph_range") if isinstance(base.get("ph_range"), dict) else {}

    moisture_override = override.get("moisture") if isinstance(override.get("moisture"), dict) else {}
    npk_override = override.get("npk_min_ppm") if isinstance(override.get("npk_min_ppm"), dict) else {}
    ph_override = override.get("ph_range") if isinstance(override.get("ph_range"), dict) else {}

    return {
        "moisture": {
            "critical": float(moisture_override.get("critical", moisture_base.get("critical", 30))),
            "warning": float(moisture_override.get("warning", moisture_base.get("warning", 45))),
        },
        "npk_min_ppm": {
            "nitrogen": float(npk_override.get("nitrogen", npk_base.get("nitrogen", 50))),
            "phosphorus": float(npk_override.get("phosphorus", npk_base.get("phosphorus", 25))),
            "potassium": float(npk_override.get("potassium", npk_base.get("potassium", 30))),
        },
        "ph_range": {
            "min": float(ph_override.get("min", ph_base.get("min", 6.0))),
            "max": float(ph_override.get("max", ph_base.get("max", 7.5))),
        },
    }


def _append_sensor_reading(snapshot: dict) -> None:
    sensor_readings_window.append(
        {
            "at": datetime.utcnow().isoformat(),
            "moisture": _to_float(snapshot.get("moisture")),
        }
    )
    if len(sensor_readings_window) > 24:
        del sensor_readings_window[:-24]


def _is_moisture_dropping() -> bool:
    readings = [item.get("moisture") for item in sensor_readings_window if _to_float(item.get("moisture")) is not None]
    vals = [float(v) for v in readings[-3:]]
    return len(vals) == 3 and vals[0] > vals[1] > vals[2]


def _alert_payload(code: str, severity: str, title: str, message: str, category: str) -> dict:
    return {
        "code": code,
        "severity": severity,
        "title": title,
        "message": message,
        "category": category,
        "created_at": datetime.utcnow().isoformat(),
    }


def _moisture_logic(snapshot: dict, thresholds: dict) -> list[dict]:
    alerts: list[dict] = []
    moisture = _to_float(snapshot.get("moisture"))
    if moisture is None:
        return alerts
    critical = _to_float((thresholds.get("moisture") or {}).get("critical")) or 30.0
    warning = _to_float((thresholds.get("moisture") or {}).get("warning")) or 45.0

    if moisture < critical:
        alerts.append(
            _alert_payload(
                "critical_irrigation",
                "High",
                "Critical Irrigation Alert",
                f"Soil moisture is {moisture:.1f}%. Start irrigation immediately to protect crop health.",
                "moisture",
            )
        )
    elif moisture < warning:
        alerts.append(
            _alert_payload(
                "warning_irrigation",
                "Medium",
                "Warning Alert",
                f"Soil moisture is {moisture:.1f}%. Plan irrigation soon to avoid stress.",
                "moisture",
            )
        )
    return alerts


def _npk_logic(snapshot: dict, thresholds: dict) -> list[dict]:
    alerts: list[dict] = []
    npk_min = thresholds.get("npk_min_ppm") if isinstance(thresholds.get("npk_min_ppm"), dict) else {}
    for nutrient_key in ("nitrogen", "phosphorus", "potassium"):
        value = _to_float(snapshot.get(nutrient_key))
        minimum = _to_float(npk_min.get(nutrient_key))
        if value is None or minimum is None:
            continue
        if value < minimum:
            label = nutrient_key.capitalize()
            alerts.append(
                _alert_payload(
                    f"npk_deficiency_{nutrient_key}",
                    "Medium",
                    "Nutrient Deficiency Alert",
                    f"Low {label} detected ({value:.1f} ppm). Target is at least {minimum:.1f} ppm for this crop.",
                    "npk",
                )
            )
    return alerts


def _ph_logic(snapshot: dict) -> list[dict]:
    alerts: list[dict] = []
    ph_value = _to_float(snapshot.get("ph"))
    if ph_value is None:
        return alerts
    if ph_value < 6.0:
        alerts.append(
            _alert_payload(
                "ph_acidic",
                "Medium",
                "Acidic Soil Alert",
                f"Soil pH is {ph_value:.2f}. Consider liming to improve nutrient uptake.",
                "ph",
            )
        )
    elif ph_value > 7.5:
        alerts.append(
            _alert_payload(
                "ph_alkaline",
                "Medium",
                "Alkaline Soil Alert",
                f"Soil pH is {ph_value:.2f}. Consider sulfur/acidifying amendments and organic matter.",
                "ph",
            )
        )
    return alerts


def _weather_logic(snapshot: dict, weather: dict | None) -> list[dict]:
    alerts: list[dict] = []
    moisture = _to_float(snapshot.get("moisture"))
    temp = _to_float(snapshot.get("temp"))
    if moisture is None:
        return alerts

    current = weather.get("current") if isinstance(weather, dict) else {}
    daily = weather.get("daily") if isinstance(weather, dict) else []
    current_temp = _to_float((current or {}).get("temperature_c"))
    effective_temp = current_temp if current_temp is not None else temp

    if effective_temp is not None and effective_temp > 35.0 and moisture < 40.0:
        alerts.append(
            _alert_payload(
                "weather_high_priority_irrigation",
                "High",
                "High Priority Irrigation Alert",
                f"Temperature is {effective_temp:.1f}C and moisture is {moisture:.1f}%. Irrigate urgently.",
                "weather",
            )
        )

    next_three_days = daily[:3] if isinstance(daily, list) else []
    rain_probs = [_to_float((day if isinstance(day, dict) else {}).get("rain_chance_pct")) or 0.0 for day in next_three_days]
    no_rain_3_days = len(rain_probs) == 3 and all(prob < 25.0 for prob in rain_probs)
    if no_rain_3_days and _is_moisture_dropping():
        alerts.append(
            _alert_payload(
                "weather_preventive_irrigation",
                "Medium",
                "Preventive Irrigation Alert",
                "No meaningful rainfall is forecast for 3 days and soil moisture trend is falling. Plan preventive irrigation.",
                "weather",
            )
        )

    heavy_rain = any(((_to_float((day if isinstance(day, dict) else {}).get("rain_chance_pct")) or 0.0) >= 75.0) for day in (daily or [])[:2])
    if heavy_rain and moisture > 70.0:
        alerts.append(
            _alert_payload(
                "weather_waterlogging_risk",
                "High",
                "Waterlogging Risk Alert",
                "Heavy rainfall is forecast while soil moisture is already high. Improve drainage and pause irrigation.",
                "weather",
            )
        )

    return alerts


def _advisory_logic(snapshot: dict, thresholds: dict, npk_alerts: list[dict], ph_alerts: list[dict]) -> list[dict]:
    if not npk_alerts and not ph_alerts:
        return []

    ph_value = _to_float(snapshot.get("ph"))
    ph_range = thresholds.get("ph_range") if isinstance(thresholds.get("ph_range"), dict) else {}
    min_ph = _to_float(ph_range.get("min")) or 6.0
    max_ph = _to_float(ph_range.get("max")) or 7.5

    nutrient_names: list[str] = []
    for alert in npk_alerts:
        code = str(alert.get("code") or "")
        if code.endswith("nitrogen"):
            nutrient_names.append("Nitrogen")
        elif code.endswith("phosphorus"):
            nutrient_names.append("Phosphorus")
        elif code.endswith("potassium"):
            nutrient_names.append("Potassium")
    nutrient_label = ", ".join(sorted(set(nutrient_names))) if nutrient_names else "nutrients"

    ph_state = "within normal range"
    if ph_value is not None and ph_value < min_ph:
        ph_state = "acidic"
    elif ph_value is not None and ph_value > max_ph:
        ph_state = "alkaline"

    return [
        _alert_payload(
            "advisory_nutrient_absorption_risk",
            "Medium" if nutrient_names else "Low",
            "Intelligent Advisory",
            f"Low {nutrient_label} detected. Soil is {ph_state}. Nutrient absorption may reduce.",
            "advisory",
        )
    ]


def _resolve_alert_coordinates() -> tuple[float, float]:
    lat = _to_float(sensor_data.get("latitude"))
    lon = _to_float(sensor_data.get("longitude"))
    if lat is not None and lon is not None:
        return lat, lon
    env_lat = _to_float(os.getenv("ALERT_LAT", "17.3850"))
    env_lon = _to_float(os.getenv("ALERT_LON", "78.4867"))
    return env_lat or 17.3850, env_lon or 78.4867


def _fetch_alert_weather() -> dict | None:
    lat, lon = _resolve_alert_coordinates()
    openweather_key = os.getenv("OPENWEATHER_API_KEY", "").strip()
    try:
        if openweather_key:
            return _fetch_weather_openweather(lat, lon, openweather_key)
        return _fetch_weather_open_meteo(lat, lon)
    except Exception:  # noqa: BLE001
        return None


def _create_alert_tables() -> None:
    # Firestore is schemaless; collections are created on first write.
    return None


def _alert_within_cooldown(code: str, crop_type: str, cooldown_minutes: int) -> bool:
    db = _firestore_client()
    if db is None:
        return False
    cutoff_epoch = (datetime.utcnow() - timedelta(minutes=max(1, cooldown_minutes))).timestamp()
    coll = db.collection("alert_events")
    docs = (
        coll.where("crop_type", "==", crop_type)
        .order_by("created_at_epoch", direction=firestore.Query.DESCENDING)
        .limit(100)
        .stream()
    )
    for doc in docs:
        payload = doc.to_dict() or {}
        if str(payload.get("code") or "") != code:
            continue
        created_at_epoch = _to_float(payload.get("created_at_epoch")) or 0.0
        if created_at_epoch >= cutoff_epoch:
            return True
    return False


def _save_alert_event(alert: dict, crop_type: str, sensor_snapshot: dict, weather_snapshot: dict | None) -> None:
    db = _firestore_client()
    if db is None:
        return
    now = datetime.utcnow()
    db.collection("alert_events").add(
        {
            "code": str(alert.get("code") or ""),
            "severity": str(alert.get("severity") or "Low"),
            "title": str(alert.get("title") or "Alert"),
            "message": str(alert.get("message") or ""),
            "category": str(alert.get("category") or "general"),
            "crop_type": crop_type,
            "sensor_snapshot_json": json.dumps(sensor_snapshot),
            "weather_snapshot_json": json.dumps(weather_snapshot or {}),
            "created_at": now.strftime("%Y-%m-%d %H:%M:%S"),
            "created_at_epoch": now.timestamp(),
        }
    )


def _save_sensor_reading(sensor_snapshot: dict, raw_payload: dict | None = None) -> tuple[bool, str]:
    db = _firestore_client()
    if db is None:
        return False, "Firestore is not configured."

    now = datetime.utcnow()
    payload = {
        "moisture": _to_float(sensor_snapshot.get("moisture")),
        "moisture_raw": _to_float(sensor_snapshot.get("moisture_raw")),
        "ph": _to_float(sensor_snapshot.get("ph")),
        "temp": _to_float(sensor_snapshot.get("temp")),
        "nitrogen": _to_float(sensor_snapshot.get("nitrogen")),
        "phosphorus": _to_float(sensor_snapshot.get("phosphorus")),
        "potassium": _to_float(sensor_snapshot.get("potassium")),
        "latitude": _to_float(sensor_snapshot.get("latitude")),
        "longitude": _to_float(sensor_snapshot.get("longitude")),
        "source": "update-endpoint",
        "created_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "created_at_epoch": now.timestamp(),
    }

    if isinstance(raw_payload, dict):
        relay = raw_payload.get("relay")
        if relay is not None and str(relay).strip() != "":
            payload["relay"] = str(relay).strip()

    try:
        db.collection("sensor_readings").add(payload)
        return True, ""
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def _alert_history(limit: int = 50) -> list[dict]:
    safe_limit = max(1, min(limit, 200))
    db = _firestore_client()
    if db is None:
        return []
    docs = (
        db.collection("alert_events")
        .order_by("created_at_epoch", direction=firestore.Query.DESCENDING)
        .limit(safe_limit)
        .stream()
    )
    rows: list[dict] = []
    for doc in docs:
        payload = doc.to_dict() or {}
        payload["id"] = doc.id
        if isinstance(payload, dict):
            rows.append(payload)
    return rows


def _notify_via_fcm(alert: dict) -> None:
    ok, _ = _firebase_ready()
    if not ok:
        return
    title = str(alert.get("title") or "Agrisense Alert")
    body = str(alert.get("message") or "")
    tokens = _load_push_tokens()
    for token in list(tokens.keys()):
        try:
            msg = messaging.Message(
                token=token,
                notification=messaging.Notification(title=title, body=body),
            )
            messaging.send(msg)
        except Exception:  # noqa: BLE001
            continue


def _notify_via_twilio(alert: dict) -> None:
    sid = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
    auth_token = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
    from_phone = os.getenv("TWILIO_FROM_PHONE", "").strip()
    to_phone = os.getenv("TWILIO_TO_PHONE", "").strip()
    if not all([sid, auth_token, from_phone, to_phone]):
        return

    body = f"{alert.get('title', 'Agrisense Alert')}: {alert.get('message', '')}"
    try:
        requests.post(
            f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",
            data={"From": from_phone, "To": to_phone, "Body": body[:1500]},
            auth=(sid, auth_token),
            timeout=20,
        )
    except Exception:  # noqa: BLE001
        return


def _notification_handler(alerts: list[dict]) -> None:
    for alert in alerts:
        _notify_via_fcm(alert)
        _notify_via_twilio(alert)


def _evaluate_alerts(snapshot: dict, thresholds: dict, weather: dict | None) -> list[dict]:
    moisture_alerts = _moisture_logic(snapshot, thresholds)
    npk_alerts = _npk_logic(snapshot, thresholds)
    ph_alerts = _ph_logic(snapshot)
    weather_alerts = _weather_logic(snapshot, weather)
    advisory_alerts = _advisory_logic(snapshot, thresholds, npk_alerts, ph_alerts)
    return moisture_alerts + npk_alerts + ph_alerts + weather_alerts + advisory_alerts


def _run_alert_engine() -> dict:
    global _alert_engine_last_run_at
    _init_db()
    _create_alert_tables()
    profile = _latest_profile() or {}
    crop_type = str((profile or {}).get("crop_type") or "default").strip().lower()
    thresholds = _thresholds_for_crop(crop_type)
    weather_snapshot = _fetch_alert_weather()
    snapshot = dict(sensor_data)
    alerts = _evaluate_alerts(snapshot, thresholds, weather_snapshot)

    fresh_alerts: list[dict] = []
    for alert in alerts:
        code = str(alert.get("code") or "")
        if not code:
            continue
        if _alert_within_cooldown(code, crop_type, ALERT_COOLDOWN_MINUTES):
            continue
        _save_alert_event(alert, crop_type, snapshot, weather_snapshot)
        fresh_alerts.append(alert)

    if fresh_alerts:
        _notification_handler(fresh_alerts)

    _alert_engine_last_run_at = datetime.utcnow()

    return {
        "crop_type": crop_type,
        "thresholds": thresholds,
        "sensor": snapshot,
        "weather": weather_snapshot,
        "alerts": alerts,
        "new_alerts": fresh_alerts,
    }


def _run_alert_engine_if_due(force: bool = False) -> dict | None:
    if force:
        return _run_alert_engine()
    if _alert_engine_last_run_at is None:
        return _run_alert_engine()
    elapsed = (datetime.utcnow() - _alert_engine_last_run_at).total_seconds()
    if elapsed >= ALERT_ENGINE_INTERVAL_SECONDS:
        return _run_alert_engine()
    return None


def _start_alert_engine_background() -> None:
    global _alert_engine_started
    if _alert_engine_started:
        return

    def _worker() -> None:
        while True:
            with _alert_engine_lock:
                try:
                    _run_alert_engine()
                except Exception:  # noqa: BLE001
                    pass
            time.sleep(max(60, ALERT_ENGINE_INTERVAL_SECONDS))

    thread = threading.Thread(target=_worker, daemon=True, name="alert-engine")
    thread.start()
    _alert_engine_started = True


def _firebase_ready() -> tuple[bool, str]:
    if firebase_admin is None or credentials is None or messaging is None or firestore is None:
        return False, "firebase-admin is not installed on server."

    service_account_file = os.getenv("FIREBASE_SERVICE_ACCOUNT_FILE", "").strip()
    service_account_json = os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON", "").strip()
    service_account_json_b64 = os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON_B64", "").strip()
    project_id = os.getenv("FIREBASE_PROJECT_ID", "agrisens-fca57").strip() or "agrisens-fca57"
    database_url = os.getenv("FIREBASE_DATABASE_URL", "https://agrisens-fca57-default-rtdb.firebaseio.com").strip()
    app_options = {"projectId": project_id}
    if database_url:
        app_options["databaseURL"] = database_url
    if not firebase_admin._apps:  # type: ignore[attr-defined]
        parsed_credentials: dict | None = None
        if service_account_json:
            try:
                candidate = json.loads(service_account_json)
                if isinstance(candidate, dict):
                    parsed_credentials = candidate
                else:
                    return False, "FIREBASE_SERVICE_ACCOUNT_JSON is not a valid JSON object."
            except Exception as exc:  # noqa: BLE001
                return False, f"Unable to parse FIREBASE_SERVICE_ACCOUNT_JSON: {exc}"
        elif service_account_json_b64:
            try:
                decoded = base64.b64decode(service_account_json_b64).decode("utf-8")
                candidate = json.loads(decoded)
                if isinstance(candidate, dict):
                    parsed_credentials = candidate
                else:
                    return False, "FIREBASE_SERVICE_ACCOUNT_JSON_B64 is not a valid encoded JSON object."
            except Exception as exc:  # noqa: BLE001
                return False, f"Unable to parse FIREBASE_SERVICE_ACCOUNT_JSON_B64: {exc}"

        if parsed_credentials is not None:
            cred = credentials.Certificate(parsed_credentials)
            firebase_admin.initialize_app(cred, app_options)
        elif service_account_file:
            service_account_path = Path(service_account_file)
            if not service_account_path.is_absolute():
                service_account_path = BASE_DIR / service_account_file
            if not service_account_path.exists():
                return False, f"Service account file not found: {service_account_path}"
            cred = credentials.Certificate(str(service_account_path))
            firebase_admin.initialize_app(cred, app_options)
        else:
            # Fallback to Application Default Credentials when available.
            try:
                firebase_admin.initialize_app(options=app_options)
            except Exception:
                default_path = BASE_DIR / "firebase-service-account.json"
                return (
                    False,
                    "Firebase credentials are not configured and ADC is unavailable. "
                    "Set FIREBASE_SERVICE_ACCOUNT_JSON (or FIREBASE_SERVICE_ACCOUNT_JSON_B64) "
                    "or FIREBASE_SERVICE_ACCOUNT_FILE "
                    f"(for example: {default_path}).",
                )
    return True, ""


def _is_authorized_admin(request_token: str) -> bool:
    expected_token = os.getenv("ADMIN_API_TOKEN", "").strip()
    if not expected_token:
        # If not configured, keep backward compatibility for local/dev use.
        return True
    return request_token == expected_token


def _allow_public_push_bootstrap() -> bool:
    raw = os.getenv("PUSH_PUBLIC_BOOTSTRAP", "true").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _firestore_client():
    ok, _ = _firebase_ready()
    if not ok or firestore is None:
        return None
    try:
        return firestore.client()
    except Exception:  # noqa: BLE001
        return None


def _sync_sensor_realtime(sensor_snapshot: dict) -> tuple[bool, str]:
    ok, reason = _firebase_ready()
    if not ok:
        return False, reason
    if firebase_db is None:
        return False, "firebase-admin db module is unavailable."

    payload = dict(sensor_snapshot)
    payload["updated_at"] = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    payload["updated_at_epoch"] = datetime.utcnow().timestamp()
    try:
        firebase_db.reference("/live/sensor").set(payload)
        return True, ""
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def _init_db() -> bool:
    # Kept function name for compatibility with existing route calls.
    return _firestore_client() is not None


def _latest_profile() -> dict | None:
    try:
        db = _firestore_client()
        if db is None:
            rows = _load_local_profiles()
            return rows[0] if rows else None
        docs = (
            db.collection("farm_profiles")
            .order_by("created_at_epoch", direction=firestore.Query.DESCENDING)
            .limit(1)
            .stream()
        )
        for doc in docs:
            row = doc.to_dict() or {}
            row["id"] = doc.id
            if isinstance(row, dict):
                return row
        return None
    except Exception:  # noqa: BLE001
        return None


def _profile_records(limit: int = 25) -> list[dict]:
    safe_limit = max(1, min(limit, 100))
    try:
        db = _firestore_client()
        if db is None:
            return _load_local_profiles()[:safe_limit]
        docs = (
            db.collection("farm_profiles")
            .order_by("created_at_epoch", direction=firestore.Query.DESCENDING)
            .limit(safe_limit)
            .stream()
        )
        rows: list[dict] = []
        for doc in docs:
            payload = doc.to_dict() or {}
            payload["id"] = doc.id
            if isinstance(payload, dict):
                rows.append(payload)
        return rows
    except Exception:  # noqa: BLE001
        return []


def _save_profile(payload: dict) -> dict:
    now = datetime.utcnow()
    profile = {
        "language": str(payload.get("language") or "en-US").strip(),
        "farmer_name": str(payload.get("farmer_name") or "").strip(),
        "total_land_acres": float(payload.get("total_land_acres") or 0),
        "crop_type": str(payload.get("crop_type") or "").strip(),
        "soil_type": str(payload.get("soil_type") or "").strip(),
        "irrigation_method": str(payload.get("irrigation_method") or "").strip(),
        "water_availability": str(payload.get("water_availability") or "").strip(),
        "district_location": str(payload.get("district_location") or "").strip(),
        "firebase_uid": str(payload.get("firebase_uid") or "").strip(),
        "auth_source": str(payload.get("auth_source") or "").strip(),
        "created_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "updated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "created_at_epoch": now.timestamp(),
    }
    db = _firestore_client()
    if db is None:
        profile["storage_mode"] = "local-fallback"
        return _save_local_profile(profile)
    try:
        ref = db.collection("farm_profiles").document()
        profile["id"] = ref.id
        profile["storage_mode"] = "firestore"
        ref.set(profile)
        return profile
    except Exception:
        # If Firestore write fails (rules/permissions/network), keep onboarding functional.
        profile["id"] = profile.get("id") or str(int(time.time() * 1000))
        profile["storage_mode"] = "local-fallback"
        return _save_local_profile(profile)


def _load_local_profiles() -> list[dict]:
    candidates = [FARM_PROFILES_PATH]
    tmp_profiles_path = Path(os.getenv("FARM_PROFILES_TMP_PATH", "/tmp/farm_profiles.json")).resolve()
    if tmp_profiles_path not in candidates:
        candidates.append(tmp_profiles_path)

    for path in candidates:
        if not path.exists():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, list):
                continue
            rows = [item for item in payload if isinstance(item, dict)]
            rows.sort(key=lambda row: _to_float(row.get("created_at_epoch")) or 0.0, reverse=True)
            return rows
        except Exception:  # noqa: BLE001
            continue
    return []


def _save_local_profiles(rows: list[dict]) -> None:
    serialized = json.dumps(rows, indent=2)
    targets = [FARM_PROFILES_PATH, Path(os.getenv("FARM_PROFILES_TMP_PATH", "/tmp/farm_profiles.json")).resolve()]
    last_error: Exception | None = None
    for path in targets:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(serialized, encoding="utf-8")
            return
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            continue
    if last_error is not None:
        raise last_error


def _save_local_profile(profile: dict) -> dict:
    rows = _load_local_profiles()
    local_id = str(int(time.time() * 1000))
    profile["id"] = profile.get("id") or local_id
    rows.insert(0, profile)
    _save_local_profiles(rows)
    return profile


def _default_farm_records() -> dict[str, dict[str, object]]:
    return {
        "2025": {
            "year": 2025,
            "farmer_name": "Varshini Reddy",
            "land_area_acres": 6.2,
            "crop_type": "Paddy",
            "total_yield_tons": 12.5,
            "avg_soil_moisture_pct": 68,
            "soil_health_score": 77,
            "total_rainfall_mm": 950,
            "irrigation_method": "Drip Irrigation",
            "fertilizer_used": "NPK 20-20-20",
            "profit_loss_summary": "Profit of INR 1,85,000 after seasonal costs.",
        },
        "2024": {
            "year": 2024,
            "farmer_name": "Varshini Reddy",
            "land_area_acres": 6.0,
            "crop_type": "Maize",
            "total_yield_tons": 14.2,
            "avg_soil_moisture_pct": 75,
            "soil_health_score": 82,
            "total_rainfall_mm": 1100,
            "irrigation_method": "Sprinkler",
            "fertilizer_used": "Urea + DAP",
            "profit_loss_summary": "Profit of INR 2,10,000 driven by strong yield.",
        },
        "2023": {
            "year": 2023,
            "farmer_name": "Varshini Reddy",
            "land_area_acres": 5.8,
            "crop_type": "Groundnut",
            "total_yield_tons": 10.8,
            "avg_soil_moisture_pct": 62,
            "soil_health_score": 70,
            "total_rainfall_mm": 890,
            "irrigation_method": "Canal + Supplemental Drip",
            "fertilizer_used": "Organic Compost + NPK 10-26-26",
            "profit_loss_summary": "Minor loss of INR 22,000 due to low rainfall period.",
        },
    }


def _load_farm_records() -> dict[str, dict[str, object]]:
    if not FARM_RECORDS_PATH.exists():
        return _default_farm_records()
    try:
        payload = json.loads(FARM_RECORDS_PATH.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            return payload
    except Exception:  # noqa: BLE001
        pass
    return _default_farm_records()


def _farm_record_for_year(year: int) -> dict[str, object] | None:
    records = _load_farm_records()
    item = records.get(str(year))
    if isinstance(item, dict):
        return item
    return None


@app.before_request
def _bootstrap_background_services():
    _start_alert_engine_background()


@app.get("/")
def index():
    force_new = str(request.args.get("new") or "").strip().lower() in {"1", "true", "yes"}
    if force_new:
        return send_from_directory(BASE_DIR, "onboarding.html")
    _init_db()
    if _latest_profile() is None:
        return send_from_directory(BASE_DIR, "onboarding.html")
    return send_from_directory(BASE_DIR, "index.html")


@app.get("/dashboard")
def dashboard_alias():
    force_new = str(request.args.get("new") or "").strip().lower() in {"1", "true", "yes"}
    if force_new:
        return send_from_directory(BASE_DIR, "onboarding.html")
    _init_db()
    if _latest_profile() is None:
        return send_from_directory(BASE_DIR, "onboarding.html")
    return send_from_directory(BASE_DIR, "index.html")


@app.get("/onboarding")
def onboarding_page():
    _init_db()
    return send_from_directory(BASE_DIR, "onboarding.html")


@app.get("/signup")
def signup_page():
    _init_db()
    return send_from_directory(BASE_DIR, "onboarding.html")


@app.get("/records")
def records_page():
    _init_db()
    return send_from_directory(BASE_DIR, "records.html")


@app.get("/records/<int:year>")
def record_detail_page(year: int):
    _init_db()
    return send_from_directory(BASE_DIR, "records.html")


@app.get("/api/farm-profile")
def farm_profile():
    _init_db()
    profile = _latest_profile()
    if profile is None:
        return jsonify({"profile": None, "onboarded": False})
    return jsonify({"profile": profile, "onboarded": True})


@app.get("/api/farm-profile/records")
def farm_profile_records():
    _init_db()
    limit = request.args.get("limit", default=25, type=int)
    return jsonify({"records": _profile_records(limit=limit)})


@app.post("/api/onboarding")
def onboarding_submit():
    body = request.get_json(silent=True) or {}
    required_fields = [
        "farmer_name",
        "total_land_acres",
        "crop_type",
        "soil_type",
        "irrigation_method",
        "water_availability",
        "district_location",
    ]
    missing = [field for field in required_fields if str(body.get(field) or "").strip() == ""]
    if missing:
        return jsonify({"error": f"Missing required fields: {', '.join(missing)}"}), 400

    try:
        total_land = float(body.get("total_land_acres"))
        if total_land <= 0:
            return jsonify({"error": "total_land_acres must be greater than 0"}), 400
    except Exception:  # noqa: BLE001
        return jsonify({"error": "total_land_acres must be a valid number"}), 400

    payload = {
        "language": str(body.get("language") or "en-US").strip(),
        "farmer_name": str(body.get("farmer_name") or "").strip(),
        "total_land_acres": total_land,
        "crop_type": str(body.get("crop_type") or "").strip(),
        "soil_type": str(body.get("soil_type") or "").strip(),
        "irrigation_method": str(body.get("irrigation_method") or "").strip(),
        "water_availability": str(body.get("water_availability") or "").strip(),
        "district_location": str(body.get("district_location") or "").strip(),
        "firebase_uid": str(body.get("firebase_uid") or "").strip(),
        "auth_source": "firebase-anonymous" if str(body.get("firebase_uid") or "").strip() else "none",
    }
    try:
        saved = _save_profile(payload)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": f"Failed to save onboarding profile: {exc}"}), 500
    return jsonify({"message": "Onboarding saved", "profile": saved})


@app.get("/api/farm-records")
def farm_records():
    return jsonify(_load_farm_records())


@app.get("/api/farm-records/<int:year>")
def farm_record_by_year(year: int):
    record = _farm_record_for_year(year)
    if not record:
        return jsonify({"error": f"No farm record found for year {year}"}), 404
    return jsonify(record)


@app.get("/api/farm-records/<int:year>/pdf")
def farm_record_pdf(year: int):
    record = _farm_record_for_year(year)
    if not record:
        return jsonify({"error": f"No farm record found for year {year}"}), 404
    if canvas is None or A4 is None:
        return jsonify({"error": "reportlab is not installed on server."}), 500

    fields = [
        ("Year", record.get("year", year)),
        ("Farmer Name", record.get("farmer_name", "N/A")),
        ("Land Area (acres)", record.get("land_area_acres", "N/A")),
        ("Crop Type", record.get("crop_type", "N/A")),
        ("Total Yield (tons)", record.get("total_yield_tons", "N/A")),
        ("Average Soil Moisture (%)", record.get("avg_soil_moisture_pct", "N/A")),
        ("Soil Health Score", record.get("soil_health_score", "N/A")),
        ("Total Rainfall (mm)", record.get("total_rainfall_mm", "N/A")),
        ("Irrigation Method", record.get("irrigation_method", "N/A")),
        ("Fertilizer Used", record.get("fertilizer_used", "N/A")),
        ("Profit/Loss Summary", record.get("profit_loss_summary", "N/A")),
    ]

    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    page_w, page_h = A4
    y = page_h - 60

    pdf.setFont("Helvetica-Bold", 18)
    pdf.drawString(50, y, f"Farm Report - {year}")
    y -= 18
    pdf.setFont("Helvetica", 10)
    pdf.drawString(50, y, f"Generated on {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
    y -= 28

    for label, value in fields:
        if y < 70:
            pdf.showPage()
            y = page_h - 60
        pdf.setFont("Helvetica-Bold", 11)
        pdf.drawString(50, y, f"{label}:")
        pdf.setFont("Helvetica", 11)
        text = str(value)
        pdf.drawString(215, y, text[:95])
        y -= 22

    pdf.showPage()
    pdf.save()
    buffer.seek(0)
    return send_file(
        buffer,
        mimetype="application/pdf",
        as_attachment=True,
        download_name=f"farm-report-{year}.pdf",
    )


@app.get("/api/push/config")
def push_config():
    cfg = _push_config()
    enabled = all([cfg["apiKey"], cfg["projectId"], cfg["messagingSenderId"], cfg["appId"], cfg["vapidKey"]])
    return jsonify({"enabled": enabled, "config": cfg})


@app.post("/api/push/register-token")
def register_push_token():
    admin_token = request.headers.get("X-Admin-Token", "").strip()
    if not _allow_public_push_bootstrap() and not _is_authorized_admin(admin_token):
        return jsonify({"error": "Unauthorized"}), 401

    body = request.get_json(silent=True) or {}
    token = str(body.get("token") or "").strip()
    if not token:
        return jsonify({"error": "token is required"}), 400

    tokens = _load_push_tokens()
    tokens[token] = {"created_at": datetime.utcnow().isoformat()}
    _save_push_tokens(tokens)
    return jsonify({"message": "Push token registered", "total_tokens": len(tokens)})


@app.post("/api/push/send-test")
def send_test_push():
    admin_token = request.headers.get("X-Admin-Token", "").strip()
    if not _allow_public_push_bootstrap() and not _is_authorized_admin(admin_token):
        return jsonify({"error": "Unauthorized"}), 401

    body = request.get_json(silent=True) or {}
    token = str(body.get("token") or "").strip()
    title = str(body.get("title") or "Agrisense").strip()
    message = str(body.get("message") or "Test push notification").strip()

    ok, reason = _firebase_ready()
    if not ok:
        return jsonify({"error": reason}), 500

    if not token:
        return jsonify({"error": "token is required"}), 400

    try:
        msg = messaging.Message(
            token=token,
            notification=messaging.Notification(title=title, body=message),
            webpush=messaging.WebpushConfig(
                notification=messaging.WebpushNotification(
                    title=title,
                    body=message,
                    icon="/favicon.ico",
                )
            ),
        )
        message_id = messaging.send(msg)
        return jsonify({"message": "Push sent", "message_id": message_id})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": f"Push send failed: {exc}"}), 500


@app.get("/firebase-messaging-sw.js")
def firebase_messaging_sw():
    cfg = _push_config()
    script = f"""
importScripts('https://www.gstatic.com/firebasejs/10.13.2/firebase-app-compat.js');
importScripts('https://www.gstatic.com/firebasejs/10.13.2/firebase-messaging-compat.js');

firebase.initializeApp({json.dumps({k: cfg[k] for k in ['apiKey', 'authDomain', 'projectId', 'storageBucket', 'messagingSenderId', 'appId']})});
const messaging = firebase.messaging();

messaging.onBackgroundMessage((payload) => {{
  const title = payload?.notification?.title || 'Agrisense';
  const options = {{
    body: payload?.notification?.body || 'You have a new update.',
    icon: '/favicon.ico'
  }};
  self.registration.showNotification(title, options);
}});
""".strip()
    return Response(script, mimetype="application/javascript")


def _authorize_sensor_client() -> tuple[bool, object | None]:
    # Sensor token auth intentionally disabled per current deployment requirement.
    return True, None


def _extract_sensor_payload() -> dict | None:
    # Preferred: JSON body from modern clients.
    payload = request.get_json(silent=True)
    if isinstance(payload, dict):
        return payload

    # ESP32 often sends x-www-form-urlencoded or multipart form-data.
    form_payload: dict[str, object] = {}
    for key in ("moisture", "moisture_percent", "moisture_raw", "ph", "temp", "nitrogen", "phosphorus", "potassium", "latitude", "longitude"):
        value = request.form.get(key)
        if value is not None and str(value).strip() != "":
            form_payload[key] = value
    if form_payload:
        return form_payload

    # Simple fallback for ESP/URL-based sends:
    # /update?moisture=70&temp=29&sensor_token=...
    query_payload: dict[str, object] = {}
    for key in ("moisture", "moisture_percent", "moisture_raw", "ph", "temp", "nitrogen", "phosphorus", "potassium", "latitude", "longitude"):
        value = request.args.get(key)
        if value is not None and str(value).strip() != "":
            query_payload[key] = value
    if query_payload:
        return query_payload

    return None


def _raw_to_moisture_percent(raw_value: float) -> float:
    dry = _to_float(os.getenv("MOISTURE_RAW_DRY", "3200")) or 3200.0
    wet = _to_float(os.getenv("MOISTURE_RAW_WET", "1400")) or 1400.0
    if dry == wet:
        dry = 3200.0
        wet = 1400.0
    percent = ((raw_value - dry) / (wet - dry)) * 100.0
    return max(0.0, min(100.0, percent))


@app.route('/update-moisture', methods=['POST'])
def update_moisture():
    global sensor_data
    moisture = request.form.get('moisture')
    if moisture:
        sensor_data["moisture"] = moisture
        print(f"Moisture received: {moisture}%")
    return "OK", 200


@app.route("/update", methods=["GET", "POST"])
def update_data():
    global sensor_data

    authorized, auth_error = _authorize_sensor_client()
    if not authorized:
        return auth_error

    payload = _extract_sensor_payload()
    if not isinstance(payload, dict):
        return jsonify({"error": "Invalid payload. Send JSON, form data, or query params."}), 400

    def _pick_numeric(field: str, fallback: float) -> float:
        parsed = _to_float(payload.get(field, fallback))
        return fallback if parsed is None else parsed

    existing_moisture = _to_float(sensor_data.get("moisture")) or 0.0
    existing_moisture_raw = _to_float(sensor_data.get("moisture_raw"))
    incoming_moisture = _to_float(payload.get("moisture"))
    incoming_moisture_percent = _to_float(payload.get("moisture_percent"))
    incoming_moisture_raw = _to_float(payload.get("moisture_raw"))

    moisture_percent = existing_moisture
    moisture_raw = existing_moisture_raw
    if incoming_moisture_percent is not None:
        moisture_percent = max(0.0, min(100.0, incoming_moisture_percent))
        if incoming_moisture_raw is not None:
            moisture_raw = incoming_moisture_raw
        elif incoming_moisture is not None and incoming_moisture > 100:
            moisture_raw = incoming_moisture
    elif incoming_moisture_raw is not None:
        moisture_raw = incoming_moisture_raw
        moisture_percent = _raw_to_moisture_percent(incoming_moisture_raw)
    elif incoming_moisture is not None:
        if incoming_moisture > 100:
            moisture_raw = incoming_moisture
            moisture_percent = _raw_to_moisture_percent(incoming_moisture)
        else:
            moisture_percent = incoming_moisture

    merged = {
        "moisture": moisture_percent,
        "moisture_percent": moisture_percent,
        "ph": _pick_numeric("ph", _to_float(sensor_data.get("ph")) or 7.0),
        "temp": _pick_numeric("temp", _to_float(sensor_data.get("temp")) or 25.0),
        "nitrogen": _pick_numeric("nitrogen", _to_float(sensor_data.get("nitrogen")) or 50.0),
        "phosphorus": _pick_numeric("phosphorus", _to_float(sensor_data.get("phosphorus")) or 25.0),
        "potassium": _pick_numeric("potassium", _to_float(sensor_data.get("potassium")) or 30.0),
    }
    if moisture_raw is not None:
        merged["moisture_raw"] = moisture_raw
    now = datetime.utcnow()
    merged["updated_at"] = now.strftime("%Y-%m-%d %H:%M:%S")
    merged["updated_at_epoch"] = now.timestamp()

    lat = _to_float(payload.get("latitude"))
    lon = _to_float(payload.get("longitude"))
    if lat is not None and lon is not None:
        merged["latitude"] = lat
        merged["longitude"] = lon
    else:
        if _to_float(sensor_data.get("latitude")) is not None:
            merged["latitude"] = _to_float(sensor_data.get("latitude"))
        if _to_float(sensor_data.get("longitude")) is not None:
            merged["longitude"] = _to_float(sensor_data.get("longitude"))

    sensor_data = merged
    _append_sensor_reading(sensor_data)
    firestore_saved, firestore_error = _save_sensor_reading(sensor_data, payload)
    realtime_saved, realtime_error = _sync_sensor_realtime(sensor_data)
    engine_result = None
    with _alert_engine_lock:
        engine_result = _run_alert_engine_if_due(force=False)

    return jsonify(
        {
            "status": "Data received successfully",
            "data": sensor_data,
            "firestore_saved": firestore_saved,
            "firestore_error": firestore_error,
            "realtime_saved": realtime_saved,
            "realtime_error": realtime_error,
            "alert_engine_ran": engine_result is not None,
            "new_alerts": (engine_result or {}).get("new_alerts", []),
        }
    )


@app.get("/data")
def get_data():
    return jsonify(sensor_data)


def _overall_alert_status(alerts: list[dict]) -> str:
    if any(str(a.get("severity", "")).lower() == "high" for a in alerts):
        return "Critical"
    if any(str(a.get("severity", "")).lower() == "medium" for a in alerts):
        return "Warning"
    return "Safe"


@app.get("/api/alerts/live")
def alerts_live():
    _init_db()
    profile = _latest_profile() or {}
    crop_type = str((profile or {}).get("crop_type") or "default").strip().lower()
    thresholds = _thresholds_for_crop(crop_type)
    snapshot = dict(sensor_data)
    weather_snapshot = _fetch_alert_weather()
    alerts = _evaluate_alerts(snapshot, thresholds, weather_snapshot)
    return jsonify(
        {
            "sensor": snapshot,
            "weather": weather_snapshot,
            "crop_type": crop_type,
            "thresholds": thresholds,
            "alerts": alerts,
            "status": _overall_alert_status(alerts),
            "next_run_seconds": ALERT_ENGINE_INTERVAL_SECONDS,
        }
    )


@app.get("/api/alerts/history")
def alerts_history():
    _init_db()
    limit = request.args.get("limit", default=50, type=int)
    try:
        rows = _alert_history(limit=limit)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": f"Unable to load alert history: {exc}"}), 503
    return jsonify({"history": rows})


@app.post("/api/alerts/run")
def alerts_run():
    admin_token = request.headers.get("X-Admin-Token", "").strip()
    if not _is_authorized_admin(admin_token):
        return jsonify({"error": "Unauthorized"}), 401

    with _alert_engine_lock:
        try:
            result = _run_alert_engine_if_due(force=True)
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": f"Alert engine failed: {exc}"}), 500
    return jsonify({"message": "Alert engine executed", "result": result})


@app.get("/api/alerts/config")
def alerts_config_get():
    crop_type = str(request.args.get("crop_type") or "").strip().lower()
    payload = _load_crop_thresholds()
    if crop_type:
        return jsonify({"crop_type": crop_type, "thresholds": _thresholds_for_crop(crop_type)})
    return jsonify({"thresholds": payload})


@app.post("/api/alerts/config")
def alerts_config_update():
    admin_token = request.headers.get("X-Admin-Token", "").strip()
    if not _is_authorized_admin(admin_token):
        return jsonify({"error": "Unauthorized"}), 401

    body = request.get_json(silent=True) or {}
    crop_type = str(body.get("crop_type") or "").strip().lower()
    thresholds = body.get("thresholds")
    if not crop_type:
        return jsonify({"error": "crop_type is required"}), 400
    if not isinstance(thresholds, dict):
        return jsonify({"error": "thresholds must be an object"}), 400

    merged = _load_crop_thresholds()
    merged[crop_type] = thresholds
    _save_crop_thresholds(merged)
    return jsonify({"message": "Thresholds updated", "crop_type": crop_type, "thresholds": _thresholds_for_crop(crop_type)})


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


def _farm_profile_context(profile: dict | None) -> str:
    if not profile:
        return (
            "No farm profile available. Ask the user to complete onboarding first. "
            "Do not guess farm-specific values."
        )
    return (
        "Farmer profile:\n"
        f"- Farmer Name: {profile.get('farmer_name', 'N/A')}\n"
        f"- Land Area (acres): {profile.get('total_land_acres', 'N/A')}\n"
        f"- Crop Type: {profile.get('crop_type', 'N/A')}\n"
        f"- Soil Type: {profile.get('soil_type', 'N/A')}\n"
        f"- Irrigation Method: {profile.get('irrigation_method', 'N/A')}\n"
        f"- Water Availability: {profile.get('water_availability', 'N/A')}\n"
        f"- District/Location: {profile.get('district_location', 'N/A')}\n"
    )


def _chat_with_openai(question: str, language: str, profile: dict | None) -> tuple[bool, str]:
    openai_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not openai_key:
        return False, "OPENAI_API_KEY is not configured."

    model = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip() or "gpt-4o-mini"
    system_prompt = (
        "You are AgroBot, a practical agriculture advisor. "
        "Always answer using only the farmer profile below. "
        "If a value is missing, explicitly say it is missing and ask for that value. "
        "Give actionable, safe, concise farming advice and keep responses agriculture-focused. "
        f"Respond in {language}.\n\n"
        f"{_farm_profile_context(profile)}"
    )
    try:
        response = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {openai_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": question},
                ],
                "temperature": 0.3,
            },
            timeout=30,
        )
        payload = response.json()
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)

    if not response.ok:
        if isinstance(payload, dict):
            err = payload.get("error") or {}
            if isinstance(err, dict) and err.get("message"):
                return False, str(err.get("message"))
        return False, f"OpenAI request failed with status {response.status_code}."

    choices = payload.get("choices") or []
    if not choices:
        return False, "OpenAI returned no choices."
    message = choices[0].get("message") if isinstance(choices[0], dict) else {}
    content = message.get("content") if isinstance(message, dict) else ""
    reply = str(content or "").strip()
    if not reply:
        return False, "OpenAI returned empty content."
    return True, reply


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


@app.post("/api/chatbot/query")
def chatbot_query():
    _init_db()
    body = request.get_json(silent=True) or {}
    question = str(body.get("question") or "").strip()
    language = str(body.get("language") or "English").strip()
    if not question:
        return jsonify({"error": "question is required"}), 400

    profile = _latest_profile()
    ok, reply = _chat_with_openai(question, language, profile)
    if not ok:
        return jsonify({"error": f"Chatbot unavailable: {reply}"}), 502
    return jsonify({"reply": reply, "profile_used": bool(profile)})


@app.post("/api/chat")
def chat():
    _init_db()
    google_ai_key = os.getenv("GOOGLE_AI_API_KEY", "").strip()
    if not google_ai_key:
        return jsonify({"error": "GOOGLE_AI_API_KEY is not set on server."}), 500

    body = request.get_json(silent=True) or {}
    question = (body.get("question") or "").strip()
    lang_name = (body.get("langName") or "English").strip()
    telugu_style_instruction = (body.get("teluguStyleInstruction") or "").strip()
    location_context = _format_location_context(body.get("location"))
    farm_context = _farm_profile_context(_latest_profile())

    if not question:
        return jsonify({"error": "Question is required."}), 400

    prompt = (
        f"{_system_prompt(lang_name, telugu_style_instruction)}\n"
        f"{farm_context}\n"
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


@app.get("/api/firebase/status")
def firebase_status():
    ok, reason = _firebase_ready()
    using_json = bool(os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON", "").strip())
    using_json_b64 = bool(os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON_B64", "").strip())
    using_file = bool(os.getenv("FIREBASE_SERVICE_ACCOUNT_FILE", "").strip())
    project_id = os.getenv("FIREBASE_PROJECT_ID", "agrisens-fca57").strip() or "agrisens-fca57"
    return jsonify(
        {
            "ready": ok,
            "reason": reason,
            "project_id": project_id,
            "credential_source": (
                "service_account_json"
                if using_json
                else "service_account_json_b64"
                if using_json_b64
                else "service_account_file"
                if using_file
                else "adc_or_none"
            ),
        }
    )


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    debug_enabled = os.getenv("FLASK_DEBUG", "").strip().lower() in {"1", "true", "yes", "on"}
    _start_alert_engine_background()
    app.run(host="0.0.0.0", port=port, debug=debug_enabled)
