"""Wi-Fi (NetworkManager, via nmcli) and Bluetooth (BlueZ, via bluetoothctl)
control for the fleet dashboard's Wi-Fi and Bluetooth popups.

Same "plain function + send_response callback" shape as remote_ops.py --
engine.py's dispatch runs each handler in its own thread (several of these
block for seconds: a Wi-Fi rescan, a Bluetooth scan, a network switch).

Every command runs as an argv list, never shell=True -- SSIDs, passwords
and device addresses all arrive over the network. Output is parsed with
LC_ALL=C so error matching doesn't depend on the robot's locale.

Privileges: nmcli/bluetoothctl normally work for the logged-in user
(polkit / BlueZ's D-Bus policy). A headless robot user often isn't allowed,
so run_privileged() retries through passwordless sudo, and only if that
also needs a password does the dashboard get asked for the robot's sudo
password -- the same "try passwordless first, prompt only when needed"
flow remote_ops.handle_reboot uses.

Things verified against real nmcli 1.46 / bluetoothctl 5.72 (not assumed):
- `bluetoothctl --timeout N <cmd>` waits the full N seconds even for an
  instant command like `list`, and with --timeout a FAILED connect/pair
  (e.g. "Device ... not available") still exits 0 -- so --timeout is only
  used for `scan`, and connect/pair success is read from the output text.
- nmcli -t escapes ':' inside values as '\\:' (every BSSID has five).
"""
import glob
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request

NMCLI_TIMEOUT_SEC = 10.0
WIFI_RESCAN_TIMEOUT_SEC = 30.0
# nmcli's own --wait for an activation (it defaults to 90s); the subprocess
# timeout around it is a little longer so nmcli reports its own timeout.
WIFI_CONNECT_WAIT_SEC = 45
# After switching networks, how long the robot keeps trying to reach the
# XPARO server through the new one before switching back to the previous
# network (only when the dashboard asked for auto-revert).
AUTO_REVERT_WINDOW_SEC = 45.0
REACHABILITY_POLL_SEC = 3.0
REACHABILITY_TIMEOUT_SEC = 5.0

BT_TIMEOUT_SEC = 10.0
BT_SCAN_SEC = 8
BT_CONNECT_TIMEOUT_SEC = 30.0
BT_PAIR_TIMEOUT_SEC = 45.0
# A busy room can list dozens of nearby devices; `bluetoothctl info` is one
# call per device.
MAX_BT_DEVICES = 60

MAX_SSID_BYTES = 32
MAC_RE = re.compile(r'^[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}$')
UUID_RE = re.compile(r'^[0-9A-Fa-f-]{36}$')
ANSI_RE = re.compile(r'\x1b\[[0-9;]*[A-Za-z]|[\x01\x02\r]')

_C_LOCALE_ENV = {**os.environ, "LC_ALL": "C", "LANG": "C"}

# One Wi-Fi change and one Bluetooth scan/action at a time -- two
# concurrent network switches from two dashboard tabs would fight each
# other (and the auto-revert of one would undo the other).
_wifi_change_lock = threading.Lock()
_bluetooth_lock = threading.Lock()


# ----------------------------------------------------------------------
# Command running
# ----------------------------------------------------------------------
def _run(argv, timeout, input_text=None):
    """(returncode, combined stdout+stderr). returncode is None when the
    command couldn't run at all (not installed, timed out)."""
    try:
        # stdin is closed unless something is fed in -- bluetoothctl
        # otherwise reads commands from whatever stdin it inherits.
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, env=_C_LOCALE_ENV,
            **({"input": input_text} if input_text is not None else {"stdin": subprocess.DEVNULL}),
        )
        return proc.returncode, ANSI_RE.sub('', (proc.stdout or "") + (proc.stderr or ""))
    except subprocess.TimeoutExpired:
        return None, f"{argv[0]} timed out after {timeout:.0f}s"
    except FileNotFoundError:
        return None, f"{argv[0]} is not installed"
    except Exception as e:
        return None, str(e)


_PERMISSION_MARKERS = (
    "not authorized", "insufficient privileges", "permission denied",
    "access denied", "accessdenied", "notpermitted", "not permitted",
)


def _is_permission_error(output):
    low = output.lower()
    return any(marker in low for marker in _PERMISSION_MARKERS)


def _sudo_wants_password(output):
    low = output.lower()
    return "password is required" in low or "incorrect password" in low or "sorry, try again" in low


def run_privileged(argv, timeout, sudo_password=None):
    """Runs argv as-is first; if the only problem is permissions, retries
    through `sudo -n` (passwordless sudo). Returns (returncode, output,
    needs_sudo_password).

    With sudo_password, goes straight to `sudo -S` with the password on
    stdin (never in argv, where `ps` would show it). -k makes sudo always
    read it, even with cached credentials -- otherwise the password line
    would be left on stdin for the command itself. needs_sudo_password is
    then True only if sudo rejected it.
    """
    if sudo_password:
        rc, out = _run(["sudo", "-k", "-S", "-p", "", "--"] + list(argv), timeout, input_text=sudo_password + "\n")
        return rc, out, (rc != 0 and _sudo_wants_password(out))
    rc, out = _run(argv, timeout)
    if rc == 0 or not _is_permission_error(out):
        return rc, out, False
    sudo_rc, sudo_out = _run(["sudo", "-n", "--"] + list(argv), timeout)
    if sudo_rc == 0:
        return sudo_rc, sudo_out, False
    if _sudo_wants_password(sudo_out):
        # Show the original, meaningful error ("not authorized to control
        # networking"), not sudo's own "a password is required".
        return rc, out, True
    return sudo_rc, sudo_out, False


def _sudo_message(sudo_password, what):
    if sudo_password:
        return "The robot rejected that sudo password."
    return f"This robot needs its sudo password to {what}."


def _first_error_line(output, fallback):
    for line in output.splitlines():
        line = line.strip()
        if line:
            return line[len("Error: "):] if line.startswith("Error: ") else line
    return fallback


# ----------------------------------------------------------------------
# nmcli parsing
# ----------------------------------------------------------------------
def nm_split(line):
    """Splits one `nmcli -t` line on unescaped ':' and unescapes '\\:' and
    '\\\\' -- a plain split(':') breaks every BSSID into six pieces."""
    fields, current, i = [], [], 0
    while i < len(line):
        ch = line[i]
        if ch == '\\' and i + 1 < len(line):
            current.append(line[i + 1])
            i += 2
            continue
        if ch == ':':
            fields.append(''.join(current))
            current = []
        else:
            current.append(ch)
        i += 1
    fields.append(''.join(current))
    return fields


def _nm_rows(output, width):
    rows = []
    for line in output.splitlines():
        if not line.strip():
            continue
        fields = nm_split(line)
        if len(fields) < width:
            fields += [''] * (width - len(fields))
        rows.append(fields[:width])
    return rows


def _band(freq_text):
    try:
        mhz = int(freq_text.split()[0])
    except (ValueError, IndexError):
        return None
    if mhz >= 5925:
        return "6 GHz"
    if mhz >= 4900:
        return "5 GHz"
    return "2.4 GHz"


def security_kind(security):
    """'open' | 'enterprise' | 'secured' -- what the dashboard needs to
    decide whether to ask for a password (and whether it can connect at
    all: 802.1X needs identities/certificates nmcli's quick connect can't
    take)."""
    sec = (security or "").strip()
    if sec in ("", "--"):
        return "open"
    if "802.1X" in sec:
        return "enterprise"
    return "secured"


def _wifi_devices():
    rc, out = _run(["nmcli", "-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device"], NMCLI_TIMEOUT_SEC)
    if rc != 0:
        return []
    return [
        {"ifname": dev, "state": state, "connection": conn or None}
        for dev, dev_type, state, conn in _nm_rows(out, 4)
        if dev_type == "wifi"
    ]


def _saved_wifi_profiles():
    """Saved Wi-Fi profiles with their real SSID -- a profile's NAME is
    usually its SSID but not always ("Home 2", renamed profiles)."""
    rc, out = _run(["nmcli", "-t", "-f", "NAME,UUID,TYPE,DEVICE,ACTIVE", "connection", "show"], NMCLI_TIMEOUT_SEC)
    if rc != 0:
        return []
    profiles = [
        {"name": name, "uuid": uuid, "device": device or None, "active": active == "yes"}
        for name, uuid, conn_type, device, active in _nm_rows(out, 5)
        if conn_type == "802-11-wireless"
    ]
    if not profiles:
        return []
    rc, out = _run(
        ["nmcli", "-t", "-f", "connection.uuid,802-11-wireless.ssid,connection.autoconnect", "connection", "show"]
        + [p["uuid"] for p in profiles],
        NMCLI_TIMEOUT_SEC,
    )
    details, current = {}, {}
    if rc == 0:
        for line in out.splitlines() + [""]:
            if not line.strip():
                if current.get("connection.uuid"):
                    details[current["connection.uuid"]] = current
                current = {}
                continue
            fields = nm_split(line)
            if len(fields) >= 2:
                current[fields[0]] = ':'.join(fields[1:])
    for profile in profiles:
        info = details.get(profile["uuid"], {})
        profile["ssid"] = info.get("802-11-wireless.ssid") or profile["name"]
        profile["autoconnect"] = info.get("connection.autoconnect", "yes") != "no"
    return profiles


def _active_wifi_connection(ifname=None):
    for profile in _saved_wifi_profiles():
        if profile["active"] and (ifname is None or profile["device"] == ifname):
            return profile
    return None


def _ip4_address(ifname):
    rc, out = _run(["nmcli", "-t", "-f", "IP4.ADDRESS", "device", "show", ifname], NMCLI_TIMEOUT_SEC)
    if rc != 0:
        return None
    for fields in _nm_rows(out, 2):
        if fields[0].startswith("IP4.ADDRESS") and fields[1]:
            return fields[1].split('/')[0]
    return None


def parse_wifi_list(output):
    """Rows of `nmcli -t -f IN-USE,BSSID,SSID,CHAN,FREQ,SIGNAL,SECURITY,DEVICE
    device wifi list`, grouped to one entry per SSID (an SSID usually has
    several access points/bands; the strongest one is shown). Hidden
    networks (empty SSID) are counted, not listed -- there's nothing to
    click on; they're joined by typing the name instead."""
    by_ssid, hidden = {}, 0
    for in_use, bssid, ssid, chan, freq, signal, security, device in _nm_rows(output, 8):
        if not ssid:
            hidden += 1
            continue
        try:
            signal_pct = int(signal)
        except ValueError:
            signal_pct = 0
        band = _band(freq)
        entry = by_ssid.get(ssid)
        if entry is None:
            entry = by_ssid[ssid] = {
                "ssid": ssid, "signal": signal_pct, "security": security if security != "--" else "",
                "security_kind": security_kind(security), "in_use": False, "bands": [],
                "bssid": bssid, "channel": chan, "device": device,
            }
        elif signal_pct > entry["signal"]:
            entry.update({"signal": signal_pct, "bssid": bssid, "channel": chan})
        if band and band not in entry["bands"]:
            entry["bands"].append(band)
        if in_use.strip() == "*":
            entry["in_use"] = True
            entry["device"] = device
    networks = sorted(by_ssid.values(), key=lambda n: (not n["in_use"], -n["signal"], n["ssid"].lower()))
    for n in networks:
        n["bands"].sort()
    return networks, hidden


# ----------------------------------------------------------------------
# Capability detection -- reported in robot info (data.connectivity) so
# the dashboard can disable the Wi-Fi/Bluetooth buttons up front instead
# of only finding out after a click.
# ----------------------------------------------------------------------
def _has_wifi_hardware():
    return bool(glob.glob('/sys/class/net/*/wireless') + glob.glob('/sys/class/net/*/phy80211'))


def _has_bluetooth_hardware():
    return bool(glob.glob('/sys/class/bluetooth/hci*'))


def detect_wifi():
    if not shutil.which("nmcli"):
        return {"available": False, "reason": (
            "This robot has Wi-Fi hardware, but NetworkManager (nmcli) isn't installed."
            if _has_wifi_hardware() else "No Wi-Fi hardware or NetworkManager found on this robot."
        )}
    rc, out = _run(["nmcli", "-t", "-f", "RUNNING", "general"], NMCLI_TIMEOUT_SEC)
    if rc != 0 or out.strip() != "running":
        return {"available": False, "reason": "NetworkManager isn't running on this robot."}
    devices = _wifi_devices()
    if not devices:
        return {"available": False, "reason": "No Wi-Fi hardware found on this robot."}
    managed = [d for d in devices if d["state"] != "unmanaged"]
    if not managed:
        return {"available": False, "reason": (
            "This robot's Wi-Fi hardware isn't managed by NetworkManager, so it can't be controlled from here."
        )}
    return {"available": True, "reason": "", "interfaces": [d["ifname"] for d in managed]}


def detect_bluetooth():
    has_adapter = _has_bluetooth_hardware()
    if not shutil.which("bluetoothctl"):
        return {"available": False, "reason": (
            "This robot has a Bluetooth adapter, but BlueZ (bluetoothctl) isn't installed."
            if has_adapter else "No Bluetooth hardware or BlueZ found on this robot."
        )}
    # `list` answers instantly when bluetoothd is up; when it's down,
    # bluetoothctl sits at "Waiting to connect to bluetoothd..." forever,
    # hence the subprocess timeout (not --timeout, see module docstring).
    rc, out = _run(["bluetoothctl", "list"], 5.0)
    if rc == 0 and "Controller " in out:
        return {"available": True, "reason": ""}
    if rc is None or "waiting to connect" in out.lower():
        return {"available": False, "reason": "The Bluetooth service (bluetoothd) isn't running on this robot."}
    return {"available": False, "reason": "No Bluetooth adapter found on this robot."}


def _wifi_summary(capability):
    """Adds the current network to a detect_wifi() result, for the robot
    info panel. Uses the cached scan list (no rescan -- this runs on every
    connect)."""
    if not capability.get("available"):
        return capability
    rc, out = _run(["nmcli", "-t", "-f", "WIFI", "radio"], NMCLI_TIMEOUT_SEC)
    summary = {**capability, "radio_enabled": rc == 0 and out.strip() == "enabled", "ssid": None, "signal": None}
    rc, out = _run(["nmcli", "-t", "-f", "ACTIVE,SSID,SIGNAL", "device", "wifi", "list", "--rescan", "no"], NMCLI_TIMEOUT_SEC)
    if rc == 0:
        for active, ssid, signal in _nm_rows(out, 3):
            if active == "yes":
                summary["ssid"] = ssid or None
                summary["signal"] = int(signal) if signal.isdigit() else None
                break
    return summary


def _bluetooth_summary(capability):
    if not capability.get("available"):
        return capability
    controller = _bluetooth_controller()
    return {**capability, "powered": bool(controller and controller["powered"]),
            "name": controller["name"] if controller else None}


def detect_connectivity():
    """{"wifi": {...}, "bluetooth": {...}} for robot info. Never raises --
    robot info must still be sent if probing fails."""
    result = {}
    for key, probe in (("wifi", lambda: _wifi_summary(detect_wifi())),
                       ("bluetooth", lambda: _bluetooth_summary(detect_bluetooth()))):
        try:
            result[key] = probe()
        except Exception as e:
            result[key] = {"available": False, "reason": f"Couldn't check: {e}"}
    return result


# ----------------------------------------------------------------------
# Wi-Fi
# ----------------------------------------------------------------------
def get_wifi_state(rescan=False):
    capability = detect_wifi()
    state = {
        **capability, "radio_enabled": False, "radio_hw_enabled": False, "interfaces": [],
        "networks": [], "hidden_count": 0, "saved": [], "error": None, "scanned_at": time.time(),
    }
    if not capability["available"]:
        return state

    rc, out = _run(["nmcli", "-t", "-f", "WIFI-HW,WIFI", "radio"], NMCLI_TIMEOUT_SEC)
    if rc == 0:
        fields = (_nm_rows(out, 2) or [["", ""]])[0]
        state["radio_hw_enabled"] = fields[0] in ("enabled", "missing")  # "missing" = no rfkill switch at all
        state["radio_enabled"] = fields[1] == "enabled"

    interfaces = []
    for device in _wifi_devices():
        if device["state"] == "unmanaged":
            continue
        device["ip4"] = _ip4_address(device["ifname"]) if device["state"] == "connected" else None
        interfaces.append(device)
    state["interfaces"] = interfaces

    saved = _saved_wifi_profiles()
    state["saved"] = saved

    if state["radio_enabled"]:
        list_argv = ["nmcli", "-t", "-f", "IN-USE,BSSID,SSID,CHAN,FREQ,SIGNAL,SECURITY,DEVICE", "device", "wifi", "list"]
        rc, out = _run(list_argv + ["--rescan", "yes" if rescan else "auto"],
                       WIFI_RESCAN_TIMEOUT_SEC if rescan else NMCLI_TIMEOUT_SEC)
        if rc != 0 and rescan:
            # e.g. "Scanning not allowed while unavailable" -- still show
            # whatever NetworkManager already knows about.
            state["error"] = _first_error_line(out, "Rescan failed")
            rc, out = _run(list_argv + ["--rescan", "no"], NMCLI_TIMEOUT_SEC)
        if rc == 0:
            networks, hidden = parse_wifi_list(out)
            saved_by_ssid = {}
            for profile in saved:
                saved_by_ssid.setdefault(profile["ssid"], profile)
            for network in networks:
                profile = saved_by_ssid.get(network["ssid"])
                network["saved"] = profile is not None
                network["saved_uuid"] = profile["uuid"] if profile else None
            state["networks"], state["hidden_count"] = networks, hidden
        elif not state["error"]:
            state["error"] = _first_error_line(out, "Couldn't list Wi-Fi networks")
    return state


def handle_get_wifi_networks(rescan, send_response):
    send_response({"WIFI_NETWORKS": get_wifi_state(rescan=rescan)})


def _classify_wifi_failure(output, password_given):
    """(message, needs_password) for a failed nmcli connect."""
    low = output.lower()
    if "secrets were required" in low or "property is missing" in low or "no secrets" in low:
        if password_given:
            return "The network rejected that password.", True
        return "This network needs a password.", True
    if "802-11-wireless-security.psk" in low and "invalid" in low:
        return "That password isn't valid for this network (WPA passwords are 8-63 characters).", True
    if "no network with ssid" in low:
        return ("Network not found. It may be out of range -- rescan, or if it's a hidden network, "
                "add it by name."), False
    if "timeout" in low or "timed out" in low:
        return "Timed out while connecting -- the network may be out of range or not handing out an address.", False
    return _first_error_line(output, "Couldn't connect."), False


def wifi_connect(ssid, password=None, ifname=None, hidden=False, sudo_password=None):
    """Connects and returns {"success", "message", "uuid", "needs_password",
    "needs_sudo_password"}.

    - A saved profile for this SSID is reused (`connection up`); a new
      password for it is written into that profile first, so a changed
      router password doesn't leave a stale duplicate profile behind.
    - Otherwise `nmcli device wifi connect` creates the profile. If that
      attempt fails, any profile it left behind is deleted, so the next
      try starts clean (older nmcli versions keep the broken profile, and
      every retry would then silently reuse its wrong password).

    NOTE: nmcli only takes a new Wi-Fi password as an argument (`password
    X` / `wifi-sec.psk X`); --ask is documented as not for scripts. The
    password is therefore visible in the process list for the few seconds
    nmcli runs, to users on the robot itself -- who can already read saved
    Wi-Fi passwords through NetworkManager anyway.
    """
    result = {"success": False, "message": "", "uuid": None, "needs_password": False, "needs_sudo_password": False}
    saved = [p for p in _saved_wifi_profiles() if p["ssid"] == ssid]
    existing = saved[0] if saved else None
    wait = ["--wait", str(WIFI_CONNECT_WAIT_SEC)]
    timeout = WIFI_CONNECT_WAIT_SEC + 15

    if existing is not None:
        if password:
            rc, out, needs_sudo = run_privileged(
                ["nmcli", "connection", "modify", "uuid", existing["uuid"], "802-11-wireless-security.psk", password],
                NMCLI_TIMEOUT_SEC, sudo_password,
            )
            if rc != 0:
                message, needs_password = _classify_wifi_failure(out, True)
                result.update(message=message, needs_password=needs_password, needs_sudo_password=needs_sudo)
                return result
        argv = ["nmcli"] + wait + ["connection", "up", "uuid", existing["uuid"]]
        if ifname:
            argv += ["ifname", ifname]
        rc, out, needs_sudo = run_privileged(argv, timeout, sudo_password)
        uuid = existing["uuid"]
    else:
        argv = ["nmcli"] + wait + ["device", "wifi", "connect", ssid]
        if password:
            argv += ["password", password]
        if ifname:
            argv += ["ifname", ifname]
        if hidden:
            argv += ["hidden", "yes"]
        rc, out, needs_sudo = run_privileged(argv, timeout, sudo_password)
        match = re.search(r"successfully activated with '([0-9a-fA-F-]{36})'", out)
        uuid = match.group(1) if match else None
        if rc != 0:
            for profile in _saved_wifi_profiles():
                if profile["ssid"] == ssid and not profile["active"]:
                    run_privileged(["nmcli", "connection", "delete", "uuid", profile["uuid"]],
                                   NMCLI_TIMEOUT_SEC, sudo_password)

    if rc == 0:
        if uuid is None:
            active = _active_wifi_connection(ifname)
            uuid = active["uuid"] if active and active["ssid"] == ssid else None
        result.update(success=True, uuid=uuid, message=f"Connected to {ssid}.")
        return result
    if needs_sudo:
        result.update(needs_sudo_password=True, message=_sudo_message(sudo_password, "change network settings"))
        return result
    message, needs_password = _classify_wifi_failure(out, bool(password))
    result.update(message=message, needs_password=needs_password)
    return result


def http_reachable(url, timeout=REACHABILITY_TIMEOUT_SEC):
    """Any HTTP answer at all (even a 404/403) means the server is
    reachable over the current network; only a connection-level failure
    doesn't."""
    try:
        with urllib.request.urlopen(urllib.request.Request(url, method="HEAD"), timeout=timeout):
            return True
    except urllib.error.HTTPError:
        return True
    except Exception:
        return False


def handle_wifi_connect(req, send_response, is_server_reachable=None, on_network_changed=None,
                        sleep=time.sleep, monotonic=time.monotonic):
    """req: {request_id, ssid, password?, ifname?, hidden?, sudo_password?,
    auto_revert?}. Replies with exactly one WIFI_CONNECT_RESULT.

    The robot's connection to the dashboard may be riding on the very Wi-Fi
    being changed, so engine.py passes a send_response that queues the
    result until the robot is back online (_send_important_dict), and an
    on_network_changed that makes the websocket reconnect at once. Without
    that, the old socket would be dead but still look open until TCP gave
    up on it, many minutes later.

    auto_revert: after switching, keep checking that the XPARO server is
    reachable through the new network; if it still isn't after
    AUTO_REVERT_WINDOW_SEC, switch back to the previous network, so a
    network with no internet (or a captive portal) can't strand a robot
    nobody can physically reach.
    """
    req = req or {}
    request_id = req.get("request_id")
    ssid = str(req.get("ssid") or "")
    password = req.get("password") or None
    ifname = req.get("ifname") or None
    sudo_password = req.get("sudo_password") or None
    base = {"request_id": request_id, "ssid": ssid, "success": False, "reverted": False,
            "needs_password": False, "needs_sudo_password": False}

    if not ssid or len(ssid.encode("utf-8")) > MAX_SSID_BYTES:
        send_response({"WIFI_CONNECT_RESULT": {**base, "message": "A Wi-Fi network name must be 1-32 bytes."}})
        return
    if not _wifi_change_lock.acquire(blocking=False):
        send_response({"WIFI_CONNECT_RESULT": {**base, "message": "Another Wi-Fi change is already in progress on this robot."}})
        return
    try:
        capability = detect_wifi()
        if not capability["available"]:
            send_response({"WIFI_CONNECT_RESULT": {**base, "message": capability["reason"]}})
            return
        previous = _active_wifi_connection(ifname)
        result = wifi_connect(ssid, password=password, ifname=ifname,
                              hidden=bool(req.get("hidden")), sudo_password=sudo_password)
        if not result["success"]:
            send_response({"WIFI_CONNECT_RESULT": {
                **base, "message": result["message"], "needs_password": result["needs_password"],
                "needs_sudo_password": result["needs_sudo_password"],
            }})
            return

        changed = previous is None or result["uuid"] is None or previous["uuid"] != result["uuid"]
        if changed and on_network_changed is not None:
            on_network_changed()

        if (req.get("auto_revert") and changed and previous is not None and is_server_reachable is not None):
            deadline = monotonic() + AUTO_REVERT_WINDOW_SEC
            reachable = False
            while True:
                if is_server_reachable():
                    reachable = True
                    break
                if monotonic() >= deadline:
                    break
                sleep(REACHABILITY_POLL_SEC)
            if not reachable:
                rc, out, _ = run_privileged(
                    ["nmcli", "--wait", str(WIFI_CONNECT_WAIT_SEC), "connection", "up", "uuid", previous["uuid"]],
                    WIFI_CONNECT_WAIT_SEC + 15, sudo_password,
                )
                if on_network_changed is not None:
                    on_network_changed()
                if rc == 0:
                    message = (f"Connected to {ssid}, but the robot couldn't reach XPARO through it within "
                               f"{AUTO_REVERT_WINDOW_SEC:.0f}s, so it switched back to {previous['ssid']}.")
                else:
                    message = (f"Connected to {ssid}, but the robot couldn't reach XPARO through it, and "
                               f"switching back to {previous['ssid']} failed: {_first_error_line(out, 'unknown error')}")
                send_response({"WIFI_CONNECT_RESULT": {
                    **base, "reverted": rc == 0, "reverted_to": previous["ssid"], "message": message,
                }})
                return

        send_response({"WIFI_CONNECT_RESULT": {
            **base, "success": True, "message": result["message"],
            "previous_ssid": previous["ssid"] if previous else None,
        }})
    finally:
        _wifi_change_lock.release()


def handle_wifi_forget(req, send_response):
    """Deletes a saved (inactive) profile. The active one is refused:
    deleting it disconnects the robot, which may be its only way back to
    the dashboard."""
    req = req or {}
    uuid = str(req.get("uuid") or "")
    base = {"request_id": req.get("request_id"), "action": "forget", "uuid": uuid, "success": False,
            "needs_sudo_password": False}
    if not UUID_RE.match(uuid):
        send_response({"WIFI_ACTION_RESULT": {**base, "message": "Unknown saved network."}})
        return
    profile = next((p for p in _saved_wifi_profiles() if p["uuid"] == uuid), None)
    if profile is None:
        send_response({"WIFI_ACTION_RESULT": {**base, "message": "That saved network no longer exists."}})
        return
    if profile["active"]:
        send_response({"WIFI_ACTION_RESULT": {**base, "message": (
            "That's the network the robot is using right now -- connect it to another network first."
        )}})
        return
    sudo_password = req.get("sudo_password") or None
    rc, out, needs_sudo = run_privileged(["nmcli", "connection", "delete", "uuid", uuid],
                                         NMCLI_TIMEOUT_SEC, sudo_password)
    send_response({"WIFI_ACTION_RESULT": {
        **base, "success": rc == 0, "needs_sudo_password": needs_sudo,
        "message": f"Forgot {profile['ssid']}." if rc == 0 else (
            _sudo_message(sudo_password, "change network settings") if needs_sudo
            else _first_error_line(out, "Couldn't forget that network.")
        ),
    }})


def handle_wifi_radio(req, send_response):
    """Turns the Wi-Fi radio on. Turning it off is deliberately not offered:
    on a robot that reaches the dashboard over Wi-Fi, that's a one-way trip."""
    req = req or {}
    base = {"request_id": req.get("request_id"), "action": "radio_on", "success": False, "needs_sudo_password": False}
    sudo_password = req.get("sudo_password") or None
    rc, out, needs_sudo = run_privileged(["nmcli", "radio", "wifi", "on"], NMCLI_TIMEOUT_SEC, sudo_password)
    send_response({"WIFI_ACTION_RESULT": {
        **base, "success": rc == 0, "needs_sudo_password": needs_sudo,
        "message": "Wi-Fi turned on." if rc == 0 else (
            _sudo_message(sudo_password, "change network settings") if needs_sudo
            else _first_error_line(out, "Couldn't turn Wi-Fi on.")
        ),
    }})


# ----------------------------------------------------------------------
# Bluetooth
# ----------------------------------------------------------------------
def _bluetooth_controller():
    rc, out = _run(["bluetoothctl", "show"], BT_TIMEOUT_SEC)
    if rc != 0 or not out.startswith("Controller "):
        return None
    header = out.splitlines()[0].split()
    props = _key_values(out)
    return {
        "address": header[1] if len(header) > 1 else None,
        "name": props.get("Alias") or props.get("Name"),
        "powered": props.get("Powered") == "yes",
        "discoverable": props.get("Discoverable") == "yes",
        "pairable": props.get("Pairable") == "yes",
        "discovering": props.get("Discovering") == "yes",
    }


def _key_values(output):
    """`bluetoothctl show/info` body -> {Key: first value}."""
    props = {}
    for line in output.splitlines()[1:]:
        line = line.strip()
        if ':' in line:
            key, _, value = line.partition(':')
            props.setdefault(key.strip(), value.strip())
    return props


def _parse_rssi(value):
    """'0xffffffc4 (-60)' (BlueZ 5.6x+) or '-60' (older)."""
    if not value:
        return None
    match = re.search(r'\((-?\d+)\)', value) or re.match(r'^(-?\d+)$', value)
    return int(match.group(1)) if match else None


def parse_bluetooth_devices(output):
    """`bluetoothctl devices` -> [(address, name)]."""
    devices = []
    for line in output.splitlines():
        parts = line.strip().split(' ', 2)
        if len(parts) >= 2 and parts[0] == "Device" and MAC_RE.match(parts[1]):
            devices.append((parts[1].upper(), parts[2] if len(parts) > 2 else ""))
    return devices


def parse_scan_rssi(output):
    """Last RSSI per device from `scan on` output ("[CHG] Device <addr>
    RSSI: 0xffffffb5 (-75)") -- BlueZ drops RSSI from `info` as soon as
    discovery stops, so this is the only place a scan's signal strength
    survives."""
    rssi = {}
    for match in re.finditer(r'Device ([0-9A-Fa-f:]{17}) RSSI: ([^\n]+)', output):
        value = _parse_rssi(match.group(2).strip())
        if value is not None:
            rssi[match.group(1).upper()] = value
    return rssi


def parse_bluetooth_info(address, fallback_name, output):
    props = _key_values(output) if output.startswith("Device ") else {}
    name = props.get("Alias") or props.get("Name") or fallback_name or address
    # BlueZ names a nameless device after its own address ("4B-8B-5C-...").
    named = bool(props.get("Name")) or (bool(name) and name.replace('-', ':').upper() != address)
    return {
        "address": address, "name": name, "named": named,
        "icon": props.get("Icon"),
        "paired": props.get("Paired") == "yes",
        "trusted": props.get("Trusted") == "yes",
        "connected": props.get("Connected") == "yes",
        "blocked": props.get("Blocked") == "yes",
        "rssi": _parse_rssi(props.get("RSSI")),
    }


def get_bluetooth_state(scan=False):
    capability = detect_bluetooth()
    state = {**capability, "controller": None, "devices": [], "error": None, "scanned": False}
    if not capability["available"]:
        return state
    controller = _bluetooth_controller()
    state["controller"] = controller
    scan_rssi = {}
    if scan:
        if controller and controller["powered"]:
            _, scan_out = _run(["bluetoothctl", "--timeout", str(BT_SCAN_SEC), "scan", "on"], BT_SCAN_SEC + 10)
            scan_rssi = parse_scan_rssi(scan_out)
            state["scanned"] = True
        else:
            state["error"] = "Turn Bluetooth on to scan for devices."
    rc, out = _run(["bluetoothctl", "devices"], BT_TIMEOUT_SEC)
    if rc != 0:
        state["error"] = _first_error_line(out, "Couldn't list Bluetooth devices")
        return state
    devices = []
    for address, name in parse_bluetooth_devices(out)[:MAX_BT_DEVICES]:
        rc, info = _run(["bluetoothctl", "info", address], BT_TIMEOUT_SEC)
        device = parse_bluetooth_info(address, name, info if rc == 0 else "")
        if device["rssi"] is None:
            device["rssi"] = scan_rssi.get(address)
        devices.append(device)
    devices.sort(key=lambda d: (not d["connected"], not d["paired"], -(d["rssi"] if d["rssi"] is not None else -999),
                                d["name"].lower()))
    state["devices"] = devices
    return state


def handle_get_bluetooth_devices(scan, send_response):
    if scan and not _bluetooth_lock.acquire(blocking=False):
        # A scan is already running -- answer with the current list rather
        # than starting a second discovery session on the same adapter.
        send_response({"BLUETOOTH_DEVICES": {**get_bluetooth_state(scan=False),
                                             "error": "A Bluetooth scan is already running on this robot."}})
        return
    try:
        send_response({"BLUETOOTH_DEVICES": get_bluetooth_state(scan=scan)})
    finally:
        if scan:
            _bluetooth_lock.release()


BLUETOOTH_ACTIONS = ("power_on", "power_off", "pair", "connect", "disconnect", "remove")
_BT_FAILURE_MARKERS = ("not available", "failed", "org.bluez.error", "error:")


def _bt_ok(rc, output, success_marker=None):
    low = output.lower()
    if success_marker is not None:
        return success_marker.lower() in low
    return rc == 0 and not any(marker in low for marker in _BT_FAILURE_MARKERS)


def _bt_failure_message(output, fallback):
    for line in output.splitlines():
        line = line.strip()
        if line and any(marker in line.lower() for marker in _BT_FAILURE_MARKERS):
            return line
    return _first_error_line(output, fallback)


def bluetooth_action(action, address=None, sudo_password=None):
    """Returns {"success", "message", "needs_sudo_password"}."""
    if action not in BLUETOOTH_ACTIONS:
        return {"success": False, "message": f"Unknown Bluetooth action: {action}", "needs_sudo_password": False}
    if action in ("power_on", "power_off"):
        state = "on" if action == "power_on" else "off"
        rc, out, needs_sudo = run_privileged(["bluetoothctl", "power", state], BT_TIMEOUT_SEC, sudo_password)
        if action == "power_on" and not _bt_ok(rc, out) and ("blocked" in out.lower() or "rfkill" in out.lower()):
            # Soft-blocked by rfkill -- `power on` can't lift that itself.
            _, _, needs_sudo = run_privileged(["rfkill", "unblock", "bluetooth"], BT_TIMEOUT_SEC, sudo_password)
            if not needs_sudo:
                time.sleep(1.0)
                rc, out, needs_sudo = run_privileged(["bluetoothctl", "power", state], BT_TIMEOUT_SEC, sudo_password)
        ok = _bt_ok(rc, out)
        return {"success": ok, "needs_sudo_password": needs_sudo and not ok,
                "message": f"Bluetooth turned {state}." if ok else _bt_failure_message(out, f"Couldn't turn Bluetooth {state}.")}

    address = str(address or "").upper()
    if not MAC_RE.match(address):
        return {"success": False, "message": "That isn't a valid Bluetooth device address.", "needs_sudo_password": False}

    if action == "pair":
        # NoInputNoOutput = "Just Works" pairing, which is what gamepads,
        # most headsets and BLE peripherals use. A device that insists on
        # a PIN/passkey fails with AuthenticationFailed/Rejected.
        rc, out, needs_sudo = run_privileged(
            ["bluetoothctl", "--agent", "NoInputNoOutput", "pair", address], BT_PAIR_TIMEOUT_SEC, sudo_password,
        )
        if not (_bt_ok(rc, out, "Pairing successful") or "alreadyexists" in out.lower()):
            low = out.lower()
            if "authentication" in low:
                message = ("This device wants a PIN or passkey confirmation, which can't be answered remotely. "
                           "Put it in pairing mode and try again, or pair it once on the robot itself.")
            elif "not available" in low:
                message = "The robot can't see that device any more -- put it in pairing mode and scan again."
            else:
                message = _bt_failure_message(out, "Pairing failed.")
            return {"success": False, "message": message, "needs_sudo_password": needs_sudo}
        # Trusted = the device may reconnect by itself later (e.g. a
        # gamepad after the robot reboots), then connect it now.
        run_privileged(["bluetoothctl", "trust", address], BT_TIMEOUT_SEC, sudo_password)
        rc, out, _ = run_privileged(["bluetoothctl", "connect", address], BT_CONNECT_TIMEOUT_SEC, sudo_password)
        if _bt_ok(rc, out, "Connection successful"):
            return {"success": True, "message": "Paired and connected.", "needs_sudo_password": False}
        return {"success": True, "needs_sudo_password": False,
                "message": f"Paired, but couldn't connect yet: {_bt_failure_message(out, 'connection failed')}"}

    argv, success_marker, done = {
        "connect": (["bluetoothctl", "connect", address], "Connection successful", "Connected."),
        "disconnect": (["bluetoothctl", "disconnect", address], None, "Disconnected."),
        "remove": (["bluetoothctl", "remove", address], None, "Removed -- it will need pairing again."),
    }[action]
    rc, out, needs_sudo = run_privileged(argv, BT_CONNECT_TIMEOUT_SEC, sudo_password)
    ok = _bt_ok(rc, out, success_marker)
    return {"success": ok, "needs_sudo_password": needs_sudo and not ok,
            "message": done if ok else _bt_failure_message(out, f"Couldn't {action}.")}


def handle_bluetooth_action(req, send_response):
    req = req or {}
    action = req.get("action")
    base = {"request_id": req.get("request_id"), "action": action, "address": req.get("address")}
    capability = detect_bluetooth()
    if not capability["available"]:
        send_response({"BLUETOOTH_ACTION_RESULT": {**base, "success": False, "needs_sudo_password": False,
                                                    "message": capability["reason"]}})
        return
    with _bluetooth_lock:
        result = bluetooth_action(action, req.get("address"), req.get("sudo_password") or None)
    if result["needs_sudo_password"]:
        result["message"] = _sudo_message(req.get("sudo_password"), "control Bluetooth")
    send_response({"BLUETOOTH_ACTION_RESULT": {**base, **result}})
