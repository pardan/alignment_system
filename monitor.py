import time
from led_control import control_led, cleanup
import subprocess
import json
import os
import threading

####################################################################
# --- Configuration Loading ---
CONFIG_FILE = 'config.json'

def load_config():
    """Loads configuration from the JSON file."""
    # Default values in case the file doesn't exist or is invalid
    defaults = {
        "target_rssi": -80,
        "actuator_calibrate": 6,
        "IP_RADIO": "172.20.25.5",
        "SNMP_PORT": 161,
        "SNMP_COMMUNITY": "public",
        "OID_RSSI": "1.3.6.1.4.1.1807.113.2.11.1.2.1.1"
    }
    if not os.path.exists(CONFIG_FILE):
        print(f"Warning: '{CONFIG_FILE}' not found. Using default settings.")
        return defaults
    
    try:
        with open(CONFIG_FILE, 'r') as f:
            print(f"Loading configuration from '{CONFIG_FILE}'...")
            return json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        print(f"Error reading '{CONFIG_FILE}': {e}. Using default settings.")
        return defaults
    
# Load configuration at the start of the script
config = load_config()
    
# Assign variables from the loaded config
IP_RADIO = config.get('IP_RADIO')
OID_RSSI = config.get('OID_RSSI')
community = config.get('SNMP_COMMUNITY')
port = config.get('SNMP_PORT')

################################################################################


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


rssi_values = [-50, -95]
blink_not_connect_bs = [-94, -95]

blinking = False  # Flag to control blinking state
blink_thread = None  # Thread for continuous blinking

def blink_led_continuous():
    """Continuously blink LED in a separate thread"""
    while blinking:
        for rssi in rssi_values:
            if not blinking:  # Check if we should stop blinking
                break
            control_led(rssi)
            time.sleep(0.2)  # Blink interval

def blink_not_connect_continuous():
    """Continuously blink LED for not connect scenario in a separate thread"""
    while blinking:
        for rssi in blink_not_connect_bs:
            if not blinking:
                break
            control_led(rssi)
            time.sleep(0.2)  # Blink interval

def start_blinking():
    """Start the continuous blinking thread"""
    global blinking, blink_thread
    if not blinking:
        blinking = True
        blink_thread = threading.Thread(target=blink_led_continuous)
        blink_thread.daemon = True  # Thread will exit when main program exits
        blink_thread.start()

def start_blinking_not_connect():
    """Start the continuous blinking thread for not connect scenario"""
    global blinking, blink_thread
    if not blinking:
        blinking = True
        blink_thread = threading.Thread(target=blink_not_connect_continuous)
        blink_thread.daemon = True  # Thread will exit when main program exits
        blink_thread.start()

def stop_blinking():
    """Stop the continuous blinking"""
    global blinking
    blinking = False
    if blink_thread and blink_thread.is_alive():
        blink_thread.join(timeout=1)  # Wait for thread to finish

try:
    while True:
        try:
            # Ambil data RSSI dari perangkat radio
            rssi = int(get_rssi(IP_RADIO, port, OID_RSSI, community))
            print(f"{rssi} dBm")
                
            # Kontrol LED berdasarkan RSSI
            if rssi == -1:
                # Special case: RSSI is -1, use blink_not_connect_bs
                print("RSSI is -1, blinking not connect pattern...")
                stop_blinking()
                start_blinking_not_connect()
            else:
                # Normal RSSI value
                control_led(rssi)
                stop_blinking()  # Stop blinking when RSSI is successful
        except Exception as e:
            print("Failed to get RSSI...")
            stop_blinking()
            start_blinking()  # Start continuous blinking when RSSI fails

        time.sleep(1)  # Loop setiap 1 detik

except KeyboardInterrupt:
    print("Monitoring dihentikan.")
    cleanup()
