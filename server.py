"""Serveur local Jump Pilot PC.
Lance un serveur Flask sur http://127.0.0.1:8000 qui sert l'interface web
et pilote le robot via le backend pysumo.exe (socketio sur localhost:4532).
"""
import os, sys, time, threading, subprocess, atexit

# Localiser pysumo.exe
SP_PATH = None
for p in sys.path:
    cand = os.path.join(p, "pysumo", "pysumo.exe")
    if os.path.exists(cand):
        SP_PATH = cand
        break
if SP_PATH is None:
    # fallback: site-packages
    import site
    for d in site.getsitepackages():
        cand = os.path.join(d, "pysumo", "pysumo.exe")
        if os.path.exists(cand):
            SP_PATH = cand
            break

import socketio
from flask import Flask, request, jsonify, send_from_directory

app = Flask(__name__, static_folder="static")
drone_proc = None
sio = None
drone_connected = False
battery = -1
posture = "unknown"
conn_lock = threading.Lock()

def start_backend():
    global drone_proc
    if drone_proc is not None and drone_proc.poll() is None:
        return
    if SP_PATH and os.path.exists(SP_PATH):
        drone_proc = subprocess.Popen(SP_PATH, creationflags=0x00000008)
        atexit.register(lambda: drone_proc.kill() if drone_proc and drone_proc.poll() is None else None)

def connect_drone():
    global sio, drone_connected, battery, posture
    with conn_lock:
        if sio is not None:
            try:
                sio.disconnect()
            except Exception:
                pass
            sio = None
        drone_connected = False
    start_backend()
    time.sleep(2)
    sio = socketio.Client()
    @sio.on("connect")
    def on_connect():
        sio.emit("pythonConnected")
    @sio.on("disconnect")
    def on_disconnect():
        global drone_connected
        drone_connected = False
    @sio.on("droneConnected")
    def on_droneConnected():
        global drone_connected, posture
        drone_connected = True
        posture = "jumper"
        sio.emit("postureJumper")
    @sio.on("postureJumper")
    def on_pj():
        global posture
        posture = "jumper"
    @sio.on("postureKicker")
    def on_pk():
        global posture
        posture = "kicker"
    @sio.on("postureStanding")
    def on_ps():
        global posture
        posture = "standing"
    @sio.on("battery")
    def on_bat(data):
        global battery
        try:
            battery = int(data)
        except Exception:
            battery = data
    @sio.on("ping")
    def on_ping():
        sio.emit("pong")
    try:
        sio.connect("http://localhost:4532", wait_timeout=10)
    except Exception as e:
        print("connect error:", e)
        return False
    # wait for droneConnected
    for _ in range(50):
        if drone_connected:
            break
        time.sleep(0.2)
    return drone_connected

def emit(cmd, payload=None):
    global sio, drone_connected
    if sio is None or not drone_connected:
        return False
    try:
        if payload is None:
            sio.emit(cmd)
        else:
            sio.emit(cmd, payload)
        return True
    except Exception as e:
        print("emit error:", e)
        return False

# ---------- routes ----------
@app.route("/")
def index():
    return send_from_directory("static", "index.html")

@app.route("/api/status")
def status():
    return jsonify({"connected": drone_connected, "battery": battery, "posture": posture})

@app.route("/api/connect", methods=["POST"])
def api_connect():
    ok = connect_drone()
    return jsonify({"connected": ok})

@app.route("/api/cmd", methods=["POST"])
def api_cmd():
    data = request.get_json(silent=True) or {}
    action = data.get("action")
    speed = int(data.get("speed", 40))
    speed = max(0, min(127, speed))
    mapping = {
        "forward": ("forward", {"speed": speed}),
        "backward": ("backward", {"speed": speed}),
        "left": ("left", {"speed": speed}),
        "right": ("right", {"speed": speed}),
        "stop": ("droneStop", None),
        "highJump": ("animationsHighJump", None),
        "longJump": ("animationsLongJump", None),
        "postureJumper": ("postureJumper", None),
        "postureKicker": ("postureKicker", None),
        "postureStanding": ("postureStanding", None),
        "animStop": ("animationsStop", None),
    }
    if action not in mapping:
        return jsonify({"ok": False, "error": "unknown action"}), 400
    cmd, payload = mapping[action]
    ok = emit(cmd, payload)
    # update posture locally for jumps
    if action in ("highJump", "longJump"):
        global posture
        posture = "moving"
    return jsonify({"ok": ok})

if __name__ == "__main__":
    # connexion initiale en arrière-plan
    threading.Thread(target=connect_drone, daemon=True).start()
    print("Jump Pilot PC -> http://127.0.0.1:8000")
    app.run(host="127.0.0.1", port=8000, debug=False, threaded=True)