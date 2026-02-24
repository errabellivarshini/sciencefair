import React, { useEffect, useMemo, useRef, useState } from "react";
import {
  ActivityIndicator,
  Alert,
  Platform,
  Pressable,
  SafeAreaView,
  ScrollView,
  StatusBar,
  StyleSheet,
  Text,
  TextInput,
  View,
} from "react-native";
import * as Device from "expo-device";
import * as Notifications from "expo-notifications";
import * as Speech from "expo-speech";

const DEFAULT_BASE_URL =
  (process.env.EXPO_PUBLIC_API_BASE_URL || "").trim() ||
  (Platform.OS === "android" ? "http://10.0.2.2:5000" : "http://127.0.0.1:5000");

const LANG_OPTIONS = [
  { value: "en-US", label: "English" },
  { value: "hi-IN", label: "Hindi" },
  { value: "te-IN", label: "Telugu" },
  { value: "ta-IN", label: "Tamil" },
  { value: "kn-IN", label: "Kannada" },
];

const TELUGU_STYLES = [
  { value: "standard", label: "Standard (easy)" },
  { value: "telangana", label: "Telangana slang" },
  { value: "andhra", label: "Andhra slang" },
  { value: "rayalaseema", label: "Rayalaseema slang" },
];

const QUICK_QUESTIONS = ["What crop should I grow?", "How is my soil health?", "When will it rain?"];

function getTeluguStyleInstruction(langValue, styleValue) {
  if (langValue !== "te-IN") return "";
  if (styleValue === "telangana") {
    return "Use Telangana Telugu slang naturally, but keep wording easy to understand.";
  }
  if (styleValue === "andhra") {
    return "Use Andhra Telugu slang naturally, but keep wording easy to understand.";
  }
  if (styleValue === "rayalaseema") {
    return "Use Rayalaseema Telugu slang naturally, but keep wording easy to understand.";
  }
  return "Use clear, simple standard Telugu. Avoid heavy regional slang and difficult words.";
}

function SensorCard({ label, value, hint }) {
  return (
    <View style={styles.sensorCard}>
      <Text style={styles.sensorLabel}>{label}</Text>
      <Text style={styles.sensorValue}>{value}</Text>
      <Text style={styles.sensorHint}>{hint}</Text>
    </View>
  );
}

function chipStyle(selected) {
  return [styles.chip, selected ? styles.chipSelected : null];
}

async function parseJsonSafe(response) {
  const raw = await response.text();
  if (!raw) return {};
  try {
    return JSON.parse(raw);
  } catch {
    return { raw };
  }
}

Notifications.setNotificationHandler({
  handleNotification: async () => ({
    shouldPlaySound: true,
    shouldSetBadge: false,
    shouldShowBanner: true,
    shouldShowList: true,
  }),
});

export default function App() {
  const voiceRef = useRef(null);
  const notificationListener = useRef(null);
  const responseListener = useRef(null);
  const [messages, setMessages] = useState([
    {
      id: "welcome",
      role: "bot",
      text: "Hello! I am AgroBot. Ask about crops, soil, and weather.",
    },
  ]);
  const [question, setQuestion] = useState("");
  const [sending, setSending] = useState(false);
  const [listening, setListening] = useState(false);
  const [lang, setLang] = useState("en-US");
  const [teluguStyle, setTeluguStyle] = useState("standard");
  const [sensorData, setSensorData] = useState({
    moisture: 72,
    ph: 6.8,
    temp: 24,
    nitrogen: 55,
  });
  const [baseUrl, setBaseUrl] = useState(DEFAULT_BASE_URL);
  const [urlDraft, setUrlDraft] = useState(DEFAULT_BASE_URL);
  const [lastNetworkError, setLastNetworkError] = useState("");
  const [voiceInputAvailable, setVoiceInputAvailable] = useState(false);
  const [pushStatus, setPushStatus] = useState("Push notifications not configured yet.");

  const CHAT_API_URL = useMemo(() => `${baseUrl.replace(/\/+$/, "")}/api/chat`, [baseUrl]);
  const SENSOR_API_URL = useMemo(() => `${baseUrl.replace(/\/+$/, "")}/data`, [baseUrl]);

  const langName = useMemo(() => {
    const match = LANG_OPTIONS.find((item) => item.value === lang);
    return match ? match.label : "English";
  }, [lang]);

  useEffect(() => {
    let mounted = true;

    try {
      // Optional native speech-to-text module. Works in dev build/bare app.
      const maybeVoice = require("@react-native-voice/voice");
      const Voice = maybeVoice?.default || maybeVoice;
      if (Voice && mounted) {
        voiceRef.current = Voice;
        Voice.onSpeechResults = (event) => {
          const transcript = event?.value?.[0]?.trim();
          if (transcript) {
            setQuestion(transcript);
            ask(transcript);
          }
        };
        Voice.onSpeechError = () => {
          setListening(false);
          setMessages((prev) => [
            ...prev,
            {
              id: `${Date.now()}-ve`,
              role: "bot",
              text: "I could not capture your voice clearly. Please try again or type your question.",
            },
          ]);
        };
        Voice.onSpeechEnd = () => setListening(false);
        setVoiceInputAvailable(true);
      }
    } catch {
      setVoiceInputAvailable(false);
    }

    return () => {
      mounted = false;
      Speech.stop();
      const Voice = voiceRef.current;
      if (Voice) {
        try {
          Voice.destroy().then(Voice.removeAllListeners);
        } catch {
          // No-op cleanup fallback.
        }
      }
    };
  }, []);

  useEffect(() => {
    let mounted = true;

    async function setupPushNotifications() {
      if (!Device.isDevice) {
        if (mounted) {
          setPushStatus("Push works on physical device. Emulator/simulator may not receive FCM.");
        }
        return;
      }

      try {
        if (Platform.OS === "android") {
          await Notifications.setNotificationChannelAsync("default", {
            name: "default",
            importance: Notifications.AndroidImportance.MAX,
            vibrationPattern: [0, 250, 250, 250],
            lightColor: "#16a34a",
          });
        }

        const existingPermission = await Notifications.getPermissionsAsync();
        let finalStatus = existingPermission.status;

        if (finalStatus !== "granted") {
          const requestedPermission = await Notifications.requestPermissionsAsync();
          finalStatus = requestedPermission.status;
        }

        if (finalStatus !== "granted") {
          if (mounted) setPushStatus("Notification permission denied.");
          return;
        }

        const nativeToken = await Notifications.getDevicePushTokenAsync();
        const token = String(nativeToken?.data || "").trim();
        if (!token) {
          if (mounted) setPushStatus("Could not fetch FCM token.");
          return;
        }

        const registerResponse = await fetch(`${baseUrl.replace(/\/+$/, "")}/api/push/register`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            token,
            platform: Platform.OS,
          }),
        });

        if (!registerResponse.ok) {
          const body = await registerResponse.json().catch(() => ({}));
          const message = body?.error || "Token registration failed.";
          throw new Error(message);
        }

        if (mounted) setPushStatus("Push notifications enabled.");
      } catch (error) {
        if (mounted) setPushStatus(`Push setup failed: ${error.message}`);
      }
    }

    setupPushNotifications();

    notificationListener.current = Notifications.addNotificationReceivedListener(() => {
      setPushStatus("Push received.");
    });
    responseListener.current = Notifications.addNotificationResponseReceivedListener(() => {
      setPushStatus("Push opened.");
    });

    return () => {
      mounted = false;
      if (notificationListener.current) {
        Notifications.removeNotificationSubscription(notificationListener.current);
      }
      if (responseListener.current) {
        Notifications.removeNotificationSubscription(responseListener.current);
      }
    };
  }, [baseUrl]);

  useEffect(() => {
    fetchSensors();
    const timer = setInterval(fetchSensors, 5000);
    return () => clearInterval(timer);
  }, [SENSOR_API_URL]);

  async function fetchSensors() {
    try {
      const response = await fetch(SENSOR_API_URL);
      if (!response.ok) throw new Error("Sensor feed unavailable");
      const data = await response.json();
      setSensorData((prev) => ({
        moisture: Number(data.moisture ?? prev.moisture),
        ph: Number(data.ph ?? prev.ph),
        temp: Number(data.temp ?? prev.temp),
        nitrogen: Number(data.nitrogen ?? prev.nitrogen),
      }));
      setLastNetworkError("");
    } catch {
      setSensorData((prev) => ({
        moisture: 68 + Math.random() * 8,
        ph: 6.5 + Math.random() * 0.5,
        temp: 22 + Math.random() * 4,
        nitrogen: 50 + Math.random() * 10,
      }));
      setLastNetworkError(`Cannot reach ${SENSOR_API_URL}`);
    }
  }

  async function ask(input) {
    const trimmed = input.trim();
    if (!trimmed || sending) return;

    const userMessage = { id: `${Date.now()}-u`, role: "user", text: trimmed };
    setMessages((prev) => [...prev, userMessage]);
    setQuestion("");
    setSending(true);

    try {
      const response = await fetch(CHAT_API_URL, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          question: trimmed,
          langName,
          teluguStyle,
          teluguStyleInstruction: getTeluguStyleInstruction(lang, teluguStyle),
        }),
      });

      const data = await parseJsonSafe(response);
      if (!response.ok) {
        const fallback = typeof data?.raw === "string" ? data.raw.slice(0, 180) : "";
        throw new Error(data?.error || fallback || `Server error (${response.status})`);
      }

      const reply = data?.reply || "Empty reply";
      setLastNetworkError("");
      setMessages((prev) => [...prev, { id: `${Date.now()}-b`, role: "bot", text: reply }]);

      if (reply && reply !== "Empty reply") {
        Speech.stop();
        Speech.speak(reply, {
          language: lang,
          pitch: 1,
          rate: 0.95,
        });
      }
    } catch (error) {
      setLastNetworkError(`Cannot reach ${CHAT_API_URL}`);
      setMessages((prev) => [
        ...prev,
        {
          id: `${Date.now()}-e`,
          role: "bot",
          text: `Unable to reach server (${CHAT_API_URL}): ${error.message}`,
        },
      ]);
    } finally {
      setSending(false);
    }
  }

  async function startVoiceInput() {
    if (!voiceInputAvailable) {
      Alert.alert(
        "Voice Input Not Available",
        "Install @react-native-voice/voice and use a development build for Android/iOS voice input. Text-to-speech is enabled already."
      );
      return;
    }

    const Voice = voiceRef.current;
    if (!Voice) return;

    try {
      if (listening) {
        await Voice.stop();
        setListening(false);
      } else {
        await Voice.start(lang);
        setListening(true);
      }
    } catch {
      setListening(false);
      Alert.alert("Microphone Error", "Unable to start voice input. Check mic permission and try again.");
    }
  }

  const moistureText = `${sensorData.moisture.toFixed(0)}%`;
  const phText = sensorData.ph.toFixed(1);
  const tempText = `${sensorData.temp.toFixed(0)}C`;
  const nitrogenText = sensorData.nitrogen > 60 ? "High" : sensorData.nitrogen > 45 ? "Med" : "Low";

  return (
    <SafeAreaView style={styles.safeArea}>
      <StatusBar barStyle="dark-content" />
      <ScrollView style={styles.screen} contentContainerStyle={styles.content}>
        <Text style={styles.title}>AgroBot Mobile</Text>
        <Text style={styles.subtitle}>AI field advisor for your farm</Text>

        <View style={styles.card}>
          <Text style={styles.sectionTitle}>Server Connection</Text>
          <Text style={styles.sectionLabel}>Backend URL</Text>
          <View style={styles.inputRow}>
            <TextInput
              value={urlDraft}
              onChangeText={setUrlDraft}
              placeholder="http://192.168.1.15:5000"
              placeholderTextColor="#6b7280"
              style={styles.input}
              autoCapitalize="none"
              autoCorrect={false}
            />
            <Pressable onPress={() => setBaseUrl(urlDraft.trim() || DEFAULT_BASE_URL)} style={styles.sendBtn}>
              <Text style={styles.sendBtnText}>Apply</Text>
            </Pressable>
          </View>
          <Text style={styles.connectionText}>Using: {baseUrl}</Text>
          <Text style={styles.connectionText}>{pushStatus}</Text>
          {lastNetworkError ? <Text style={styles.errorText}>{lastNetworkError}</Text> : null}
        </View>

        <View style={styles.card}>
          <Text style={styles.sectionTitle}>Live Field Conditions</Text>
          <View style={styles.sensorGrid}>
            <SensorCard label="Moisture" value={moistureText} hint="Optimal level" />
            <SensorCard label="pH" value={phText} hint="Near neutral" />
            <SensorCard label="Temp" value={tempText} hint="Current" />
            <SensorCard label="Nitrogen" value={nitrogenText} hint="N status" />
          </View>
        </View>

        <View style={styles.card}>
          <Text style={styles.sectionTitle}>Language</Text>
          <ScrollView horizontal showsHorizontalScrollIndicator={false} contentContainerStyle={styles.rowWrap}>
            {LANG_OPTIONS.map((item) => (
              <Pressable key={item.value} style={chipStyle(lang === item.value)} onPress={() => setLang(item.value)}>
                <Text style={styles.chipText}>{item.label}</Text>
              </Pressable>
            ))}
          </ScrollView>

          {lang === "te-IN" ? (
            <>
              <Text style={styles.sectionLabel}>Telugu style</Text>
              <ScrollView horizontal showsHorizontalScrollIndicator={false} contentContainerStyle={styles.rowWrap}>
                {TELUGU_STYLES.map((item) => (
                  <Pressable
                    key={item.value}
                    style={chipStyle(teluguStyle === item.value)}
                    onPress={() => setTeluguStyle(item.value)}
                  >
                    <Text style={styles.chipText}>{item.label}</Text>
                  </Pressable>
                ))}
              </ScrollView>
            </>
          ) : null}
        </View>

        <View style={styles.card}>
          <Text style={styles.sectionTitle}>Chat</Text>

          <ScrollView style={styles.chatBox} contentContainerStyle={styles.chatContent}>
            {messages.map((msg) => (
              <View
                key={msg.id}
                style={[styles.messageBubble, msg.role === "user" ? styles.userBubble : styles.botBubble]}
              >
                <Text style={msg.role === "user" ? styles.userText : styles.botText}>{msg.text}</Text>
              </View>
            ))}
            {sending ? (
              <View style={[styles.messageBubble, styles.botBubble, styles.loaderRow]}>
                <ActivityIndicator size="small" color="#2f855a" />
                <Text style={styles.botText}>Thinking...</Text>
              </View>
            ) : null}
          </ScrollView>

          <ScrollView horizontal showsHorizontalScrollIndicator={false} contentContainerStyle={styles.rowWrap}>
            {QUICK_QUESTIONS.map((q) => (
              <Pressable key={q} style={styles.quickBtn} onPress={() => ask(q)}>
                <Text style={styles.quickBtnText}>{q}</Text>
              </Pressable>
            ))}
          </ScrollView>

          <View style={styles.inputRow}>
            <Pressable
              onPress={startVoiceInput}
              style={[styles.micBtn, listening ? styles.micBtnActive : null]}
              disabled={sending}
            >
              <Text style={styles.micBtnText}>{listening ? "Stop" : "Mic"}</Text>
            </Pressable>
            <TextInput
              value={question}
              onChangeText={setQuestion}
              placeholder="Ask about crops, soil, weather..."
              placeholderTextColor="#6b7280"
              style={styles.input}
              editable={!sending}
            />
            <Pressable
              onPress={() => ask(question)}
              style={[styles.sendBtn, sending ? styles.sendBtnDisabled : null]}
              disabled={sending}
            >
              <Text style={styles.sendBtnText}>Send</Text>
            </Pressable>
          </View>
        </View>
      </ScrollView>
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  safeArea: {
    flex: 1,
    backgroundColor: "#f3f9f4",
  },
  screen: {
    flex: 1,
  },
  content: {
    padding: 16,
    gap: 14,
  },
  title: {
    fontSize: 28,
    fontWeight: "800",
    color: "#1f2937",
  },
  subtitle: {
    marginTop: 2,
    fontSize: 14,
    color: "#4b5563",
    marginBottom: 4,
  },
  card: {
    backgroundColor: "#ffffff",
    borderRadius: 18,
    padding: 14,
    borderWidth: 1,
    borderColor: "#d1d5db",
    gap: 10,
  },
  sectionTitle: {
    fontSize: 18,
    fontWeight: "700",
    color: "#111827",
  },
  sectionLabel: {
    marginTop: 4,
    fontSize: 13,
    color: "#374151",
    fontWeight: "600",
  },
  sensorGrid: {
    flexDirection: "row",
    flexWrap: "wrap",
    gap: 10,
  },
  sensorCard: {
    width: "48%",
    backgroundColor: "#f8fafc",
    borderWidth: 1,
    borderColor: "#e5e7eb",
    borderRadius: 12,
    paddingVertical: 10,
    paddingHorizontal: 12,
    gap: 2,
  },
  sensorLabel: {
    fontSize: 13,
    color: "#4b5563",
    fontWeight: "600",
  },
  sensorValue: {
    fontSize: 22,
    fontWeight: "800",
    color: "#14532d",
  },
  sensorHint: {
    fontSize: 12,
    color: "#6b7280",
  },
  rowWrap: {
    gap: 8,
    paddingVertical: 2,
  },
  chip: {
    paddingVertical: 7,
    paddingHorizontal: 10,
    borderRadius: 999,
    borderWidth: 1,
    borderColor: "#cbd5e1",
    backgroundColor: "#f8fafc",
  },
  chipSelected: {
    borderColor: "#16a34a",
    backgroundColor: "#dcfce7",
  },
  chipText: {
    fontSize: 12,
    color: "#1f2937",
    fontWeight: "600",
  },
  chatBox: {
    maxHeight: 300,
    borderRadius: 12,
    borderWidth: 1,
    borderColor: "#e5e7eb",
    backgroundColor: "#f9fafb",
  },
  chatContent: {
    gap: 8,
    padding: 10,
  },
  messageBubble: {
    maxWidth: "90%",
    borderRadius: 12,
    paddingVertical: 8,
    paddingHorizontal: 10,
  },
  userBubble: {
    alignSelf: "flex-end",
    backgroundColor: "#16a34a",
  },
  botBubble: {
    alignSelf: "flex-start",
    backgroundColor: "#e5e7eb",
  },
  userText: {
    color: "#ffffff",
    fontSize: 14,
  },
  botText: {
    color: "#111827",
    fontSize: 14,
  },
  quickBtn: {
    paddingVertical: 7,
    paddingHorizontal: 10,
    borderRadius: 10,
    backgroundColor: "#ecfdf5",
    borderWidth: 1,
    borderColor: "#86efac",
  },
  quickBtnText: {
    fontSize: 12,
    color: "#166534",
    fontWeight: "600",
  },
  inputRow: {
    flexDirection: "row",
    gap: 8,
    marginTop: 4,
  },
  input: {
    flex: 1,
    borderWidth: 1,
    borderColor: "#d1d5db",
    borderRadius: 10,
    backgroundColor: "#ffffff",
    paddingHorizontal: 10,
    paddingVertical: 9,
    color: "#111827",
  },
  sendBtn: {
    backgroundColor: "#16a34a",
    borderRadius: 10,
    paddingHorizontal: 14,
    justifyContent: "center",
  },
  sendBtnDisabled: {
    opacity: 0.55,
  },
  sendBtnText: {
    color: "#ffffff",
    fontWeight: "700",
  },
  loaderRow: {
    flexDirection: "row",
    alignItems: "center",
    gap: 8,
  },
  connectionText: {
    fontSize: 12,
    color: "#374151",
  },
  errorText: {
    fontSize: 12,
    color: "#b91c1c",
    fontWeight: "600",
  },
  micBtn: {
    backgroundColor: "#ecfdf5",
    borderRadius: 10,
    paddingHorizontal: 12,
    justifyContent: "center",
    borderWidth: 1,
    borderColor: "#86efac",
  },
  micBtnActive: {
    backgroundColor: "#fee2e2",
    borderColor: "#fca5a5",
  },
  micBtnText: {
    color: "#065f46",
    fontWeight: "700",
  },
});
