# AgroBot Mobile App

This folder contains a React Native app (Expo) version of your AgroBot website.

## 1) Start backend first

From project root (`sciencefair`):

```powershell
pip install -r requirements.txt
python app.py
```

Your Flask server should run on `http://localhost:5000`.

## 2) Run mobile app

```powershell
cd mobile-app
npm install
npm run start
```

Then open in:
- Android emulator (`a` in Expo terminal)
- iOS simulator (`i` in Expo terminal, macOS only)
- Expo Go app (scan QR)

## API base URL notes

`App.js` currently uses:

- `http://10.0.2.2:5000` for Android emulator

If you run on a real phone, replace with your computer's LAN IP, for example:

- `http://192.168.1.15:5000`

## Features included

- Live soil cards (`/data`, auto-refresh every 5s)
- Chat to AgroBot (`/api/chat`)
- Language selector + Telugu dialect selector
- Quick question chips
- Mobile-first layout
- Firebase push notifications (FCM token registration + server send API)

## Firebase push setup

1. Install new client dependencies:

```powershell
cd mobile-app
npx expo install expo-notifications expo-device
```

2. Add your Firebase service account JSON file in the project root (example: `firebase-service-account.json`).
3. Set backend env in root `.env`:

```env
FIREBASE_SERVICE_ACCOUNT_FILE=firebase-service-account.json
```

4. Start backend and app again.
5. Use a physical Android/iOS device and allow notification permission.

### Send a test notification

Use this request against your Flask backend:

```powershell
Invoke-RestMethod -Method POST -Uri http://127.0.0.1:5000/api/push/send -ContentType "application/json" -Body '{"title":"AgroBot Alert","body":"Irrigate field 2 this evening."}'
```

### Automatic use-case alerts

When your sensor device posts data to `POST /update`, backend rules auto-trigger push notifications for:

- Low soil moisture (`moisture < 35`)
- High temperature (`temp > 35`)
- pH out of range (`ph < 5.8` or `ph > 7.8`)
- Low nitrogen (`nitrogen < 40`)

Alerts are de-duplicated and repeated only after `ALERT_COOLDOWN_SECONDS` (default `1800`).

### Rain warning + red buzzer

Backend also checks rain risk from OpenWeather and triggers a red warning if rain is about to start.

Set these in root `.env`:

```env
WEATHER_API_KEY=your_openweather_api_key
WEATHER_LAT=17.3850
WEATHER_LON=78.4867
WEATHER_CACHE_SECONDS=300
RAIN_POP_THRESHOLD=0.5
RAIN_LOOKAHEAD_HOURS=2
```

When warning is active, `POST /update` response includes:

- Push notification: `AgroBot RED WEATHER WARNING`
- Device command in `device_commands`:
  - `type: buzzer`
  - `mode: red_warning`
  - `state: on`
  - `pattern: rapid_beep`

Your IoT firmware should read `device_commands` from `/update` response and turn on the red buzzer accordingly.
