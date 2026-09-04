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
import uuid
from urllib import error, request
from pathlib import Path


STATUS_FILE = Path("alignment_status.json")
COMMAND_DIR = Path("alignment_commands")
COMMAND_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{7,127}$")


def is_valid_command_id(value):
    return isinstance(value, str) and bool(COMMAND_ID_RE.fullmatch(value))


def is_valid_session_id(value):
    return isinstance(value, str) and bool(SESSION_ID_RE.fullmatch(value))


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


def determine_local_ip(peer_ip, port=5000):
    """Choose the outbound local IPv4 used to reach the configured peer only."""
    peer = ipaddress.ip_address(peer_ip)
    if peer.version != 4:
        raise ValueError("Only IPv4 peer addresses are supported.")
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect((str(peer), port))
        return sock.getsockname()[0]
    finally:
        sock.close()


def determine_role(local_ip, peer_ip):
    local = ipaddress.ip_address(local_ip)
    peer = ipaddress.ip_address(peer_ip)
    if local.version != 4 or peer.version != 4 or local == peer:
        raise ValueError("Local and peer addresses must be distinct IPv4 addresses.")
    return "coordinator" if int(local) < int(peer) else "peer"


def new_session_id():
    return f"joint-{uuid.uuid4().hex}"


def is_success_outcome(outcome):
    return outcome in {"target_reached", "best_position_found", "signal_recovered"}


def select_preferred_link(local_rssi, peer_rssi, current=None):
    """Return the side with the stronger RSSI, keeping ties stable.

    ``-1`` is the radio's no-signal sentinel: a valid signal on the other
    controller wins.  If both controllers report ``-1``, no link is selected.
    The caller is responsible for ensuring both samples are fresh.
    """
    valid = lambda value: isinstance(value, int) and not isinstance(value, bool)
    if not valid(local_rssi) or not valid(peer_rssi):
        return None
    if local_rssi == -1 and peer_rssi == -1:
        return None
    if local_rssi == -1:
        return "peer"
    if peer_rssi == -1:
        return "local"
    if local_rssi > peer_rssi:
        return "local"
    if peer_rssi > local_rssi:
        return "peer"
    return current if current in {"local", "peer"} else "local"


class PeerApiClient:
    """Small stdlib-only client for the authenticated peer API."""

    def __init__(self, peer_ip, token, timeout_sec=3):
        self.peer_ip = peer_ip
        self.token = token
        self.timeout_sec = timeout_sec
        self.base_url = f"http://{peer_ip}:5000/api/internal/alignment"

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
                    raise RuntimeError("Peer returned an invalid response.")
                return decoded
        except (error.URLError, error.HTTPError, TimeoutError, ValueError) as exc:
            raise RuntimeError(f"Peer request failed: {exc}")

    def status(self):
        return self._request("status").get("alignment", {})

    def command(self, command, session_id, command_id, result=None):
        payload = {
            "command": command,
            "session_id": session_id,
            "command_id": command_id,
        }
        if result is not None:
            payload["result"] = result
        return self._request("command", payload)
