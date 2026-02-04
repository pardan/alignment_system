#!/usr/bin/env python3

import gpiod
import time
import sys
import signal

# GPIO Configuration
GPIO_CHIP = "gpiochip0"
PIN_CONTROL = 21    # gpiochip0 pin 21 - control pin
PIN_MONITOR = 13   # gpiochip0 pin 13 - monitor pin  
PIN_TIMED = 20     # gpiochip0 pin 20 - timed control pin

# Timing Configuration
TIMED_DURATION = 5  # Duration in seconds for pin 20 to stay HIGH (configurable)

# Global variables for cleanup
chip = None
line_control = None
line_monitor = None
line_timed = None

def signal_handler(sig, frame):
    """Handle Ctrl+C gracefully"""
    print("\nInterrupt received, cleaning up GPIO...")
    cleanup_gpio()
    sys.exit(0)

def setup_gpio():
    """Initialize GPIO lines"""
    global chip, line_control, line_monitor, line_timed
    
    try:
        # Initialize the GPIO chip
        chip = gpiod.Chip(GPIO_CHIP)
        print(f"Initialized {GPIO_CHIP}")
        
        # Setup control pin (21) as output
        line_control = chip.get_line(PIN_CONTROL)
        line_control.request(consumer="gpio_control", type=gpiod.LINE_REQ_DIR_OUT)
        print(f"Setup GPIO {PIN_CONTROL} as output (control)")
        
        # Setup monitor pin (13) as input
        line_monitor = chip.get_line(PIN_MONITOR)
        line_monitor.request(consumer="gpio_monitor", type=gpiod.LINE_REQ_DIR_IN)
        print(f"Setup GPIO {PIN_MONITOR} as input (monitor)")
        
        # Setup timed pin (20) as output
        line_timed = chip.get_line(PIN_TIMED)
        line_timed.request(consumer="gpio_timed", type=gpiod.LINE_REQ_DIR_OUT)
        print(f"Setup GPIO {PIN_TIMED} as output (timed)")
        
        return True
        
    except Exception as e:
        print(f"Error setting up GPIO: {e}")
        return False

def cleanup_gpio():
    """Clean up GPIO resources"""
    global chip, line_control, line_monitor, line_timed
    
    try:
        # Set all outputs to low before cleanup
        if line_control:
            line_control.set_value(0)
            print("Set GPIO 21 to LOW (cleanup)")
        
        if line_timed:
            line_timed.set_value(0)
            print("Set GPIO 20 to LOW (cleanup)")
            
        # Release GPIO lines
        if line_control:
            line_control.release()
        if line_monitor:
            line_monitor.release()
        if line_timed:
            line_timed.release()
            
        # Close chip
        if chip:
            chip.close()
            
        print("GPIO cleanup completed")
        
    except Exception as e:
        print(f"Error during GPIO cleanup: {e}")

def safe_set_value(line, value, pin_name):
    """Safely set GPIO line value with error handling"""
    try:
        line.set_value(value)
        state = "HIGH" if value == 1 else "LOW"
        print(f"Set GPIO {pin_name} to {state}")
        return True
    except Exception as e:
        print(f"Error setting GPIO {pin_name}: {e}")
        return False

def safe_get_value(line, pin_name):
    """Safely read GPIO line value with error handling"""
    try:
        value = line.get_value()
        state = "HIGH" if value == 1 else "LOW"
        print(f"GPIO {pin_name} is {state}")
        return value
    except Exception as e:
        print(f"Error reading GPIO {pin_name}: {e}")
        return None

def execute_gpio_sequence():
    """Execute the main GPIO control sequence"""
    print("\n=== Starting GPIO Control Sequence ===")
    
    # Step 1: Set gpiochip0 pin 21 to high initially
    print("\nStep 1: Setting GPIO 21 to HIGH...")
    if not safe_set_value(line_control, 1, "21"):
        return False
    
    # Step 2: Monitor gpiochip0 pin 13 until it becomes low
    print("\nStep 2: Monitoring GPIO 13 until it becomes LOW...")
    monitor_start_time = time.time()
    timeout = 300  # 5 minutes timeout for monitoring
    
    while True:
        # Check for timeout
        if time.time() - monitor_start_time > timeout:
            print(f"Timeout: GPIO 13 did not become LOW within {timeout} seconds")
            return False
        
        # Read monitor pin
        monitor_value = safe_get_value(line_monitor, "13")
        
        if monitor_value is None:
            return False  # Error reading pin
        
        if monitor_value == 0:  # Pin is LOW
            print("GPIO 13 is LOW - condition met!")
            break
        else:
            print(f"GPIO 13 is HIGH, waiting... ({int(time.time() - monitor_start_time)}s elapsed)")
            time.sleep(0.1)  # Poll every 100ms
    
    # Step 3: When pin 13 becomes low, set pin 21 to low
    print("\nStep 3: Setting GPIO 21 to LOW (GPIO 13 is now LOW)...")
    if not safe_set_value(line_control, 0, "21"):
        return False
    
    # Small delay to ensure the state change is registered
    time.sleep(0.01)
    
    # # Step 4: Set gpiochip0 pin 20 to high for configured duration
    # print(f"\nStep 4: Setting GPIO 20 to HIGH for {TIMED_DURATION} seconds...")
    # print("Settling for 2 seconds before setting GPIO 20...")
    # time.sleep(2)
    # if not safe_set_value(line_timed, 1, "20"):
    #     return False
    
    # # Wait for configured duration while showing progress
    # print(f"Waiting for {TIMED_DURATION} seconds...")
    # for i in range(TIMED_DURATION, 0, -1):
    #     print(f"Time remaining: {i} seconds", end='\r')
    #     time.sleep(1)
    # print(f"\n{TIMED_DURATION} seconds completed.")
    
    # # Set GPIO 20 back to LOW after duration
    # print("Setting GPIO 20 back to LOW...")
    # safe_set_value(line_timed, 0, "20")
    
    print("\n=== GPIO Control Sequence Completed Successfully ===")
    return True

def main():
    """Main function"""
    print("GPIO Control Sequence Script")
    print(f"Using {GPIO_CHIP}")
    print(f"Control Pin: {PIN_CONTROL}")
    print(f"Monitor Pin: {PIN_MONITOR}")
    print(f"Timed Pin: {PIN_TIMED}")
    print(f"Timed Duration: {TIMED_DURATION} seconds")
    
    # Set up signal handler for graceful exit
    signal.signal(signal.SIGINT, signal_handler)
    
    # Setup GPIO
    if not setup_gpio():
        print("Failed to setup GPIO. Exiting.")
        sys.exit(1)
    
    try:
        # Execute the GPIO sequence
        success = execute_gpio_sequence()
        
        if success:
            print("Sequence completed successfully.")
            sys.exit(0)
        else:
            print("Sequence failed.")
            sys.exit(1)
            
    except Exception as e:
        print(f"Unexpected error: {e}")
        sys.exit(1)
        
    finally:
        # Always cleanup GPIO
        cleanup_gpio()

if __name__ == "__main__":
    main()