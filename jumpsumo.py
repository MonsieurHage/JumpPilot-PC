"""Pure-Python controller for Parrot Jumping Sumo / Jumping Night / Jumping Race.
Ported from the C core of Jump Pilot (JPClient.c / JPProtocol.c).
No native compiler required. Works on Windows, Linux, macOS.
"""
import socket, json, struct, threading, time, ctypes, logging, os

def rd16(p): return p[0] | (p[1] << 8)
def rd32(p): return p[0] | (p[1]<<8) | (p[2]<<16) | (p[3]<<24)
def wr16(p, v): p[0]=v & 0xff; p[1]=(v>>8)&0xff
def wr32(p, v):
    p[0]=v&0xff; p[1]=(v>>8)&0xff; p[2]=(v>>16)&0xff; p[3]=(v>>24)&0xff

def encode_frame(out_type, buffer, seq, data):
    n = len(data)
    out = bytearray(7 + n)
    out[0] = out_type
    out[1] = buffer
    out[2] = seq
    wr32(out[3:], n + 7)  # size field at offset 3
    out[7:7+n] = data
    return bytes(out)

def next_frames(packet):
    """Yield (type, buffer, sequence, data) frames from a UDP packet."""
    off = 0
    n = len(packet)
    while off < n:
        if n - off < 7:
            break
        h = packet[off:]
        t = h[0]; b = h[1]; s = h[2]
        length = rd32(h[3:7])
        if t < 1 or t > 4 or length < 7 or length > n - off:
            break
        data = packet[off+7: off+length]
        yield t, b, s, data
        off += length

def make_command(project, cls, cmd, args=b''):
    out = bytearray(4 + len(args))
    out[0] = project
    out[1] = cls
    out[2] = cmd & 0xff
    out[3] = (cmd >> 8) & 0xff
    out[4:4+len(args)] = args
    return bytes(out)

def make_pcmd(speed, turn):
    def clamp(v): return max(-100, min(100, v))
    out = bytearray(7)
    out[0]=3; out[1]=0; out[2]=0; out[3]=0
    out[4] = 1 if (speed or turn) else 0
    out[5] = clamp(speed) & 0xff
    out[6] = clamp(turn) & 0xff
    return bytes(out)

def parse_discovery(text):
    # text is bytes/str JSON. Return (port, fragment_size, max_fragments) or None.
    import json
    try:
        if isinstance(text, (bytes, bytearray)):
            text = text.decode('utf-8', errors='replace')
        # strip trailing NUL
        text = text.rstrip('\x00').strip()
        # find JSON object
        start = text.find('{')
        end = text.rfind('}')
        if start < 0 or end < start:
            return None
        obj = json.loads(text[start:end+1])
    except Exception:
        return None
    if obj.get('status', -1) != 0:
        return None
    port = obj.get('c2d_port')
    fs = obj.get('arstream_fragment_size', 65000)
    mx = obj.get('arstream_fragment_maximum_number', 4)
    if not isinstance(port, int) or port < 1 or port > 65535:
        return None
    if fs < 1 or fs > 65000 or mx < 1 or mx > 128 or fs*mx > 8*1024*1024:
        return None
    return port, fs, mx


class JumpSumo:
    def __init__(self, host='192.168.2.1', discovery_port=44444, on_state=None, on_battery=None, on_alert=None):
        self.host = host
        self.discovery_port = discovery_port
        self.on_state = on_state
        self.on_battery = on_battery
        self.on_alert = on_alert
        self.lock = threading.Lock()
        self.stop = False
        self.connected = False
        self.armed = False
        self.speed = 0
        self.turn = 0
        self.input_time = 0.0
        self.halt_pending = False
        self.critical_battery = False
        self.q = []  # command queue
        self.qlock = threading.Lock()
        self.pending = None  # (bytes, action, time, retries)
        self.last_jump = 0.0
        self.last_pcmd = 0.0
        self.last_recv = 0.0
        self.thread = None
        self.started = False
        self.fd = None
        self.dest_port = 0
        self.sequences = [0]*256
        self.seen = [False]*256
        self.last_seq = [0]*256

    def now(self):
        return time.monotonic()

    def connect(self):
        if self.started:
            self.disconnect()
        self.stop = False
        with self.lock:
            self.armed = False; self.speed = 0; self.turn = 0
            self.halt_pending = False; self.critical_battery = False
            self.q = []
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.started = True
        self.thread.start()

    def disconnect(self):
        self.stop = True
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=3)
        self.started = False
        self.connected = False
        if self.fd:
            try: self.fd.close()
            except: pass
            self.fd = None

    def arm(self, enabled):
        with self.lock:
            if not enabled:
                self.armed = False; self.speed = 0; self.turn = 0; self.halt_pending = True
                # purge movement actions, keep init commands
                self.q = [c for c in self.q if not c[1]]
                if self.connected:
                    self.q.append((make_command(3,2,1,None) if False else make_command(3,2,1,b''), 0))
                    self.q.append((make_command(3,2,4,b'\x00'*4), 0))
                return
            self.armed = self.connected and not self.critical_battery
            self.speed = 0; self.turn = 0; self.input_time = self.now()

    def drive(self, speed, turn):
        with self.lock:
            if self.armed and self.connected:
                self.speed = speed; self.turn = turn; self.input_time = self.now()

    def halt(self):
        with self.lock:
            self.armed = False; self.speed = 0; self.turn = 0; self.halt_pending = True
            self.q = [c for c in self.q if not c[1]]
            if self.connected:
                self.q.append((make_command(3,2,1,b''), 0))
                self.q.append((make_command(3,2,4,b'\x00'*4), 0))

    def jump(self, high):
        # high: True/1 = high jump, False/0 = long jump
        with self.lock:
            t = self.now()
            if not (self.connected and self.armed and t - self.last_jump >= 3.0):
                return -1
            arg = struct.pack('<I', 1 if high else 0)
            # queue command with action=1
            cmd = make_command(3, 2, 3, arg)
            self.q.append((cmd, 1))
            self.last_jump = t
            self.speed = 0; self.turn = 0; self.input_time = t
            return 0

    def _state(self, value, msg):
        if self.on_state: self.on_state(value, msg)

    def _battery(self, v):
        if self.on_battery: self.on_battery(v)

    def _alert(self, v):
        if self.on_alert: self.on_alert(v)

    def _discover(self, d2c_port):
        import socket as s
        fd = s.socket(s.AF_INET, s.SOCK_STREAM)
        fd.settimeout(9.0)
        try:
            fd.connect((self.host, self.discovery_port))
        except Exception as e:
            msg = "TCP connect failed: %r" % e
            print(msg, flush=True)
            logging.info(msg) if False else None
            return None, msg
        req = ('{"d2c_port":%d,"controller_type":"PC","controller_name":"Jump Pilot PC"}\x00' % d2c_port).encode()
        try:
            fd.sendall(req)
        except Exception as e:
            fd.close(); return None, "send failed: %r" % e
        # receive
        data = b''
        deadline = time.time() + 9.0
        result = None
        while time.time() < deadline:
            try:
                fd.settimeout(max(0.1, deadline-time.time()))
                chunk = fd.recv(4096)
                if not chunk:
                    break
                data += chunk
                print("DISCOVERY RAW:", repr(data[:500]), flush=True)
                res = parse_discovery(data)
                if res is not None:
                    result = res; break
                if b'\x00' in data:
                    break
            except socket.timeout:
                break
            except Exception as e:
                print("recv exc:", repr(e), flush=True)
                break
        fd.close()
        if result is None:
            result = parse_discovery(data)
        if result is None:
            return None, "no valid discovery response; raw=%r" % (data[:300],)
        return result, None

    def _run(self):
        self._state(1, "Connexion au drone.")
        # UDP socket
        fd = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            fd.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 2*1024*1024)
        except Exception:
            pass
        fd.bind(('0.0.0.0', 0))
        fd.setblocking(False)
        self.fd = fd
        local_port = fd.getsockname()[1]
        # discovery
        res, err = self._discover(local_port)
        if self.stop:
            self._cleanup(fd); return
        if res is None:
            self._state(3, "Drone inaccessible. Vérifiez son Wi-Fi puis réessayez.")
            self._cleanup(fd); return
        c2d_port, fs, mx = res
        self.dest_port = c2d_port
        dest = (self.host, c2d_port)
        # init commands
        with self.lock:
            self.connected = False
            self.q = []
            self.q.append((make_command(0,2,0,b''), 0))  # Common.Settings.AllSettings
            self.q.append((make_command(0,4,0,b''), 0))  # Common.Common.AllStates
            en = bytes([1])
            self.q.append((make_command(3,18,0,en), 0))  # VideoEnable
        last_recv = time.monotonic()
        last_pcmd = 0.0
        pending = None  # (bytes, action, t0, retries)
        active = False
        sequences = [0]*256
        seen = [False]*256
        last_seq = [0]*256
        # helper to send
        def sendto(data):
            try:
                fd.sendto(data, dest)
            except Exception:
                pass
        def transmit(pkt_type, buffer, data, save=None):
            seqno = sequences[buffer]
            frame = encode_frame(pkt_type, buffer, seqno, data)
            sequences[buffer] = (sequences[buffer] + 1) & 0xff
            sendto(frame)
            return frame
        while not self.stop:
            t = time.monotonic()
            # read speed/turn
            with self.lock:
                armed = self.armed
                speed = self.speed
                turn = self.turn
                halt = self.halt_pending
                self.halt_pending = False
                # get next command
                cmd = None
                if self.q:
                    cmd = self.q.pop(0)
            if halt and pending is not None and pending[1]:
                pending = None
            # PCMD
            if armed and (t - self.input_time <= 0.350):
                if t - last_pcmd >= 0.05 or halt:
                    pcmd = make_pcmd(speed, turn)
                    transmit(2, 10, pcmd)
                    last_pcmd = t
            # command queue
            if pending is None and cmd is not None:
                cbytes, caction = cmd
                frame = transmit(4, 11, cbytes)
                pending = (frame, caction, t, 0)
            elif pending is not None and (t - pending[2] >= 0.150):
                if pending[3] >= 5:
                    if pending[1]:
                        self._state(4 if False else 1, "Saut non confirmé par le drone.")
                    pending = None
                else:
                    sendto(pending[0])
                    pending = (pending[0], pending[1], t, pending[3]+1)
            # receive
            ready = True
            batch = 0
            while batch < 32:
                try:
                    pkt, addr = fd.recvfrom(65536)
                except BlockingIOError:
                    break
                except Exception:
                    break
                if not pkt:
                    break
                if addr[0] != self.host:
                    batch += 1
                    continue
                # validate frames
                frames = list(next_frames(pkt))
                # check validity: parse all first
                ok = True
                off = 0
                # simple: if next_frames yields and no exception, ok
                last_recv = t
                if not active:
                    active = True
                    with self.lock:
                        self.connected = True
                    self._state(2, "Connecté à commandes désactivées")
                for (ftype, buf, seq, fdata) in frames:
                    if ftype == 4 and buf < 128:
                        # ACK
                        ack_seq = bytes([seq])
                        transmit(1, buf + 128, ack_seq)
                    if ftype == 1:
                        if buf == 139 and len(fdata) == 1 and pending is not None and len(pending[0]) >= 3 and fdata[0] == pending[0][2]:
                            pending = None
                        continue
                    if buf == 0:
                        transmit(2, 1, fdata)
                        continue
                    if buf == 125:
                        # video fragment - skip (no video handling)
                        continue
                    if buf in (126, 127):
                        continue
                    # buffer 126/127 handled above; process others
                    # battery / alert handling for buffer 0? Actually battery comes in frames with buffer != 126/127
                    # The C code checks frame.buffer != 126 && !=127 then processes battery/alert.
                    # But we already continued for 126/127. For other buffers, process:
                    if len(fdata) >= 5 and fdata[0] == 0 and fdata[1] == 5:
                        try:
                            bat = fdata[4]
                            if bat <= 100:
                                self._battery(bat)
                        except Exception:
                            pass
                    if len(fdata) >= 8 and fdata[0] == 3 and fdata[1] == 1:
                        try:
                            import struct as st
                            alert = st.unpack('<I', fdata[4:8])[0]
                            self._alert(alert)
                            if alert == 1:
                                self.halt()
                        except Exception:
                            pass
                    if len(fdata) >= 8 and fdata[0] == 3 and fdata[1] == 3:
                        try:
                            val = rd32(fdata[4:8])
                            if val:
                                self._state(1, "Mécanisme de saut bloqué ou trop chaud.")
                        except Exception:
                            pass
                batch += 1
            # watchdog
            if time.monotonic() - last_recv > 3.0 and active:
                self._state(3, "Liaison perdue. Commandes désactivées ; reconnectez-vous.")
                with self.lock:
                    self.connected = False; self.armed = False
                break
            time.sleep(0.01)
        # cleanup: best effort stop
        try:
            zero = make_pcmd(0, 0)
            for _ in range(3):
                transmit(2, 10, zero)
            cancel = make_command(3, 2, 1, b'')
            transmit(4, 11, cancel)
            off = bytes([0])
            disable = make_command(3, 18, 0, off)
            transmit(4, 11, disable)
        except Exception:
            pass
        self._cleanup(fd)
        if not self.stop:
            pass
        if not (self.stop):
            self._state(0, "Déconnecté")
        with self.lock:
            self.connected = False; self.armed = False

    def _cleanup(self, fd):
        try: fd.close()
        except: pass
        self.fd = None

    def is_ready(self):
        return self.connected


# Standalone test
if __name__ == '__main__':
    def on_state(v, m): print("STATE", v, m, flush=True)
    def on_bat(v): print("BATTERY", v, flush=True)
    def on_alert(v): print("ALERT", v, flush=True)
    js = JumpSumo(on_state=on_state, on_battery=on_bat, on_alert=on_alert)
    js.connect()
    print("waiting for connection...")
    for i in range(50):
        if js.connected:
            break
        time.sleep(0.2)
    if js.connected:
        print("CONNECTED. arming...")
        js.arm(True)
        time.sleep(1)
        js.drive(30, 0)
        time.sleep(2)
        js.drive(0, 0)
        js.halt()
        print("done")
    else:
        print("NOT CONNECTED")
    time.sleep(2)
    js.disconnect()