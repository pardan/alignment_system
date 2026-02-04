import gpiod
import time

# Gunakan chip GPIO sesuai pemetaan Anda
gpio_mapping = {
    1: ("gpiochip0", 24),
    2: ("gpiochip0", 23),
    3: ("gpiochip0", 22),
    4: ("gpiochip0", 27),
    5: ("gpiochip0", 18),
    6: ("gpiochip0", 4),
    7: ("gpiochip0", 26),
    8: ("gpiochip0", 16),
    9: ("gpiochip0", 17),
}

# Inisialisasi chip GPIO
gpio_chips = {}
gpio_lines = {}

# Request akses ke GPIO sebagai output
for num, (chip_name, pin) in gpio_mapping.items():
    if chip_name not in gpio_chips:
        gpio_chips[chip_name] = gpiod.Chip(chip_name)
    
    line = gpio_chips[chip_name].get_line(pin)
    line.request(consumer="LED", type=gpiod.LINE_REQ_DIR_OUT)
    gpio_lines[num] = line  # Simpan line yang sudah diminta

def reset_leds():
    """Matikan semua LED sebelum menyalakan yang baru."""
    for line in gpio_lines.values():
        line.set_value(0)

def turn_on_leds(led_numbers):
    """Menyalakan LED berdasarkan nomor yang diberikan."""
    reset_leds()
    for num in led_numbers:
        if num in gpio_lines:
            gpio_lines[num].set_value(1)

def cleanup():
    """Matikan semua LED dan bersihkan GPIO."""
    reset_leds()
    for line in gpio_lines.values():
        line.release()

def run_led_sequence():
    """Menjalankan urutan LED yang diminta."""
    try:
        # Start with all LEDs off
        reset_leds()
        time.sleep(0.1)
        
        while True:
            # Turn on LED 1
            turn_on_leds([1])
            time.sleep(0.1)
            
            # Turn on LEDs 1, 2
            turn_on_leds([1, 2])
            time.sleep(0.1)
            
            # Turn on LEDs 1, 2, 3
            turn_on_leds([1, 2, 3])
            time.sleep(0.1)
            
            # Turn on LEDs 1, 2, 3, 4
            turn_on_leds([1, 2, 3, 4])
            time.sleep(0.1)
            
            # Turn on LEDs 1, 2, 3, 4, 5
            turn_on_leds([1, 2, 3, 4, 5])
            time.sleep(0.1)
            
            # Turn on LEDs 1, 2, 3, 4, 5, 6
            turn_on_leds([1, 2, 3, 4, 5, 6])
            time.sleep(0.1)
            
            # Turn on LEDs 1, 2, 3, 4, 5, 6, 7
            turn_on_leds([1, 2, 3, 4, 5, 6, 7])
            time.sleep(0.1)
            
            # Turn on LEDs 1, 2, 3, 4, 5, 6, 7, 8
            turn_on_leds([1, 2, 3, 4, 5, 6, 7, 8])
            time.sleep(0.1)
            
            # Turn on LEDs 1, 2, 3, 4, 5, 6, 7, 8, 9
            turn_on_leds([1, 2, 3, 4, 5, 6, 7, 8, 9])
            time.sleep(0.1)
            
            # Turn off all LEDs before repeating the cycle
            reset_leds()
            time.sleep(0.1)
            
    except KeyboardInterrupt:
        print("Program dihentikan oleh pengguna")
    finally:
        cleanup()

if __name__ == "__main__":
    run_led_sequence()