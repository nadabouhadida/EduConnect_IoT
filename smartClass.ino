#include <WiFi.h>
#include <FirebaseESP32.h>
#include <Wire.h>
#include <Adafruit_Sensor.h>
#include <Adafruit_TSL2591.h>
#include <ESP32Servo.h>
#include "DHT.h"

// ─────────────────────────────────────────────
//  CONFIGURATION WiFi & Firebase
// ─────────────────────────────────────────────
#define WIFI_SSID     "****"
#define WIFI_PASSWORD "****"
#define DATABASE_SECRET "6i8aEv6Rs0mMKB9AVD9m8RO6YDJqM41jGw1mcJbS"
#define DATABASE_URL     "https://smart-classroom-71e1a-default-rtdb.europe-west1.firebasedatabase.app"

// ─────────────────────────────────────────────
//  PINS
// ─────────────────────────────────────────────
#define MQ_PIN          32
#define VIBR_PIN        12
#define SDA_PIN         15
#define SCL_PIN         27
#define SERVO_FAN_PIN   13
#define SERVO_DOOR_PIN  18
#define LED_PIN         23
#define DHT_PIN         33

// ─────────────────────────────────────────────
//  SEUILS CAPTEURS
// ─────────────────────────────────────────────
#define GAZ_SEUIL            1200
#define LUX_ETEINT            500
#define LUX_NORMAL            400
#define LUX_PROJECTEUR        150
#define CHOC_SEUIL_RISQUE       3
#define CHOC_FENETRE_MS      2000UL

// Validation capteur MQ
#define MQ_MIN_VALIDE          50
#define MQ_MAX_VALIDE        4000
#define MQ_CHAUFFE_MS       30000UL
#define MQ_NB_LECTURES          5
#define MQ_SEUIL_VARIANCE      80

// ─────────────────────────────────────────────
//  TIMING
// ─────────────────────────────────────────────
#define INTERVALLE_MS        10000UL
#define WIFI_TIMEOUT_MS      15000UL
#define FAN_DETACH_DELAI_MS    300UL

// ─────────────────────────────────────────────
//  OBJETS GLOBAUX
// ─────────────────────────────────────────────
FirebaseData    fbdo;
FirebaseAuth    auth;
FirebaseConfig  config;

Adafruit_TSL2591 tsl = Adafruit_TSL2591(2591);
Servo            ventilateur;
Servo            servoPorte;
DHT              dht(DHT_PIN, DHT22);

// ─────────────────────────────────────────────
//  ÉTAT DU SYSTÈME
// ─────────────────────────────────────────────
struct SystemState {
    bool fanOn            = false;
    bool doorOpen         = false;
    bool ledOn            = false;
    bool tslOk            = false;
    bool mqOk             = false;
    bool professorPresent = false;
    bool autoMode         = false;
};
static SystemState state;

// ─────────────────────────────────────────────
//  VARIABLES TIMING NON BLOQUANT
// ─────────────────────────────────────────────
static unsigned long derniereLecture = 0;
static unsigned long mqDemarrage     = 0;
static unsigned long fanStopTime     = 0;

// ─────────────────────────────────────────────
//  VARIABLES ISR (volatile obligatoire)
// ─────────────────────────────────────────────
volatile uint32_t chocCompteur  = 0;
volatile bool     chocAlertFlag = false;
static   uint32_t chocSnapshot  = 0;
static   unsigned long chocFenetreDebut = 0;

// ─────────────────────────────────────────────
//  JSON RÉUTILISABLE
// ─────────────────────────────────────────────
static FirebaseJson jsonSensors;
static FirebaseJson jsonAlerte;
static FirebaseJson jsonReset;


// ============================================================
//  ISR — INTERRUPTION CHOC
// ============================================================
void IRAM_ATTR ISR_choc()
{
    chocCompteur++;
    if (chocCompteur >= CHOC_SEUIL_RISQUE)
        chocAlertFlag = true;
}


// ============================================================
//  WIFI
// ============================================================
bool connecterWiFi()
{
    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
    Serial.print("[WiFi] Connexion");
    unsigned long debut = millis();
    while (WiFi.status() != WL_CONNECTED) {
        if (millis() - debut > WIFI_TIMEOUT_MS) {
            Serial.println("\n[WiFi] TIMEOUT");
            return false;
        }
        delay(500);
        Serial.print(".");
    }
    Serial.printf("\n[WiFi] IP : %s\n", WiFi.localIP().toString().c_str());
    return true;
}

static inline bool verifierWiFi()
{
    if (WiFi.status() == WL_CONNECTED) return true;
    Serial.println("[WiFi] Perdu — tentative de reconnexion...");
    WiFi.disconnect();
    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
    return false;
}


// ============================================================
//  ACTIONNEURS — SERVO FAN
// ============================================================
void demarrerVentilateur()
{
    if (!ventilateur.attached()) ventilateur.attach(SERVO_FAN_PIN, 500, 2400);
    ventilateur.write(0);
    fanStopTime = 0;
    Serial.println("[FAN] Ventilateur DEMARRE");
}

void arreterVentilateur()
{
    if (ventilateur.attached()) {
        ventilateur.write(90);
        fanStopTime = millis();
    }
    Serial.println("[FAN] Ventilateur ARRET (detach dans 300 ms)");
}

static inline void tickFanDetach()
{
    if (fanStopTime > 0 && millis() - fanStopTime >= FAN_DETACH_DELAI_MS) {
        if (ventilateur.attached()) ventilateur.detach();
        fanStopTime = 0;
        Serial.println("[FAN] Servo detaché");
    }
}


// ============================================================
//  ACTIONNEURS — SERVO PORTE & LED
// ============================================================
void ouvrirPorte()
{
    if (!servoPorte.attached()) servoPorte.attach(SERVO_DOOR_PIN, 500, 2400);
    servoPorte.write(90);
    Serial.println("[PORTE] Ouverte 90 deg");
}

void fermerPorte()
{
    if (!servoPorte.attached()) servoPorte.attach(SERVO_DOOR_PIN, 500, 2400);
    servoPorte.write(0);
    Serial.println("[PORTE] Fermée 0 deg");
}

static inline void allumerLED()
{
    if (state.ledOn) return;
    digitalWrite(LED_PIN, HIGH);
    state.ledOn = true;
    Serial.println("[LED] Allumée");
}

static inline void eteindreLED()
{
    if (!state.ledOn) return;
    digitalWrite(LED_PIN, LOW);
    state.ledOn = false;
    Serial.println("[LED] Éteinte");
}


// ============================================================
//  FIREBASE — HELPERS
// ============================================================
static bool envoyerJSON(const char* path, FirebaseJson& json)
{
    if (!Firebase.ready()) return false;
    if (Firebase.updateNode(fbdo, path, json)) return true;
    Serial.printf("[Firebase] ERREUR %s : %s\n", path, fbdo.errorReason().c_str());
    return false;
}

static bool lireFirebaseBool(const char* path, bool defaut, bool& valeur)
{
    if (Firebase.getBool(fbdo, path)) {
        valeur = fbdo.boolData();
        return true;
    }
    if (fbdo.errorReason() == "path not exist") {
        Firebase.setBool(fbdo, path, defaut);
        valeur = defaut;
        Serial.printf("[Firebase] %s créé -> %s\n", path, defaut ? "true" : "false");
    } else {
        Serial.printf("[Firebase] ERREUR %s : %s\n", path, fbdo.errorReason().c_str());
    }
    return false;
}

void envoyerAlerteChoc(uint32_t nbChocs)
{
    jsonAlerte.clear();
    jsonAlerte.set("shock_value", (int)nbChocs);
    jsonAlerte.set("timestamp",   (int)(millis() / 1000));
    if (envoyerJSON("/sensors", jsonAlerte))
        Serial.printf("[CHOC] Alerte envoyée : %d chocs\n", nbChocs);
}


// ============================================================
//  CAPTEUR MQ — VALIDATION PAR STABILITÉ
// ============================================================
bool verifierCapteurMQ()
{
    int lectures[MQ_NB_LECTURES];
    int minVal = 4095, maxVal = 0, somme = 0;

    for (int i = 0; i < MQ_NB_LECTURES; i++) {
        lectures[i] = analogRead(MQ_PIN);
        if (lectures[i] < minVal) minVal = lectures[i];
        if (lectures[i] > maxVal) maxVal = lectures[i];
        somme += lectures[i];
        delay(10);
    }
    const int moyenne  = somme / MQ_NB_LECTURES;
    const int variance = maxVal - minVal;

    if (variance > MQ_SEUIL_VARIANCE) {
        Serial.println("[MQ] Signal instable");
        return false;
    }
    if (moyenne < MQ_MIN_VALIDE || moyenne > MQ_MAX_VALIDE) {
        Serial.printf("[MQ] Hors plage (%d)\n", moyenne);
        return false;
    }
    if (millis() - mqDemarrage < MQ_CHAUFFE_MS) {
        Serial.printf("[MQ] Chauffe : %lu s restantes\n",
                      (MQ_CHAUFFE_MS - (millis() - mqDemarrage)) / 1000);
        return false;
    }
    return true;
}


// ============================================================
//  LECTURE FIREBASE : professor_present + auto_mode
// ============================================================
void lireEtatPresenceEtAuto()
{
    bool prof = state.professorPresent;
    lireFirebaseBool("/notifications/professor_present", false, prof);
    if (prof != state.professorPresent) {
        state.professorPresent = prof;
        Serial.printf("[PRESENCE] professor_present -> %s\n", prof ? "true" : "false");
    }

    bool autoM = state.autoMode;
    lireFirebaseBool("/controls/auto_mode", false, autoM);
    if (autoM != state.autoMode) {
        state.autoMode = autoM;
        Serial.printf("[AUTO] auto_mode -> %s\n", autoM ? "true" : "false");
    }
}


// ============================================================
//  DOOR_OPEN — LECTURE + APPLICATION
// ============================================================
void appliquerDoorOpen()
{
    bool nouvelEtat = state.doorOpen;
    lireFirebaseBool("/controls/door_open", false, nouvelEtat);

    if (nouvelEtat == state.doorOpen) return;
    state.doorOpen = nouvelEtat;
    Serial.printf("[DOOR] -> %s\n", state.doorOpen ? "true" : "false");

    if (state.doorOpen) {
        ouvrirPorte();
    } else {
        fermerPorte();
        eteindreLED();

        if (state.fanOn) {
            state.fanOn = false;
            arreterVentilateur();
            Firebase.setBool(fbdo, "/controls/fan_on", false);
        }

        jsonReset.clear();
        jsonReset.set("light_detected", -1);
        envoyerJSON("/sensors", jsonReset);
        Serial.println("[LUX] Détection désactivée");
    }
}


// ============================================================
//  LECTURE CAPTEURS → PUBLICATION FIREBASE
// ============================================================
void lireEtPublierCapteurs()
{
    jsonSensors.clear();

    // ── GAZ ───────────────────────────────────────────────────
    state.mqOk = verifierCapteurMQ();
    if (state.mqOk) {
        int gaz = 0;
        for (int i = 0; i < MQ_NB_LECTURES; i++) { gaz += analogRead(MQ_PIN); delay(5); }
        gaz /= MQ_NB_LECTURES;
        Serial.printf("[GAZ] %d | %s\n", gaz, gaz > GAZ_SEUIL ? "GAZ DETECTE !" : "Air normal");
        jsonSensors.set("gas_detected", gaz);
    } else {
        jsonSensors.set("gas_detected", -1);
    }

    // ── TEMPERATURE & HUMIDITE ────────────────────────────────
    const bool dhtActif = state.professorPresent && state.autoMode;

    if (dhtActif) {
        const float temp = dht.readTemperature();
        const float humi = dht.readHumidity();
        if (isnan(temp) || isnan(humi)) {
            Serial.println("[DHT22] Lecture échouée");
            jsonSensors.set("temperature", -1);
            jsonSensors.set("humidity",    -1);
        } else {
            Serial.printf("[DHT22] %.1f C | %.1f %%\n", temp, humi);
            jsonSensors.set("temperature", temp);
            jsonSensors.set("humidity",    humi);
        }
    } else {
        Serial.println("[DHT22] Suspendu — professor_present=false ou auto_mode=false");
        jsonSensors.set("temperature", -1);
        jsonSensors.set("humidity",    -1);
    }

    // ── LUMIERE ───────────────────────────────────────────────
    float lux = -1.0f;
    const bool tslActif = state.professorPresent && state.autoMode;

    if (tslActif && state.tslOk) {
        sensors_event_t event;
        tsl.getEvent(&event);
        lux = event.light;

        if (lux > 0.0f && !isnan(lux)) {
            bool ledSouhaitee;
            const char* etatLux;
            if      (lux >= LUX_ETEINT)    { etatLux = "naturelle";      ledSouhaitee = false; }
            else if (lux >= LUX_NORMAL)     { etatLux = "compens. fbl";   ledSouhaitee = true;  }
            else if (lux >= LUX_PROJECTEUR) { etatLux = "compens. forte"; ledSouhaitee = true;  }
            else                            { etatLux = "projecteur";     ledSouhaitee = true;  }

            Serial.printf("[LUX] %.1f lux | %s\n", lux, etatLux);

            if (ledSouhaitee != state.ledOn) {
                ledSouhaitee ? allumerLED() : eteindreLED();
                Firebase.setBool(fbdo, "/controls/led_on", state.ledOn);
                Serial.printf("[LED] led_on Firebase -> %s\n", state.ledOn ? "true" : "false");
            }
        } else {
            Serial.println("[LUX] Lecture invalide");
            lux = -1.0f;
        }

    } else if (!tslActif) {
        Serial.println("[LUX] Suspendu — professor_present=false ou auto_mode=false");
    } else {
        Serial.println("[LUX] TSL2591 absent");
    }
    jsonSensors.set("light_detected", lux);

    // ── CHOC ──────────────────────────────────────────────────
    noInterrupts();
    const uint32_t chocActuel = chocCompteur;
    interrupts();
    jsonSensors.set("shock_value", (int)chocActuel);
    Serial.printf("[CHOC] %d impulsions\n", chocActuel);

    // ── Envoi groupé ──────────────────────────────────────────
    if (envoyerJSON("/sensors", jsonSensors))
        Serial.println("[Firebase] /sensors OK");
}


// ============================================================
//  LECTURE CONTROLES FIREBASE → ACTIONNEURS
// ============================================================
void appliquerControles()
{
    // ── Ventilateur ───────────────────────────────────────────
    if (!state.doorOpen) {
        if (state.fanOn) {
            state.fanOn = false;
            arreterVentilateur();
            Firebase.setBool(fbdo, "/controls/fan_on", false);
        } else {
            Serial.println("[FAN] Suspendu — door_open = false");
        }
    } else {
        bool fanSouhaite = state.fanOn;
        lireFirebaseBool("/controls/fan_on", false, fanSouhaite);
        if (fanSouhaite != state.fanOn) {
            state.fanOn = fanSouhaite;
            fanSouhaite ? demarrerVentilateur() : arreterVentilateur();
        }
    }

    // ── LED ───────────────────────────────────────────────────
    // En auto_mode, la LED est pilotée par le TSL — commande manuelle ignorée
    if (!state.autoMode) {
        bool ledSouhaitee = state.ledOn;
        lireFirebaseBool("/controls/led_on", false, ledSouhaitee);
        Serial.printf("[LED] Manuel Firebase = %s\n", ledSouhaitee ? "true" : "false");
        if (ledSouhaitee != state.ledOn)
            ledSouhaitee ? allumerLED() : eteindreLED();
    } else {
        Serial.println("[LED] Mode auto actif — led_on Firebase ignoré");
    }
}


// ============================================================
//  SETUP
// ============================================================
void setup()
{
    Serial.begin(115200);
    delay(500);

    analogReadResolution(12);
    pinMode(VIBR_PIN, INPUT);
    pinMode(LED_PIN,  OUTPUT);
    digitalWrite(LED_PIN, LOW);

    attachInterrupt(digitalPinToInterrupt(VIBR_PIN), ISR_choc, RISING);
    chocFenetreDebut = millis();
    mqDemarrage      = millis();

    dht.begin();
    Serial.println("[DHT22] Initialisé");

    if (!connecterWiFi())
        Serial.println("[SYSTEME] Mode dégradé sans WiFi");

    config.database_url               = DATABASE_URL;
    config.signer.tokens.legacy_token = DATABASE_SECRET;
    Firebase.begin(&config, &auth);
    Firebase.reconnectWiFi(true);
    fbdo.setResponseSize(1024);
    Serial.println("[Firebase] Initialisé");

    Wire.begin(SDA_PIN, SCL_PIN);
    state.tslOk = tsl.begin();
    if (state.tslOk) {
        tsl.setGain(TSL2591_GAIN_MED);
        tsl.setTiming(TSL2591_INTEGRATIONTIME_300MS);
        Serial.println("[TSL2591] OK");
    } else {
        Serial.println("[TSL2591] Non détecté !");
    }

    arreterVentilateur();
    fermerPorte();

    Serial.println("======== Smart Classroom prêt ========");
}


// ============================================================
//  LOOP
// ============================================================
void loop()
{
    const unsigned long maintenant = millis();

    tickFanDetach();

    if (!verifierWiFi()) {
        delay(1000);
        return;
    }

    bool alertePendante = false;
    noInterrupts();
    if (chocAlertFlag) {
        alertePendante = true;
        chocSnapshot   = chocCompteur;
        chocAlertFlag  = false;
        chocCompteur   = 0;
    }
    interrupts();

    if (alertePendante) {
        Serial.printf("[ISR] Alerte choc ! %d impulsions\n", chocSnapshot);
        envoyerAlerteChoc(chocSnapshot);
        chocFenetreDebut = maintenant;
    }

    if (maintenant - chocFenetreDebut >= CHOC_FENETRE_MS) {
        noInterrupts();
        chocCompteur  = 0;
        chocAlertFlag = false;
        interrupts();
        chocFenetreDebut = maintenant;
    }

    if (maintenant - derniereLecture < INTERVALLE_MS) return;
    derniereLecture = maintenant;

    if (!Firebase.ready()) {
        Serial.println("[Firebase] Non disponible");
        return;
    }

    Serial.println("\n========= Nouvelle lecture =========");
    appliquerDoorOpen();
    lireEtatPresenceEtAuto();
    lireEtPublierCapteurs();
    appliquerControles();
    Serial.println("====================================\n");
}