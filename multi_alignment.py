"""Local IPC and network helpers for multi-controller alignment.

This module intentionally contains no Flask, GPIO, or SNMP code.  Both the
web process and ``auto_new.py`` use it so only the latter ever controls GPIO.
"""

import ipaddress
import json
import os
import re
import socket
import tempfile
import time
from urllib import error, request
from pathlib import Path


STATUS_FILE = Path("alignment_status.json")
COMMAND_DIR = Path("alignment_commands")
COMMAND_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def is_valid_command_id(value):
    return isinstance(value, str) and bool(COMMAND_ID_RE.fullmatch(value))


def atomic_write_json(path, payload):
    """Write JSON atomically so readers never see a partial status snapshot."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)


def read_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else default
    except (OSError, json.JSONDecodeError):
        return default


def publish_status(payload, path=STATUS_FILE):
    data = dict(payload)
    data["updated_at"] = time.time()
    atomic_write_json(path, data)


def read_status(path=STATUS_FILE):
    return read_json(path, default={})


def enqueue_command(command, directory=COMMAND_DIR):
    """Create one command file; an existing ID is the idempotency signal."""
    command_id = command.get("command_id")
    if not is_valid_command_id(command_id):
        raise ValueError("Invalid command ID.")
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{command_id}.json"
    try:
        fd = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(command, handle, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    return True


def consume_commands(directory=COMMAND_DIR):
    """Yield and remove queued commands in filename order.

    Commands are written atomically as distinct files. A corrupt command is
    discarded because it cannot safely request motion.
    """
    directory = Path(directory)
    if not directory.exists():
        return []
    commands = []
    for path in sorted(directory.glob("*.json")):
        claimed = path.with_suffix(".processing")
        try:
            os.replace(path, claimed)
        except FileNotFoundError:
            continue
        command = read_json(claimed)
        try:
            claimed.unlink()
        except FileNotFoundError:
            pass
        if command:
            commands.append(command)
    return commands


def determine_local_ip(paired_controller_ip, port=5000):
    """Choose the outbound local IPv4 used to reach the configured paired controller only."""
    paired = ipaddress.ip_address(paired_controller_ip)
    if paired.version != 4:
        raise ValueError("Only IPv4 paired-controller addresses are supported.")
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect((str(paired), port))
        return sock.getsockname()[0]
    finally:
        sock.close()


def determine_role(local_ip, paired_controller_ip):
    local = ipaddress.ip_address(local_ip)
    paired = ipaddress.ip_address(paired_controller_ip)
    if local.version != 4 or paired.version != 4 or local == paired:
        raise ValueError("Local and paired-controller addresses must be distinct IPv4 addresses.")
    return "master" if int(local) < int(paired) else "slave"


def is_success_outcome(outcome):
    return outcome in {"target_reached", "best_position_found", "signal_recovered"}


def select_preferred_link(local_rssi, slave_rssi, current=None, switch_threshold_db=0):
    """Return the preferred side, avoiding switches for small RSSI differences.

    ``-1`` is the radio's no-signal sentinel: a valid signal on the other
    controller wins.  If both controllers report ``-1``, no link is selected.
    When a link is already active, the other side must be stronger by at least
    ``switch_threshold_db`` to replace it.  The caller is responsible for
    ensuring both samples are fresh.
    """
    valid = lambda value: isinstance(value, int) and not isinstance(value, bool)
    if (
        not valid(local_rssi)
        or not valid(slave_rssi)
        or not valid(switch_threshold_db)
        or switch_threshold_db < 0
    ):
        return None
    if local_rssi == -1 and slave_rssi == -1:
        return None
    if local_rssi == -1:
        return "slave"
    if slave_rssi == -1:
        return "local"
    if current == "local" and slave_rssi > local_rssi:
        return "slave" if slave_rssi - local_rssi >= switch_threshold_db else "local"
    if current == "slave" and local_rssi > slave_rssi:
        return "local" if local_rssi - slave_rssi >= switch_threshold_db else "slave"
    if local_rssi > slave_rssi:
        return "local"
    if slave_rssi > local_rssi:
        return "slave"
    return current if current in {"local", "slave"} else "local"


def select_preferred_link_with_freshness(
    local_rssi, local_fresh, slave_rssi, slave_fresh, current=None,
    switch_threshold_db=0,
):
    """Choose the fresh controller; retain the current link only if both are stale."""
    if slave_fresh and not local_fresh:
        return "slave"
    if local_fresh and not slave_fresh:
        return "local"
    if not local_fresh:
        return None
    return select_preferred_link(
        local_rssi, slave_rssi, current, switch_threshold_db
    )


def requires_pre_scan_handover(role, local_link_active, slave_rssi_fresh):
    """Only hand over an active Master link to a Slave with fresh RSSI."""
    return (
        role == "master"
        and local_link_active is True
        and slave_rssi_fresh is True
    )


class PairedControllerApiClient:
    """Small stdlib-only client for the authenticated paired-controller API."""

    def __init__(self, paired_controller_ip, token, timeout_sec=3):
        self.paired_controller_ip = paired_controller_ip
        self.token = token
        self.timeout_sec = timeout_sec
        self.base_url = f"http://{paired_controller_ip}:5000/api/internal/alignment"

    def _request(self, path, payload=None):
        headers = {"X-Multi-Alignment-Token": self.token}
        data = None
        method = "GET"
        if payload is not None:
            method = "POST"
            headers["Content-Type"] = "application/json"
            data = json.dumps(payload).encode("utf-8")
        req = request.Request(
            f"{self.base_url}/{path}", data=data, headers=headers, method=method
        )
        try:
            with request.urlopen(req, timeout=self.timeout_sec) as response:
                decoded = json.loads(response.read().decode("utf-8"))
                if not isinstance(decoded, dict):
                    raise RuntimeError("Paired controller returned an invalid response.")
                return decoded
        except (error.URLError, error.HTTPError, TimeoutError, ValueError) as exc:
            raise RuntimeError(f"Paired-controller request failed: {exc}")

    def status(self):
        return self._request("status").get("alignment", {})

    def command(self, command, command_id, result=None):
        payload = {
            "command": command,
            "command_id": command_id,
        }
        if result is not None:
            payload["result"] = result
        return self._request("command", payload)
