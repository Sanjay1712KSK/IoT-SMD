/*
  Smart Door Monitor - Arduino Uno ("the brain")

  - Reads the HC-SR04 ultrasonic sensor every 70 ms, median of the last 5 readings (filters spikes).
  - Debounce: needs 3 consecutive filtered readings closer than 100 cm before it counts as "someone there".
  - Cooldown: after a check, no new check for 15 s (prevents repeated alerts for the same visitor).
  - Sends "T,<cm>" to the ESP32-CAM, then waits (WITHOUT blocking) up to 10 s for the reply:
        KNOWN:<name>   -> LCD "Welcome, <name>", short chirp
        UNKNOWN        -> LCD "UNKNOWN PERSON", buzzer alarm
        NOFACE         -> LCD "No face seen"
        ERR / timeout  -> LCD "No server reply"
  - ESP32 may also send "IP:<address>" once after connecting to WiFi -> shown on the LCD (setup aid).
  - Optional PIR sensor (PIR_MODE below).

  Libraries: "LiquidCrystal I2C" by Frank de Brabander (Library Manager). Wire + SoftwareSerial are built in.
  Board: Arduino Uno. On a Mega you can use Serial1 (pins 18/19) instead of SoftwareSerial - see ESP_LINK.
*/
#include <Wire.h>
#include <LiquidCrystal_I2C.h>
#include <SoftwareSerial.h>

// ============================ PINS & SETTINGS (edit here) ============================
const uint8_t PIN_TRIG    = 7;    // HC-SR04 TRIG
const uint8_t PIN_ECHO    = 6;    // HC-SR04 ECHO
const uint8_t PIN_PIR     = 2;    // PIR OUT (only used if PIR_MODE != 0)
const uint8_t PIN_BUZZER  = 8;    // buzzer + (other leg to GND)
const uint8_t PIN_ESP_RX  = 10;   // Arduino RX  <- ESP32-CAM U0T (GPIO1). Direct wire is OK (3.3 V reads as HIGH).
const uint8_t PIN_ESP_TX  = 11;   // Arduino TX  -> ESP32-CAM U0R (GPIO3) THROUGH the 1k/2k divider (5 V -> 3.3 V)!
// LCD: I2C on A4 (SDA) / A5 (SCL). Common addresses are 0x27 and 0x3F.
const uint8_t LCD_ADDR    = 0x27;

const int           TRIGGER_CM       = 100;    // closer than this = someone at the door
const uint8_t       DEBOUNCE_COUNT   = 3;      // consecutive close readings needed
const unsigned long COOLDOWN_MS      = 15000;  // no new check for this long after one
const unsigned long REPLY_TIMEOUT_MS = 10000;  // give up waiting for the ESP32/laptop after this
const unsigned long RESULT_SHOW_MS   = 5000;   // how long the result stays on the LCD
const unsigned long SAMPLE_MS        = 70;     // HC-SR04 needs >= 60 ms between pings
const int           MAX_CM           = 400;    // "nothing in range"

// PIR_MODE: 0 = PIR not used
//           1 = PIR motion OR close distance triggers a check  (PIR as a secondary wake-up)
//           2 = PIR motion AND close distance needed           (fewer false triggers, e.g. a parked bike)
const uint8_t PIR_MODE = 0;

const bool BUZZER_IS_PASSIVE = false;  // true if your buzzer needs tone() (no built-in oscillator)

// ESP_LINK_USB = 1: talk to the "ESP32" over the USB cable (hardware Serial) instead of pins 10/11.
// Used for testing with tools/fake_esp32.py --serial COMx (no ESP32 needed). Debug prints are then off.
#define ESP_LINK_USB 0
// =======================================================================================

#if ESP_LINK_USB
  #define ESP_LINK Serial
  #define DBG(x)
#else
  SoftwareSerial espSerial(PIN_ESP_RX, PIN_ESP_TX);
  #define ESP_LINK espSerial
  #define DBG(x) Serial.println(x)
#endif

LiquidCrystal_I2C lcd(LCD_ADDR, 16, 2);

enum State { IDLE, WAITING, SHOWING };
State state = IDLE;

// --- distance filtering
int samples[5] = {MAX_CM, MAX_CM, MAX_CM, MAX_CM, MAX_CM};
uint8_t sampleIdx = 0;
int distanceCm = MAX_CM;        // median-filtered distance
uint8_t closeCount = 0;         // consecutive "close" filtered readings

// --- timing
unsigned long lastSampleAt = 0, lastLcdAt = 0, sentAt = 0, shownAt = 0, lastTriggerAt = 0;
unsigned long showFor = RESULT_SHOW_MS;
bool everTriggered = false;

// --- serial line from the ESP32
char lineBuf[40];
uint8_t lineLen = 0;

// --- non-blocking buzzer: toggles `beepToggles` times, every `beepStepMs`
uint8_t beepToggles = 0;
unsigned long beepStepMs = 0, beepLastAt = 0;
bool buzzerOn = false;

// ---------------------------------------------------------------------------- LCD helpers
void lcdLine(uint8_t row, const char* text) {
  // Overwrite the whole row (pad with spaces) instead of lcd.clear() -> no flicker.
  char buf[17];
  snprintf(buf, sizeof(buf), "%-16.16s", text);
  lcd.setCursor(0, row);
  lcd.print(buf);
}

void showResult(const char* top, const char* bottom, unsigned long ms) {
  lcdLine(0, top);
  lcdLine(1, bottom);
  state = SHOWING;
  shownAt = millis();
  showFor = ms;
}

// ---------------------------------------------------------------------------- buzzer
void setBuzzer(bool on) {
  buzzerOn = on;
  if (BUZZER_IS_PASSIVE) { if (on) tone(PIN_BUZZER, 2000); else noTone(PIN_BUZZER); }
  else digitalWrite(PIN_BUZZER, on ? HIGH : LOW);
}

void beep(uint8_t times, unsigned long stepMs) {   // e.g. beep(3, 300) = 3 beeps of 300 ms
  beepToggles = times * 2;
  beepStepMs = stepMs;
  beepLastAt = millis() - stepMs;  // start immediately
  setBuzzer(false);
}

void updateBuzzer(unsigned long now) {
  if (beepToggles == 0 || now - beepLastAt < beepStepMs) return;
  beepLastAt = now;
  setBuzzer(!buzzerOn);
  beepToggles--;
  if (beepToggles == 0) setBuzzer(false);
}

// ---------------------------------------------------------------------------- ultrasonic
int readDistanceOnce() {
  digitalWrite(PIN_TRIG, LOW);
  delayMicroseconds(2);
  digitalWrite(PIN_TRIG, HIGH);
  delayMicroseconds(10);
  digitalWrite(PIN_TRIG, LOW);
  unsigned long us = pulseIn(PIN_ECHO, HIGH, 25000UL);  // 25 ms timeout ~ 4 m max
  if (us == 0) return MAX_CM;                           // no echo = nothing in range
  int cm = us / 58;                                     // speed of sound: 58 us per cm (round trip)
  return cm > MAX_CM ? MAX_CM : cm;
}

int median5(const int* v) {
  int s[5];
  memcpy(s, v, sizeof(s));
  for (uint8_t i = 1; i < 5; i++) {          // insertion sort, 5 items
    int x = s[i]; int8_t j = i - 1;
    while (j >= 0 && s[j] > x) { s[j + 1] = s[j]; j--; }
    s[j + 1] = x;
  }
  return s[2];
}

void sampleDistance() {
  samples[sampleIdx] = readDistanceOnce();
  sampleIdx = (sampleIdx + 1) % 5;
  distanceCm = median5(samples);
  if (distanceCm < TRIGGER_CM) { if (closeCount < 255) closeCount++; }
  else closeCount = 0;
}

// ---------------------------------------------------------------------------- trigger decision
bool inCooldown(unsigned long now) {
  return everTriggered && (now - lastTriggerAt < COOLDOWN_MS);  // unsigned math: safe across millis() overflow
}

bool shouldTrigger(unsigned long now) {
  if (inCooldown(now)) return false;
  bool close = closeCount >= DEBOUNCE_COUNT;
  bool motion = (PIR_MODE != 0) && digitalRead(PIN_PIR) == HIGH;
  switch (PIR_MODE) {
    case 1:  return close || motion;
    case 2:  return close && motion;
    default: return close;
  }
}

void sendTrigger(unsigned long now) {
  while (ESP_LINK.available()) ESP_LINK.read();  // drop stale bytes
  lineLen = 0;
  ESP_LINK.print("T,");
  ESP_LINK.println(distanceCm);
  DBG(String("-> T,") + distanceCm);
  state = WAITING;
  sentAt = now;
  lastTriggerAt = now;
  everTriggered = true;
  closeCount = 0;
  lcdLine(0, "Checking face...");
  lcdLine(1, "Please look up");
}

// ---------------------------------------------------------------------------- replies
void onEspLine(char* line) {
  DBG(String("<- ") + line);
  if (strncmp(line, "IP:", 3) == 0) {           // info from ESP32 after WiFi connect
    if (state != WAITING) showResult("Camera online", line + 3, 4000);
    return;
  }
  if (state != WAITING) return;                 // late/unexpected reply: ignore

  if (strncmp(line, "KNOWN:", 6) == 0) {
    showResult("Welcome,", line + 6, RESULT_SHOW_MS);
    beep(1, 120);
  } else if (strcmp(line, "UNKNOWN") == 0) {
    showResult("UNKNOWN PERSON", "Owner alerted", RESULT_SHOW_MS);
    beep(5, 300);
  } else if (strcmp(line, "NOFACE") == 0) {
    showResult("No face seen", "Look at camera", RESULT_SHOW_MS);
    beep(2, 80);
  } else if (strcmp(line, "ERR") == 0) {
    showResult("No server reply", "Server/cam error", RESULT_SHOW_MS);
    beep(2, 80);
  }
  // anything else (e.g. ESP32 boot noise) is ignored and we keep waiting
}

void readEsp() {
  while (ESP_LINK.available()) {
    char c = ESP_LINK.read();
    if (c == '\n') {
      lineBuf[lineLen] = '\0';
      if (lineLen > 0) onEspLine(lineBuf);
      lineLen = 0;
    } else if (c >= 32 && c < 127 && lineLen < sizeof(lineBuf) - 1) {
      lineBuf[lineLen++] = c;                   // printable only; '\r' and garbage dropped
    }
  }
}

// ---------------------------------------------------------------------------- main
void setup() {
#if !ESP_LINK_USB
  Serial.begin(115200);                         // USB debug output
#endif
  ESP_LINK.begin(9600);
  pinMode(PIN_TRIG, OUTPUT);
  pinMode(PIN_ECHO, INPUT);
  pinMode(PIN_PIR, INPUT);
  pinMode(PIN_BUZZER, OUTPUT);
  setBuzzer(false);

  lcd.init();
  lcd.backlight();
  lcdLine(0, "Smart Door");
  lcdLine(1, "Starting...");
  DBG(F("Smart Door Arduino ready"));
  // Note: a PIR needs ~30-60 s after power-up to settle; early triggers are possible.
}

void loop() {
  unsigned long now = millis();
  updateBuzzer(now);
  readEsp();

  if (now - lastSampleAt >= SAMPLE_MS) {
    lastSampleAt = now;
    sampleDistance();
  }

  switch (state) {
    case IDLE:
      if (shouldTrigger(now)) {
        sendTrigger(now);
      } else if (now - lastLcdAt >= 500) {      // refresh idle screen twice a second
        lastLcdAt = now;
        char buf[17];
        lcdLine(0, inCooldown(now) ? "Smart Door (zz)" : "Smart Door");
        if (distanceCm >= MAX_CM) snprintf(buf, sizeof(buf), "Dist: ---");
        else snprintf(buf, sizeof(buf), "Dist: %d cm", distanceCm);
        lcdLine(1, buf);
      }
      break;

    case WAITING:
      if (now - sentAt >= REPLY_TIMEOUT_MS) {
        DBG(F("Reply timeout"));
        showResult("No server reply", "Laptop/WiFi down", RESULT_SHOW_MS);
        beep(2, 80);
      }
      break;

    case SHOWING:
      if (now - shownAt >= showFor) {
        state = IDLE;
        lastLcdAt = 0;
      }
      break;
  }
}
