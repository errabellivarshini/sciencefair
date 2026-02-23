import os
from pathlib import Path

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, request, send_from_directory


load_dotenv()

app = Flask(__name__)

BASE_DIR = Path(__file__).resolve().parent

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
        f"The user is asking in {lang_name}. Please reply in {lang_name}. "
        f"{telugu_style_instruction}"
    ).strip()


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


@app.post("/api/chat")
def chat():
    google_ai_key = os.getenv("GOOGLE_AI_API_KEY", "").strip()
    if not google_ai_key:
        return jsonify({"error": "GOOGLE_AI_API_KEY is not set on server."}), 500

    body = request.get_json(silent=True) or {}
    question = (body.get("question") or "").strip()
    lang_name = (body.get("langName") or "English").strip()
    telugu_style_instruction = (body.get("teluguStyleInstruction") or "").strip()

    if not question:
        return jsonify({"error": "Question is required."}), 400

    prompt = (
        f"{_system_prompt(lang_name, telugu_style_instruction)}\n\n"
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
