"""PipeWire interop: live control values, runtime set-param, meters, monitor,
restart. All via the pw-* CLI tools (no native bindings needed)."""
import array
import json
import math
import re
import subprocess
import threading
import time

FILTER_INPUT = "effect_input.rnnoise"    # graph-controls live on this node
FILTER_OUTPUT = "effect_output.rnnoise"  # virtual source


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
    """[(node.name, description)] for ALSA capture devices."""
    r = run("pw-dump")
    try:
        d = json.loads(r.stdout)
    except json.JSONDecodeError:
        return []
    devs = []
    for obj in d:
        props = obj.get("info", {}).get("props", {}) or {}
        name = props.get("node.name", "")
        if name.startswith("alsa_input.") and \
                props.get("media.class") == "Audio/Source":
            devs.append((name, props.get("node.description", name)))
    return devs


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
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
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
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def stop(self):
        if self.proc:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.proc.kill()
            self.proc = None
