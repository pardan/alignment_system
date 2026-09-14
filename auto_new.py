import os
import json
import time
import subprocess
import signal
import gpiod
from threading import Thread, Event
from alignment_scheduler import AutoAlignmentScheduler
from multi_alignment import (
    PairedControllerApiClient,
    consume_commands,
    determine_local_ip,
    determine_role,
    is_success_outcome,
    publish_status,
    requires_pre_scan_handover,
    select_preferred_link,
    select_preferred_link_with_freshness,
)
from snmp_filter_integration import (
    get_non_zero_entries,
    get_entries_with_specific_values,
    configure_snmp_entries_for_calibration,
    test_snmp_entries_with_rssi,
    enable_best_entry,
    extract_last_digit,
    run_snmpwalk,
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
    "PAIRED_CONTROLLER_IP": "",
    "MULTI_ALIGNMENT_API_TOKEN": "",
    "MULTI_RSSI_COMPARE_INTERVAL_SEC": 5,
    "MULTI_PAIR_FAILURE_THRESHOLD": 3,
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
paired_controller_ip = cfg["PAIRED_CONTROLLER_IP"]
multi_alignment_api_token = cfg["MULTI_ALIGNMENT_API_TOKEN"]
multi_rssi_compare_interval_sec = cfg["MULTI_RSSI_COMPARE_INTERVAL_SEC"]
multi_pair_failure_threshold = cfg["MULTI_PAIR_FAILURE_THRESHOLD"]
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
paired_client = None
last_scan_outcome = None
last_scan_success = None
paired_last_error = None
link_filters_active = None
paired_failure_count = 0
paired_available = False
standalone_failover = False


def multi_enabled():
    return alignment_mode == "multi"


def configure_multi_alignment():
    """Resolve role only within the configured two-controller pair."""
    global multi_local_ip, multi_role, paired_client, paired_last_error
    if not multi_enabled():
        return
    try:
        multi_local_ip = determine_local_ip(paired_controller_ip)
        multi_role = determine_role(multi_local_ip, paired_controller_ip)
        paired_client = PairedControllerApiClient(paired_controller_ip, multi_alignment_api_token)
        print(
            f"[MULTI] enabled: local={multi_local_ip} paired={paired_controller_ip} "
            f"role={multi_role}"
        )
    except (ValueError, OSError) as error:
        paired_last_error = str(error)
        print(f"[MULTI] unavailable: {error}")


def multi_status_snapshot(scheduler):
    fresh = rssi_is_fresh()
    return {
        "alignment_mode": alignment_mode,
        "local_ip": multi_local_ip,
        "paired_controller_ip": paired_controller_ip if multi_enabled() else None,
        "role": multi_role,
        "rssi": latest_rssi,
        "rssi_fresh": fresh,
        "signal_lost": is_rssi_signal_lost(latest_rssi) if fresh else None,
        "scheduler_state": scheduler.state,
        "last_scan_outcome": last_scan_outcome,
        "last_scan_success": last_scan_success,
        "link_filters_active": link_filters_active,
        "paired_available": paired_available,
        "paired_failure_count": paired_failure_count,
        "standalone_failover": standalone_failover,
        "paired_last_error": paired_last_error,
    }


def publish_multi_status(scheduler):
    publish_status(multi_status_snapshot(scheduler))


def send_paired_command(command, command_id, result=None):
    """Use a distinct id for retriable, idempotent paired-controller commands."""
    if paired_client is None:
        raise RuntimeError("Paired-controller client is unavailable.")
    return paired_client.command(command, command_id, result=result)


def paired_status_or_none():
    """Use HTTP reachability for paired-controller failover, not wall-clock time."""
    global paired_available, paired_failure_count, standalone_failover, paired_last_error
    if paired_client is None:
        return None
    try:
        status = paired_client.status()
    except RuntimeError as error:
        paired_last_error = str(error)
        paired_failure_count += 1
        paired_available = False
        if paired_failure_count >= multi_pair_failure_threshold:
            if not standalone_failover:
                print(
                    f"[MULTI] paired controller unavailable after {paired_failure_count} failed checks; "
                    "entering standalone failover"
                )
            standalone_failover = True
        return None
    if standalone_failover:
        print("[MULTI] paired-controller connection restored; leaving standalone failover")
    paired_available = True
    paired_failure_count = 0
    standalone_failover = False
    paired_last_error = None
    return status


def refresh_paired_availability():
    """Probe the paired controller once per comparison interval."""
    if multi_enabled() and paired_client is not None:
        return paired_status_or_none()
    return None


def set_link_filters_active(active):
    """Enable or disable every configured radio filter for link selection."""
    global link_filters_active
    entries = get_entries_with_specific_values(
        snmp_filter_host,
        snmp_filter_community,
        snmp_filter_oid,
        port,
        target_frequencies_hz,
    )
    if not entries:
        print("[MULTI] link selection skipped: no configurable frequency filters found")
        return False
    value = "1" if active else "2"
    action = "enabling" if active else "disabling"
    print(f"[MULTI] {action} {len(entries)} frequency filter(s) for link selection")
    succeeded = True
    for _, _, last_digit in entries:
        oid = f"{snmp_filter_set_oid_base}.{last_digit}"
        if not run_snmpset(
            snmp_filter_host, snmp_filter_set_community, oid, value, "i", port, verbose=False
        ):
            succeeded = False
    expected_value = int(value)
    states = {
        extract_last_digit(oid): raw_value
        for oid, raw_value in run_snmpwalk(
            snmp_filter_host, snmp_filter_community, snmp_filter_set_oid_base, port
        )
        if extract_last_digit(oid) is not None
    }
    mismatches = []
    for _, _, last_digit in entries:
        try:
            actual_value = int(states.get(last_digit, ""))
        except (TypeError, ValueError):
            actual_value = None
        if actual_value != expected_value:
            mismatches.append(f"{last_digit}={states.get(last_digit, 'missing')}")
    if mismatches:
        succeeded = False
        print(
            f"[MULTI] filter verification failed; expected {expected_value}, "
            f"got {', '.join(mismatches)}"
        )
    if succeeded:
        link_filters_active = active
        print(f"[MULTI] local frequency filters are now {'active' if active else 'disabled'}")
    else:
        print(f"[MULTI] failed to finish {action} local frequency filters")
    return succeeded


def read_link_filters_active():
    """Read the physical state of every configured local frequency filter.

    The process can restart while a filter is enabled.  In that case the
    in-memory ``link_filters_active`` value is not authoritative; the SNMP
    enable OIDs (``.4``) are.  Return ``None`` for an incomplete or mixed
    result so the selection code re-applies its desired state safely.
    """
    entries = get_entries_with_specific_values(
        snmp_filter_host,
        snmp_filter_community,
        snmp_filter_oid,
        port,
        target_frequencies_hz,
    )
    if not entries:
        print("[MULTI] cannot read local filter state: no configured frequency filters found")
        return None
    states = {
        extract_last_digit(oid): raw_value
        for oid, raw_value in run_snmpwalk(
            snmp_filter_host, snmp_filter_community, snmp_filter_set_oid_base, port
        )
        if extract_last_digit(oid) is not None
    }
    values = []
    for _, _, last_digit in entries:
        try:
            values.append(int(states[last_digit]))
        except (KeyError, TypeError, ValueError):
            print(f"[MULTI] cannot read local filter state: filter {last_digit} is missing or invalid")
            return None
    if all(value == 1 for value in values):
        return True
    if all(value == 2 for value in values):
        return False
    print(f"[MULTI] local filter state is mixed: {values}")
    return None

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
    global run_once_active, run_once_was_aborted, link_filters_active
    run_once_active = False
    run_once_was_aborted = outcome == "aborted"
    all_low()
    stop_led_sequence()
    restored_filters = False
    if restore_filters:
        print("\n[ALIGNMENT] Re-enabling all non-zero SNMP entries...")
        all_non_zero_entries = get_non_zero_entries(
            snmp_filter_host, snmp_filter_community, snmp_filter_oid, port, max_entries=None
        )
        if all_non_zero_entries:
            restored_filters = True
            for _, _, last_digit in all_non_zero_entries:
                enable_oid = f"{snmp_filter_set_oid_base}.{last_digit}"
                if not run_snmpset(
                    snmp_filter_host,
                    snmp_filter_set_community,
                    enable_oid,
                    '1',
                    'i',
                    port,
                    verbose=False,
                ):
                    restored_filters = False
            if restored_filters:
                print("[ALIGNMENT] All non-zero SNMP entries re-enabled")
            else:
                print("[ALIGNMENT] Failed to re-enable one or more SNMP entries")

    # A completed scan enables a best filter before returning.  Likewise, a
    # successful recovery path above enables every filter.  Keep the
    # coordination state aligned with the physical radio, otherwise the
    # Master can incorrectly conclude that only the Slave is active.
    if multi_enabled() and (is_success_outcome(outcome) or restored_filters):
        link_filters_active = True
        print("[MULTI] local frequency filters are active after alignment")
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
    link_selection_interval_sec = multi_rssi_compare_interval_sec
    next_link_selection_at = time.time()
    
    scheduler = AutoAlignmentScheduler(
        auto_boot_rssi_minus_one_count,
        auto_signal_loss_duration_sec,
        auto_rescan_cooldown_sec,
        auto_rescan_max_attempts,
        auto_signal_loss_rssi_threshold
    )
    previous_state = scheduler.state
    last_scheduler_sample_count = -1

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

    def run_multi_local_scan(reason):
        """Run this controller's independent recovery scan in multi mode."""
        global last_scan_outcome, last_scan_success
        print(f"[MULTI] local recovery scan requested: reason={reason}")
        if multi_role == "master" and link_filters_active is True:
            paired_status = paired_status_or_none()
            slave_rssi_fresh = bool(paired_status and paired_status.get("rssi_fresh"))
            if requires_pre_scan_handover(
                multi_role, link_filters_active, slave_rssi_fresh
            ):
                print("[MULTI] Master link is active; handing over to Slave before local scan")
                if not set_paired_link_active(True):
                    print("[MULTI] Master scan deferred: Slave link did not confirm active")
                    scheduler.complete_scan("handover_failed", latest_rssi, rssi_is_fresh(), time.time())
                    publish_multi_status(scheduler)
                    return None, False
                if not set_link_filters_active(False):
                    print("[MULTI] Master scan deferred: could not release Master link after Slave handover")
                    scheduler.complete_scan("handover_failed", latest_rssi, rssi_is_fresh(), time.time())
                    publish_multi_status(scheduler)
                    return None, False
            else:
                print("[MULTI] Slave RSSI is not fresh; Master scanning without handover")
        if scheduler.state != scheduler.ALIGNING and not scheduler.begin_local_scan(reason):
            print("[MULTI] local recovery scan held by scheduler state or retry limit")
            return None, False
        outcome = run_once(reason)
        last_scan_outcome = outcome
        last_scan_success = is_success_outcome(outcome)
        scheduler.complete_scan(outcome, latest_rssi, rssi_is_fresh(), time.time())
        publish_multi_status(scheduler)
        return outcome, last_scan_success

    def set_paired_link_active(active):
        """Ask the Slave to change link state and confirm it before switching locally."""
        global paired_last_error
        command_id = f"set-link-{int(time.time() * 1000)}"
        try:
            send_paired_command("set_link_active", command_id, {"active": active})
        except RuntimeError as error:
            paired_last_error = str(error)
            print(f"[MULTI] link selection could not request paired-controller change: {error}")
            return False

        deadline = time.time() + max(3, multi_rssi_compare_interval_sec + 1)
        while time.time() < deadline:
            time.sleep(0.1)
            paired_status = paired_status_or_none()
            if paired_status is None:
                continue
            if paired_status.get("link_filters_active") is active:
                return True
        paired_last_error = (
            f"Paired controller did not confirm link {'activation' if active else 'deactivation'}."
        )
        print(f"[MULTI] {paired_last_error}")
        return False

    def update_preferred_link(now, paired_status=None, paired_checked=False):
        """Keep the stronger paired radio active while both controllers are idle."""
        global paired_last_error, link_filters_active
        if (
            multi_role != "master"
            or paired_client is None
            or run_once_active
        ):
            return False
        if paired_status is None and not paired_checked:
            paired_status = paired_status_or_none()
        if paired_status is None:
            print("[MULTI] link selection waiting: paired-controller status unavailable")
            return False
        if paired_status.get("scheduler_state") == scheduler.ALIGNING:
            print("[MULTI] link selection waiting: paired-controller scan is in progress")
            return False
        local_rssi_fresh = rssi_is_fresh()
        paired_rssi_fresh = bool(paired_status.get("rssi_fresh"))
        preferred = select_preferred_link_with_freshness(
            latest_rssi,
            local_rssi_fresh,
            paired_status.get("rssi"),
            paired_rssi_fresh,
            "local" if link_filters_active else "slave" if link_filters_active is False else None,
        )
        if preferred is None:
            print(
                "[MULTI] link selection waiting: RSSI sample is not fresh "
                f"(local_fresh={local_rssi_fresh}, paired_fresh={paired_rssi_fresh})"
            )
            return False
        if not local_rssi_fresh:
            print(
                "[MULTI] local RSSI unavailable while paired-controller RSSI is fresh; "
                "activating paired controller link"
            )
        else:
            print(
            f"[MULTI] comparing link RSSI: local={latest_rssi} dBm, "
            f"paired={paired_status.get('rssi')} dBm, preferred={preferred}"
            )
        desired_local_active = preferred == "local"
        # Do not rely solely on the in-memory flag: after auto.service is
        # restarted it begins as None even if the Master filter remains
        # enabled in the radio.  Reconcile from SNMP before deciding a link is
        # already selected or whether a disable command is necessary.
        physical_local_active = read_link_filters_active()
        if physical_local_active is not None:
            if link_filters_active != physical_local_active:
                print(
                    "[MULTI] reconciling local filter state from SNMP: "
                    f"{'active' if physical_local_active else 'disabled'}"
                )
            link_filters_active = physical_local_active
        paired_active = paired_status.get("link_filters_active")
        if (
            link_filters_active == desired_local_active
            and paired_active == (not desired_local_active)
        ):
            print(f"[MULTI] preferred link={preferred} already active")
            return True
        if desired_local_active:
            # Bring the target up first.  If the remote disable fails, both
            # links remain usable rather than leaving the radio with no link.
            if not link_filters_active and not set_link_filters_active(True):
                return False
            if paired_active and not set_paired_link_active(False):
                return False
            changed = True
        else:
            # Confirm Slave link activation before taking the Master link down.
            if not paired_active and not set_paired_link_active(True):
                return False
            # ``None`` means SNMP reported a mixed/unreadable physical state;
            # force an explicit disable instead of assuming the Master is off.
            if link_filters_active is not False and not set_link_filters_active(False):
                return False
            changed = True
        if changed:
            print(
                f"[MULTI] preferred link={preferred}; local RSSI={latest_rssi} dBm, "
                f"paired-controller RSSI={paired_status.get('rssi')} dBm"
            )
            publish_multi_status(scheduler)
        return changed

    def run_scheduled_scan(reason):
        if multi_enabled():
            run_multi_local_scan(reason)
        else:
            run_single_scan(reason)

    while True:
        # Check manual buttons first (highest priority)
        check_manual_buttons()
        
        if multi_enabled():
            # The only remote control retained in independent-recovery mode is
            # Master selecting the active link.  Leave commands queued while a
            # local scan owns the radio filters.
            if not run_once_active:
                for command in consume_commands():
                    if (
                        multi_role == "slave"
                        and command.get("command") == "set_link_active"
                    ):
                        desired_active = (command.get("result") or {}).get("active")
                        if isinstance(desired_active, bool):
                            set_link_filters_active(desired_active)
                            publish_multi_status(scheduler)
            publish_multi_status(scheduler)

        now = time.time()
        if (
            multi_enabled()
            and multi_role is None
            and now >= next_multi_role_retry_at
        ):
            configure_multi_alignment()
            next_multi_role_retry_at = now + multi_role_retry_interval_sec
        if multi_enabled() and now >= next_link_selection_at:
            next_link_selection_at = now + link_selection_interval_sec
            cached_paired_status = refresh_paired_availability()
            update_preferred_link(
                now,
                paired_status=cached_paired_status,
                paired_checked=True,
            )
            if standalone_failover and not run_once_active and not link_filters_active:
                print("[MULTI] standalone failover: enabling local frequency filters")
                set_link_filters_active(True)
        automatic_reason = None
        if rssi_sample_count != last_scheduler_sample_count:
            last_scheduler_sample_count = rssi_sample_count
            automatic_reason = scheduler.observe_rssi(
                latest_rssi, rssi_is_fresh(), now
            )
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
            if manual_reason:
                run_scheduled_scan(manual_reason)
            else:
                print("[SCHEDULER] manual request ignored: alignment in progress or waiting for RSSI")
            
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
