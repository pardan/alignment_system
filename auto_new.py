import os
import json
import time
import subprocess
import signal
import gpiod
from threading import Thread, Event
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
    "IP_RADIO": "172.20.25.5",
    "SNMP_PORT": 161,
    "SNMP_COMMUNITY": "public",
    "OID_RSSI": "1.3.6.1.4.1.1807.113.2.11.1.2.1.1",
    "degrees_per_step": 6.0,
    "settle_sec": 5,
    "iteration_actuator": 3,
    "actuator_speed" : 0.5,
    "max_try" : 1,
    "360_in_sec": 68
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
IP_RADIO           = cfg["IP_RADIO"]
OID_RSSI           = cfg["OID_RSSI"]
community          = cfg["SNMP_COMMUNITY"]
port               = cfg["SNMP_PORT"]
degrees_per_step   = cfg["degrees_per_step"]
settle_sec         = cfg["settle_sec"]
iteration_actuator = cfg["iteration_actuator"]
actuator_speed     = cfg["actuator_speed"]
max_try            = cfg["max_try"]
# SNMP Filter Configuration
snmp_filter_host           = cfg["IP_RADIO"]
snmp_filter_community      = cfg["SNMP_COMMUNITY"]
snmp_filter_set_community  = "public"
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
stop_rssi     = Event()  # stop RSSI worker
led_process   = None     # subprocess for led_sequence.py

# Manual control flags
manual_active = False    # Track if manual movement is active
run_once_active = False  # Track if run_once is currently running
manual_direction = None  # Track current manual direction ('up' or 'down')
abort_button_pressed_time = 0  # Track when abort button (GPIO19) was first pressed (legacy)
run_once_was_aborted = False  # Track if run_once was aborted to prevent auto-restart

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
    global latest_rssi, last_rssi_time
    while not stop_rssi.is_set():
        r = get_rssi(IP_RADIO, port, OID_RSSI, community)
        if r is not None:
            latest_rssi = r
            last_rssi_time = time.time()
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
    Horizontal sweep of H_STEPS steps; stop early if target reached.
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
            continue

        print(f"[{i+1}/{H_STEPS} {name}] RSSI: {r} dBm" +
              (f" (Entry: {current_entry})" if entries_tested and current_entry else ""))

        if r > best_rssi:
            best_rssi, best_idx = r, i
            if entries_tested and current_entry is not None:
                best_entry = current_entry

        if r >= target_rssi:
            dur = time.time() - start_time
            print(f"Target RSSI {target_rssi} dBm reached at step #{i+1} ({r} dBm)")
            print(f"Total Time: {format_duration(dur)}")
            return {
                "status": "target",
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
    current_time = time.time()
    if latest_rssi is None or (current_time - last_rssi_time) > 5:  # No RSSI update for 5 seconds
        if latest_rssi is None:
            print("No RSSI connection available.")
        else:
            print(f"RSSI connection lost (last update {current_time - last_rssi_time:.1f}s ago).")
        return False
    return True

def cleanup_and_abort():
    """Common cleanup sequence when connection is lost"""
    global run_once_active, run_once_was_aborted
    run_once_active = False  # Reset the flag when aborting
    run_once_was_aborted = True  # Set flag to prevent auto-restart
    all_low()
    stop_led_sequence()
    
    # Re-enable all non-zero SNMP entries during abort
    print("\n[ABORT] Re-enabling all non-zero SNMP entries...")
    all_non_zero_entries = get_non_zero_entries(
        snmp_filter_host,
        snmp_filter_community,
        snmp_filter_oid,
        port,
        max_entries=None  # Get all non-zero entries
    )
    
    if all_non_zero_entries:
        print(f"[ABORT] Enabling {len(all_non_zero_entries)} non-zero SNMP entries...")
        for _, _, last_digit in all_non_zero_entries:
            enable_oid = f"{snmp_filter_set_oid_base}.{last_digit}"
            run_snmpset(snmp_filter_host, snmp_filter_set_community, enable_oid, '1', 'i', port, verbose=False)
        print("[ABORT] All non-zero SNMP entries re-enabled")
    else:
        print("[ABORT] No non-zero SNMP entries found to re-enable")
    
    start_monitor_service()

def run_once():
    global latest_rssi, run_once_active, abort_button_pressed_time, run_once_was_aborted
    start = time.time()
    
    # Set flag to indicate run_once is active
    run_once_active = True
    abort_button_pressed_time = 0  # Reset abort button timer
    run_once_was_aborted = False  # Reset abort flag at start of new run
    
    # Stop monitor service and start LED sequence at the beginning of each run
    stop_monitor_service()
    start_led_sequence()

    # Check if RSSI is available and connection is active
    if not check_rssi_connection():
        cleanup_and_abort()
        return False  # Indicate failure

    # ---- Get SNMP entries with specific values ----
    print("\nGetting SNMP entries with specific values...")
    target_values = [10507500, 10514500, 10521500, 10528500, 10535500, 10542500]
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
            cleanup_and_abort()
            return False
        
        if not check_rssi_connection():
            cleanup_and_abort()
            return False
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
            cleanup_and_abort()
            return False
        
        # Check RSSI connection before each sweep
        if not check_rssi_connection():
            cleanup_and_abort()
            return False
             
        if direction == "RIGHT":
            result = sweep_steps(line_main, line_alt, "RIGHT", start, snmp_entries)
        else:
            result = sweep_steps(line_alt, line_main, "LEFT", start, snmp_entries)

        # Check RSSI connection after sweep
        if not check_rssi_connection() or result.get("status") == "connection_lost":
            cleanup_and_abort()
            return False
        
        # Check if sweep was aborted
        if result.get("status") == "aborted":
            cleanup_and_abort()
            return False

        if result.get("status") in ("target", "best_found"):
            time.sleep(1)
            # Check RSSI before vertical refine
            # Check for abort signal before vertical refine
            if abort_is_active():
                print("[ABORT] GPIO6 is LOW - aborting before vertical refine!")
                cleanup_and_abort()
                return False

            if not check_rssi_connection() or not vertical_refine(iterations=iteration_actuator, bump_sec=actuator_speed, settle=settle_sec):
                cleanup_and_abort()
                return False
            break

        if result.get("status") == "no_best":
            no_best_tries += 1
            print(f"[no_best attempt {no_best_tries}/2]")
            if no_best_tries > max_try - 1:
                print("Reached 'no_best' trying attempts. Stopping.")
                break
            time.sleep(1)
            # Check for abort signal during the sleep
            if abort_is_active():
                print("[ABORT] GPIO6 is LOW - aborting during no_best retry!")
                cleanup_and_abort()
                return False
                
            bump_up(actuator_speed)
            direction = "LEFT" if direction == "RIGHT" else "RIGHT"

    # Run completed - stop LED sequence and restart monitor service
    stop_led_sequence()
    start_monitor_service()
    
    # # Enable all SNMP entries after run is complete
    # if snmp_entries is not None:
    #     print("\nEnabling all SNMP entries after run completion...")
    #     for _, _, last_digit in snmp_entries:
    #         enable_oid = f"{snmp_filter_set_oid_base}.{last_digit}"
    #         run_snmpset(snmp_filter_host, snmp_filter_set_community, enable_oid, '1', 'i', port, verbose=False)
    #     print("All SNMP entries enabled after run completion")
    
    # Clear flag to indicate run_once is no longer active
    run_once_active = False
    
    print("Run complete.\n")

# =========================
# Main loop: wait for button, run
# =========================
try:
    print("Ready. Monitoring RSSI and button press...")
    
    # Start RSSI monitoring thread
    stop_rssi.clear()
    rssi_thread = Thread(target=rssi_worker, daemon=True)
    rssi_thread.start()
    
    rssi_was_minus_one = False  # Track if RSSI was -1 to avoid repeated triggers
    button_pressed_before = False  # Track if button has been pressed at least once
    
    while True:
        # Check manual buttons first (highest priority)
        check_manual_buttons()
        
        # Check for button press (existing functionality)
        if button_is_pressed():
            # Debounce: wait for stable low ~20ms
            time.sleep(0.02)
            if not button_is_pressed():
                continue
            print("[BUTTON] Start pressed.")
            button_pressed_before = True  # Mark that button has been pressed at least once
            
            # Reset GPIO state before each run
            all_low()
            
            # Execute one full run
            success = run_once()
            
            # Only force outputs LOW if run was successful
            if success is not False:
                all_low()
            
            # Wait for button release
            while button_is_pressed():
                time.sleep(0.02)
        
        # # Check RSSI for -1 value (only after button has been pressed at least once)
        # if button_pressed_before and latest_rssi is not None and latest_rssi == -1:
        #     if not rssi_was_minus_one:  # Only trigger once when RSSI first becomes -1
        #         # Don't auto-restart if run_once was aborted
        #         if not run_once_was_aborted:
        #             print("[RSSI] RSSI is -1, triggering sequence...")
        #             rssi_was_minus_one = True
                    
        #             # Execute the required sequence
        #             all_low()
        #             success = run_once()
        #             if success is not False:
        #                 all_low()
        #         else:
        #             print("[RSSI] RSSI is -1 but run_once was aborted, skipping auto-restart")
        # else:
        #     # Reset the flag when RSSI is no longer -1
        #     rssi_was_minus_one = False
        #     # Reset abort flag when RSSI is no longer -1 (allowing future auto-restarts)
        #     if run_once_was_aborted and latest_rssi != -1:
        #         run_once_was_aborted = False
        #         print("[RSSI] RSSI recovered, clearing abort flag")
            
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
