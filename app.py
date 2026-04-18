import io
import os, csv, json, base64, pickle, smtplib, threading, time
from datetime import datetime, date, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text      import MIMEText
from email.mime.image     import MIMEImage
from pathlib              import Path

import cv2
import numpy as np
import face_recognition
import requests as http_requests
from flask      import Flask, request, jsonify, render_template, send_file, send_from_directory
from flask_cors import CORS

app = Flask(__name__, template_folder='templates', static_folder='static')
CORS(app, resources={r"/api/*": {"origins": "*"}})

# ── Paths ──────────────────────────────────────────────────────────────────────
BASE_DIR        = Path(__file__).parent
FACES_DIR       = BASE_DIR / "faces"
ATTENDANCE_DIR  = BASE_DIR / "attendance"
ENCODINGS_FILE  = BASE_DIR / "encodings.pkl"
ALARM_FACES_DIR = BASE_DIR / "alarm_captures"
DEBUG_DIR       = BASE_DIR / "debug"

for d in (FACES_DIR, ATTENDANCE_DIR, ALARM_FACES_DIR, DEBUG_DIR):
    d.mkdir(exist_ok=True)

# ── Firebase config ────────────────────────────────────────────────────────────
FIREBASE_URL    = "https://smart-classroom-71e1a-default-rtdb.europe-west1.firebasedatabase.app"
FIREBASE_SECRET = ""

PROFESSOR_NAME  = "Professor"

TIME_WINDOWS = [
    (8,  10),
    (14, 16),  
]

# Shock / alarm
SHOCK_LOW             = 0
ALARM_DURATION_SECS   = 60
SHOCK_POLL_INTERVAL   = 10

# Admin email
ADMIN_EMAIL   = "jemahamza81@gmail.com"
SMTP_FROM     = "jemahamza81@gmail.com"
SMTP_PASSWORD = "yhxp pgwo lsda tynd"
SMTP_HOST     = "smtp.gmail.com"
SMTP_PORT     = 587

# ── In-memory state ────────────────────────────────────────────────────────────
known_encodings  = []
known_names      = []
today_attendance = {}

_state_lock       = threading.Lock()
_shock_value      = 0
_alarm_active     = False
_alarm_start_time = None
_alarm_faces      = []

# ── Firebase helpers ───────────────────────────────────────────────────────────
def _fb_params():
    return {"auth": FIREBASE_SECRET} if FIREBASE_SECRET else {}

def firebase_get(path: str):
    try:
        r = http_requests.get(
            f"{FIREBASE_URL}/{path}.json",
            params=_fb_params(), timeout=5
        )
        return r.json() if r.status_code == 200 else None
    except Exception as e:
        print(f"[FIREBASE] GET error ({path}): {e}")
        return None

def firebase_set(path: str, value):
    try:
        r = http_requests.put(
            f"{FIREBASE_URL}/{path}.json",
            json=value, params=_fb_params(), timeout=5
        )
        if r.status_code not in (200, 204):
            print(f"[FIREBASE] PUT error ({path}): {r.status_code} {r.text}")
        return True
    except Exception as e:
        print(f"[FIREBASE] PUT error ({path}): {e}")
        return False

def firebase_push(path: str, value):
    try:
        r = http_requests.post(
            f"{FIREBASE_URL}/{path}.json",
            json=value, params=_fb_params(), timeout=5
        )
        if r.status_code in (200, 201):
            return r.json().get("name")
        print(f"[FIREBASE] PUSH error ({path}): {r.status_code} {r.text}")
        return None
    except Exception as e:
        print(f"[FIREBASE] PUSH error ({path}): {e}")
        return None

# ── Time-window helper ─────────────────────────────────────────────────────────
def _in_time_window() -> bool:
    hour = datetime.now().hour + datetime.now().minute / 60.0
    for start, end in TIME_WINDOWS:
        if start <= hour < end:
            return True
    return False

# ── Camera state ───────────────────────────────────────────────────────────────
def get_camera_state() -> dict:
    with _state_lock:
        alarm   = _alarm_active
        shock   = _shock_value
        elapsed = int((datetime.now() - _alarm_start_time).total_seconds()) \
                  if alarm and _alarm_start_time else 0

    in_window = _in_time_window()

    if alarm:
        remaining = max(0, ALARM_DURATION_SECS - elapsed)
        return {
            "camera_active":       True,
            "mode":                "alarm",
            "shock_value":         shock,
            "time_window":         in_window,
            "alarm_active":        True,
            "alarm_elapsed_sec":   elapsed,
            "alarm_remaining_sec": remaining,
        }
    elif in_window:
        return {
            "camera_active":       True,
            "mode":                "scheduled",
            "shock_value":         shock,
            "time_window":         True,
            "alarm_active":        False,
            "alarm_elapsed_sec":   0,
            "alarm_remaining_sec": 0,
        }
    else:
        return {
            "camera_active":       False,
            "mode":                "off",
            "shock_value":         shock,
            "time_window":         False,
            "alarm_active":        False,
            "alarm_elapsed_sec":   0,
            "alarm_remaining_sec": 0,
        }

# ── Background shock monitor ───────────────────────────────────────────────────
# ── Background shock monitor ───────────────────────────────────────────────────
def shock_monitor():
    global _shock_value, _alarm_active, _alarm_start_time, _alarm_faces

    while True:
        time.sleep(SHOCK_POLL_INTERVAL)
        try:
            raw = firebase_get("sensors/shock_value")
            val = int(raw) if raw is not None else 0
        except Exception:
            val = 0

        with _state_lock:
            _shock_value = val

            if not _alarm_active and val  > 0:
                _alarm_active     = True
                _alarm_start_time = datetime.now()
                _alarm_faces      = []
                print(f"[ALARM] 🚨 shock_value={val} → ALARM ON for {ALARM_DURATION_SECS}s")

                # Push alarm_start to Firebase history/security
                firebase_push("history/security", {
                    "type":      "alarm_start",
                    "shock":     val,
                    "details":   "Shock sensor triggered — alarm activated",
                    "timestamp": datetime.now().isoformat()
                })
                firebase_set("security/alarm_active",  True)
                firebase_set("security/alarm_started", datetime.now().isoformat())

            elif _alarm_active:
                elapsed   = (datetime.now() - _alarm_start_time).total_seconds()
                timed_out = elapsed >= ALARM_DURATION_SECS
                calmed    = val == SHOCK_LOW

                if timed_out or calmed:
                    faces_snapshot = list(_alarm_faces)
                    start_snapshot = _alarm_start_time
                    _alarm_active     = False
                    _alarm_start_time = None
                    _alarm_faces      = []
                    print(f"[ALARM] ✅ ALARM OFF (captured {len(faces_snapshot)} images)")

                    # Update Firebase
                    firebase_push("history/security", {
                        "type":      "alarm_end",
                        "shock":     val,
                        "details":   f"Alarm deactivated. {len(faces_snapshot)} faces captured.",
                        "timestamp": datetime.now().isoformat()
                    })
                    firebase_set("security/alarm_active", False)

                    threading.Thread(
                        target=_send_alarm_report,
                        args=(faces_snapshot, start_snapshot),
                        daemon=True
                    ).start()

def _send_alarm_report(face_paths: list, started_at: datetime):
    try:
        msg            = MIMEMultipart()
        msg["From"]    = SMTP_FROM
        msg["To"]      = ADMIN_EMAIL
        msg["Subject"] = f"[SmartClass ALERT] Shock event – {started_at.strftime('%Y-%m-%d %H:%M')}"

        html = f"""
        <h2 style="color:#c0392b">🚨 Security Alert — Shock Sensor Triggered</h2>
        <table style="font-family:Arial,sans-serif;font-size:14px">
          <tr><td style="color:#888;padding:4px 16px 4px 0">Event started</td>
              <td><b>{started_at.strftime('%Y-%m-%d %H:%M:%S')}</b></td></tr>
          <tr><td style="color:#888;padding:4px 16px 4px 0">Report generated</td>
              <td><b>{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</b></td></tr>
          <tr><td style="color:#888;padding:4px 16px 4px 0">Faces captured</td>
              <td><b>{len(face_paths)}</b></td></tr>
        </table>
        <p style="margin-top:16px;font-family:Arial,sans-serif;font-size:13px">
          All face images detected during the alarm window are attached.<br>
          Please review and take appropriate action.
        </p>
        <hr>
        <p style="color:#aaa;font-size:11px;font-family:Arial,sans-serif">SmartClass Automated Security System</p>
        """
        msg.attach(MIMEText(html, "html"))

        for path in face_paths:
            try:
                with open(path, "rb") as f:
                    data = f.read()
                part = MIMEImage(data, name=Path(path).name)
                part.add_header("Content-Disposition", "attachment", filename=Path(path).name)
                msg.attach(part)
            except Exception as e:
                print(f"[EMAIL] Could not attach {path}: {e}")

        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
            s.starttls()
            s.login(SMTP_FROM, SMTP_PASSWORD)
            s.sendmail(SMTP_FROM, ADMIN_EMAIL, msg.as_string())

        print(f"[EMAIL] ✅ Report sent to {ADMIN_EMAIL} ({len(face_paths)} images).")
    except Exception as e:
        print(f"[EMAIL] ❌ Failed: {e}")

# ── Sensor history pusher ──────────────────────────────────────────────────────
def sensor_history_pusher():
    """Periodically reads current sensors and pushes a snapshot to history/sensors."""
    while True:
        time.sleep(30)  # every 30 seconds
        try:
            raw = firebase_get("sensors")
            if raw:
                raw["timestamp"] = datetime.now().isoformat()
                key = firebase_push("history/sensors", raw)
                if key:
                    print(f"[HISTORY] Pushed sensor snapshot → {key}")
        except Exception as e:
            print(f"[HISTORY] Push error: {e}")



# ── Sensor history pusher ──────────────────────────────────────────────────────
def sensor_history_pusher():
    while True:
        time.sleep(30)
        try:
            raw = firebase_get("sensors")
            if raw:
                raw["timestamp"] = datetime.now().isoformat()
                key = firebase_push("history/sensors", raw)
                if key:
                    print(f"[HISTORY] Pushed sensor snapshot → {key}")
        except Exception as e:
            print(f"[HISTORY] Push error: {e}")

# ── Face encoding helpers ──────────────────────────────────────────────────────
def load_encodings():
    global known_encodings, known_names
    
    known_encodings = []
    known_names = []
    
    if ENCODINGS_FILE.exists():
        try:
            with open(ENCODINGS_FILE, "rb") as f:
                data = pickle.load(f)
            known_encodings = data.get("encodings", [])
            known_names = data.get("names", [])
            print(f"[INFO] Loaded {len(known_names)} encodings from cache.")
            if len(known_names) > 0:
                return
        except Exception as e:
            print(f"[ERROR] Failed to load encodings: {e}")
    
    rebuild_encodings()

def rebuild_encodings():
    global known_encodings, known_names
    known_encodings, known_names = [], []
    
    if not FACES_DIR.exists():
        print("[WARNING] Faces directory does not exist!")
        return
    
    face_count = 0
    for sd in sorted(FACES_DIR.iterdir()):
        if not sd.is_dir():
            continue
        
        person_name = sd.name
        person_faces = 0
        
        for img_path in sd.glob("*"):
            if img_path.suffix.lower() not in [".jpg", ".jpeg", ".png"]:
                continue
                
            try:
                img = face_recognition.load_image_file(str(img_path))
                face_locations = face_recognition.face_locations(img, model="hog")
                
                if not face_locations:
                    print(f"[WARNING] No face found in {img_path}")
                    continue
                
                encs = face_recognition.face_encodings(img, face_locations, num_jitters=2)
                
                if encs:
                    known_encodings.append(encs[0])
                    known_names.append(person_name)
                    person_faces += 1
                    face_count += 1
                    
            except Exception as e:
                print(f"[ERROR] Processing {img_path}: {e}")
        
        if person_faces > 0:
            print(f"[INFO] Loaded {person_faces} faces for {person_name}")
    
    try:
        with open(ENCODINGS_FILE, "wb") as f:
            pickle.dump({"encodings": known_encodings, "names": known_names}, f)
        print(f"[INFO] Saved {face_count} encodings for {len(set(known_names))} persons.")
    except Exception as e:
        print(f"[ERROR] Failed to save encodings: {e}")

def get_today_csv() -> Path:
    return ATTENDANCE_DIR / f"{date.today().isoformat()}.csv"

def mark_presence(name: str):
    now = datetime.now().strftime("%H:%M:%S")
    path = get_today_csv()
    
    if name in today_attendance:
        today_attendance[name]["count"] += 1
        today_attendance[name]["last_seen"] = now
        return
    
    today_attendance[name] = {"time": now, "count": 1}

    exists = path.exists()
    with open(path, "a", newline="") as f:
        w = csv.writer(f)
        if not exists:
            w.writerow(["Name", "Date", "Time"])
        w.writerow([name, date.today().isoformat(), now])
    print(f"[ATTENDANCE] {name} at {now}")

    firebase_push("history/attendance", {
        "name":      name,
        "date":      date.today().isoformat(),
        "time":      now,
        "timestamp": datetime.now().isoformat()
    })

    if name.strip().lower() == PROFESSOR_NAME.strip().lower():
        print("[FIREBASE] Professor detected → setting professor_present & door_open...")
        firebase_set("notifications/professor_present", True)
        firebase_set("controls/door_open", True)
        firebase_push("history/activity", {
            "event":     "professor_arrived",
            "label":     "Professor arrived",
            "timestamp": datetime.now().isoformat()
        })
    else:
        firebase_push("history/activity", {
            "event":     "student_detected",
            "label":     f"Student {name} detected",
            "name":      name,
            "timestamp": datetime.now().isoformat()
        })

def load_today_attendance():
    global today_attendance
    today_attendance = {}
    path = get_today_csv()
    if path.exists():
        with open(path) as f:
            for row in csv.DictReader(f):
                today_attendance[row["Name"]] = {"time": row["Time"], "count": 1}

# ═════════════════════════════════════════════════════════════════════════════
# ── Flask Routes ──────────────────────────────────────────────────────────────
# ═════════════════════════════════════════════════════════════════════════════

@app.route("/")
def index():
    templates_dir = BASE_DIR / "templates"
    if (templates_dir / "index.html").exists():
        return render_template("index.html")
    return send_from_directory(str(BASE_DIR), "index.html")

# ─── Frame Processing ─────────────────────────────────────────────────────────

@app.route("/api/frame", methods=["POST"])
def receive_frame():
    global _alarm_faces

    cam_state = get_camera_state()

    if not cam_state["camera_active"]:
        return jsonify({
            "faces_detected": 0,
            "results":        [],
            "timestamp":      datetime.now().isoformat(),
            "camera_state":   cam_state
        }), 200

    # Decode image
    try:
        if request.is_json:
            img_bytes = base64.b64decode(request.get_json()["image"])
        else:
            img_bytes = request.data
    except Exception as e:
        print(f"[ERROR] Failed to decode image: {e}")
        return jsonify({"error": "Failed to decode image", "camera_state": cam_state}), 400
    # inside receive_frame, after img_bytes = request.data
    global last_frame_bytes, last_frame_time
    last_frame_bytes = img_bytes
    last_frame_time = datetime.now()
    # Debug save
    if not hasattr(receive_frame, '_n'):
        receive_frame._n = 0
    receive_frame._n += 1
    
    if receive_frame._n % 10 == 0:
        try:
            dbg = DEBUG_DIR / f"frame_{receive_frame._n:06d}.jpg"
            dbg.write_bytes(img_bytes)
            print(f"[DEBUG] Saved frame #{receive_frame._n} ({len(img_bytes)} bytes)")
        except Exception as e:
            print(f"[DEBUG] Failed to save: {e}")

    # Decode image with OpenCV
    np_arr = np.frombuffer(img_bytes, np.uint8)
    frame = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)
    
    if frame is None:
        print("[ERROR] Failed to decode image with OpenCV")
        return jsonify({"error": "Invalid image format", "camera_state": cam_state}), 400

    if frame.size == 0:
        print("[ERROR] Empty frame received")
        return jsonify({"error": "Empty frame", "camera_state": cam_state}), 400

    print(f"[FRAME] Received frame: {frame.shape}, alarm={cam_state['alarm_active']}")

    # ========== IMPROVED FACE DETECTION ==========
    try:
        rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        
        height, width = rgb_frame.shape[:2]
        
        # Resize if too large (helps ESP32-CAM processing)
        max_size = 800
        if max(height, width) > max_size:
            scale = max_size / max(height, width)
            new_width = int(width * scale)
            new_height = int(height * scale)
            rgb_frame = cv2.resize(rgb_frame, (new_width, new_height))
            print(f"[FRAME] Resized to {new_height}x{new_width}")
        
        # Detect faces
        face_locations = face_recognition.face_locations(rgb_frame, model="hog")
        
        print(f"[FACE] Found {len(face_locations)} face(s)")
        
        if not face_locations:
            return jsonify({
                "faces_detected": 0,
                "results":        [],
                "timestamp":      datetime.now().isoformat(),
                "camera_state":   cam_state
            })
        
        # Get encodings with num_jitters for better accuracy
        face_encodings = face_recognition.face_encodings(
            rgb_frame, 
            face_locations, 
            num_jitters=2
        )
        
    except Exception as e:
        print(f"[ERROR] Face detection failed: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({
            "error": f"Face detection error: {str(e)}",
            "camera_state": cam_state
        }), 500
    # =============================================

    results = []
    
    if not known_encodings:
        print("[WARNING] No known encodings loaded! Cannot recognize faces.")
        for i, loc in enumerate(face_locations):
            results.append({
                "name": "Unknown (no database)", 
                "marked": False, 
                "location": loc
            })
    else:
        for face_encoding, face_location in zip(face_encodings, face_locations):
            name = "Unknown"
            marked = False
            confidence = 0.0
            
            try:
                matches = face_recognition.compare_faces(
                    known_encodings, 
                    face_encoding, 
                    tolerance=0.50
                )
                face_distances = face_recognition.face_distance(known_encodings, face_encoding)
                
                best_match_index = None
                if len(face_distances) > 0:
                    best_match_index = int(np.argmin(face_distances))
                    confidence = float(1 - face_distances[best_match_index])
                
                if best_match_index is not None and matches[best_match_index]:
                    name = known_names[best_match_index]
                    
                    if face_distances[best_match_index] < 0.55:
                        mark_presence(name)
                        marked = True
                        print(f"[RECOGNIZED] {name} (confidence: {confidence:.2%})")
                    else:
                        print(f"[LOW CONFIDENCE] {name} (distance: {face_distances[best_match_index]:.3f})")
                        name = "Unknown"
                else:
                    print(f"[UNKNOWN] Face not in database (best confidence: {confidence:.2%})")
                    
            except Exception as e:
                print(f"[ERROR] Face comparison failed: {e}")
            
            results.append({
                "name": name, 
                "marked": marked, 
                "location": face_location,
                "confidence": round(confidence, 3)
            })

    # Alarm mode: save face crops
    if cam_state["alarm_active"]:
        for i, (result, face_location) in enumerate(zip(results, face_locations)):
            try:
                top, right, bottom, left = face_location
                padding = 20
                top = max(0, top - padding)
                left = max(0, left - padding)
                bottom = min(frame.shape[0], bottom + padding)
                right = min(frame.shape[1], right + padding)
                
                crop = frame[top:bottom, left:right]
                
                if crop.size > 0:
                    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
                    safe_name = result["name"].replace(" ", "_")
                    fp = ALARM_FACES_DIR / f"face_{safe_name}_{ts}_{i}.jpg"
                    cv2.imwrite(str(fp), crop)
                    
                    with _state_lock:
                        _alarm_faces.append(str(fp))
                    
                    print(f"[ALARM] Saved crop: {fp.name}")
                    
                    firebase_push("history/security", {
                        "type":      "face_captured",
                        "name":      result["name"],
                        "file":      fp.name,
                        "timestamp": datetime.now().isoformat()
                    })
            except Exception as e:
                print(f"[ERROR] Failed to save crop: {e}")

    # Push sensor snapshot during alarm if faces detected
    if cam_state["alarm_active"] and results:
        try:
            raw_sensors = firebase_get("sensors")
            if raw_sensors:
                raw_sensors["timestamp"] = datetime.now().isoformat()
                raw_sensors["faces_detected"] = len(results)
                firebase_push("history/sensors", raw_sensors)
        except Exception as e:
            print(f"[ERROR] Failed to push sensors: {e}")

    return jsonify({
        "faces_detected": len(results),
        "results":        results,
        "timestamp":      datetime.now().isoformat(),
        "camera_state":   cam_state
    })


@app.route("/api/camera-state")
def camera_state_endpoint():
    return jsonify(get_camera_state())


# ─── Attendance ────────────────────────────────────────────────────────────────
# app.py – add after the existing attendance endpoints

@app.route("/api/attendance/student/<path:name>")
def get_student_attendance(name):
    """Return all dates (present AND absent) for a specific student."""
    all_csv = sorted(ATTENDANCE_DIR.glob("*.csv"), reverse=True)
    records = []
    for csv_path in all_csv:
        date_str = csv_path.stem
        present = False
        time_val = "-"
        try:
            with open(csv_path) as f:
                reader = csv.DictReader(f)
                for row in reader:
                    if row["Name"].strip().lower() == name.strip().lower():
                        present = True
                        time_val = row.get("Time", "-")
                        break
        except Exception:
            continue
        records.append({"date": date_str, "time": time_val, "present": present})
    return jsonify(records)


# --- Auth (dynamic, based on faces/ folder) ---

@app.route("/api/faces/list")
def list_faces():
    """Return all persons in faces/ with their role."""
    persons = []
    if FACES_DIR.exists():
        for d in sorted(FACES_DIR.iterdir()):
            if d.is_dir():
                role = "professor" if d.name.strip().lower() == PROFESSOR_NAME.strip().lower() else "student"
                persons.append({"name": d.name, "role": role})
    return jsonify(persons)


@app.route("/api/auth/login", methods=["POST"])
def auth_login():
    """
    Authenticate using a face-folder name + password.
    Default password for every account is 'demo123'.
    Custom passwords can be set in passwords.json: {"FolderName": "mypassword"}.
    The folder 'Professeur' gets role='professor'; all others get role='student'.
    """
    data = request.get_json() or {}
    name = data.get("name", "").strip()
    password = data.get("password", "")

    if not name:
        return jsonify({"error": "Name is required"}), 400

    matched_folder = None
    if FACES_DIR.exists():
        for d in FACES_DIR.iterdir():
            if d.is_dir() and d.name.strip().lower() == name.lower():
                matched_folder = d
                break

    if matched_folder is None:
        return jsonify({"error": "Account not found. Ask your professor to register you."}), 404

    PASSWORDS_FILE = BASE_DIR / "passwords.json"
    passwords = {}
    if PASSWORDS_FILE.exists():
        try:
            with open(PASSWORDS_FILE) as pf:
                passwords = json.load(pf)
        except Exception:
            pass

    expected = passwords.get(matched_folder.name, "demo123")
    if password != expected:
        return jsonify({"error": "Incorrect password"}), 401

    role = "professor" if matched_folder.name.strip().lower() == PROFESSOR_NAME.strip().lower() else "student"
    safe_email = matched_folder.name.lower().replace(" ", ".")

    return jsonify({"name": matched_folder.name, "role": role, "email": safe_email})




@app.route("/api/attendance/today")
def get_today():
    records = []
    path = get_today_csv()
    if path.exists():
        with open(path) as f:
            records = list(csv.DictReader(f))
    
    total = len(set(known_names))
    present_names = [r["Name"] for r in records]
    absent = [n for n in set(known_names) if n not in present_names]
    
    return jsonify({
        "date":              date.today().isoformat(),
        "records":           records,
        "total_present":     len(records),
        "total_students":    total,
        "absent":            total - len(records),
        "absent_names":      absent,
        "present_names":     present_names,
        "professor_present": PROFESSOR_NAME in present_names
    })


@app.route("/api/attendance/history")
def get_history():
    files = sorted(ATTENDANCE_DIR.glob("*.csv"), reverse=True)[:30]
    history = []
    for f in files:
        try:
            rows = list(csv.DictReader(open(f)))
            history.append({
                "date":  f.stem,
                "count": len(rows),
                "names": [r["Name"] for r in rows],
                "rate":  round(len(rows) / max(len(set(known_names)), 1) * 100, 1)
            })
        except Exception:
            pass
    return jsonify(history)


# ─── Students ─────────────────────────────────────────────────────────────────

@app.route("/api/students")
def get_students():
    students = []
    for d in sorted(FACES_DIR.iterdir()):
        if d.is_dir():
            photos = list(d.glob("*.jpg")) + list(d.glob("*.jpeg")) + list(d.glob("*.png"))
            students.append({
                "name":   d.name,
                "photos": len(photos),
                "role":   "professor" if d.name.lower() == PROFESSOR_NAME.lower() else "student"
            })
    return jsonify(students)


@app.route("/api/students/add", methods=["POST"])
def add_student():
    data = request.get_json()
    name = data.get("name", "").strip()
    imgs = data.get("images", [])
    role = data.get("role", "student")
    
    if not name:
        return jsonify({"error": "Name required"}), 400
    
    sd = FACES_DIR / name
    sd.mkdir(exist_ok=True)
    
    saved = 0
    for i, b64 in enumerate(imgs):
        try:
            img_data = base64.b64decode(b64)
            (sd / f"{i+1}.jpg").write_bytes(img_data)
            saved += 1
        except Exception as e:
            print(f"[ADD] Image {i} error: {e}")
    
    rebuild_encodings()
    
    firebase_set(f"users/{name.replace(' ','_')}", {"name": name, "role": role})
    
    return jsonify({
        "message":         f"Added {name} with {saved} photos.",
        "total_encodings": len(known_names),
        "unique_persons":  len(set(known_names))
    })


@app.route("/api/rebuild", methods=["POST"])
def rebuild():
    rebuild_encodings()
    return jsonify({
        "message":        "Encodings rebuilt.",
        "total":          len(known_names),
        "unique_persons": len(set(known_names))
    })


# ─── Sensors ───────────────────────────────────────────────

@app.route("/api/sensors", methods=["POST"])
def post_sensors():
    data = request.get_json()
    if not data:
        return jsonify({"error": "No data"}), 400

    ts = datetime.now().isoformat()
    payload = {
        "temperature":    data.get("temperature", 0),
        "humidity":       data.get("humidity", 0),
        "gas_detected":   data.get("gas_detected", 0),
        "light_detected": data.get("light_detected", 0),
        "shock_value":    data.get("shock_value", False),
        "timestamp":      ts
    }

    firebase_set("sensors", {k: v for k, v in payload.items() if k != "timestamp"})
    firebase_push("history/sensors", payload)

    print(f"[SENSORS] temp={payload['temperature']} hum={payload['humidity']} shock={payload['shock_value']}")
    return jsonify({"ok": True, "timestamp": ts})


@app.route("/api/sensors/current")
def get_current_sensors():
    data = firebase_get("sensors")
    return jsonify(data or {})


# ─── Controls ────────────────────────────────────────────────────

@app.route("/api/controls")
def get_controls():
    data = firebase_get("controls")
    return jsonify(data or {"door_open": False, "fan_on": False, "led_on": False})


@app.route("/api/controls/<key>", methods=["PUT"])
def set_control(key):
    allowed = {"door_open", "fan_on", "led_on"}
    if key not in allowed:
        return jsonify({"error": f"Unknown control '{key}'"}), 400
    
    data = request.get_json()
    val = data.get("value")
    if val is None:
        return jsonify({"error": "Missing 'value'"}), 400
    
    ok = firebase_set(f"controls/{key}", val)
    
    firebase_push("history/activity", {
        "event":     "control_change",
        "key":       key,
        "value":     val,
        "label":     f"{key.replace('_',' ').title()} set to {'ON' if val else 'OFF'}",
        "timestamp": datetime.now().isoformat()
    })
    
    return jsonify({"ok": ok, "key": key, "value": val})


# ─── Security ─────────────────────────────────────────────────

@app.route("/api/security")
def get_security():
    data = firebase_get("security")
    with _state_lock:
        return jsonify({
            "alarm_active":    _alarm_active,
            "shock_value":     _shock_value,
            "alarm_started":   _alarm_start_time.isoformat() if _alarm_start_time else None,
            "firebase_status": data or {}
        })


@app.route("/api/security/alarm", methods=["POST"])
def trigger_alarm():
    """Manually trigger or clear alarm (for testing / admin control)."""
    global _alarm_active, _alarm_start_time, _alarm_faces
    data   = request.get_json()
    active = data.get("active", False)
    with _state_lock:
        if active and not _alarm_active:
            _alarm_active     = True
            _alarm_start_time = datetime.now()
            _alarm_faces      = []
            firebase_set("security/alarm_active", True)
            firebase_set("security/alarm_started", datetime.now().isoformat())
            firebase_push("history/security", {
                "type": "alarm_manual_start",
                "details": "Alarm triggered manually via API",
                "timestamp": datetime.now().isoformat()
            })
        elif not active and _alarm_active:
            _alarm_active     = False
            _alarm_start_time = None
            _alarm_faces      = []
            firebase_set("security/alarm_active", False)
            firebase_push("history/security", {
                "type": "alarm_manual_end",
                "details": "Alarm cleared manually via API",
                "timestamp": datetime.now().isoformat()
            })
    return jsonify({"alarm_active": _alarm_active})

# ─── History ─────────────────────────────────────────────────

@app.route("/api/history/sensors")
def get_sensor_history():
    limit = int(request.args.get("limit", 50))
    raw = firebase_get("history/sensors")
    if not raw:
        return jsonify([])
    items = [{"key": k, **v} for k, v in raw.items()]
    items.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
    return jsonify(items[:limit])


@app.route("/api/history/security")
def get_security_history():
    limit = int(request.args.get("limit", 50))
    raw = firebase_get("history/security")
    if not raw:
        return jsonify([])
    items = [{"key": k, **v} for k, v in raw.items()]
    items.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
    return jsonify(items[:limit])


@app.route("/api/history/activity")
def get_activity_history():
    limit = int(request.args.get("limit", 20))
    raw = firebase_get("history/activity")
    if not raw:
        return jsonify([])
    items = [{"key": k, **v} for k, v in raw.items()]
    items.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
    return jsonify(items[:limit])


# ─── Notifications ────────────────────────────────────────────

@app.route("/api/notifications")
def get_notifications():
    data = firebase_get("notifications")
    return jsonify(data or {})


@app.route("/api/notifications", methods=["POST"])
def post_notification():
    data = request.get_json()
    key = data.get("key")
    val = data.get("value")
    if not key:
        return jsonify({"error": "Missing key"}), 400
    ok = firebase_set(f"notifications/{key}", val)
    return jsonify({"ok": ok})


# ─── Users ─────────────────────────────────────────────────

@app.route("/api/users")
def get_users():
    data = firebase_get("users")
    return jsonify(data or {})


# ─── Status ─────────────────────────────────────────────────

@app.route("/api/status")
def status():
    cam = get_camera_state()
    return jsonify({
        "students":          len(set(n for n in known_names if n.lower() != PROFESSOR_NAME.lower())),
        "total_encodings":   len(known_names),
        "unique_persons":    len(set(known_names)),
        "present_today":     len(today_attendance),
        "professor_present": PROFESSOR_NAME in today_attendance,
        "server_time":       datetime.now().isoformat(),
        "firebase_url":      FIREBASE_URL,
        "alarm_active":      _alarm_active,
        "shock_value":       _shock_value,
        **cam
    })


@app.route("/api/health")
def health():
    return jsonify({"status": "ok", "timestamp": datetime.now().isoformat()})


# ─── Alarm captures ───────────────────────────────────────

@app.route("/api/alarm-captures")
def list_alarm_captures():
    files = sorted(ALARM_FACES_DIR.glob("*.jpg"), reverse=True)[:20]
    return jsonify([
        {
            "filename": f.name, 
            "size": f.stat().st_size, 
            "modified": datetime.fromtimestamp(f.stat().st_mtime).isoformat()
        }
        for f in files
    ])


@app.route("/api/alarm-captures/<filename>")
def serve_capture(filename):
    return send_from_directory(str(ALARM_FACES_DIR), filename)

# app.py – add after other routes
last_frame_bytes = None
last_frame_time = None

@app.route("/api/latest-frame")
def latest_frame():
    global last_frame_bytes, last_frame_time
    if last_frame_bytes is None:
        return jsonify({"error": "No frame yet"}), 404
    return send_file(
        io.BytesIO(last_frame_bytes),
        mimetype='image/jpeg',
        as_attachment=False,
        download_name='latest.jpg'
    )
# ═════════════════════════════════════════════════════════════════════════════
# ── Start ──────────────────────────────────────────────────────────────────────
# ═════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("SmartClass IoT Backend - Starting up...")
    print("=" * 60)
    
    load_today_attendance()
    print(f"[INFO] Loaded {len(today_attendance)} attendance records for today")
    
    load_encodings()
    
    if not known_encodings:
        print("[WARNING] No face encodings loaded! Face recognition will not work.")
        print(f"[INFO] Please add face images to: {FACES_DIR}")
        print(f"[INFO] Folder structure: faces/PersonName/photo.jpg")
    else:
        print(f"[INFO] Ready to recognize {len(set(known_names))} persons")
    
    threading.Thread(target=shock_monitor, daemon=True, name="ShockMonitor").start()
    print(f"[INFO] Shock monitor started (poll every {SHOCK_POLL_INTERVAL}s)")
    
    threading.Thread(target=sensor_history_pusher, daemon=True, name="HistoryPusher").start()
    print("[INFO] Sensor history pusher started (push every 30s)")
    
    print("=" * 60)
    print(f"Server running on http://0.0.0.0:5001")
    print(f"Firebase: {FIREBASE_URL}")
    print("=" * 60)
    
    app.run(host="0.0.0.0", port=5001, debug=False, threaded=True)