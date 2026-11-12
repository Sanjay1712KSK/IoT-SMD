/*
  Smart Door Monitor - ESP32-CAM (AI Thinker, OV2640): camera + WiFi ONLY, no decisions.

  1. MJPEG stream:   http://<ip>:81/stream      (the laptop backend reads this)
     Single photo:   http://<ip>/capture        Status page: http://<ip>/
  2. Serial relay (UART0 = U0R/U0T pins, 9600 baud, newline-terminated):
        Arduino -> "T,<cm>"  -> HTTP GET <BACKEND_URL>/trigger?d=<cm>&token=<API_TOKEN>
        reply line -> Arduino  ("KNOWN:<name>", "UNKNOWN", "NOFACE", or "ERR" if anything fails)
     After WiFi connects it also sends "IP:<address>" once, so the Arduino LCD can show the IP.

  Because UART0 is the Arduino link, NO debug output is printed (it would confuse the Arduino).

  Red LED (GPIO33, on the back) status:
     fast blink         = connecting to WiFi
     3 long blinks      = WiFi connected, stream running
     slow blink (1 Hz)  = WiFi lost, reconnecting
     rapid double blink = camera init failed (check ribbon cable / power) -> reboots after 10 s
     short flicker      = relayed a trigger

  Based on Espressif's CameraWebServer example, trimmed to what we need.
  Board settings: see docs/wiring.md (AI Thinker ESP32-CAM, PSRAM enabled, Core Debug Level: None).
  Works with ESP32 Arduino core 2.0.x and 3.x.
*/
#include "esp_camera.h"
#include "esp_http_server.h"
#include "esp_log.h"
#include "driver/gpio.h"
#include "soc/soc.h"
#include "soc/rtc_cntl_reg.h"
#include <WiFi.h>
#include <WiFiMulti.h>
#include "lwip/sockets.h"
#include <HTTPClient.h>
#include <ESPmDNS.h>
#include "secrets.h"   // copy secrets.h.example -> secrets.h and fill in (not committed to git)

// ---------------- AI Thinker ESP32-CAM pin map ----------------
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
#define LED_PIN           33   // small red LED, active LOW

// DEBUG_SERIAL 1 = troubleshooting build: Serial at 115200 with step-by-step messages and the reason for the
// last reset. NOT for normal use (the Arduino link needs the quiet 9600 baud mode). `python tools/flash.py esp32 --debug`
#ifndef DEBUG_SERIAL
#define DEBUG_SERIAL 0
#endif
// Status lines ("LOG:...") are printed in BOTH modes. The Arduino ignores any line it does not know,
// so they are harmless on the 9600 link and make problems visible: python tools/flash.py monitor --port COMx
#define DLOG(...) do { Serial.print("LOG:"); Serial.printf(__VA_ARGS__); Serial.flush(); } while (0)

// Weak 5 V supplies (e.g. powered through the Arduino + breadboard) dip when the camera/WiFi start, which
// trips the brownout detector and reboots the board in a loop. 1 = ignore those dips (common ESP32-CAM fix).
#ifndef DISABLE_BROWNOUT
#define DISABLE_BROWNOUT 1
#endif

#ifndef BACKEND_PORT
#define BACKEND_PORT 8000
#endif

#ifndef MDNS_NAME
#define MDNS_NAME "doorcam"     // -> http://doorcam.local:81/stream
#endif

static const unsigned long HTTP_TIMEOUT_MS = 6000;   // Arduino waits 10 s, so stay well below that

// The backend is the client that watches /stream, so we remember its IP and send triggers there.
// This means the laptop's IP never has to be typed in. BACKEND_URL is only the fallback.
volatile uint32_t streamClientIp = 0;
WiFiMulti wifiMulti;

httpd_handle_t streamHttpd = NULL;
httpd_handle_t webHttpd = NULL;

// ---------------------------------------------------------------- LED helpers
void led(bool on) { digitalWrite(LED_PIN, on ? LOW : HIGH); }
void blink(int times, int onMs, int offMs) {
  for (int i = 0; i < times; i++) { led(true); delay(onMs); led(false); delay(offMs); }
}

// ---------------------------------------------------------------- camera
bool initCamera() {
  camera_config_t c = {};
  c.ledc_channel = LEDC_CHANNEL_0;
  c.ledc_timer = LEDC_TIMER_0;
  c.pin_d0 = Y2_GPIO_NUM;  c.pin_d1 = Y3_GPIO_NUM;  c.pin_d2 = Y4_GPIO_NUM;  c.pin_d3 = Y5_GPIO_NUM;
  c.pin_d4 = Y6_GPIO_NUM;  c.pin_d5 = Y7_GPIO_NUM;  c.pin_d6 = Y8_GPIO_NUM;  c.pin_d7 = Y9_GPIO_NUM;
  c.pin_xclk = XCLK_GPIO_NUM;
  c.pin_pclk = PCLK_GPIO_NUM;
  c.pin_vsync = VSYNC_GPIO_NUM;
  c.pin_href = HREF_GPIO_NUM;
  c.pin_sccb_sda = SIOD_GPIO_NUM;
  c.pin_sccb_scl = SIOC_GPIO_NUM;
  c.pin_pwdn = PWDN_GPIO_NUM;
  c.pin_reset = RESET_GPIO_NUM;
  c.xclk_freq_hz = 20000000;
  c.pixel_format = PIXFORMAT_JPEG;        // the sensor compresses for us -> cheap to stream
  c.grab_mode = CAMERA_GRAB_LATEST;       // always send the newest frame (low latency)
  if (psramFound()) {
    c.frame_size = FRAMESIZE_VGA;         // 640x480: good balance of face size vs. WiFi bandwidth
    c.jpeg_quality = 12;                  // 0-63, lower = better quality
    c.fb_count = 2;
    c.fb_location = CAMERA_FB_IN_PSRAM;
  } else {
    c.frame_size = FRAMESIZE_QVGA;
    c.jpeg_quality = 15;
    c.fb_count = 1;
    c.fb_location = CAMERA_FB_IN_DRAM;
  }
  if (esp_camera_init(&c) != ESP_OK) return false;

  sensor_t* s = esp_camera_sensor_get();
  // Uncomment if the image is upside down / mirrored for your mounting:
  // s->set_vflip(s, 1);
  // s->set_hmirror(s, 1);
  s->set_brightness(s, 1);
  return true;
}

// ---------------------------------------------------------------- HTTP handlers
#define PART_BOUNDARY "123456789000000000000987654321"
static const char* STREAM_CONTENT_TYPE = "multipart/x-mixed-replace;boundary=" PART_BOUNDARY;
static const char* STREAM_BOUNDARY = "\r\n--" PART_BOUNDARY "\r\n";
static const char* STREAM_PART = "Content-Type: image/jpeg\r\nContent-Length: %u\r\n\r\n";

static void rememberStreamClient(httpd_req_t* req) {
  struct sockaddr_storage ss;
  socklen_t len = sizeof(ss);
  if (getpeername(httpd_req_to_sockfd(req), (struct sockaddr*)&ss, &len) != 0) return;
  if (ss.ss_family == AF_INET) streamClientIp = ((struct sockaddr_in*)&ss)->sin_addr.s_addr;
  else if (ss.ss_family == AF_INET6) streamClientIp = ((struct sockaddr_in6*)&ss)->sin6_addr.un.u32_addr[3];  // IPv4-mapped
}

static esp_err_t streamHandler(httpd_req_t* req) {
  char partBuf[64];
  rememberStreamClient(req);
  esp_err_t res = httpd_resp_set_type(req, STREAM_CONTENT_TYPE);
  if (res != ESP_OK) return res;
  httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
  while (true) {
    camera_fb_t* fb = esp_camera_fb_get();
    if (!fb) return ESP_FAIL;
    size_t hlen = snprintf(partBuf, sizeof(partBuf), STREAM_PART, (unsigned)fb->len);
    res = httpd_resp_send_chunk(req, STREAM_BOUNDARY, strlen(STREAM_BOUNDARY));
    if (res == ESP_OK) res = httpd_resp_send_chunk(req, partBuf, hlen);
    if (res == ESP_OK) res = httpd_resp_send_chunk(req, (const char*)fb->buf, fb->len);
    esp_camera_fb_return(fb);
    if (res != ESP_OK) return res;   // client disconnected -> end this stream
  }
}

static esp_err_t captureHandler(httpd_req_t* req) {
  camera_fb_t* fb = esp_camera_fb_get();
  if (!fb) { httpd_resp_send_500(req); return ESP_FAIL; }
  httpd_resp_set_type(req, "image/jpeg");
  esp_err_t res = httpd_resp_send(req, (const char*)fb->buf, fb->len);
  esp_camera_fb_return(fb);
  return res;
}

static esp_err_t indexHandler(httpd_req_t* req) {
  char page[256];
  snprintf(page, sizeof(page),
           "ESP32-CAM door camera OK\nstream:  http://%s:81/stream\ncapture: http://%s/capture\nbackend: %s\nRSSI: %d dBm\n",
           WiFi.localIP().toString().c_str(), WiFi.localIP().toString().c_str(), BACKEND_URL, WiFi.RSSI());
  httpd_resp_set_type(req, "text/plain");
  return httpd_resp_send(req, page, strlen(page));
}

void startServers() {
  httpd_config_t cfg = HTTPD_DEFAULT_CONFIG();
  cfg.server_port = 80;
  httpd_uri_t indexUri = {"/", HTTP_GET, indexHandler, NULL};
  httpd_uri_t captureUri = {"/capture", HTTP_GET, captureHandler, NULL};
  if (httpd_start(&webHttpd, &cfg) == ESP_OK) {
    httpd_register_uri_handler(webHttpd, &indexUri);
    httpd_register_uri_handler(webHttpd, &captureUri);
  }
  // The stream gets its own server (port 81) because a stream request never finishes.
  cfg.server_port = 81;
  cfg.ctrl_port += 1;
  httpd_uri_t streamUri = {"/stream", HTTP_GET, streamHandler, NULL};
  if (httpd_start(&streamHttpd, &cfg) == ESP_OK) {
    httpd_register_uri_handler(streamHttpd, &streamUri);
  }
}

// ---------------------------------------------------------------- WiFi
void connectWiFi() {
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);                 // power-save adds lag/stutter to the stream
  WiFi.setAutoReconnect(true);
#ifdef STATIC_IP
  IPAddress ip, gw, mask(255, 255, 255, 0);
  ip.fromString(STATIC_IP); gw.fromString(STATIC_GATEWAY);
  WiFi.config(ip, gw, mask, gw);
#endif
  // Up to 3 networks (secrets.h): joins whichever is in range, strongest first.
  wifiMulti.addAP(WIFI_SSID, WIFI_PASSWORD);
#ifdef WIFI_SSID2
  wifiMulti.addAP(WIFI_SSID2, WIFI_PASSWORD2);
#endif
#ifdef WIFI_SSID3
  wifiMulti.addAP(WIFI_SSID3, WIFI_PASSWORD3);
#endif
  unsigned long start = millis();
  while (wifiMulti.run(5000) != WL_CONNECTED && millis() - start < 30000) blink(2, 50, 150);
}

// Troubleshooting aid: list the 2.4 GHz networks the ESP32 can see (5 GHz networks never show up).
void logVisibleNetworks() {
  int n = WiFi.scanNetworks();
  DLOG("networks visible: %d\n", n);
  for (int i = 0; i < n && i < 12; i++) {
    DLOG("  '%s'  signal %d dBm  channel %d  security %d\n",
         WiFi.SSID(i).c_str(), (int)WiFi.RSSI(i), (int)WiFi.channel(i), (int)WiFi.encryptionType(i));
  }
  WiFi.scanDelete();
}

void announceIP() {
  Serial.print("IP:");
  Serial.println(WiFi.localIP());
}

unsigned long lastWifiCheck = 0, lastSlowBlink = 0;
bool wasConnected = false;

void maintainWiFi() {
  bool connected = WiFi.status() == WL_CONNECTED;
  unsigned long now = millis();
  if (connected && !wasConnected) {      // just (re)connected
    blink(3, 400, 200);
    announceIP();
  }
  wasConnected = connected;
  if (connected) return;
  if (now - lastSlowBlink >= 500) { lastSlowBlink = now; digitalWrite(LED_PIN, !digitalRead(LED_PIN)); }
  if (now - lastWifiCheck >= 10000) {    // nudge a reconnect every 10 s
    lastWifiCheck = now;
    DLOG("WiFi not connected (status %d), retrying\n", (int)WiFi.status());
    logVisibleNetworks();
    wifiMulti.run(5000);
  }
}

// ---------------------------------------------------------------- serial relay
bool validReply(const String& r) {
  return r == "UNKNOWN" || r == "NOFACE" || r == "ERR" || (r.startsWith("KNOWN:") && r.length() > 6);
}

String askBackend(const String& distance) {
  if (WiFi.status() != WL_CONNECTED) return "ERR";
  HTTPClient http;
  String base = streamClientIp ? "http://" + IPAddress(streamClientIp).toString() + ":" + String(BACKEND_PORT)
                               : String(BACKEND_URL);
  String url = base + "/trigger?d=" + distance + "&token=" + API_TOKEN;
  DLOG("trigger -> %s\n", base.c_str());
  http.setConnectTimeout(3000);
  http.setTimeout(HTTP_TIMEOUT_MS);
  if (!http.begin(url)) return "ERR";
  String reply = "ERR";
  int code = http.GET();
  if (code == 200) {
    reply = http.getString();
    reply.trim();
    if (!validReply(reply)) reply = "ERR";
  }
  http.end();
  return reply;
}

void handleLine(String line) {
  line.trim();
  if (!line.startsWith("T,")) return;   // not for us (noise)
  String d = line.substring(2);
  for (unsigned i = 0; i < d.length(); i++) if (!isDigit(d[i])) return;
  if (d.length() == 0 || d.length() > 4) return;
  led(true);
  String reply = askBackend(d);
  led(false);
  Serial.println(reply);
}

String lineBuf;

// ---------------------------------------------------------------- main
void setup() {
#if DISABLE_BROWNOUT
  WRITE_PERI_REG(RTC_CNTL_BROWN_OUT_REG, 0);  // must be first: see DISABLE_BROWNOUT above
#endif
  pinMode(LED_PIN, OUTPUT);
  led(false);

#if DEBUG_SERIAL
  Serial.begin(115200);
  Serial.setDebugOutput(true);
  delay(200);
#else
  Serial.begin(9600);                   // UART0 = link to Arduino
  // The Arduino sends "open-drain" (only pulls the line LOW, never drives 5 V), so the ESP32's
  // internal pull-up provides the HIGH level (3.3 V) on U0R. This is why no resistors are needed.
  gpio_pullup_en(GPIO_NUM_3);
  Serial.setDebugOutput(false);
  esp_log_level_set("*", ESP_LOG_NONE);
#endif
  lineBuf.reserve(40);
  DLOG("boot, last reset reason: %d (1=power-on 3=sw 4=panic 5/6/7=watchdog 9=BROWNOUT)\n",
       (int)esp_reset_reason());
  DLOG("PSRAM found: %s, free heap: %u\n", psramFound() ? "yes" : "NO", (unsigned)ESP.getFreeHeap());

  DLOG("camera init...\n");
  if (!initCamera()) {
    DLOG("CAMERA INIT FAILED (ribbon cable? power?)\n");
    unsigned long t = millis();
    while (millis() - t < 10000) { blink(2, 80, 80); delay(500); }
    ESP.restart();
  }
  DLOG("camera OK, connecting to WiFi '%s'...\n", WIFI_SSID);
  connectWiFi();
  DLOG("WiFi network: %s\n", WiFi.SSID().c_str());
  DLOG("WiFi status %d (3=connected, 1=SSID not found, 4=wrong password/failed, 6=disconnected), IP %s\n",
       (int)WiFi.status(), WiFi.localIP().toString().c_str());
  MDNS.begin(MDNS_NAME);
  startServers();
  DLOG("servers started: http://%s:81/stream\n", WiFi.localIP().toString().c_str());
}

void loop() {
  maintainWiFi();
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\n') { handleLine(lineBuf); lineBuf = ""; }
    else if (c != '\r' && lineBuf.length() < 32) lineBuf += c;
  }
  delay(2);
}
