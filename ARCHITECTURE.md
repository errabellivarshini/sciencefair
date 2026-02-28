# Agrisense Multilingual Onboarding + Profile-Aware Chatbot

## Project Structure

- `app.py`
  - Flask routes (onboarding, profile APIs, weather, chatbot, push, sensor data)
  - MySQL persistence for farm profile records and alerts
  - OpenAI context-injected chatbot route
- `onboarding.html`
  - React (CDN) onboarding app
  - Language selection + voice guided stepper
  - Speech Recognition + Text-to-Speech + dropdown/manual input
- `index.html`
  - Main dashboard + chat UI
  - Profile summary + records cards loaded from backend APIs
- `records.html`
  - Year records analytics + modal report details + PDF download trigger
- `data/farm_records.json`
  - Year-wise sample record data
- `requirements.txt`
  - Flask + ReportLab + existing dependencies

## Database Model

MySQL database: `agrisense`

Table: `farm_profiles`

- `id` (INTEGER PRIMARY KEY AUTOINCREMENT)
- `language` (TEXT)
- `farmer_name` (TEXT)
- `total_land_acres` (REAL)
- `crop_type` (TEXT)
- `soil_type` (TEXT)
- `irrigation_method` (TEXT)
- `water_availability` (TEXT)
- `district_location` (TEXT)
- `created_at` (TEXT)
- `updated_at` (TEXT)

## API Routes

- `GET /onboarding` -> onboarding UI
- `POST /api/onboarding` -> save onboarding profile
- `GET /api/farm-profile` -> latest profile
- `GET /api/farm-profile/records?limit=6` -> profile history cards
- `POST /api/chatbot/query` -> OpenAI chat with farm-profile context injection
- Existing routes remain available (`/api/chat`, `/api/weather`, `/records`, etc.)

## Chatbot Context Injection

The backend builds a strict profile context block:

- Farmer Name
- Land Area
- Crop Type
- Soil Type
- Irrigation Method
- Water Availability
- District/Location

This profile block is injected into the system message before sending to OpenAI.
If no profile exists, the model is instructed to ask for onboarding instead of guessing.
