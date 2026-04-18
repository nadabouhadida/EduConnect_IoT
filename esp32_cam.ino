#include "esp_camera.h"
#include <WiFi.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>   

// ── USER CONFIGURATION ────────────────────────────────────────────────────────
const char* WIFI_SSID  = "Orange-45E9";
const char* WIFI_PASS  = "08GAL3TR5E3";

// Python server endpoints
const char* SERVER_FRAME  = "http://192.168.1.104:5001/api/frame";        // POST jpeg here
const char* SERVER_STATE  = "http://192.168.1.104:5001/api/camera-state"; // GET when idle

// How often to capture when camera is active (ms)
const unsigned long CAPTURE_INTERVAL = 3000;

// How often to check the server when camera is OFF / idle (ms)
const unsigned long IDLE_CHECK_INTERVAL = 15000;   // ask every 15 s while idle
// ─────────────────────────────────────────────────────────────────────────────

// ── Camera pins (AI-Thinker ESP32-CAM) ───────────────────────────────────────
#define PWDN_GPIO_NUM     32
#define RESET_GPIO_NUM    -1
#define XCLK_GPIO_NUM      0
#define SIOD_GPIO_NUM     26
#define SIOC_GPIO_NUM     27
#define Y9_GPIO_NUM       35
#define Y8_GPIO_NUM       34
#define Y7_GPIO_NUM       39
#define Y6_GPIO_NUM       36
#define Y5_GPIO_NUM       21
#define Y4_GPIO_NUM       19
#define Y3_GPIO_NUM       18
#define Y2_GPIO_NUM        5
#define VSYNC_GPIO_NUM    25
#define HREF_GPIO_NUM     23
#define PCLK_GPIO_NUM     22
#define LED_FLASH          4

// ── Runtime state ─────────────────────────────────────────────────────────────
bool          cameraActive  = false;   // updated from server response
String        currentMode   = "off";   // "scheduled" | "alarm" | "off"
int           shockValue    = 0;
bool          alarmActive   = false;

// ── Helper: blink LED ─────────────────────────────────────────────────────────
void blinkLed(int n, int onMs = 100, int offMs = 100) {
  for (int i = 0; i < n; i++) {
    digitalWrite(LED_FLASH, HIGH); delay(onMs);
    digitalWrite(LED_FLASH, LOW);  delay(offMs);
  }
}

// ── Helper: parse camera_state from a JSON string ────────────────────────────
void parseCameraState(const String& json) {
  // camera_state can be either the root object (from /api/camera-state)
  // or nested under "camera_state" key (from /api/frame response)
  StaticJsonDocument<512> doc;
  if (deserializeJson(doc, json) != DeserializationError::Ok) {
    Serial.println("[JSON] Parse error");
    return;
  }

  // Support both flat response (/api/camera-state) and nested (/api/frame)
  JsonObject state;
  if (doc.containsKey("camera_state")) {
    state = doc["camera_state"].as<JsonObject>();
  } else {
    state = doc.as<JsonObject>();
  }

  bool  prevAlarm  = alarmActive;
  bool  prevActive = cameraActive;

  cameraActive = state["camera_active"] | false;
  currentMode  = state["mode"] | "off";
  shockValue   = state["shock_value"] | 0;
  alarmActive  = state["alarm_active"] | false;

  int remaining = state["alarm_remaining_sec"] | 0;

  Serial.printf("[STATE] camera_active=%s  mode=%s  shock=%d  alarm=%s  remaining=%d s\n",
    cameraActive ? "YES" : "no",
    currentMode.c_str(),
    shockValue,
    alarmActive ? "YES" : "no",
    remaining);

  // LED signal on mode changes
  if (alarmActive && !prevAlarm) {
    Serial.println("[ALARM] 🚨 Alarm activated by server!");
    blinkLed(6, 150, 80);
  } else if (!alarmActive && prevAlarm) {
    Serial.println("[ALARM] ✅ Alarm ended.");
    blinkLed(3, 400, 200);
  } else if (cameraActive && !prevActive) {
    Serial.println("[INFO] Camera activated (scheduled window).");
    blinkLed(2, 200, 100);
  }
}

// ── Helper: GET /api/camera-state (used during idle) ─────────────────────────
void fetchCameraState() {
  if (WiFi.status() != WL_CONNECTED) return;
  HTTPClient http;
  http.begin(SERVER_STATE);
  http.setTimeout(5000);
  int code = http.GET();
  if (code == HTTP_CODE_OK) {
    parseCameraState(http.getString());
  } else {
    Serial.printf("[STATE] GET failed HTTP %d\n", code);
  }
  http.end();
}

// ── setup() ───────────────────────────────────────────────────────────────────
void setup() {
  Serial.begin(115200);
  Serial.println("\n[SmartClass] Booting...");

  pinMode(LED_FLASH, OUTPUT);
  digitalWrite(LED_FLASH, LOW);

  // Camera config
  camera_config_t config;
  config.ledc_channel = LEDC_CHANNEL_0;
  config.ledc_timer   = LEDC_TIMER_0;
  config.pin_d0       = Y2_GPIO_NUM;
  config.pin_d1       = Y3_GPIO_NUM;
  config.pin_d2       = Y4_GPIO_NUM;
  config.pin_d3       = Y5_GPIO_NUM;
  config.pin_d4       = Y6_GPIO_NUM;
  config.pin_d5       = Y7_GPIO_NUM;
  config.pin_d6       = Y8_GPIO_NUM;
  config.pin_d7       = Y9_GPIO_NUM;
  config.pin_xclk     = XCLK_GPIO_NUM;
  config.pin_pclk     = PCLK_GPIO_NUM;
  config.pin_vsync    = VSYNC_GPIO_NUM;
  config.pin_href     = HREF_GPIO_NUM;
  config.pin_sccb_sda = SIOD_GPIO_NUM;
  config.pin_sccb_scl = SIOC_GPIO_NUM;
  config.pin_pwdn     = PWDN_GPIO_NUM;
  config.pin_reset    = RESET_GPIO_NUM;
  config.xclk_freq_hz = 20000000;
  config.pixel_format = PIXFORMAT_JPEG;
  config.frame_size   = FRAMESIZE_VGA;
  config.jpeg_quality = 8;
  config.fb_count     = 1;

  esp_err_t err = esp_camera_init(&config);
  if (err != ESP_OK) {
    Serial.printf("[ERROR] Camera init failed: 0x%x\n", err);
    return;
  }

  // Fix dark image – original sensor tuning preserved
  sensor_t* s = esp_camera_sensor_get();
  s->set_brightness(s, 1);
  s->set_contrast(s, 1);
  s->set_saturation(s, 0);
  s->set_whitebal(s, 1);
  s->set_awb_gain(s, 1);
  s->set_wb_mode(s, 0);
  s->set_exposure_ctrl(s, 1);
  s->set_aec2(s, 1);
  s->set_ae_level(s, 1);
  s->set_gain_ctrl(s, 1);
  s->set_agc_gain(s, 0);
  s->set_gainceiling(s, (gainceiling_t)6);
  s->set_bpc(s, 1);
  s->set_wpc(s, 1);
  s->set_raw_gma(s, 1);
  s->set_lenc(s, 1);

  Serial.println("[INFO] Camera ready.");

  // Warm up camera (discard first frames while exposure adjusts)
  Serial.println("[INFO] Warming up...");
  for (int i = 0; i < 5; i++) {
    camera_fb_t* fb = esp_camera_fb_get();
    if (fb) esp_camera_fb_return(fb);
    delay(200);
  }

  // WiFi
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  Serial.print("[INFO] Connecting WiFi");
  int tries = 0;
  while (WiFi.status() != WL_CONNECTED && tries < 30) {
    delay(500); Serial.print("."); tries++;
  }
  if (WiFi.status() == WL_CONNECTED) {
    Serial.println("\n[INFO] WiFi: " + WiFi.localIP().toString());
  } else {
    Serial.println("\n[ERROR] WiFi failed!");
    return;
  }

  // Initial state check from server
  Serial.println("[INFO] Fetching initial camera state from server...");
  fetchCameraState();
}

// ── loop() ────────────────────────────────────────────────────────────────────
void loop() {

  // WiFi watchdog
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("[WARN] WiFi lost, reconnecting...");
    WiFi.reconnect();
    delay(2000);
    return;
  }

  // ── IDLE mode: camera is OFF ───────────────────────────────────────────────
  if (!cameraActive) {
    Serial.println("[INFO] Camera OFF — checking server state in "
                   + String(IDLE_CHECK_INTERVAL / 1000) + " s...");
    delay(IDLE_CHECK_INTERVAL);
    fetchCameraState();
    return;
  }

  // ── ACTIVE mode: capture and send frame ───────────────────────────────────
  camera_fb_t* fb = esp_camera_fb_get();
  if (!fb) {
    Serial.println("[ERROR] Capture failed");
    delay(CAPTURE_INTERVAL);
    return;
  }

  Serial.printf("[INFO] Captured %zu bytes  mode=%s\n", fb->len, currentMode.c_str());

  HTTPClient http;
  http.begin(SERVER_FRAME);
  http.addHeader("Content-Type", "image/jpeg");
  http.setTimeout(10000);

  int httpCode = http.POST(fb->buf, fb->len);

  if (httpCode == HTTP_CODE_OK) {
    String resp = http.getString();
    Serial.println("[RESPONSE] " + resp.substring(0, 120) + "...");

    // Parse camera_state from server response
    parseCameraState(resp);

    // LED ack: different pattern for alarm vs scheduled
    if (alarmActive) {
      blinkLed(2, 50, 50);   // rapid double-blink in alarm mode
    } else {
      digitalWrite(LED_FLASH, HIGH);
      delay(100);
      digitalWrite(LED_FLASH, LOW);
    }

  } else {
    Serial.printf("[ERROR] HTTP %d\n", httpCode);
  }

  http.end();
  esp_camera_fb_return(fb);
  delay(CAPTURE_INTERVAL);
}
