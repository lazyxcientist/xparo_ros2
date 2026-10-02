"""Covers connectivity.py (Wi-Fi via nmcli, Bluetooth via bluetoothctl).
Commands are faked at connectivity._run, so nothing here touches the
machine's real network or Bluetooth. The command-output samples below are
copied from real nmcli 1.46 / bluetoothctl 5.72 runs."""
import threading

import pytest

from xparo import connectivity as c


class FakeRunner:
    """Answers each argv with the first matching (prefix -> (rc, output))
    rule; records every call."""

    def __init__(self, rules):
        self.rules = rules
        self.calls = []

    def __call__(self, argv, timeout, input_text=None):
        self.calls.append((list(argv), input_text))
        for prefix, reply in self.rules:
            if list(argv[:len(prefix)]) == list(prefix):
                return reply(argv) if callable(reply) else reply
        raise AssertionError(f"unexpected command: {argv}")

    def ran(self, prefix):
        return [argv for argv, _ in self.calls if argv[:len(prefix)] == list(prefix)]


@pytest.fixture
def tools(monkeypatch):
    monkeypatch.setattr(c.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(c, "_has_wifi_hardware", lambda: True)
    monkeypatch.setattr(c, "_has_bluetooth_hardware", lambda: True)


def install(monkeypatch, rules):
    runner = FakeRunner(rules)
    monkeypatch.setattr(c, "_run", runner)
    return runner


WIFI_LIST = (
    "*:AA\\:16\\:65\\:F3\\:6A\\:4F:Xpankaj:11:2462 MHz:79:WPA2:wlp0s20f3\n"
    " :DC\\:EA\\:E7\\:AF\\:79\\:61:Jai Shree Ram:5:2432 MHz:75:WPA2:wlp0s20f3\n"
    " :DC\\:EA\\:E7\\:AF\\:79\\:5F:Jai Shree Ram:36:5180 MHz:47:WPA2:wlp0s20f3\n"
    " :DE\\:62\\:79\\:45\\:02\\:38::3:2422 MHz:57:WPA2:wlp0s20f3\n"
    " :11\\:22\\:33\\:44\\:55\\:66:Cafe\\:Guest:6:2437 MHz:40::wlp0s20f3\n"
    " :11\\:22\\:33\\:44\\:55\\:77:Office:1:2412 MHz:30:WPA2 802.1X:wlp0s20f3\n"
)
DEVICES = "wlp0s20f3:wifi:connected:Xpankaj\np2p-dev-wlp0s20f3:wifi-p2p:disconnected:\nlo:loopback:connected (externally):lo\n"
CONNECTIONS = (
    "Xpankaj:8d22d937-0fdf-497f-b321-46622088e37b:802-11-wireless:wlp0s20f3:yes\n"
    "lo:dd31c9c7-061f-4d99-8d75-283694e2ce58:loopback:lo:yes\n"
    "Home 2:4fdbd2bf-bfb3-4b2c-94dc-cd77176c65ca:802-11-wireless::no\n"
)
CONNECTION_DETAILS = (
    "connection.uuid:8d22d937-0fdf-497f-b321-46622088e37b\n802-11-wireless.ssid:Xpankaj\nconnection.autoconnect:yes\n\n"
    "connection.uuid:4fdbd2bf-bfb3-4b2c-94dc-cd77176c65ca\n802-11-wireless.ssid:Jai Shree Ram\nconnection.autoconnect:yes\n"
)
XPANKAJ = "8d22d937-0fdf-497f-b321-46622088e37b"
HOME2 = "4fdbd2bf-bfb3-4b2c-94dc-cd77176c65ca"


def wifi_rules(overrides=None):
    rules = {
        ("nmcli", "-t", "-f", "RUNNING", "general"): (0, "running\n"),
        ("nmcli", "-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device"): (0, DEVICES),
        ("nmcli", "-t", "-f", "NAME,UUID,TYPE,DEVICE,ACTIVE", "connection", "show"): (0, CONNECTIONS),
        ("nmcli", "-t", "-f", "connection.uuid,802-11-wireless.ssid,connection.autoconnect"): (0, CONNECTION_DETAILS),
        ("nmcli", "-t", "-f", "WIFI-HW,WIFI", "radio"): (0, "enabled:enabled\n"),
        ("nmcli", "-t", "-f", "WIFI", "radio"): (0, "enabled\n"),
        ("nmcli", "-t", "-f", "IP4.ADDRESS", "device", "show"): (0, "IP4.ADDRESS[1]:172.31.160.241/24\n"),
        ("nmcli", "-t", "-f", "IN-USE,BSSID,SSID,CHAN,FREQ,SIGNAL,SECURITY,DEVICE"): (0, WIFI_LIST),
        ("nmcli", "-t", "-f", "ACTIVE,SSID,SIGNAL"): (0, "yes:Xpankaj:79\nno:Jai Shree Ram:75\n"),
    }
    rules.update(overrides or {})
    return list(rules.items())


# ------------------------------------------------------------------
# nmcli parsing
# ------------------------------------------------------------------
def test_nm_split_unescapes_colons_and_backslashes():
    assert c.nm_split("*:AA\\:16\\:65:My\\\\Net:79") == ["*", "AA:16:65", "My\\Net", "79"]
    assert c.nm_split("a::b") == ["a", "", "b"]


def test_parse_wifi_list_groups_by_ssid_and_classifies_security():
    networks, hidden = c.parse_wifi_list(WIFI_LIST)
    by_ssid = {n["ssid"]: n for n in networks}

    assert hidden == 1  # the empty-SSID row
    assert networks[0]["ssid"] == "Xpankaj" and networks[0]["in_use"]  # in-use first
    assert networks[0]["bssid"] == "AA:16:65:F3:6A:4F"
    # Two access points of one SSID -> one entry, strongest signal, both bands.
    assert by_ssid["Jai Shree Ram"]["signal"] == 75
    assert by_ssid["Jai Shree Ram"]["bands"] == ["2.4 GHz", "5 GHz"]
    assert by_ssid["Cafe:Guest"]["security_kind"] == "open"  # escaped colon inside an SSID
    assert by_ssid["Office"]["security_kind"] == "enterprise"
    assert by_ssid["Xpankaj"]["security_kind"] == "secured"


# ------------------------------------------------------------------
# Capability detection
# ------------------------------------------------------------------
def test_detect_wifi_without_nmcli_names_what_is_missing(monkeypatch):
    monkeypatch.setattr(c.shutil, "which", lambda name: None)
    monkeypatch.setattr(c, "_has_wifi_hardware", lambda: True)
    result = c.detect_wifi()
    assert result["available"] is False
    assert "nmcli" in result["reason"] and "Wi-Fi hardware" in result["reason"]


def test_detect_wifi_networkmanager_not_running(monkeypatch, tools):
    install(monkeypatch, [(("nmcli",), (8, "Error: NetworkManager is not running.\n"))])
    assert c.detect_wifi() == {"available": False, "reason": "NetworkManager isn't running on this robot."}


def test_detect_wifi_no_wifi_device(monkeypatch, tools):
    install(monkeypatch, wifi_rules({
        ("nmcli", "-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device"): (0, "eth0:ethernet:connected:Wired\n"),
    }))
    result = c.detect_wifi()
    assert result["available"] is False and "No Wi-Fi hardware" in result["reason"]


def test_detect_wifi_unmanaged_device_is_not_controllable(monkeypatch, tools):
    install(monkeypatch, wifi_rules({
        ("nmcli", "-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device"): (0, "wlan0:wifi:unmanaged:\n"),
    }))
    result = c.detect_wifi()
    assert result["available"] is False and "isn't managed by NetworkManager" in result["reason"]


def test_detect_bluetooth_daemon_down(monkeypatch, tools):
    install(monkeypatch, [(("bluetoothctl", "list"), (None, "bluetoothctl timed out after 5s"))])
    result = c.detect_bluetooth()
    assert result["available"] is False and "bluetoothd" in result["reason"]


def test_detect_bluetooth_no_adapter(monkeypatch, tools):
    install(monkeypatch, [(("bluetoothctl", "list"), (0, ""))])
    assert c.detect_bluetooth()["reason"] == "No Bluetooth adapter found on this robot."


def test_detect_connectivity_reports_both_and_never_raises(monkeypatch, tools):
    install(monkeypatch, wifi_rules() + [
        (("bluetoothctl", "list"), (0, "Controller 78:AF:08:71:BA:9B robot [default]\n")),
        (("bluetoothctl", "show"), (0, "Controller 78:AF:08:71:BA:9B (public)\n\tAlias: robot\n\tPowered: yes\n")),
    ])
    result = c.detect_connectivity()
    assert result["wifi"] == {"available": True, "reason": "", "interfaces": ["wlp0s20f3"],
                              "radio_enabled": True, "ssid": "Xpankaj", "signal": 79}
    assert result["bluetooth"] == {"available": True, "reason": "", "powered": True, "name": "robot"}

    def boom(*a, **k):
        raise RuntimeError("probe blew up")
    monkeypatch.setattr(c, "detect_wifi", boom)
    assert c.detect_connectivity()["wifi"]["available"] is False


# ------------------------------------------------------------------
# Wi-Fi state
# ------------------------------------------------------------------
def test_get_wifi_state_marks_saved_networks_by_real_ssid(monkeypatch, tools):
    runner = install(monkeypatch, wifi_rules())
    state = c.get_wifi_state(rescan=True)

    assert state["available"] and state["radio_enabled"]
    assert state["interfaces"] == [{"ifname": "wlp0s20f3", "state": "connected", "connection": "Xpankaj",
                                    "ip4": "172.31.160.241"}]
    by_ssid = {n["ssid"]: n for n in state["networks"]}
    # Profile "Home 2" is saved for SSID "Jai Shree Ram" -- matched by SSID, not name.
    assert by_ssid["Jai Shree Ram"]["saved"] and by_ssid["Jai Shree Ram"]["saved_uuid"] == HOME2
    assert not by_ssid["Office"]["saved"]
    assert runner.ran(["nmcli", "-t", "-f", "IN-USE,BSSID,SSID,CHAN,FREQ,SIGNAL,SECURITY,DEVICE"])[0][-2:] == ["--rescan", "yes"]


def test_get_wifi_state_radio_off_skips_scan(monkeypatch, tools):
    runner = install(monkeypatch, wifi_rules({("nmcli", "-t", "-f", "WIFI-HW,WIFI", "radio"): (0, "enabled:disabled\n")}))
    state = c.get_wifi_state(rescan=True)
    assert state["radio_enabled"] is False and state["radio_hw_enabled"] is True
    assert state["networks"] == []
    assert not runner.ran(["nmcli", "-t", "-f", "IN-USE,BSSID,SSID,CHAN,FREQ,SIGNAL,SECURITY,DEVICE"])


def test_failed_rescan_still_returns_cached_list(monkeypatch, tools):
    def wifi_list(argv):
        if argv[-1] == "yes":
            return 1, "Error: Scanning not allowed while unavailable.\n"
        return 0, WIFI_LIST
    install(monkeypatch, wifi_rules({("nmcli", "-t", "-f", "IN-USE,BSSID,SSID,CHAN,FREQ,SIGNAL,SECURITY,DEVICE"): wifi_list}))
    state = c.get_wifi_state(rescan=True)
    assert state["error"] == "Scanning not allowed while unavailable."
    assert len(state["networks"]) == 4


# ------------------------------------------------------------------
# Privileges
# ------------------------------------------------------------------
def test_run_privileged_falls_back_to_passwordless_sudo(monkeypatch):
    runner = install(monkeypatch, [
        (("nmcli",), (1, "Error: Connection activation failed: Not authorized to control networking.\n")),
        (("sudo", "-n", "--", "nmcli"), (0, "Device 'wlan0' successfully activated\n")),
    ])
    rc, out, needs = c.run_privileged(["nmcli", "radio", "wifi", "on"], 5)
    assert (rc, needs) == (0, False)
    assert runner.ran(["sudo", "-n", "--", "nmcli", "radio", "wifi", "on"])


def test_run_privileged_asks_for_sudo_password_and_keeps_original_error(monkeypatch):
    install(monkeypatch, [
        (("nmcli",), (1, "Error: Not authorized to control networking.\n")),
        (("sudo", "-n"), (1, "sudo: a password is required\n")),
    ])
    rc, out, needs = c.run_privileged(["nmcli", "radio", "wifi", "on"], 5)
    assert needs is True and "Not authorized" in out


def test_run_privileged_sends_sudo_password_on_stdin_never_argv(monkeypatch):
    runner = install(monkeypatch, [(("sudo", "-k", "-S"), (0, ""))])
    c.run_privileged(["nmcli", "radio", "wifi", "on"], 5, sudo_password="hunter2")
    argv, stdin = runner.calls[0]
    assert "hunter2" not in " ".join(argv)
    assert stdin == "hunter2\n"


def test_run_privileged_does_not_sudo_for_ordinary_failures(monkeypatch):
    runner = install(monkeypatch, [(("nmcli",), (10, "Error: No network with SSID 'x' found.\n"))])
    rc, out, needs = c.run_privileged(["nmcli", "device", "wifi", "connect", "x"], 5)
    assert (rc, needs) == (10, False) and not runner.ran(["sudo"])


# ------------------------------------------------------------------
# Wi-Fi connect
# ------------------------------------------------------------------
def test_connect_new_network_uses_device_wifi_connect(monkeypatch, tools):
    runner = install(monkeypatch, wifi_rules() + [
        (("nmcli", "--wait"), (0, "Device 'wlp0s20f3' successfully activated with '11111111-2222-3333-4444-555555555555'.\n")),
    ])
    result = c.wifi_connect("vivo Y01", password="secretpass")
    assert result["success"] and result["uuid"] == "11111111-2222-3333-4444-555555555555"
    connect_argv = runner.ran(["nmcli", "--wait"])[0]
    assert connect_argv[3:] == ["device", "wifi", "connect", "vivo Y01", "password", "secretpass"]


def test_connect_saved_network_reuses_profile_and_updates_password(monkeypatch, tools):
    runner = install(monkeypatch, wifi_rules() + [
        (("nmcli", "connection", "modify"), (0, "")),
        (("nmcli", "--wait"), (0, "Connection successfully activated\n")),
    ])
    result = c.wifi_connect("Jai Shree Ram", password="newpass123")
    assert result["success"] and result["uuid"] == HOME2
    assert runner.ran(["nmcli", "connection", "modify"])[0] == [
        "nmcli", "connection", "modify", "uuid", HOME2, "802-11-wireless-security.psk", "newpass123"]
    assert runner.ran(["nmcli", "--wait"])[0][3:] == ["connection", "up", "uuid", HOME2]
    assert not runner.ran(["nmcli", "--wait", str(c.WIFI_CONNECT_WAIT_SEC), "device"])


def test_connect_needing_password_asks_for_one(monkeypatch, tools):
    install(monkeypatch, wifi_rules() + [
        (("nmcli", "--wait"), (4, "Error: Connection activation failed: Secrets were required, but not provided.\n")),
        (("nmcli", "connection", "delete"), (0, "")),
    ])
    result = c.wifi_connect("vivo Y01")
    assert result == {"success": False, "message": "This network needs a password.", "uuid": None,
                      "needs_password": True, "needs_sudo_password": False}


def test_failed_new_connection_deletes_the_profile_it_left_behind(monkeypatch, tools):
    calls = {"n": 0}

    def connections(argv):
        calls["n"] += 1
        # First look (before connecting): no profile. After the failed
        # attempt, nmcli has left an inactive "vivo Y01" profile behind.
        if calls["n"] == 1:
            return 0, CONNECTIONS
        return 0, CONNECTIONS + "vivo Y01:99999999-8888-7777-6666-555555555555:802-11-wireless::no\n"

    runner = install(monkeypatch, wifi_rules({
        ("nmcli", "-t", "-f", "NAME,UUID,TYPE,DEVICE,ACTIVE", "connection", "show"): connections,
    }) + [
        (("nmcli", "--wait"), (4, "Error: Connection activation failed: Secrets were required, but not provided.\n")),
        (("nmcli", "connection", "delete"), (0, "")),
    ])
    result = c.wifi_connect("vivo Y01", password="wrongpass")
    assert result["needs_password"] and result["message"] == "The network rejected that password."
    assert runner.ran(["nmcli", "connection", "delete"]) == [
        ["nmcli", "connection", "delete", "uuid", "99999999-8888-7777-6666-555555555555"]]


def test_connect_network_out_of_range(monkeypatch, tools):
    install(monkeypatch, wifi_rules() + [
        (("nmcli", "--wait"), (10, "Error: No network with SSID 'Nope' found.\n")),
    ])
    result = c.wifi_connect("Nope")
    assert not result["success"] and not result["needs_password"] and "out of range" in result["message"]


# ------------------------------------------------------------------
# handle_wifi_connect -- auto-revert and the reconnect hook
# ------------------------------------------------------------------
class Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def _switch_rules(after_switch_active_uuid):
    """Before the switch Xpankaj is active; after it, the new network."""
    state = {"switched": False}

    def connections(argv):
        if not state["switched"]:
            return 0, CONNECTIONS
        return 0, ("Xpankaj:" + XPANKAJ + ":802-11-wireless::no\n"
                   "New:" + after_switch_active_uuid + ":802-11-wireless:wlp0s20f3:yes\n")

    def connect(argv):
        state["switched"] = True
        return 0, f"Device 'wlp0s20f3' successfully activated with '{after_switch_active_uuid}'.\n"

    return wifi_rules({("nmcli", "-t", "-f", "NAME,UUID,TYPE,DEVICE,ACTIVE", "connection", "show"): connections}) + [
        (("nmcli", "--wait", str(c.WIFI_CONNECT_WAIT_SEC), "device"), connect),
        (("nmcli", "--wait", str(c.WIFI_CONNECT_WAIT_SEC), "connection", "up"), (0, "Connection successfully activated\n")),
    ]


NEW_UUID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def test_switch_that_reaches_xparo_is_kept_and_reconnects_the_websocket(monkeypatch, tools):
    runner = install(monkeypatch, _switch_rules(NEW_UUID))
    sent, reconnects = [], []
    c.handle_wifi_connect(
        {"request_id": "r1", "ssid": "New", "password": "pw123456", "auto_revert": True},
        sent.append, is_server_reachable=lambda: True, on_network_changed=lambda: reconnects.append(1),
    )
    result = sent[0]["WIFI_CONNECT_RESULT"]
    assert result["success"] is True and result["request_id"] == "r1" and result["previous_ssid"] == "Xpankaj"
    assert reconnects == [1]
    assert not runner.ran(["nmcli", "--wait", str(c.WIFI_CONNECT_WAIT_SEC), "connection", "up"])


def test_switch_that_cannot_reach_xparo_reverts_to_previous_network(monkeypatch, tools):
    runner = install(monkeypatch, _switch_rules(NEW_UUID))
    clock, sent, reconnects = Clock(), [], []
    c.handle_wifi_connect(
        {"request_id": "r2", "ssid": "New", "password": "pw123456", "auto_revert": True},
        sent.append, is_server_reachable=lambda: False, on_network_changed=lambda: reconnects.append(1),
        sleep=clock.sleep, monotonic=clock.monotonic,
    )
    result = sent[0]["WIFI_CONNECT_RESULT"]
    assert result["success"] is False and result["reverted"] is True and result["reverted_to"] == "Xpankaj"
    assert "switched back to Xpankaj" in result["message"]
    assert runner.ran(["nmcli", "--wait", str(c.WIFI_CONNECT_WAIT_SEC), "connection", "up"])[0][-2:] == ["uuid", XPANKAJ]
    assert clock.now >= c.AUTO_REVERT_WINDOW_SEC
    assert reconnects == [1, 1]  # after the switch, and again after switching back


def test_no_auto_revert_when_not_requested(monkeypatch, tools):
    runner = install(monkeypatch, _switch_rules(NEW_UUID))
    sent = []
    c.handle_wifi_connect({"request_id": "r3", "ssid": "New", "auto_revert": False}, sent.append,
                          is_server_reachable=lambda: False)
    assert sent[0]["WIFI_CONNECT_RESULT"]["success"] is True
    assert not runner.ran(["nmcli", "--wait", str(c.WIFI_CONNECT_WAIT_SEC), "connection", "up"])


def test_connect_rejects_bad_ssid_without_running_anything(monkeypatch, tools):
    runner = install(monkeypatch, [])
    sent = []
    c.handle_wifi_connect({"request_id": "r4", "ssid": "x" * 33}, sent.append)
    c.handle_wifi_connect({"request_id": "r5", "ssid": ""}, sent.append)
    assert [s["WIFI_CONNECT_RESULT"]["success"] for s in sent] == [False, False]
    assert runner.calls == []


def test_only_one_wifi_change_at_a_time(monkeypatch, tools):
    install(monkeypatch, [])
    sent = []
    assert c._wifi_change_lock.acquire(blocking=False)
    try:
        c.handle_wifi_connect({"request_id": "r6", "ssid": "New"}, sent.append)
    finally:
        c._wifi_change_lock.release()
    assert "already in progress" in sent[0]["WIFI_CONNECT_RESULT"]["message"]


def test_forget_refuses_the_active_network(monkeypatch, tools):
    runner = install(monkeypatch, wifi_rules())
    sent = []
    c.handle_wifi_forget({"request_id": "f1", "uuid": XPANKAJ}, sent.append)
    assert sent[0]["WIFI_ACTION_RESULT"]["success"] is False
    assert not runner.ran(["nmcli", "connection", "delete"])


def test_forget_deletes_an_inactive_saved_network(monkeypatch, tools):
    runner = install(monkeypatch, wifi_rules() + [(("nmcli", "connection", "delete"), (0, ""))])
    sent = []
    c.handle_wifi_forget({"request_id": "f2", "uuid": HOME2}, sent.append)
    assert sent[0]["WIFI_ACTION_RESULT"] == {
        "request_id": "f2", "action": "forget", "uuid": HOME2, "success": True,
        "needs_sudo_password": False, "message": "Forgot Jai Shree Ram."}
    assert runner.ran(["nmcli", "connection", "delete"]) == [["nmcli", "connection", "delete", "uuid", HOME2]]


# ------------------------------------------------------------------
# Bluetooth
# ------------------------------------------------------------------
BT_SHOW = ("Controller 78:AF:08:71:BA:9B (public)\n\tName: robot-host\n\tAlias: robot-host\n\tPowered: yes\n"
           "\tDiscoverable: no\n\tPairable: no\n\tDiscovering: no\n")
BT_DEVICES = ("Device 09:67:66:15:BF:C2 Boult Audio Airbass\nDevice 6E:7D:6A:93:6F:54 6E-7D-6A-93-6F-54\n"
              "Device EC:B5:0A:FA:64:85 Xbox Wireless Controller\n")
BT_INFO = {
    "09:67:66:15:BF:C2": "Device 09:67:66:15:BF:C2 (public)\n\tName: Boult Audio Airbass\n\tAlias: Boult Audio Airbass\n"
                         "\tIcon: audio-headset\n\tPaired: yes\n\tTrusted: yes\n\tBlocked: no\n\tConnected: yes\n",
    "6E:7D:6A:93:6F:54": "Device 6E:7D:6A:93:6F:54 (random)\n\tAlias: 6E-7D-6A-93-6F-54\n\tPaired: no\n"
                         "\tTrusted: no\n\tBlocked: no\n\tConnected: no\n\tRSSI: 0xffffffb5 (-75)\n",
    "EC:B5:0A:FA:64:85": "Device EC:B5:0A:FA:64:85 (public)\n\tName: Xbox Wireless Controller\n"
                         "\tAlias: Xbox Wireless Controller\n\tIcon: input-gaming\n\tPaired: no\n\tConnected: no\n",
}


def bt_rules(overrides=None):
    rules = {
        ("bluetoothctl", "list"): (0, "Controller 78:AF:08:71:BA:9B robot-host [default]\n"),
        ("bluetoothctl", "show"): (0, BT_SHOW),
        ("bluetoothctl", "devices"): (0, BT_DEVICES),
        ("bluetoothctl", "info"): lambda argv: (0, BT_INFO[argv[2]]),
        ("bluetoothctl", "--timeout"): (0, "Discovery started\n[CHG] Device EC:B5:0A:FA:64:85 RSSI: 0xffffffc4 (-60)\n"),
    }
    rules.update(overrides or {})
    return list(rules.items())


def test_bluetooth_state_lists_devices_sorted_with_scan_rssi(monkeypatch, tools):
    runner = install(monkeypatch, bt_rules())
    state = c.get_bluetooth_state(scan=True)

    assert state["controller"]["name"] == "robot-host" and state["controller"]["powered"] is True
    assert state["scanned"] is True
    assert runner.ran(["bluetoothctl", "--timeout"])[0] == ["bluetoothctl", "--timeout", str(c.BT_SCAN_SEC), "scan", "on"]
    addresses = [d["address"] for d in state["devices"]]
    assert addresses == ["09:67:66:15:BF:C2", "EC:B5:0A:FA:64:85", "6E:7D:6A:93:6F:54"]  # connected, then by RSSI
    pad = state["devices"][1]
    assert pad["rssi"] == -60 and pad["icon"] == "input-gaming" and pad["named"] is True
    unnamed = state["devices"][2]
    assert unnamed["named"] is False and unnamed["rssi"] == -75


def test_bluetooth_scan_needs_power(monkeypatch, tools):
    runner = install(monkeypatch, bt_rules({("bluetoothctl", "show"): (0, BT_SHOW.replace("Powered: yes", "Powered: no"))}))
    state = c.get_bluetooth_state(scan=True)
    assert state["error"] == "Turn Bluetooth on to scan for devices." and not runner.ran(["bluetoothctl", "--timeout"])


def test_bluetooth_action_rejects_malformed_address_without_running_anything(monkeypatch):
    runner = install(monkeypatch, [])
    result = c.bluetooth_action("connect", "AA:BB; rm -rf /")
    assert result["success"] is False and runner.calls == []


def test_connect_success_is_read_from_output_not_exit_code(monkeypatch):
    # Verified live: bluetoothctl can exit 0 on a failed connect.
    install(monkeypatch, [(("bluetoothctl", "connect"), (0, "Device 00:00:00:00:00:01 not available\n"))])
    result = c.bluetooth_action("connect", "00:00:00:00:00:01")
    assert result == {"success": False, "needs_sudo_password": False, "message": "Device 00:00:00:00:00:01 not available"}


def test_pair_trusts_and_connects(monkeypatch):
    runner = install(monkeypatch, [
        (("bluetoothctl", "--agent"), (0, "Attempting to pair with EC:B5:0A:FA:64:85\nPairing successful\n")),
        (("bluetoothctl", "trust"), (0, "Changing EC:B5:0A:FA:64:85 trust succeeded\n")),
        (("bluetoothctl", "connect"), (0, "Attempting to connect to EC:B5:0A:FA:64:85\nConnection successful\n")),
    ])
    result = c.bluetooth_action("pair", "ec:b5:0a:fa:64:85")
    assert result["success"] and result["message"] == "Paired and connected."
    assert runner.ran(["bluetoothctl", "--agent"])[0] == [
        "bluetoothctl", "--agent", "NoInputNoOutput", "pair", "EC:B5:0A:FA:64:85"]
    assert runner.ran(["bluetoothctl", "trust"]) and runner.ran(["bluetoothctl", "connect"])


def test_pair_needing_a_pin_explains_why(monkeypatch):
    install(monkeypatch, [(("bluetoothctl", "--agent"), (1, "Failed to pair: org.bluez.Error.AuthenticationFailed\n"))])
    result = c.bluetooth_action("pair", "EC:B5:0A:FA:64:85")
    assert result["success"] is False and "PIN" in result["message"]


def test_power_on_lifts_an_rfkill_block(monkeypatch):
    attempts = {"n": 0}

    def power(argv):
        attempts["n"] += 1
        if attempts["n"] == 1:
            return 1, "Failed to set power on: org.bluez.Error.Blocked\n"
        return 0, "Changing power on succeeded\n"

    monkeypatch.setattr(c.time, "sleep", lambda s: None)
    runner = install(monkeypatch, [(("bluetoothctl", "power"), power), (("rfkill", "unblock", "bluetooth"), (0, ""))])
    result = c.bluetooth_action("power_on")
    assert result["success"] and runner.ran(["rfkill", "unblock", "bluetooth"])


def test_bluetooth_action_asks_for_sudo_when_needed(monkeypatch, tools):
    install(monkeypatch, bt_rules() + [
        (("bluetoothctl", "remove"), (1, "Failed to remove device: org.freedesktop.DBus.Error.AccessDenied\n")),
        (("sudo", "-n"), (1, "sudo: a password is required\n")),
    ])
    sent = []
    c.handle_bluetooth_action({"request_id": "b1", "action": "remove", "address": "09:67:66:15:BF:C2"}, sent.append)
    result = sent[0]["BLUETOOTH_ACTION_RESULT"]
    assert result["success"] is False and result["needs_sudo_password"] is True
    assert result["message"] == "This robot needs its sudo password to control Bluetooth."


def test_second_bluetooth_scan_while_one_runs_answers_with_current_list(monkeypatch, tools):
    runner = install(monkeypatch, bt_rules())
    sent = []
    assert c._bluetooth_lock.acquire(blocking=False)
    try:
        c.handle_get_bluetooth_devices(True, sent.append)
    finally:
        c._bluetooth_lock.release()
    assert "already running" in sent[0]["BLUETOOTH_DEVICES"]["error"]
    assert len(sent[0]["BLUETOOTH_DEVICES"]["devices"]) == 3
    assert not runner.ran(["bluetoothctl", "--timeout"])


def test_wrong_sudo_password_is_reported_as_rejected(monkeypatch, tools):
    install(monkeypatch, wifi_rules() + [
        (("sudo", "-k", "-S"), (1, "Sorry, try again.\nsudo: 1 incorrect password attempt\n")),
    ])
    sent = []
    c.handle_wifi_forget({"request_id": "f3", "uuid": HOME2, "sudo_password": "nope"}, sent.append)
    result = sent[0]["WIFI_ACTION_RESULT"]
    assert result["needs_sudo_password"] is True
    assert result["message"] == "The robot rejected that sudo password."
