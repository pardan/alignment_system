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

def control_led(rssi):
    """
    Menentukan LED mana yang harus dinyalakan berdasarkan RSSI.
    """
    reset_leds()

    leds_to_turn_on = []

    if rssi == -94:
        leds_to_turn_on = [1]
    elif -93 <= rssi <= -82:
        leds_to_turn_on = [1, 2]
    elif -81 <= rssi <= -80:
        leds_to_turn_on = [1, 2, 3]
    elif -79 <= rssi <= -78:
        leds_to_turn_on = [1, 2, 3, 4]
    elif -77 <= rssi <= -76:
        leds_to_turn_on = [1, 2, 3, 4, 5]
    elif -75 <= rssi <= -74:
        leds_to_turn_on = [1, 2, 3, 4, 5, 6]
    elif -73 <= rssi <= -72:
        leds_to_turn_on = [1, 2, 3, 4, 5, 6, 7]
    elif -71 <= rssi <= -70:
        leds_to_turn_on = [1, 2, 3, 4, 5, 6, 7, 8]
    elif -69 <= rssi < -1:
        leds_to_turn_on = [1, 2, 3, 4, 5, 6, 7, 8, 9]

    # Menyalakan LED sesuai dengan daftar
    for num in leds_to_turn_on:
        if num in gpio_lines:
            gpio_lines[num].set_value(1)

def cleanup():
    """Matikan semua LED dan bersihkan GPIO."""
    reset_leds()
    for line in gpio_lines.values():
        line.release()
