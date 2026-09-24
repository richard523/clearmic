"""PipeWire interop: live control values, runtime set-param, meters, monitor,
restart. All via the pw-* CLI tools (no native bindings needed)."""
import array
import ctypes
import json
import math
import os
import re
import signal
import subprocess
import threading
import time

FILTER_INPUT = "effect_input.rnnoise"    # graph-controls live on this node
FILTER_OUTPUT = "effect_output.rnnoise"  # virtual source

_PR_SET_PDEATHSIG = 1  # Linux prctl option


def _die_with_parent():
    """preexec_fn: ask the kernel to kill this child if the app dies,
    however it dies (crash, SIGKILL, ...). Prevents orphaned
    pw-loopback / pw-record processes routing the mic forever."""
    try:
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(
            _PR_SET_PDEATHSIG, signal.SIGTERM)
    except Exception:
        pass  # non-Linux: fall back to explicit stop() only


def kill_stale():
    """Kill orphaned pw-loopback/pw-record processes from a previous,
    abnormally-exited instance that still target our nodes. Called at
    startup, before this instance spawns its own."""
    patterns = [f"pw-loopback -C {FILTER_OUTPUT}",
                f"pw-record .*--target {FILTER_OUTPUT}"]
    killed = []
    for pat in patterns:
        r = subprocess.run(["pgrep", "-f", pat],
                           capture_output=True, text=True)
        for pid_s in r.stdout.split():
            try:
                os.kill(int(pid_s), signal.SIGTERM)
                killed.append(int(pid_s))
            except (ValueError, ProcessLookupError, PermissionError):
                pass
    return killed


def run(*args):
    return subprocess.run(args, capture_output=True, text=True)


def nodes():
    """Map of node.name -> node id (as str)."""
    r = run("pw-cli", "ls", "Node")
    out, cur = {}, None
    for line in r.stdout.splitlines():
        m = re.match(r"\s*id (\d+), type", line)
        if m:
            cur = m.group(1)
            continue
        m = re.match(r'\s*node\.name = "(.+)"', line)
        if m and cur is not None:
            out[m.group(1)] = cur
            cur = None
    return out


def filter_node_id():
    return nodes().get(FILTER_INPUT)


def filter_capture_device():
    """node.name of the source currently feeding the filter chain, or
    None (chain absent, or not linked to a capture device)."""
    r = run("pw-dump")
    try:
        d = json.loads(r.stdout)
    except json.JSONDecodeError:
        return None
    names = {}
    fid = None
    for o in d:
        props = o.get("info", {}).get("props", {}) or {}
        if o.get("type", "").endswith("Node"):
            names[o["id"]] = props.get("node.name")
            if props.get("node.name") == FILTER_INPUT:
                fid = o["id"]
    if fid is None:
        return None
    for o in d:
        if o.get("type") != "PipeWire:Interface:Link":
            continue
        props = o.get("info", {}).get("props", {}) or {}
        if props.get("link.input.node") == fid:
            return names.get(props.get("link.output.node"))
    return None


# ------------------------------------------------------- bluetooth keepalive
# Restarting PipeWire makes bluetoothd unregister its A2DP endpoints;
# BT headsets drop the ACL link and never come back on their own.

_MAC_RE = re.compile(r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")


def bt_connected_devices():
    """MACs of currently connected Bluetooth devices."""
    try:
        r = subprocess.run(["bluetoothctl", "devices", "Connected"],
                           capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return []
    macs = []
    for line in r.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and _MAC_RE.match(parts[1]):
            macs.append(parts[1])
    return macs


def _bt_is_connected(mac):
    try:
        r = subprocess.run(["bluetoothctl", "info", mac],
                           capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return re.search(r"^\s*Connected: yes", r.stdout, re.M) is not None


def bt_reconnect(macs):
    """Reconnect any of `macs` that the restart dropped. Returns the
    MACs actually reconnected."""
    restored = []
    for mac in macs:
        if _bt_is_connected(mac):
            continue
        try:
            subprocess.run(["bluetoothctl", "connect", mac],
                           capture_output=True, timeout=12)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if _bt_is_connected(mac):
            restored.append(mac)
    return restored


def get_controls():
    """Live control values: {'<stage>:<port>': value} for the running chain."""
    nid = filter_node_id()
    if nid is None:
        return {}
    r = run("pw-dump", nid)
    try:
        d = json.loads(r.stdout)
    except json.JSONDecodeError:
        return {}
    ctrl = {}
    for obj in d:
        for p in obj.get("info", {}).get("params", {}).get("Props", []):
            if "params" not in p:
                continue
            v = p["params"]
            for i in range(0, len(v) - 1, 2):
                key, val = v[i], v[i + 1]
                ctrl[key] = val
    return ctrl


def _fmt_num(v):
    v = float(v)
    if v.is_integer():
        return f"{v:.1f}"
    return repr(round(v, 6))


def set_controls(pairs, toggle_keys=frozenset()):
    """Apply {'<stage>:<port>': value} live via a single pw-cli set-param."""
    nid = filter_node_id()
    if nid is None:
        raise RuntimeError("filter chain is not running")
    items = []
    for k, v in pairs.items():
        items.append(json.dumps(k))
        if k in toggle_keys:
            items.append("true" if float(v) >= 0.5 else "false")
        else:
            items.append(_fmt_num(v))
    arg = "{ params: [ " + ", ".join(items) + " ] }"
    r = run("pw-cli", "set-param", nid, "Props", arg)
    if r.returncode != 0:
        raise RuntimeError(f"pw-cli set-param failed: {r.stderr.strip()}")


def input_devices():
    """[(node.name, description)] for capture devices: ALSA or Bluetooth
    (BT mics appear as bluez_input.*/bluez_output.* Audio/Source nodes,
    present only while the headset is connected in HFP/HSP profile)."""
    r = run("pw-dump")
    try:
        d = json.loads(r.stdout)
    except json.JSONDecodeError:
        return []
    devs = []
    for obj in d:
        props = obj.get("info", {}).get("props", {}) or {}
        name = props.get("node.name", "")
        if (name.startswith("alsa_input.")
                or name.startswith("bluez_input.")
                or name.startswith("bluez_output.")) and \
                props.get("media.class") == "Audio/Source":
            devs.append((name, props.get("node.description", name)))
    return devs


# ------------------------------------------------------- bluetooth profiles
# A BT headset has two mutually exclusive states: an A2DP profile
# (high-quality playback, NO capture node) or a headset profile
# (mic available, low-quality playback). ClearMic surfaces both.

def bt_cards():
    """Bluetooth audio cards: [{card, desc, profile, mic}] via pactl.
    mic is True when the active profile exposes a capture source
    (headset-head-unit / HFP or HSP)."""
    r = run("pactl", "list", "cards")
    out, cur = [], None
    for line in r.stdout.splitlines():
        if line.startswith("Card #"):
            if cur and cur["card"].startswith("bluez_card."):
                cur["mic"] = cur["profile"].startswith("headset")
                out.append(cur)
            cur = {"card": "", "desc": "", "profile": ""}
            continue
        if cur is None:
            continue
        s = line.strip()
        if s.startswith("Name: "):
            cur["card"] = s[6:]
        elif s.startswith("Active Profile: "):
            cur["profile"] = s[16:]
        elif s.startswith("device.description = "):
            cur["desc"] = s[21:].strip('"')
    if cur and cur["card"].startswith("bluez_card."):
        cur["mic"] = cur["profile"].startswith("headset")
        out.append(cur)
    return out


def _card_profiles(card):
    """Names of the card's available profiles, from pactl."""
    r = run("pactl", "list", "cards")
    profiles, in_profiles, cur = [], False, None
    for line in r.stdout.splitlines():
        s = line.strip()
        if s.startswith("Name: "):
            cur = s[6:]
            in_profiles = False
        elif cur == card and s == "Profiles:":
            in_profiles = True
        elif in_profiles and s.startswith("Active Profile"):
            break
        elif in_profiles and ":" in s and s.endswith(")") and \
                not s.endswith("no)") and "available" in s:
            profiles.append(s.split(":", 1)[0].strip())
    return profiles


def bt_set_mic_mode(card, timeout=15.0):
    """Switch a bluetooth card to a headset (mic) profile and wait for
    its capture node to register. Returns the bluez_input node name,
    or None if the switch failed or the node never appeared."""
    r = run("pactl", "set-card-profile", card, "headset-head-unit")
    if r.returncode != 0:
        # device without HFP: fall back to any available headset profile
        prof = next((p for p in _card_profiles(card)
                     if p.startswith("headset")), None)
        if prof is None or \
                run("pactl", "set-card-profile", card,
                    prof).returncode != 0:
            return None
    mac = card.split(".", 1)[1]
    key = mac.replace(":", "").replace("_", "").lower()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for name, _ in input_devices():
            if name.startswith("bluez_input.") and \
                    name.split(".", 1)[1].replace(":", "") \
                    .replace("_", "").lower() == key:
                return name
        time.sleep(0.5)
    return None


def _bt_reconnect(card, timeout=25.0):
    """Disconnect + reconnect a bluetooth device (by card name) so that
    bluetoothd re-registers its media endpoints. A PipeWire restart
    while the device is connected can leave the A2DP endpoints
    unregistered, leaving the card with only the headset profile.
    Returns True once the card offers an A2DP profile again."""
    mac = card.split(".", 1)[1].replace("_", ":")
    try:
        subprocess.run(["bluetoothctl", "disconnect", mac],
                       capture_output=True, timeout=8)
        subprocess.run(["bluetoothctl", "connect", mac],
                       capture_output=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        pass
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        r = run("pactl", "list", "cards")
        if f"Name: {card}" in r.stdout and \
                any(p.startswith("a2dp") for p in _card_profiles(card)):
            return True
        time.sleep(0.5)
    return False


def bt_set_quality_mode(card):
    """Switch a bluetooth card back to A2DP (high-quality playback,
    no mic). If the A2DP endpoints were lost (PipeWire restart while
    connected), the device is reconnected first to re-register them.
    Tearing down an active HFP transport can wedge PipeWire (bluez5
    bug on this stack), so the daemon is probed afterwards.
    Returns "ok", "wedged" (caller should restart PipeWire) or
    "failed"."""
    prof = next((p for p in _card_profiles(card) if p.startswith("a2dp")),
                None)
    if prof is None:
        if not _bt_reconnect(card):
            return "failed"
        prof = next((p for p in _card_profiles(card)
                     if p.startswith("a2dp")), None)
        if prof is None:
            return "failed"
    if run("pactl", "set-card-profile", card, prof).returncode != 0:
        return "failed"
    try:
        r = subprocess.run(["pw-cli", "ls", "Node"], capture_output=True,
                           timeout=6)
        if r.returncode == 0:
            return "ok"
    except subprocess.TimeoutExpired:
        pass
    return "wedged"


def restart_pipewire(timeout=25.0):
    """Restart the PipeWire user services; wait for the chain to return.
    Bluetooth devices that were connected before the restart and got
    dropped by it are reconnected."""
    bt_before = bt_connected_devices()
    subprocess.run(
        ["systemctl", "--user", "restart",
         "pipewire.service", "pipewire-pulse.service", "wireplumber.service"],
        capture_output=True)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(0.25)
        if filter_node_id() is not None:
            time.sleep(0.5)   # let links settle
            restored = bt_reconnect(bt_before)
            return True, restored
    restored = bt_reconnect(bt_before)   # chain still down, restore BT anyway
    return False, restored


class Meter:
    """Peak level meter fed by `pw-record` raw s16 mono from a target node."""

    def __init__(self, target):
        self.target = target
        self.proc = None
        self._lock = threading.Lock()
        self.level = 0.0        # linear peak, 0..1
        self._stop = threading.Event()

    def start(self):
        self._stop.clear()
        self.proc = subprocess.Popen(
            ["pw-record", "--raw", "--format", "s16", "--channels", "1",
             "--rate", "48000", "--latency", "20ms",
             "--target", self.target, "-"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            preexec_fn=_die_with_parent)
        t = threading.Thread(target=self._read, daemon=True)
        t.start()

    def _read(self):
        chunk = 4096
        while not self._stop.is_set() and self.proc and \
                self.proc.poll() is None:
            data = self.proc.stdout.read(chunk)
            if not data:
                break
            buf = array.array("h")
            buf.frombytes(data)
            peak = max((abs(x) for x in buf), default=0) / 32768.0
            with self._lock:
                self.level = peak

    def get_dbfs(self):
        with self._lock:
            lv = self.level
        if lv <= 0.0:
            return -99.0
        return 20.0 * math.log10(lv)

    def stop(self):
        self._stop.set()
        if self.proc:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.proc.kill()
            self.proc = None
        self.level = 0.0


class Monitor:
    """Route the processed mic to the default sink (Listen)."""

    def __init__(self):
        self.proc = None

    @property
    def active(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self):
        if self.active:
            return
        self.proc = subprocess.Popen(
            ["pw-loopback", "-C", FILTER_OUTPUT],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            preexec_fn=_die_with_parent)

    def stop(self):
        if self.proc:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.proc.kill()
            self.proc = None
