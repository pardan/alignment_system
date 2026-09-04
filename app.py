from flask import Flask, Response, render_template, request, jsonify, stream_with_context
import json
import os
import subprocess
import re
import time
import hmac
import ipaddress
import uuid
from multi_alignment import (
    enqueue_command,
    determine_local_ip,
    determine_role,
    is_valid_command_id,
    is_valid_session_id,
    PeerApiClient,
    read_status,
)


ALLOWED_LOG_SERVICES = {"monitor": "monitor.service", "auto": "auto.service"}

# --- Configuration ---
app = Flask(__name__, template_folder='templates', static_folder='static')
CONFIG_FILE = 'config.json'

# --- Default Configuration (with new SNMP fields) ---
DEFAULT_CONFIG = {
    "target_rssi": -80,
    "USE_TARGET_RSSI": True,
    "RSSI_WORSENING_TOLERANCE_DB": 3,
    "IP_RADIO": "172.20.25.5",
    "SNMP_PORT": 161,
    "SNMP_COMMUNITY": "public",
    "SNMP_WRITE_COMMUNITY": "public",
    "OID_RSSI": "1.3.6.1.4.1.1807.113.2.11.1.2.1.1",
    "degrees_per_step": 6.0,
    "settle_sec": 5,
    "iteration_actuator": 3,
    "actuator_speed": 0.5,
    "max_try": 1,
    "360_in_sec": 68,
    "AUTO_BOOT_RSSI_MINUS_ONE_COUNT": 5,
    "AUTO_SIGNAL_LOSS_RSSI_THRESHOLD": -90,
    "AUTO_SIGNAL_LOSS_DURATION_SEC": 60,
    "AUTO_RESCAN_COOLDOWN_SEC": 300,
    "AUTO_RESCAN_MAX_ATTEMPTS": None,
    "ALIGNMENT_MODE": "single",
    "PEER_ALIGNMENT_IP": "",
    "MULTI_ALIGNMENT_API_TOKEN": "",
    "MULTI_RSSI_COMPARE_INTERVAL_SEC": 5,
    "target_frequencies_hz": [
        10507500,
        10514500,
        10521500,
        10528500,
        10535500,
        10542500
    ]
}

# --- Helper Functions ---
def get_system_uptime():
    """Gets the system uptime from the Raspberry Pi or current system."""
    try:
        import platform
        system = platform.system()
        
        if system == "Linux":
            # Read the uptime in seconds from /proc/uptime (Linux/Raspberry Pi)
            with open('/proc/uptime', 'r') as f:
                uptime_seconds = float(f.readline().split()[0])
        else:
            # For Windows (testing) or other systems, return a simulated value
            # This will be replaced by the actual Linux uptime when deployed on Raspberry Pi
            return "0 days, 0 hours, 0 minutes"
        
        # Convert seconds to days, hours, minutes
        days = int(uptime_seconds // 86400)
        hours = int((uptime_seconds % 86400) // 3600)
        minutes = int((uptime_seconds % 3600) // 60)
        
        # Format the uptime string
        if days > 0:
            return f"{days} days, {hours} hours, {minutes} minutes"
        elif hours > 0:
            return f"{hours} hours, {minutes} minutes"
        else:
            return f"{minutes} minutes"
    except Exception as e:
        print(f"Error getting system uptime: {e}")
        return "Unknown"

def run_command(command):
    """Runs a shell command and returns its output."""
    try:
        # Using shell=True for systemctl commands. Be cautious with user input.
        result = subprocess.run(command, check=True, shell=True, text=True, capture_output=True)
        print(f"Command '{command}' executed successfully. Output:\n{result.stdout}")
        return result.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        print(f"Error running command '{command}': {e}\nStderr: {e.stderr if hasattr(e, 'stderr') else 'N/A'}")
        return None

def get_config():
    """Reads radio configuration from the JSON file."""
    config = DEFAULT_CONFIG.copy()
    if not os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'w') as f:
            json.dump(config, f, indent=4)
        return config
    try:
        with open(CONFIG_FILE, 'r') as f:
            config.update(json.load(f))
        return config
    except (json.JSONDecodeError, IOError):
        return config

def save_config(new_config):
    """Saves the radio configuration to the JSON file."""
    try:
        with open(CONFIG_FILE, 'w') as f:
            json.dump(new_config, f, indent=4)
        return True
    except IOError:
        return False

def normalize_target_frequencies(values):
    """Validate target frequencies in Hz and return a sorted unique list."""
    if not isinstance(values, list) or not values:
        raise ValueError("At least one target frequency is required.")

    frequencies = []
    for value in values:
        if isinstance(value, bool):
            raise ValueError("Each target frequency must be a whole number in Hz.")
        try:
            frequency = int(value)
        except (TypeError, ValueError):
            raise ValueError("Each target frequency must be a whole number in Hz.")
        if frequency <= 0:
            raise ValueError("Each target frequency must be greater than zero.")
        frequencies.append(frequency)

    return sorted(set(frequencies))

def normalize_positive_integer(value, field_name, allow_null=False):
    """Return a positive integer, accepting null only for unlimited retries."""
    if allow_null and value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be a positive whole number.")
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field_name} must be a positive whole number.")
    if normalized <= 0:
        raise ValueError(f"{field_name} must be greater than zero.")
    return normalized

def normalize_non_negative_integer(value, field_name):
    """Return an integer threshold that may be zero."""
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be a whole number of zero or more.")
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field_name} must be a whole number of zero or more.")
    if normalized < 0:
        raise ValueError(f"{field_name} must be zero or more.")
    return normalized

def normalize_rssi_loss_threshold(value):
    """Validate the RSSI threshold used alongside the -1 loss sentinel."""
    if isinstance(value, bool):
        raise ValueError("Signal-loss RSSI threshold must be a negative whole number.")
    try:
        threshold = int(value)
    except (TypeError, ValueError):
        raise ValueError("Signal-loss RSSI threshold must be a negative whole number.")
    if threshold >= 0:
        raise ValueError("Signal-loss RSSI threshold must be below zero.")
    return threshold

def normalize_boolean(value, field_name):
    """Accept only JSON booleans for explicit controller mode switches."""
    if not isinstance(value, bool):
        raise ValueError(f"{field_name} must be true or false.")
    return value


def normalize_alignment_mode(value):
    if value not in ("single", "multi"):
        raise ValueError("Alignment mode must be single or multi.")
    return value


def normalize_ipv4(value, field_name):
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be an IPv4 address.")
    try:
        address = ipaddress.ip_address(value.strip())
    except ValueError:
        raise ValueError(f"{field_name} must be an IPv4 address.")
    if address.version != 4 or address.is_unspecified or address.is_multicast:
        raise ValueError(f"{field_name} must be a routable IPv4 address.")
    return str(address)


def validate_multi_alignment_config(data):
    data["ALIGNMENT_MODE"] = normalize_alignment_mode(data["ALIGNMENT_MODE"])
    peer_ip = data.get("PEER_ALIGNMENT_IP", "")
    token = data.get("MULTI_ALIGNMENT_API_TOKEN", "")
    data["MULTI_RSSI_COMPARE_INTERVAL_SEC"] = normalize_positive_integer(
        data.get("MULTI_RSSI_COMPARE_INTERVAL_SEC"), "Multi RSSI comparison interval"
    )
    if data["ALIGNMENT_MODE"] == "single":
        data["PEER_ALIGNMENT_IP"] = peer_ip.strip() if isinstance(peer_ip, str) else ""
        data["MULTI_ALIGNMENT_API_TOKEN"] = token if isinstance(token, str) else ""
        return
    data["PEER_ALIGNMENT_IP"] = normalize_ipv4(peer_ip, "Peer alignment IP")
    if not isinstance(token, str) or len(token) < 16:
        raise ValueError("Multi-alignment API token must contain at least 16 characters.")
    data["MULTI_ALIGNMENT_API_TOKEN"] = token


def is_multi_alignment_enabled(config):
    return config.get("ALIGNMENT_MODE") == "multi"


def internal_request_is_authorized(config):
    if not is_multi_alignment_enabled(config):
        return False, "Multi-alignment is not enabled."
    supplied = request.headers.get("X-Multi-Alignment-Token", "")
    expected = config.get("MULTI_ALIGNMENT_API_TOKEN", "")
    if not expected or not hmac.compare_digest(supplied, expected):
        return False, "Invalid multi-alignment token."
    try:
        peer_ip = str(ipaddress.ip_address(config["PEER_ALIGNMENT_IP"]))
        remote_ip = str(ipaddress.ip_address(request.remote_addr or ""))
    except ValueError:
        return False, "Invalid peer request address."
    if remote_ip != peer_ip:
        return False, "Request is not from the configured peer IP."
    return True, None


def status_for_internal_api(config):
    status = read_status()
    status.setdefault("alignment_mode", config.get("ALIGNMENT_MODE", "single"))
    status.setdefault("peer_configured", bool(config.get("PEER_ALIGNMENT_IP")))
    return status


def redact_public_multi_status(status):
    """Public dashboard status intentionally contains no shared token."""
    return {
        key: status.get(key)
        for key in (
            "alignment_mode", "local_ip", "peer_ip", "role", "rssi",
            "rssi_fresh", "signal_lost", "scheduler_state",
            "active_session_id", "last_scan_outcome", "last_scan_success",
            "peer_assisted_hold", "peer_last_error", "link_filters_active",
        )
    }

def get_active_connection():
    """Finds the name of the active network connection."""
    output = run_command("nmcli -t -f NAME,TYPE connection show --active")
    if not output:
        return None
    for line in output.splitlines():
        if 'ethernet' in line:
            return line.split(':')[0]
    for line in output.splitlines():
        if 'wifi' in line:
            return line.split(':')[0]
    return output.splitlines()[0].split(':')[0] if output.splitlines() else None
    
def prefix_to_subnet(prefix):
    """Converts CIDR prefix to subnet mask."""
    if not prefix or not prefix.isdigit(): return ""
    bits = 0
    for i in range(32 - int(prefix), 32):
        bits |= (1 << i)
    return ".".join([str((bits >> i) & 255) for i in [24, 16, 8, 0]])

def get_rssi(ip, port, oid, community):
    """
    Mengambil nilai RSSI dari perangkat radio menggunakan SNMP.
    """
    command = f"snmpget -v 2c -c {community} {ip}:{port} {oid}"
    output = subprocess.getoutput(command)
    
    try:
        rssi_value = int(output.split(":")[-1].strip())

        # Koreksi skala jika RSSI terlalu kecil (misalnya -7698)
        if rssi_value < -10000:
            rssi_value = rssi_value / 100  # Koreksi faktor skala

        return int(rssi_value/100)

    except ValueError:
        return None

# --- API Routes ---

# == Radio Config API ==
@app.route('/api/config', methods=['GET', 'POST'])
def api_config():
    if request.method == 'GET':
        return jsonify(get_config())
    
    if request.method == 'POST':
        data = request.json
        if not isinstance(data, dict) or not all(k in data for k in DEFAULT_CONFIG.keys()):
            return jsonify({"status": "error", "message": "Missing radio config keys."}), 400
        
        # Validate max_try value
        if 'max_try' in data:
            try:
                max_try = int(data['max_try'])
                if max_try < 1 or max_try > 3:
                    return jsonify({"status": "error", "message": "Auto Alignment Max Try must be a value between 1 and 3"}), 400
            except (ValueError, TypeError):
                return jsonify({"status": "error", "message": "Auto Alignment Max Try must be a valid integer between 1 and 3"}), 400

        try:
            data["target_frequencies_hz"] = normalize_target_frequencies(
                data["target_frequencies_hz"]
            )
            data["USE_TARGET_RSSI"] = normalize_boolean(
                data["USE_TARGET_RSSI"], "Use target RSSI"
            )
            data["RSSI_WORSENING_TOLERANCE_DB"] = normalize_non_negative_integer(
                data["RSSI_WORSENING_TOLERANCE_DB"], "RSSI worsening tolerance"
            )
            data["AUTO_BOOT_RSSI_MINUS_ONE_COUNT"] = normalize_positive_integer(
                data["AUTO_BOOT_RSSI_MINUS_ONE_COUNT"], "Boot RSSI -1 count"
            )
            data["AUTO_SIGNAL_LOSS_RSSI_THRESHOLD"] = normalize_rssi_loss_threshold(
                data["AUTO_SIGNAL_LOSS_RSSI_THRESHOLD"]
            )
            data["AUTO_SIGNAL_LOSS_DURATION_SEC"] = normalize_positive_integer(
                data["AUTO_SIGNAL_LOSS_DURATION_SEC"], "Signal-loss duration"
            )
            data["AUTO_RESCAN_COOLDOWN_SEC"] = normalize_positive_integer(
                data["AUTO_RESCAN_COOLDOWN_SEC"], "Rescan cooldown"
            )
            data["AUTO_RESCAN_MAX_ATTEMPTS"] = normalize_positive_integer(
                data["AUTO_RESCAN_MAX_ATTEMPTS"], "Automatic rescan maximum attempts", allow_null=True
            )
            validate_multi_alignment_config(data)
        except ValueError as error:
            return jsonify({"status": "error", "message": str(error)}), 400
        
        if save_config(data):
            # Reload and restart the service after saving the new config
            print("Configuration saved. Reloading systemd and restarting service...")
            run_command("systemctl daemon-reload")
            run_command("systemctl restart monitor.service")
            run_command("systemctl restart auto.service")
            return jsonify({"status": "success", "message": "Radio configuration saved and service restarted!"})
        else:
            return jsonify({"status": "error", "message": "Failed to write radio config."}), 500


@app.route('/api/internal/alignment/status', methods=['GET'])
def internal_alignment_status():
    config = get_config()
    allowed, message = internal_request_is_authorized(config)
    if not allowed:
        return jsonify({"status": "error", "message": message}), 403
    return jsonify({"status": "success", "alignment": status_for_internal_api(config)})


@app.route('/api/internal/alignment/command', methods=['POST'])
def internal_alignment_command():
    config = get_config()
    allowed, message = internal_request_is_authorized(config)
    if not allowed:
        return jsonify({"status": "error", "message": message}), 403
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"status": "error", "message": "JSON command payload required."}), 400
    command = data.get("command")
    session_id = data.get("session_id")
    command_id = data.get("command_id")
    if command not in {"start_joint_scan", "joint_scan_result", "release_peer_hold", "set_link_active"}:
        return jsonify({"status": "error", "message": "Unsupported internal command."}), 400
    if not is_valid_session_id(session_id) or not is_valid_command_id(command_id):
        return jsonify({"status": "error", "message": "Invalid session ID or command ID."}), 400
    if command in {"joint_scan_result", "release_peer_hold"}:
        result = data.get("result")
        if not isinstance(result, dict):
            return jsonify({"status": "error", "message": "Joint result payload is required."}), 400
        if command == "joint_scan_result" and not isinstance(result.get("success"), bool):
            return jsonify({"status": "error", "message": "Joint result requires a boolean success value."}), 400
        if command == "release_peer_hold" and not all(
            isinstance(result.get(key), bool)
            for key in ("coordinator_success", "peer_success")
        ):
                return jsonify({"status": "error", "message": "Release requires both success values."}), 400
    if command == "set_link_active":
        result = data.get("result")
        if not isinstance(result, dict) or not isinstance(result.get("active"), bool):
            return jsonify({"status": "error", "message": "Link selection requires a boolean active value."}), 400
    queued = enqueue_command({
        "command": command,
        "command_id": command_id,
        "session_id": session_id,
        "result": data.get("result"),
        "received_at": time.time(),
    })
    return jsonify({"status": "success", "queued": queued, "idempotent": not queued}), 202


@app.route('/api/multi-alignment/status', methods=['GET'])
def multi_alignment_status():
    config = get_config()
    if not is_multi_alignment_enabled(config):
        return jsonify({"status": "error", "message": "Multi-alignment is not enabled."}), 409
    local = redact_public_multi_status(status_for_internal_api(config))
    local["peer_rssi"] = None
    local["peer_last_outcome"] = None
    local["peer_link_filters_active"] = None
    try:
        peer_status = PeerApiClient(
            config["PEER_ALIGNMENT_IP"], config["MULTI_ALIGNMENT_API_TOKEN"]
        ).status()
        local["peer_rssi"] = peer_status.get("rssi")
        local["peer_last_outcome"] = peer_status.get("last_scan_outcome")
        local["peer_link_filters_active"] = peer_status.get("link_filters_active")
    except RuntimeError as error:
        local["peer_last_error"] = str(error)
    return jsonify(local)


@app.route('/api/logs/stream/<service_key>', methods=['GET'])
def stream_service_logs(service_key):
    """Stream only the two allowlisted systemd unit logs as Server-Sent Events."""
    service_name = ALLOWED_LOG_SERVICES.get(service_key)
    if service_name is None:
        return jsonify({"status": "error", "message": "Unknown log service."}), 404

    def generate():
        process = None
        try:
            process = subprocess.Popen(
                ["journalctl", "-u", service_name, "-n", "100", "-f", "-o", "cat"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            for line in iter(process.stdout.readline, ""):
                yield f"data: {json.dumps(line.rstrip())}\n\n"
        except OSError as error:
            yield f"event: error\ndata: {json.dumps(str(error))}\n\n"
        finally:
            if process is not None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()

    response = Response(stream_with_context(generate()), mimetype="text/event-stream")
    response.headers["Cache-Control"] = "no-cache"
    response.headers["X-Accel-Buffering"] = "no"
    return response

# == Network Config API ==
@app.route('/api/network', methods=['GET', 'POST'])
def api_network():
    conn_name = get_active_connection()
    if not conn_name:
        return jsonify({"status": "error", "message": "No active network connection found."}), 500

    if request.method == 'GET':
        output = run_command(f"nmcli -g ipv4.method,IP4.ADDRESS,IP4.GATEWAY,IP4.DNS con show '{conn_name}'")
        if output is None:
            return jsonify({"status": "error", "message": "Could not read network settings via nmcli."}), 500
        
        lines = output.splitlines()
        method_raw = lines[0] if len(lines) > 0 else 'auto'
        ip_with_prefix = lines[1] if len(lines) > 1 else ''
        gateway = lines[2] if len(lines) > 2 else ''
        dns = lines[3] if len(lines) > 3 else ''

        method = "dhcp" if "auto" in method_raw else "static"
        ip, prefix = ip_with_prefix.split('/') if '/' in ip_with_prefix else (ip_with_prefix, "")
        subnet = prefix_to_subnet(prefix)

        return jsonify({
            "method": method,
            "ipaddress": ip,
            "subnet": subnet,
            "gateway": gateway,
            "dns": dns
        })

    if request.method == 'POST':
        data = request.json
        method = data.get('method')
        if method == 'dhcp':
            run_command(f"nmcli con mod '{conn_name}' ipv4.method auto ipv4.addresses '' ipv4.gateway '' ipv4.dns ''")
        elif method == 'static':
            ip = data.get('ipaddress')
            subnet = data.get('subnet')
            gateway = data.get('gateway', '')
            dns = data.get('dns', '')
            if not ip or not subnet:
                 return jsonify({"status": "error", "message": "IP Address and Subnet Mask are required."}), 400
            try:
                prefix = sum(bin(int(x)).count('1') for x in subnet.split('.'))
            except (ValueError, AttributeError):
                 return jsonify({"status": "error", "message": "Invalid Subnet Mask format."}), 400
            command = f"nmcli con mod '{conn_name}' ipv4.method manual ipv4.addresses {ip}/{prefix}"
            if gateway: command += f" ipv4.gateway {gateway}"
            if dns: command += f" ipv4.dns '{dns}'"
            else: command += f" ipv4.dns ''"
            run_command(command)
        else:
            return jsonify({"status": "error", "message": "Invalid method specified."}), 400
        run_command(f"nmcli con up '{conn_name}'")
        return jsonify({"status": "success", "message": "Network settings applied. Connection restarting..."})

# == RSSI API ==
@app.route('/api/rssi', methods=['GET'])
def api_rssi():
    """Returns the current RSSI value from the radio device."""
    config = get_config()
    try:
        rssi_value = get_rssi(
            config["IP_RADIO"],
            config["SNMP_PORT"],
            config["OID_RSSI"],
            config["SNMP_COMMUNITY"]
        )
        if rssi_value is not None:
            return jsonify({
                "rssi": rssi_value,
                "unit": "dBm",
                "status": "success"
            })
        else:
            return jsonify({
                "rssi": None,
                "unit": "dBm",
                "status": "error",
                "message": "Could not retrieve RSSI value"
            })
    except Exception as e:
        return jsonify({
            "rssi": None,
            "unit": "dBm",
            "status": "error",
            "message": str(e)
        })

# == System Status API ==
@app.route('/api/system', methods=['GET'])
def api_system():
    """Returns system status information including uptime, CPU usage, and RAM usage."""
    uptime = get_system_uptime()
    
    # Get CPU usage
    try:
        cpu_usage = run_command("top -bn1 | grep 'Cpu(s)' | sed 's/.*, *\\([0-9.]*\\)%* id.*/\\1/' | awk '{print 100 - $1\"%\"}'")
        if not cpu_usage:
            cpu_usage = "N/A"
    except Exception as e:
        print(f"Error getting CPU usage: {e}")
        cpu_usage = "N/A"
    
    # Get RAM usage
    try:
        # Calculate percentage using raw bytes for accuracy
        ram_percent = run_command("free | awk 'NR==2{printf \"%.1f%%\", $3*100/$2}'")
        if not ram_percent:
            ram_usage = "N/A"
        else:
            # Get human-readable values for display
            ram_details = run_command("free -h | awk 'NR==2{printf \"%s/%s\", $3, $2}'")
            ram_usage = f"{ram_percent} ({ram_details})"
    except Exception as e:
        print(f"Error getting RAM usage: {e}")
        ram_usage = "N/A"
    
    return jsonify({
        "uptime": uptime,
        "cpu_usage": cpu_usage,
        "ram_usage": ram_usage
    })

# --- Webpage Route ---
@app.route('/')
def index():
    return render_template('index.html')

# --- Main Execution ---
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
