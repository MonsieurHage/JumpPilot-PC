"""Serveur local Jump Pilot PC.
Pilote le robot via le module Python pur jumpsumo (protocole Parrot ARSDK).
Aucun compilateur natif requis.
"""
import os, sys, time, threading, logging
logging.basicConfig(filename=os.path.join(os.path.dirname(os.path.abspath(__file__)), "jump_pilot.log"),
                    level=logging.INFO, format="%(asctime)s %(message)s")
def logmsg(m):
    print(m, flush=True)
    logging.info(m)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from jumpsumo import JumpSumo
from flask import Flask, request, jsonify, send_from_directory

app = Flask(__name__, static_folder="static")

robot = None
robot_lock = threading.Lock()
connected = False
battery = -1
posture = "unknown"
last_error = ""

def on_state(value, msg):
    global connected, last_error
    # value: 0=offline,1=connecting,2=connected,3=failed,4=...
    if value == 2:
        connected = True
        last_error = ""
    elif value == 3:
        connected = False
        last_error = msg
    elif value == 0:
        connected = False
    logmsg(f"[state] {value}: {msg}")

def on_battery(v):
    global battery
    try:
        battery = int(v)
    except Exception:
        battery = v
    logmsg(f"[battery] {battery}")

def on_alert(v):
    logmsg(f"[alert] {v}")

def connect_robot():
    global robot, connected, battery, last_error
    with robot_lock:
        if robot is not None:
            try:
                robot.disconnect()
            except Exception:
                pass
            robot = None
        connected = False
        battery = -1
        last_error = ""
        robot = JumpSumo(host='192.168.2.1', on_state=on_state, on_battery=on_battery, on_alert=on_alert)
    robot.connect()
    # wait up to 12s
    for _ in range(60):
        if connected or last_error:
            break
        time.sleep(0.2)
    if connected and robot is not None:
        robot.arm(True)
    return connected

@app.route("/")
def index():
    return send_from_directory("static", "index.html")

@app.route("/api/status")
def status():
    return jsonify({"connected": connected, "battery": battery, "posture": posture, "error": last_error})

@app.route("/api/connect", methods=["POST"])
def api_connect():
    global last_error
    ok = connect_robot()
    return jsonify({"connected": ok, "error": last_error})

@app.route("/api/cmd", methods=["POST"])
def api_cmd():
    global posture
    data = request.get_json(silent=True) or {}
    action = data.get("action")
    speed = int(data.get("speed", 40))
    speed = max(0, min(127, speed))
    if robot is None or not connected:
        return jsonify({"ok": False, "error": "not connected"}), 400
    try:
        if action == "forward":
            robot.drive(speed, 0)
        elif action == "backward":
            robot.drive(-speed, 0)
        elif action == "left":
            robot.drive(0, -speed)
        elif action == "right":
            robot.drive(0, speed)
        elif action == "stop":
            robot.halt()
            robot.drive(0, 0)
        elif action == "highJump":
            robot.jump(True)
        elif action == "longJump":
            robot.jump(False)
        elif action == "postureJumper":
            robot.arm(True); posture = "jumper"
        elif action == "postureKicker":
            posture = "kicker"
        elif action == "postureStanding":
            posture = "standing"
        else:
            return jsonify({"ok": False, "error": "unknown action"}), 400
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

if __name__ == "__main__":
    threading.Thread(target=connect_robot, daemon=True).start()
    logmsg("Jump Pilot PC -> http://127.0.0.1:8000")
    app.run(host="127.0.0.1", port=8000, debug=False, threaded=True)