import os
import json
import time
import subprocess
import signal
import gpiod
from threading import Thread, Event
from alignment_scheduler import AutoAlignmentScheduler
from multi_alignment import (
    PeerApiClient,
    consume_commands,
    determine_local_ip,
    determine_role,
    is_success_outcome,
    new_session_id,
    publish_status,
)
from snmp_filter_integration import (
    get_non_zero_entries,
    get_entries_with_specific_values,
    configure_snmp_entries_for_calibration,
    test_snmp_entries_with_rssi,
    enable_best_entry,
    run_snmpset
)

# =========================
# Config
# =========================
CONFIG_FILE = 'config.json'
DEFAULTS = {
    "target_rssi": -80,
    "USE_TARGET_RSSI": True,
    "RSSI_WORSENING_TOLERANCE_DB": 3,
    "IP_RADIO": "172.20.25.6",
    "SNMP_PORT": 161,
    "SNMP_COMMUNITY": "public",
    "SNMP_WRITE_COMMUNITY": "public",
    "OID_RSSI": "1.3.6.1.4.1.1807.113.2.11.1.2.1.1",
    "degrees_per_step": 5,
    "settle_sec": 2,
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
    "target_frequencies_hz": [
        10507500,
        10514500,
        10521500,
        10528500,
        10535500,
        10542500
    ]
}

def load_config(path=CONFIG_FILE):
    if not os.path.exists(path):
        print(f"Warning: '{path}' not found. Using default settings.")
        return DEFAULTS.copy()
    try:
        with open(path, 'r') as f:
            print(f"Loading configuration from '{path}'...")
            cfg = json.load(f)
            return {**DEFAULTS, **cfg}
    except (json.JSONDecodeError, IOError) as e:
        print(f"Error reading '{path}': {e}. Using default settings.")
        return DEFAULTS.copy()

cfg = load_config()
target_rssi        = cfg["target_rssi"]
use_target_rssi    = cfg["USE_TARGET_RSSI"]
rssi_worsening_tolerance_db = cfg["RSSI_WORSENING_TOLERANCE_DB"]
IP_RADIO           = cfg["IP_RADIO"]
OID_RSSI           = cfg["OID_RSSI"]
community          = cfg["SNMP_COMMUNITY"]
port               = cfg["SNMP_PORT"]
degrees_per_step   = cfg["degrees_per_step"]
settle_sec         = cfg["settle_sec"]
iteration_actuator = cfg["iteration_actuator"]
actuator_speed     = cfg["actuator_speed"]
max_try            = cfg["max_try"]
target_frequencies_hz = cfg["target_frequencies_hz"]
auto_boot_rssi_minus_one_count = cfg["AUTO_BOOT_RSSI_MINUS_ONE_COUNT"]
auto_signal_loss_rssi_threshold = cfg["AUTO_SIGNAL_LOSS_RSSI_THRESHOLD"]
auto_signal_loss_duration_sec = cfg["AUTO_SIGNAL_LOSS_DURATION_SEC"]
auto_rescan_cooldown_sec = cfg["AUTO_RESCAN_COOLDOWN_SEC"]
auto_rescan_max_attempts = cfg["AUTO_RESCAN_MAX_ATTEMPTS"]
alignment_mode = cfg["ALIGNMENT_MODE"]
peer_alignment_ip = cfg["PEER_ALIGNMENT_IP"]
multi_alignment_api_token = cfg["MULTI_ALIGNMENT_API_TOKEN"]
# SNMP Filter Configuration
snmp_filter_host           = cfg["IP_RADIO"]
snmp_filter_community      = cfg["SNMP_COMMUNITY"]
snmp_filter_set_community  = cfg["SNMP_WRITE_COMMUNITY"]
snmp_filter_oid            = "1.3.6.1.4.1.1807.113.1.1.1.3"
snmp_filter_set_oid_base   = "1.3.6.1.4.1.1807.113.1.1.1.4"

# =========================
# GPIO setup
# =========================
GPIO_CHIP = "gpiochip0"
PIN_MAIN   = 21  # RIGHT
PIN_ALT    = 20  # LEFT
PIN_UP     = 10  # UP
PIN_DOWN   = 9   # DOWN
PIN_BUTTON = 5  # Start/Restart button (active LOW, momentary)
PIN_HOME   = 13  # Home sensor (goes LOW at rightmost startpoint)
PIN_MANUAL_UP   = 2   # Manual UP button (active LOW)
PIN_MANUAL_DOWN = 3   # Manual DOWN button (active LOW)
PIN_ABORT  = 6   # Abort signal (active LOW)

# Rotator specification: 1 second = (360/360_in_sec)°
# Calculate gpio_step_sec based on desired degrees_per_step
gpio_step_sec = degrees_per_step / (360.0 / cfg["360_in_sec"])  # Time duration per pulse
# Calculate H_STEPS based on degrees_per_step to complete 360°
H_STEPS       = int(360.0 / degrees_per_step)   # sweep length
actuator_calibrate = 6

# Print configuration info
print(f"Configuration loaded:")
print(f"  - Degrees per step: {degrees_per_step}°")
print(f"  - H_STEPS calculated: {H_STEPS} steps for 360°")
print(f"  - GPIO step duration: {gpio_step_sec:.3f}s (based on 1s = {360.0/cfg['360_in_sec']:.3f}° spec)")

chip = gpiod.Chip(GPIO_CHIP)

def request_out(pin, name):
    line = chip.get_line(pin)
    line.request(consumer=name, type=gpiod.LINE_REQ_DIR_OUT, default_vals=[0])
    return line

def request_in(pin, name):
    line = chip.get_line(pin)
    line.request(consumer=name, type=gpiod.LINE_REQ_DIR_IN)
    return line

line_main   = request_out(PIN_MAIN,  "kanan")
line_alt    = request_out(PIN_ALT,   "kiri")
line_up     = request_out(PIN_UP,    "atas")
line_down   = request_out(PIN_DOWN,  "bawah")
line_button = request_in(PIN_BUTTON, "btn")     # active LOW
line_home   = request_in(PIN_HOME,   "home")    # goes LOW at startpoint
line_manual_up   = request_in(PIN_MANUAL_UP,   "manual_up")   # active LOW
line_manual_down = request_in(PIN_MANUAL_DOWN, "manual_down") # active LOW
line_abort  = request_in(PIN_ABORT,  "abort")   # active LOW

def safe_set(line, val):
    try: line.set_value(val)
    except Exception: pass

def all_low():
    for ln in (line_main, line_alt, line_up, line_down):
        safe_set(ln, 0)

def read(line):
    try: return line.get_value()
    except Exception: return 1

# =========================
# Control flags
# =========================
latest_rssi   = None
last_rssi_time = 0  # Timestamp of last valid RSSI reading
rssi_sample_count = 0  # Monotonic count: scheduler processes each SNMP sample only once
stop_rssi     = Event()  # stop RSSI worker
led_process   = None     # subprocess for led_sequence.py

# Manual control flags
manual_active = False    # Track if manual movement is active
run_once_active = False  # Track if run_once is currently running
manual_direction = None  # Track current manual direction ('up' or 'down')
abort_button_pressed_time = 0  # Track when abort button (GPIO19) was first pressed (legacy)
run_once_was_aborted = False  # Track if run_once was aborted to prevent auto-restart
manual_request_ignored_logged = False

# Multi-alignment state.  The Flask process only appends commands to the local
# spool.  This process is the sole owner of GPIO and session state.
multi_local_ip = None
multi_role = None
peer_client = None
active_session_id = None
pending_peer_result = None
last_scan_outcome = None
last_scan_success = None
peer_last_error = None
completed_session_ids = set()


def multi_enabled():
    return alignment_mode == "multi"


def configure_multi_alignment():
    """Resolve role only within the configured two-controller pair."""
    global multi_local_ip, multi_role, peer_client, peer_last_error
    if not multi_enabled():
        return
    try:
        multi_local_ip = determine_local_ip(peer_alignment_ip)
        multi_role = determine_role(multi_local_ip, peer_alignment_ip)
        peer_client = PeerApiClient(peer_alignment_ip, multi_alignment_api_token)
        print(
            f"[MULTI] enabled: local={multi_local_ip} peer={peer_alignment_ip} "
            f"role={multi_role}"
        )
    except (ValueError, OSError) as error:
        peer_last_error = str(error)
        print(f"[MULTI] unavailable: {error}")


def multi_status_snapshot(scheduler):
    fresh = rssi_is_fresh()
    return {
        "alignment_mode": alignment_mode,
        "local_ip": multi_local_ip,
        "peer_ip": peer_alignment_ip if multi_enabled() else None,
        "role": multi_role,
        "rssi": latest_rssi,
        "rssi_fresh": fresh,
        "signal_lost": is_rssi_signal_lost(latest_rssi) if fresh else None,
        "scheduler_state": scheduler.state,
        "active_session_id": active_session_id,
        "last_scan_outcome": last_scan_outcome,
        "last_scan_success": last_scan_success,
        "peer_assisted_hold": scheduler.state == scheduler.PEER_ASSISTED_HOLD,
        "ready_for_joint_scan": (
            fresh and is_rssi_signal_lost(latest_rssi)
            and joint_eligibility_ready
            and not run_once_active
            and scheduler.state not in (scheduler.ALIGNING, scheduler.SESSION_WAITING_PEER)
        ),
        "peer_last_error": peer_last_error,
    }


def publish_multi_status(scheduler):
    publish_status(multi_status_snapshot(scheduler))


def peer_is_eligible(status):
    return bool(
        status.get("rssi_fresh")
        and status.get("signal_lost")
        and status.get("ready_for_joint_scan")
        and status.get("alignment_mode") == "multi"
    )


def send_peer_command(command, session_id, result=None):
    """Use a distinct id for retriable, idempotent peer commands."""
    if peer_client is None:
        raise RuntimeError("Peer client is unavailable.")
    command_id = f"{session_id}-{command}-{int(time.time() * 1000)}"
    return peer_client.command(command, session_id, command_id, result=result)

# =========================
# RSSI monitoring
# =========================
def get_rssi(ip, port, oid, community):
    """
    SNMP read RSSI; handles vendor scaling (centi units).
    Returns int dBm or None on parse error.
    """
    out = subprocess.getoutput(f"snmpget -v 2c -c {community} {ip}:{port} {oid}")
    try:
        raw = int(out.split(":")[-1].strip())
        if raw < -10000:
            raw = raw // 100  # vendor scale correction
        return int(raw / 100)  # final to dBm
    except ValueError:
        return None

def rssi_worker():
    global latest_rssi, last_rssi_time, rssi_sample_count
    while not stop_rssi.is_set():
        r = get_rssi(IP_RADIO, port, OID_RSSI, community)
        if r is not None:
            latest_rssi = r
            last_rssi_time = time.time()
            rssi_sample_count += 1
            #print(f"Current RSSI: {r} dBm")
        
        # Adjust sleep time based on button press and run state
        if button_is_pressed() or run_once_active:
            time.sleep(0.2)  # Faster refresh when button pressed or during run
        else:
            time.sleep(1)    # Normal refresh in standby mode

# =========================
# Button handling
# =========================
def button_is_pressed():
    # Active LOW: 0 means pressed
    return read(line_button) == 0

def manual_up_is_pressed():
    # Active LOW: 0 means pressed
    return read(line_manual_up) == 0

def manual_down_is_pressed():
    # Active LOW: 0 means pressed
    return read(line_manual_down) == 0

def abort_is_active():
    # Active LOW: 0 means abort signal is active
    return read(line_abort) == 0

def check_manual_buttons():
    """
    Check manual button states and control movement.
    Returns True if manual action was taken, False otherwise.
    """
    global manual_active, manual_direction
    
    # Skip manual control if run_once is active
    if run_once_active:
        if manual_active:
            # Stop any manual movement if run_once started
            print("[MANUAL] run_once started, stopping manual control")
            safe_set(line_up, 0)
            safe_set(line_down, 0)
            manual_active = False
            manual_direction = None
        return False
    
    up_pressed = manual_up_is_pressed()
    down_pressed = manual_down_is_pressed()
    
    # # Debug output for troubleshooting
    # if up_pressed or down_pressed:
    #     print(f"[DEBUG] Manual buttons - UP: {up_pressed}, DOWN: {down_pressed}, manual_active: {manual_active}, direction: {manual_direction}")
    
    # Interlock protection: don't allow both buttons at once
    if up_pressed and down_pressed:
        # Emergency stop - both buttons pressed
        safe_set(line_up, 0)
        safe_set(line_down, 0)
        manual_active = False
        manual_direction = None
        return False
    
    # Handle UP button
    if up_pressed and not manual_active:
        print("[MANUAL] UP button pressed - activating GPIO10")
        safe_set(line_down, 0)  # Ensure down is off first
        safe_set(line_up, 1)
        manual_active = True
        manual_direction = "up"
        return True
    elif up_pressed and manual_active and manual_direction == "up":
        # Continue holding UP
        return True
    elif not up_pressed and manual_active and manual_direction == "up":
        # Release UP button
        print("[MANUAL] UP button released - deactivating GPIO10")
        safe_set(line_up, 0)
        manual_active = False
        manual_direction = None
        return True
    
    # Handle DOWN button
    if down_pressed and not manual_active:
        print("[MANUAL] DOWN button pressed - activating GPIO9")
        safe_set(line_up, 0)  # Ensure up is off first
        safe_set(line_down, 1)
        manual_active = True
        manual_direction = "down"
        return True
    elif down_pressed and manual_active and manual_direction == "down":
        # Continue holding DOWN
        return True
    elif not down_pressed and manual_active and manual_direction == "down":
        # Release DOWN button
        print("[MANUAL] DOWN button released - deactivating GPIO9")
        safe_set(line_down, 0)
        manual_active = False
        manual_direction = None
        return True
    
    return False

# =========================
# Movement primitives
# =========================
def pulse(line, duration_sec):
    safe_set(line, 1)
    t0 = time.time()
    while (time.time() - t0) < duration_sec:
        time.sleep(0.01)
    safe_set(line, 0)

def horizontal_step(move_line, snmp_entries=None, test_snmp_entries=True):
    """
    Perform a horizontal step with optional SNMP entry testing.
    
    Args:
        move_line: GPIO line to move
        snmp_entries: List of SNMP entries to test
        test_snmp_entries: Whether to test SNMP entries after the step
    """
    pulse(move_line, gpio_step_sec)
    
    # Test SNMP entries if requested and entries are available
    if test_snmp_entries and snmp_entries:
        print("Testing SNMP entries after horizontal step...")
        result = test_snmp_entries_with_rssi(
            snmp_filter_host,
            snmp_filter_set_community,
            snmp_filter_set_oid_base,
            snmp_entries,
            port,
            settle_sec,
            lambda: latest_rssi
        )
        
        if result.get("status") == "success":
            best_entry = result.get("best_entry")
            best_rssi = result.get("best_rssi")
            print(f"Best SNMP entry: {best_entry} with RSSI: {best_rssi} dBm")
            
            # Enable the best entry
            enable_best_entry(
                snmp_filter_host,
                snmp_filter_set_community,
                snmp_filter_set_oid_base,
                best_entry,
                snmp_entries,
                port
            )
            
            return {
                "rssi": best_rssi,
                "best_entry": best_entry,
                "entries_tested": True
            }
        else:
            print("No valid SNMP entries found or testing failed")
            return {
                "rssi": latest_rssi,
                "entries_tested": False
            }
    else:
        # Original settle behavior if not testing SNMP entries
        t0 = time.time()
        while (time.time() - t0) < settle_sec:
            # Check for abort signal during settle
            if abort_is_active():
                print("[ABORT] GPIO6 is LOW - aborting during horizontal step settle!")
                return
            time.sleep(0.05)
            
        return {
            "rssi": latest_rssi,
            "entries_tested": False
        }

def bump_up(duration_sec=1.0):
    print(f"Bumping UP for {duration_sec:.2f}s ...")
    pulse(line_up, duration_sec)

def drive_until_low(move_line, sensor_line, poll_interval=0.02, safety_timeout=None):
    """
    Hold move_line HIGH until sensor_line reads LOW or timeout.
    Returns True if sensor triggered, False otherwise.
    """
    print("Driving RIGHT until GPIO13 (home) is LOW ...")
    safe_set(move_line, 1)
    t0 = time.time()
    try:
        while True:
            # Check for abort signal during home seek
            if abort_is_active():
                print("[ABORT] GPIO6 is LOW - aborting during home seek!")
                return False
            if read(sensor_line) == 0:
                print("GPIO13 LOW detected (startpoint reached).")
                return True
            if safety_timeout and (time.time() - t0) > safety_timeout:
                print("Home seek timed out.")
                return False
            time.sleep(poll_interval)
    finally:
        safe_set(move_line, 0)
    return False

def format_duration(sec):
    m, s = int(sec // 60), int(sec % 60)
    return f"{m} minutes and {s} seconds"

# =========================
# Scan strategies
# =========================
def sweep_steps(move_line, reverse_line, name, start_time, snmp_entries=None):
    """
    Horizontal sweep of H_STEPS steps; stop early if target reached when enabled.
    Track best RSSI and best SNMP entry, return to it using reverse_line.
    
    Args:
        move_line: GPIO line to move
        reverse_line: GPIO line to reverse direction
        name: Name of the direction for logging
        start_time: Start time of the sweep
        snmp_entries: List of SNMP entries to test at each step
    """
    global abort_button_pressed_time
    best_rssi = -999
    best_idx  = -1
    best_entry = None

    for i in range(H_STEPS):
        # Check for abort signal (GPIO6 active LOW)
        if abort_is_active():
            print("[ABORT] GPIO6 is LOW - aborting sweep!")
            return {"status": "aborted"}
        
        # Check RSSI connection before each step
        if not check_rssi_connection():
            return {"status": "connection_lost"}
            
        # Perform horizontal step with SNMP entry testing
        step_result = horizontal_step(move_line, snmp_entries, test_snmp_entries=(snmp_entries is not None))
        
        # Check RSSI connection after step
        if not check_rssi_connection():
            return {"status": "connection_lost"}
            
        r = step_result.get("rssi")
        current_entry = step_result.get("best_entry")
        entries_tested = step_result.get("entries_tested", False)
        
        if r is None or r == -1:
            print(f"[{i+1}/{H_STEPS} {name}] RSSI invalid (-1), skipping...")
            # -1 is SNMP's no-signal sentinel, not an inferior valid reading.
            continue

        print(f"[{i+1}/{H_STEPS} {name}] RSSI: {r} dBm" +
              (f" (Entry: {current_entry})" if entries_tested and current_entry else ""))

        if r > best_rssi:
            best_rssi, best_idx = r, i
            if entries_tested and current_entry is not None:
                best_entry = current_entry

        if use_target_rssi and r >= target_rssi:
            dur = time.time() - start_time
            print(f"Target RSSI {target_rssi} dBm reached at step #{i+1} ({r} dBm)")
            print(f"Total Time: {format_duration(dur)}")
            return {
                "status": "target",
                "best_entry": best_entry
            }

        if (
            not use_target_rssi
            and best_rssi >= target_rssi
            and best_idx >= 0
            and r < best_rssi - rssi_worsening_tolerance_db
        ):
            steps_back = i - best_idx
            print(
                f"RSSI target {target_rssi} dBm has been reached. RSSI {r} dBm is "
                f"worse than best {best_rssi} dBm by more than "
                f"{rssi_worsening_tolerance_db} dB; returning {steps_back} step(s) "
                "to the best position for fine tune."
            )
            if steps_back > 0:
                pulse(reverse_line, gpio_step_sec * steps_back)
            if best_entry is not None and snmp_entries is not None:
                enable_best_entry(
                    snmp_filter_host,
                    snmp_filter_set_community,
                    snmp_filter_set_oid_base,
                    best_entry,
                    snmp_entries,
                    port
                )
            return {
                "status": "best_found",
                "best_rssi": best_rssi,
                "best_index": best_idx,
                "best_entry": best_entry
            }

    if best_idx == -1:
        print(f"\nNo valid RSSI during {name} sweep (best stayed -999).")
        return {"status": "no_best"}

    # Return to best position
    pulses_back = H_STEPS - (best_idx + 1)
    back_sec = gpio_step_sec * pulses_back
    print("\nTarget not met after sweep.")
    print(f"Best RSSI: {best_rssi} dBm at step #{best_idx+1}" +
          (f" (Entry: {best_entry})" if best_entry else ""))
    print(f"Returning to best using "
          f"{'MAIN' if reverse_line is line_main else 'ALT'} for {back_sec:.2f}s...")

    if back_sec > 0:
        pulse(reverse_line, back_sec)

    # Enable the best SNMP entry if we have one
    if best_entry is not None and snmp_entries is not None:
        print(f"Enabling best SNMP entry {best_entry} at best position...")
        enable_best_entry(
            snmp_filter_host,
            snmp_filter_set_community,
            snmp_filter_set_oid_base,
            best_entry,
            snmp_entries,
            port
        )

    print("Returned to best RSSI position.")
    return {
        "status": "best_found",
        "best_rssi": best_rssi,
        "best_index": best_idx,
        "best_entry": best_entry
    }

def vertical_refine(iterations=10, bump_sec=1.0, settle=2.0):
    """
    Sample upwards (UP bumps) and settle between samples.
    Return to best vertical point using DOWN.
    If no valid RSSI at all, return to first position before bump.
    """
    print(f"\n--- Vertical refine ({iterations}x) start ---")
    samples = []
    total_bumps = 0

    # Check RSSI connection before initial settle
    if not check_rssi_connection():
        return False

    t0 = time.time()
    while (time.time() - t0) < settle:
        # Check for abort signal during initial settle
        if abort_is_active():
            print("[ABORT] GPIO6 is LOW - aborting during vertical refine initial settle!")
            return False
        time.sleep(0.05)
        # Check connection during settle
        if not check_rssi_connection():
            return False
            
    r = latest_rssi
    if r is not None and r != -1:
        samples.append((0, r))
        print(f"[VR idx 0] RSSI: {r} dBm")
    else:
        print("[VR idx 0] RSSI invalid (None/-1)")

    for i in range(1, iterations + 1):
        # Check connection before bump
        if not check_rssi_connection():
            return False
            
        bump_up(bump_sec)
        total_bumps += 1
        
        # Check connection after bump
        if not check_rssi_connection():
            return False
            
        t0 = time.time()
        while (time.time() - t0) < settle:
            # Check for abort signal during vertical refine settle
            if abort_is_active():
                print("[ABORT] GPIO6 is LOW - aborting during vertical refine settle!")
                return False
            time.sleep(0.05)
            # Check connection during settle
            if not check_rssi_connection():
                return False
                
        r = latest_rssi
        if r is not None and r != -1:
            samples.append((i, r))
            print(f"[VR idx {i}] RSSI: {r} dBm")
        else:
            print(f"[VR idx {i}] RSSI invalid (None/-1)")

    if not samples:
        print("--- Vertical refine: no valid RSSI at all; returning to first position before bump ---\n")
        # Return to first position before any bumps
        if total_bumps > 0:
            move_time = bump_sec * total_bumps
            print(f"Moving DOWN with line_bawah for {move_time:.2f}s to return to first position...")
            pulse(line_down, move_time)
        print("--- Vertical refine done (returned to start) ---\n")
        return False

    best_idx, best_local = max(samples, key=lambda t: t[1])
    print(f"--- Vertical refine: best RSSI {best_local} dBm at index {best_idx} of {total_bumps} ---")

    delta_down = total_bumps - best_idx
    if delta_down > 0:
        move_time = bump_sec * delta_down
        print(f"Moving DOWN with line_bawah for {move_time:.2f}s to reach best vertical index...")
        pulse(line_down, move_time)
    else:
        print("Already at best vertical index; no DOWN adjustment needed.")

    print("--- Vertical refine done ---\n")
    return True

# =========================
# Service management functions
# =========================
def stop_monitor_service():
    """Stop the monitor.service using systemctl"""
    try:
        print("Stopping monitor.service...")
        subprocess.run(["systemctl", "stop", "monitor.service"], check=True)
        print("monitor.service stopped successfully")
        return True
    except subprocess.CalledProcessError as e:
        print(f"Failed to stop monitor.service: {e}")
        return False

def start_monitor_service():
    """Start the monitor.service using systemctl"""
    try:
        print("Starting monitor.service...")
        subprocess.run(["systemctl", "start", "monitor.service"], check=True)
        print("monitor.service started successfully")
        return True
    except subprocess.CalledProcessError as e:
        print(f"Failed to start monitor.service: {e}")
        return False

def start_led_sequence():
    """Start led_sequence.py as a subprocess"""
    global led_process
    try:
        print("Starting LED sequence...")
        led_process = subprocess.Popen(["python3", "led_sequence.py"])
        print("LED sequence started")
        return True
    except Exception as e:
        print(f"Failed to start LED sequence: {e}")
        return False

def stop_led_sequence():
    """Stop the led_sequence.py subprocess"""
    global led_process
    if led_process is not None:
        try:
            print("Stopping LED sequence...")
            led_process.terminate()
            led_process.wait(timeout=5)
            print("LED sequence stopped")
            led_process = None
            return True
        except subprocess.TimeoutExpired:
            print("LED sequence did not terminate, killing...")
            led_process.kill()
            led_process.wait()
            led_process = None
            return True
        except Exception as e:
            print(f"Error stopping LED sequence: {e}")
            return False
    return True

# =========================
# One full run (from button press)
# =========================
def check_rssi_connection():
    """Check if RSSI connection is active, return True if OK, False if lost"""
    record_manual_request_during_alignment()
    current_time = time.time()
    if latest_rssi is None or (current_time - last_rssi_time) > 5:  # No RSSI update for 5 seconds
        if latest_rssi is None:
            print("No RSSI connection available.")
        else:
            print(f"RSSI connection lost (last update {current_time - last_rssi_time:.1f}s ago).")
        return False
    return True

def rssi_is_fresh():
    """A fresh RSSI can be -1; an unavailable SNMP reading is represented by None/stale."""
    return latest_rssi is not None and (time.time() - last_rssi_time) <= 5

def is_rssi_signal_lost(rssi):
    """Keep scan outcomes consistent with the scheduler loss rule."""
    return rssi is not None and (
        rssi == -1 or rssi < auto_signal_loss_rssi_threshold
    )

def record_manual_request_during_alignment():
    """Keep the Start button observable during a blocking alignment without queuing scans."""
    global manual_request_ignored_logged
    if run_once_active and button_is_pressed() and not manual_request_ignored_logged:
        print("[SCHEDULER] manual request ignored: alignment in progress")
        manual_request_ignored_logged = True

def finalize_alignment(outcome, restore_filters=False):
    """Leave the hardware and LED/monitor services in a safe, known state."""
    global run_once_active, run_once_was_aborted
    run_once_active = False
    run_once_was_aborted = outcome == "aborted"
    all_low()
    stop_led_sequence()
    if restore_filters:
        print("\n[ALIGNMENT] Re-enabling all non-zero SNMP entries...")
        all_non_zero_entries = get_non_zero_entries(
            snmp_filter_host, snmp_filter_community, snmp_filter_oid, port, max_entries=None
        )
        if all_non_zero_entries:
            for _, _, last_digit in all_non_zero_entries:
                enable_oid = f"{snmp_filter_set_oid_base}.{last_digit}"
                run_snmpset(snmp_filter_host, snmp_filter_set_community, enable_oid, '1', 'i', port, verbose=False)
            print("[ALIGNMENT] All non-zero SNMP entries re-enabled")
    start_monitor_service()
    return outcome

def cleanup_and_abort():
    """Compatibility wrapper for physical abort paths."""
    return finalize_alignment("aborted", restore_filters=True)

def run_once(reason):
    global latest_rssi, run_once_active, abort_button_pressed_time, run_once_was_aborted, manual_request_ignored_logged
    start = time.time()
    started_with_signal_lost = is_rssi_signal_lost(latest_rssi)
    
    # Set flag to indicate run_once is active
    run_once_active = True
    abort_button_pressed_time = 0  # Reset abort button timer
    run_once_was_aborted = False  # Reset abort flag at start of new run
    manual_request_ignored_logged = False
    
    print(f"[ALIGNMENT] Starting scan: reason={reason}")
    # Stop monitor service and start LED sequence at the beginning of each run
    stop_monitor_service()
    start_led_sequence()

    # Check if RSSI is available and connection is active
    if not check_rssi_connection():
        return finalize_alignment("rssi_unavailable", restore_filters=True)

    # ---- Get SNMP entries with specific values ----
    print("\nGetting SNMP entries with specific values...")
    target_values = target_frequencies_hz
    snmp_entries = get_entries_with_specific_values(
        snmp_filter_host,
        snmp_filter_community,
        snmp_filter_oid,
        port,
        target_values
    )
    
    # ---- Get all non-zero entries for calibration phase ----
    print("\nGetting all non-zero SNMP entries for calibration...")
    all_non_zero_entries = get_non_zero_entries(
        snmp_filter_host,
        snmp_filter_community,
        snmp_filter_oid,
        port,
        max_entries=None  # Get all non-zero entries, not limited
    )
    
    if not snmp_entries:
        print("No SNMP entries with target values found. Proceeding without SNMP filtering.")
        snmp_entries = None
    else:
        print(f"Found {len(snmp_entries)} SNMP entries with target values")
    
    # Disable all non-zero SNMP entries during calibration phase
    if all_non_zero_entries:
        print(f"Disabling all {len(all_non_zero_entries)} non-zero SNMP entries for calibration")
        configure_snmp_entries_for_calibration(
            snmp_filter_host,
            snmp_filter_set_community,
            snmp_filter_set_oid_base,
            all_non_zero_entries,
            port
        )

    # ---- Calibration phase ----
    # 1) DOWN N seconds (actuator_calibrate)
    t_down = Thread(target=pulse, args=(line_down, actuator_calibrate), daemon=True)

    # 2) RIGHT until GPIO13 goes LOW (startpoint/home)
    def right_to_home():
        # Optional safety timeout: e.g., 120s (adjust if needed)
        drive_until_low(line_main, line_home, poll_interval=0.02, safety_timeout=None)

    t_right = Thread(target=right_to_home, daemon=True)

    print("Calibration started: DOWN (N sec) + RIGHT until GPIO13 LOW (startpoint).")
    t_down.start(); t_right.start()
    
    # Monitor RSSI during calibration
    while t_down.is_alive() or t_right.is_alive():
        # Check for abort signal (GPIO6 active LOW)
        if abort_is_active():
            print("[ABORT] GPIO6 is LOW - aborting calibration!")
            return cleanup_and_abort()
        
        if not check_rssi_connection():
            return finalize_alignment("rssi_unavailable", restore_filters=True)
        time.sleep(0.1)
    
    print("Calibration finished (startpoint set).")

    # 3) UP N/2
    pulse(line_up, actuator_calibrate / 3.0)

    # 4) Sleep 1s
    time.sleep(1)

    print(f"Initial RSSI: {latest_rssi} dBm")

    # First direction is LEFT (because we homed to the RIGHT)
    direction = "LEFT"
    no_best_tries = 0  # single opposite-direction attempt

    # ---- Serpentine scan loop ----
    while True:
        # Check for abort signal (GPIO6 active LOW)
        if abort_is_active():
            print("[ABORT] GPIO6 is LOW - aborting serpentine scan!")
            return cleanup_and_abort()
        
        # Check RSSI connection before each sweep
        if not check_rssi_connection():
            return finalize_alignment("rssi_unavailable", restore_filters=True)
             
        if direction == "RIGHT":
            result = sweep_steps(line_main, line_alt, "RIGHT", start, snmp_entries)
        else:
            result = sweep_steps(line_alt, line_main, "LEFT", start, snmp_entries)

        # Check RSSI connection after sweep
        if not check_rssi_connection() or result.get("status") == "connection_lost":
            return finalize_alignment("rssi_unavailable", restore_filters=True)
        
        # Check if sweep was aborted
        if result.get("status") == "aborted":
            return cleanup_and_abort()

        if result.get("status") in ("target", "best_found"):
            time.sleep(1)
            # Check RSSI before vertical refine
            # Check for abort signal before vertical refine
            if abort_is_active():
                print("[ABORT] GPIO6 is LOW - aborting before vertical refine!")
                return cleanup_and_abort()

            if not check_rssi_connection():
                return finalize_alignment("rssi_unavailable", restore_filters=True)
            if not vertical_refine(iterations=iteration_actuator, bump_sec=actuator_speed, settle=settle_sec):
                return cleanup_and_abort() if abort_is_active() else finalize_alignment("failed", restore_filters=True)
            if started_with_signal_lost and not is_rssi_signal_lost(latest_rssi):
                outcome = "signal_recovered"
            else:
                outcome = "target_reached" if result.get("status") == "target" else "best_position_found"
            return finalize_alignment(outcome)

        if result.get("status") == "no_best":
            no_best_tries += 1
            print(f"[no_best attempt {no_best_tries}/2]")
            if no_best_tries > max_try - 1:
                print("Reached 'no_best' trying attempts. Stopping.")
                outcome = "signal_still_lost" if latest_rssi == -1 else "failed"
                return finalize_alignment(outcome, restore_filters=True)
            time.sleep(1)
            # Check for abort signal during the sleep
            if abort_is_active():
                print("[ABORT] GPIO6 is LOW - aborting during no_best retry!")
                return cleanup_and_abort()
                
            bump_up(actuator_speed)
            direction = "LEFT" if direction == "RIGHT" else "RIGHT"

    return finalize_alignment("failed", restore_filters=True)

# =========================
# Main loop: scheduler owns every invocation of run_once.
# =========================
try:
    print("Ready. Monitoring RSSI, scheduler, and button press...")
    
    # Start RSSI monitoring thread
    stop_rssi.clear()
    rssi_thread = Thread(target=rssi_worker, daemon=True)
    rssi_thread.start()
    configure_multi_alignment()
    multi_role_retry_interval_sec = 5
    next_multi_role_retry_at = time.time() + multi_role_retry_interval_sec
    
    scheduler = AutoAlignmentScheduler(
        auto_boot_rssi_minus_one_count,
        auto_signal_loss_duration_sec,
        auto_rescan_cooldown_sec,
        auto_rescan_max_attempts,
        auto_signal_loss_rssi_threshold
    )
    previous_state = scheduler.state
    last_scheduler_sample_count = -1
    nonlocal_pending_joint = {"session_id": None, "reason": None}
    joint_eligibility_ready = False

    def run_single_scan(reason):
        """Existing single-controller behavior, retained unchanged by default."""
        global last_scan_outcome, last_scan_success
        print(f"[SCHEDULER] scan requested: reason={reason}")
        all_low()
        outcome = run_once(reason)
        last_scan_outcome = outcome
        last_scan_success = is_success_outcome(outcome)
        is_fresh = rssi_is_fresh()
        entered_cooldown = scheduler.complete_scan(outcome, latest_rssi, is_fresh, time.time())
        if entered_cooldown:
            print(
                f"[SCHEDULER] outcome={outcome}; cooldown until "
                f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(scheduler.cooldown_until))}"
            )
        else:
            print(f"[SCHEDULER] outcome={outcome}; state={scheduler.state}")

    def run_joint_local_scan(session_id, reason):
        """Perform the local scan for one already-authorized joint session."""
        global last_scan_outcome, last_scan_success
        scheduler.begin_joint_scan(reason)
        publish_multi_status(scheduler)
        all_low()
        outcome = run_once(reason)
        last_scan_outcome = outcome
        last_scan_success = is_success_outcome(outcome)
        publish_multi_status(scheduler)
        return outcome, last_scan_success

    def defer_joint_scan(session_id, reason):
        """Run a peer-authorized scan after the main loop returns from command handling."""
        nonlocal_pending_joint["session_id"] = session_id
        nonlocal_pending_joint["reason"] = reason

    def coordinator_start_joint_scan(reason, require_peer_loss=True):
        """Authorize exactly one joint scan after verifying both paired units."""
        global active_session_id, peer_last_error, joint_eligibility_ready
        if peer_client is None or multi_role != "coordinator":
            return False
        try:
            peer_status = peer_client.status()
        except RuntimeError as error:
            peer_last_error = str(error)
            print(f"[MULTI] peer unavailable; holding automatic scan: {error}")
            publish_multi_status(scheduler)
            return False
        if require_peer_loss and not peer_is_eligible(peer_status):
            print("[MULTI] peer is not eligible for joint scan; holding automatic scan")
            publish_multi_status(scheduler)
            return False
        if not require_peer_loss and not (
            peer_status.get("alignment_mode") == "multi"
            and not peer_status.get("active_session_id")
            and peer_status.get("scheduler_state") not in (
                scheduler.ALIGNING, scheduler.SESSION_WAITING_PEER
            )
        ):
            print("[MULTI] peer is busy or unavailable for manual joint scan")
            publish_multi_status(scheduler)
            return False
        if not scheduler.begin_joint_wait(reason):
            return False
        joint_eligibility_ready = False
        active_session_id = new_session_id()
        publish_multi_status(scheduler)
        try:
            send_peer_command("start_joint_scan", active_session_id)
        except RuntimeError as error:
            peer_last_error = str(error)
            scheduler.state = scheduler.IDLE
            active_session_id = None
            print(f"[MULTI] could not start peer scan: {error}")
            publish_multi_status(scheduler)
            return False
        outcome, success = run_joint_local_scan(active_session_id, f"joint_{reason}")
        try:
            send_peer_command(
                "joint_scan_result", active_session_id,
                {"outcome": outcome, "success": success},
            )
        except RuntimeError as error:
            peer_last_error = str(error)
            print(f"[MULTI] local result could not be reported: {error}")
        return True

    def finalize_joint_session_if_ready():
        """Coordinator completes only after receiving both local and peer results."""
        global active_session_id, pending_peer_result, peer_last_error
        if multi_role != "coordinator" or not active_session_id or pending_peer_result is None:
            return
        peer_success = pending_peer_result.get("success")
        if not isinstance(peer_success, bool) or last_scan_success is None:
            return
        session_id = active_session_id
        state = scheduler.complete_joint_session(last_scan_success, peer_success, time.time())
        print(f"[MULTI] joint session={session_id} complete; state={state}")
        try:
            send_peer_command(
                "release_peer_hold", session_id,
                {
                    "coordinator_success": last_scan_success,
                    "peer_success": peer_success,
                },
            )
        except RuntimeError as error:
            peer_last_error = str(error)
        completed_session_ids.add(session_id)
        active_session_id = None
        pending_peer_result = None
        publish_multi_status(scheduler)

    def process_multi_commands():
        """Handle peer API commands in the scheduler loop, never Flask."""
        global active_session_id, pending_peer_result, last_scan_outcome, last_scan_success
        for command in consume_commands():
            command_name = command.get("command")
            session_id = command.get("session_id")
            if command_name == "start_joint_scan":
                if session_id in completed_session_ids or run_once_active:
                    continue
                active_session_id = session_id
                defer_joint_scan(session_id, "joint_peer_command")
                publish_multi_status(scheduler)
            elif command_name == "joint_scan_result" and multi_role == "coordinator":
                if session_id == active_session_id:
                    pending_peer_result = command.get("result")
                    finalize_joint_session_if_ready()
            elif command_name == "release_peer_hold" and multi_role == "peer":
                result = command.get("result") or {}
                peer_success = result.get("peer_success")
                coordinator_success = result.get("coordinator_success")
                if isinstance(peer_success, bool) and isinstance(coordinator_success, bool):
                    scheduler.complete_joint_session(peer_success, coordinator_success, time.time())
                elif scheduler.state == scheduler.ALIGNING:
                    scheduler.enter_peer_assisted_hold()
                completed_session_ids.add(session_id)
                active_session_id = None
                publish_multi_status(scheduler)

    def run_scheduled_scan(reason):
        if not multi_enabled():
            run_single_scan(reason)
            return
        if multi_role != "coordinator":
            print("[MULTI] automatic local trigger held; only coordinator can start a joint session")
            return
        coordinator_start_joint_scan(reason, require_peer_loss=reason != "manual_button")

    while True:
        # Check manual buttons first (highest priority)
        check_manual_buttons()
        
        if multi_enabled():
            process_multi_commands()
            publish_multi_status(scheduler)

        if nonlocal_pending_joint["session_id"]:
            pending_session = nonlocal_pending_joint["session_id"]
            pending_reason = nonlocal_pending_joint["reason"]
            nonlocal_pending_joint["session_id"] = None
            nonlocal_pending_joint["reason"] = None
            outcome, success = run_joint_local_scan(pending_session, pending_reason)
            try:
                send_peer_command(
                    "joint_scan_result", pending_session,
                    {"outcome": outcome, "success": success},
                )
            except RuntimeError as error:
                print(f"[MULTI] peer result delivery failed: {error}")
            publish_multi_status(scheduler)
            continue

        now = time.time()
        if (
            multi_enabled()
            and multi_role is None
            and now >= next_multi_role_retry_at
        ):
            configure_multi_alignment()
            next_multi_role_retry_at = now + multi_role_retry_interval_sec
        automatic_reason = None
        if rssi_sample_count != last_scheduler_sample_count:
            last_scheduler_sample_count = rssi_sample_count
            if multi_enabled() and multi_role == "peer":
                previous_joint_eligibility = joint_eligibility_ready
                joint_eligibility_ready = scheduler.observe_joint_eligibility(
                    latest_rssi, rssi_is_fresh(), now
                )
                if joint_eligibility_ready and not previous_joint_eligibility:
                    print("[MULTI] peer is eligible; waiting for coordinator joint session")
            elif multi_enabled() and multi_role == "coordinator":
                previous_joint_eligibility = joint_eligibility_ready
                joint_eligibility_ready = scheduler.observe_joint_eligibility(
                    latest_rssi, rssi_is_fresh(), now
                )
                if joint_eligibility_ready:
                    coordinator_start_joint_scan(
                        "boot_signal_lost" if not scheduler.has_seen_normal_rssi else "signal_loss"
                    )
                    previous_state = scheduler.state
                    continue
            elif not (multi_enabled() and scheduler.state == scheduler.PEER_ASSISTED_HOLD):
                automatic_reason = scheduler.observe_rssi(latest_rssi, rssi_is_fresh(), now)
        if automatic_reason:
            run_scheduled_scan(automatic_reason)
            previous_state = scheduler.state
            continue

        if scheduler.state != previous_state:
            if scheduler.state == scheduler.COOLDOWN:
                print("[SCHEDULER] automatic trigger held: cooldown active")
            else:
                print(f"[SCHEDULER] state changed: {previous_state} -> {scheduler.state}")
            previous_state = scheduler.state

        # A manual request may bypass cooldown, but never run alongside an alignment.
        if button_is_pressed():
            # Debounce: wait for stable low ~20ms
            time.sleep(0.02)
            if not button_is_pressed():
                continue
            print("[BUTTON] Start pressed.")
            manual_reason = scheduler.request_manual_scan()
            if manual_reason and not (multi_enabled() and multi_role != "coordinator"):
                run_scheduled_scan(manual_reason)
            else:
                print("[SCHEDULER] manual request ignored: alignment in progress, waiting for RSSI, or peer-controlled mode")
            
            # Wait for button release
            while button_is_pressed():
                time.sleep(0.02)
        
        time.sleep(0.02)

except KeyboardInterrupt:
    print("\nCTRL+C detected → forcing all GPIO LOW now.")
    stop_rssi.set()
    run_once_active = False  # Reset run_once flag
    manual_active = False    # Reset manual flag
    manual_direction = None   # Reset manual direction
    all_low()
    # Clean up LED sequence if running
    stop_led_sequence()
    start_monitor_service()
finally:
    # Ensure clean exit
    stop_rssi.set()
    run_once_active = False  # Reset run_once flag
    manual_active = False    # Reset manual flag
    manual_direction = None   # Reset manual direction
    all_low()
    # Clean up LED sequence if running
    stop_led_sequence()
    start_monitor_service()
    for ln in (line_main, line_alt, line_up, line_down):
        try: ln.release()
        except Exception: pass
    try: chip.close()
    except Exception: pass
