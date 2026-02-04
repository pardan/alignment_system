#!/usr/bin/env python3
"""
SNMP Filter Integration for sweep_steps in auto_new.py
This module provides functions to find non-zero SNMP entries and control them
during the antenna sweep process.
"""

import subprocess
import re
import time
from typing import List, Tuple, Optional, Dict

def run_snmpwalk(host: str, community: str, oid: str, port: int = 161) -> List[Tuple[str, str]]:
    """
    Execute snmpwalk command and return list of (OID, value) tuples.
    
    Args:
        host: SNMP agent IP address
        community: SNMP community string
        oid: OID to walk
        port: SNMP port (default 161)
    
    Returns:
        List of tuples containing (OID, value) pairs
    """
    try:
        # Build the snmpwalk command
        cmd = [
            'snmpwalk',
            '-v2c',
            '-c', community,
            f'{host}:{port}',
            oid
        ]
        
        print(f"Executing command: {' '.join(cmd)}")
        
        # Execute the command
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30
        )
        
        if result.returncode != 0:
            print(f"Error executing snmpwalk: {result.stderr}")
            return []
        
        # Parse the output
        oid_value_pairs = []
        for line in result.stdout.strip().split('\n'):
            if line.strip():
                # Parse SNMP output format: OID = TYPE: VALUE
                match = re.match(r'(.+?)\s+=\s+(.+?):\s+(.+)', line)
                if match:
                    oid_full = match.group(1).strip()
                    data_type = match.group(2).strip()
                    value = match.group(3).strip()
                    oid_value_pairs.append((oid_full, value))
                else:
                    # Try alternative format: OID = VALUE
                    match_alt = re.match(r'(.+?)\s+=\s+(.+)', line)
                    if match_alt:
                        oid_full = match_alt.group(1).strip()
                        value = match_alt.group(2).strip()
                        oid_value_pairs.append((oid_full, value))
        
        return oid_value_pairs
        
    except subprocess.TimeoutExpired:
        print("SNMP walk command timed out")
        return []
    except Exception as e:
        print(f"Error running snmpwalk: {e}")
        return []

def is_non_zero_value(value: str) -> bool:
    """
    Check if a value is non-zero.
    Handles numeric values and string representations.
    
    Args:
        value: The value to check
    
    Returns:
        True if value is non-zero, False otherwise
    """
    try:
        # Try to convert to integer
        num_value = int(value)
        return num_value != 0
    except ValueError:
        try:
            # Try to convert to float
            num_value = float(value)
            return abs(num_value) > 1e-10  # Account for floating point precision
        except ValueError:
            # For non-numeric values, check if it's not "0" or "No Such Instance" etc.
            return value.lower() not in ['0', 'no such instance', 'no such object', 'null', 'none']

def extract_last_digit(oid: str) -> Optional[int]:
    """
    Extract the last digit from an OID string.
    
    Args:
        oid: The OID string
    
    Returns:
        The last digit as an integer, or None if not found
    """
    try:
        # Split by dots and get the last part
        parts = oid.split('.')
        if parts:
            last_part = parts[-1]
            return int(last_part)
    except (ValueError, IndexError):
        pass
    return None

def run_snmpset(host: str, community: str, oid: str, value: str, data_type: str = 'i', port: int = 161, verbose: bool = False) -> bool:
    """
    Execute snmpset command.
    
    Args:
        host: SNMP agent IP address
        community: SNMP community string for set operations
        oid: OID to set
        value: Value to set
        data_type: Data type for the value (default 'i' for integer)
        port: SNMP port (default 161)
        verbose: Whether to print detailed command execution info
    
    Returns:
        True if command succeeded, False otherwise
    """
    try:
        # Build the snmpset command
        cmd = [
            'snmpset',
            '-v2c',
            '-c', community,
            f'{host}:{port}',
            oid,
            data_type,
            value
        ]
        
        if verbose:
            print(f"Executing command: {' '.join(cmd)}")
        
        # Execute the command
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30
        )
        
        if result.returncode != 0:
            if verbose:
                print(f"Error executing snmpset: {result.stderr}")
            return False
        
        if verbose:
            print(f"Success: {result.stdout.strip()}")
        return True
        
    except subprocess.TimeoutExpired:
        if verbose:
            print("SNMP set command timed out")
        return False
    except Exception as e:
        if verbose:
            print(f"Error running snmpset: {e}")
        return False

def get_non_zero_entries(host: str, community: str, oid: str, port: int = 161, max_entries: int = None) -> List[Tuple[str, str, int]]:
    """
    Get non-zero entries from SNMP walk and extract last digits.
    Optionally limit to the first N entries.
    
    Args:
        host: SNMP agent IP address
        community: SNMP community string
        oid: OID to walk
        port: SNMP port (default 161)
        max_entries: Maximum number of entries to return (None for no limit)
    
    Returns:
        List of tuples containing (OID, value, last_digit) for non-zero entries
    """
    # Run SNMP walk
    oid_value_pairs = run_snmpwalk(host, community, oid, port)
    
    if not oid_value_pairs:
        print("No data retrieved from SNMP walk.")
        return []
    
    # Filter non-zero values and extract last digits
    non_zero_entries = []
    for oid_full, value in oid_value_pairs:
        if is_non_zero_value(value):
            last_digit = extract_last_digit(oid_full)
            if last_digit is not None:
                non_zero_entries.append((oid_full, value, last_digit))
    
    # Apply max_entries limit if specified
    if max_entries is not None and len(non_zero_entries) > max_entries:
        non_zero_entries = non_zero_entries[:max_entries]
        print(f"Limited to first {max_entries} entries as requested")
    
    print(f"Found {len(non_zero_entries)} entries with non-zero values")
    return non_zero_entries

def get_entries_with_specific_values(host: str, community: str, oid: str, port: int = 161, target_values: List[int] = None) -> List[Tuple[str, str, int]]:
    """
    Get entries from SNMP walk that match specific target values.
    
    Args:
        host: SNMP agent IP address
        community: SNMP community string
        oid: OID to walk
        port: SNMP port (default 161)
        target_values: List of target values to match
    
    Returns:
        List of tuples containing (OID, value, last_digit) for matching entries
    """
    if target_values is None:
        print("No target values provided.")
        return []
    
    # Run SNMP walk
    oid_value_pairs = run_snmpwalk(host, community, oid, port)
    
    if not oid_value_pairs:
        print("No data retrieved from SNMP walk.")
        return []
    
    # Filter entries matching target values and extract last digits
    matching_entries = []
    for oid_full, value in oid_value_pairs:
        try:
            # Convert value to integer for comparison
            int_value = int(value)
            if int_value in target_values:
                last_digit = extract_last_digit(oid_full)
                if last_digit is not None:
                    matching_entries.append((oid_full, value, last_digit))
                    print(f"Found matching entry: OID {oid_full} with value {value} (target: {int_value})")
        except ValueError:
            # Skip non-integer values
            continue
    
    print(f"Found {len(matching_entries)} entries with target values")
    return matching_entries

def configure_snmp_entries_for_calibration(host: str, set_community: str, oid_base: str, non_zero_entries: List[Tuple[str, str, int]], port: int = 161) -> None:
    """
    Disable all SNMP entries during calibration phase.
    
    Args:
        host: SNMP agent IP address
        set_community: SNMP community string for set operations
        oid_base: Base OID for set operations (without the last digit)
        non_zero_entries: List of (OID, value, last_digit) tuples with non-zero values
        port: SNMP port (default 161)
    """
    if not non_zero_entries:
        print("No non-zero entries to configure.")
        return
    
    print("\nDisabling all SNMP entries for calibration phase")
    
    # Disable all entries
    for _, _, last_digit in non_zero_entries:
        disable_oid = f"{oid_base}.{last_digit}"
        run_snmpset(host, set_community, disable_oid, '2', 'i', port, verbose=False)
    
    print("All entries disabled for calibration")

def test_snmp_entries_with_rssi(host: str, set_community: str, oid_base: str, non_zero_entries: List[Tuple[str, str, int]], port: int = 161, settle_sec: int = 5, get_rssi_func=None) -> Dict:
    """
    Test each SNMP entry one by one and collect RSSI data.
    Enables each entry in sequence while disabling the others, and measures RSSI.
    
    Args:
        host: SNMP agent IP address
        set_community: SNMP community string for set operations
        oid_base: Base OID for set operations (without the last digit)
        non_zero_entries: List of (OID, value, last_digit) tuples with non-zero values
        port: SNMP port (default 161)
        settle_sec: Seconds to wait after enabling each entry
        get_rssi_func: Function to get current RSSI value
    
    Returns:
        Dictionary with test results including best entry and RSSI values
    """
    if not non_zero_entries:
        print("No non-zero entries to test.")
        return {"status": "no_entries"}
    
    if get_rssi_func is None:
        print("No RSSI function provided.")
        return {"status": "no_rssi_func"}
    
    print(f"\nTesting {len(non_zero_entries)} SNMP entries with RSSI measurement")
    
    results = []
    best_rssi = -999
    best_entry = None
    best_index = -1
    
    # Test each entry
    for i, (_, _, last_digit) in enumerate(non_zero_entries):
        print(f"\nTesting entry {i+1}/{len(non_zero_entries)} (last digit: {last_digit})")
        
        # Disable all entries first
        for _, _, other_digit in non_zero_entries:
            disable_oid = f"{oid_base}.{other_digit}"
            run_snmpset(host, set_community, disable_oid, '2', 'i', port, verbose=False)
        
        # Enable current entry
        enable_oid = f"{oid_base}.{last_digit}"
        if not run_snmpset(host, set_community, enable_oid, '1', 'i', port, verbose=False):
            print(f"Failed to enable entry {last_digit}")
            continue
        
        print(f"Enabled entry {last_digit}, others disabled")
        
        # Wait for signal to settle
        print(f"Waiting {settle_sec} seconds for signal to settle...")
        t0 = time.time()
        while (time.time() - t0) < settle_sec:
            time.sleep(0.1)
        
        # Get RSSI
        rssi = get_rssi_func()
        if rssi is not None and rssi != -1:
            print(f"RSSI for entry {last_digit}: {rssi} dBm")
            results.append((last_digit, rssi))
            
            if rssi > best_rssi:
                best_rssi = rssi
                best_entry = last_digit
                best_index = i
            
            # If we found a valid RSSI (not -1), stop testing further entries
            print(f"Valid RSSI found for entry {last_digit}, stopping further testing")
            break
        else:
            print(f"Invalid RSSI for entry {last_digit}: {rssi}")
            results.append((last_digit, rssi))
    
    # Disable all entries after testing
    print("\nDisabling all entries after testing")
    for _, _, last_digit in non_zero_entries:
        disable_oid = f"{oid_base}.{last_digit}"
        run_snmpset(host, set_community, disable_oid, '2', 'i', port, verbose=False)
    
    # Return results
    if best_entry is not None:
        print(f"\nBest entry: {best_entry} with RSSI: {best_rssi} dBm")
        return {
            "status": "success",
            "best_entry": best_entry,
            "best_rssi": best_rssi,
            "best_index": best_index,
            "results": results
        }
    else:
        print("\nNo valid RSSI measurements found")
        return {
            "status": "no_valid_rssi",
            "results": results
        }

def enable_best_entry(host: str, set_community: str, oid_base: str, best_entry: int, non_zero_entries: List[Tuple[str, str, int]], port: int = 161) -> bool:
    """
    Enable the best entry and disable all others.
    
    Args:
        host: SNMP agent IP address
        set_community: SNMP community string for set operations
        oid_base: Base OID for set operations (without the last digit)
        best_entry: The last digit of the best entry to enable
        non_zero_entries: List of (OID, value, last_digit) tuples with non-zero values
        port: SNMP port (default 161)
    
    Returns:
        True if successful, False otherwise
    """
    if best_entry is None:
        print("No best entry to enable.")
        return False
    
    print(f"\nEnabling best entry {best_entry} and disabling others")
    
    # Disable all entries first
    for _, _, last_digit in non_zero_entries:
        disable_oid = f"{oid_base}.{last_digit}"
        run_snmpset(host, set_community, disable_oid, '2', 'i', port, verbose=False)
    
    # Enable best entry
    enable_oid = f"{oid_base}.{best_entry}"
    if run_snmpset(host, set_community, enable_oid, '1', 'i', port, verbose=False):
        print(f"Successfully enabled best entry {best_entry}")
        return True
    else:
        print(f"Failed to enable best entry {best_entry}")
        return False