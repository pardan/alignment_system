#!/usr/bin/env python3
"""
Simulasi auto_new.py TANPA radio / Subscriber Station.

Yang disimulasikan:
  - snmpget RSSI  -> nilai RSSI palsu, dihitung dari posisi antena simulasi
  - snmpwalk/snmpset filter frekuensi (snmp_filter_integration) -> palsu

Yang TETAP asli (hardware Raspberry Pi):
  - GPIO motor (kanan/kiri/atas/bawah), tombol Start, tombol manual,
    sensor home GPIO13, abort GPIO6, LED, systemctl monitor.service

Posisi antena disimulasikan dari lamanya pin motor HIGH:
  - azimuth (derajat ke KIRI dari home): RIGHT(GPIO21) mengurangi, LEFT(GPIO20) menambah,
    kecepatan 360 / 360_in_sec derajat per detik. Di-reset ke 0 saat GPIO13 LOW
    terbaca sambil motor kanan menyala (home tercapai).
  - elevasi (detik aktuator): UP(GPIO10) menambah, DOWN(GPIO9) mengurangi,
    dibatasi 0..actuator_calibrate.

Kode auto_new.py dijalankan apa adanya (tidak di-copy), jadi perubahan di
auto_new.py otomatis ikut tersimulasi.

Contoh:
  sudo systemctl stop auto.service
  sudo python3 simulation/sim_auto.py --peak-az 120
"""
import argparse
import json
import os
import random
import runpy
import subprocess
import sys
import threading
import time

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Pin sama dengan auto_new.py
PIN_MAIN, PIN_ALT, PIN_UP, PIN_DOWN, PIN_HOME = 21, 20, 10, 9, 13
ACTUATOR_CALIBRATE = 6  # detik, sama dengan actuator_calibrate di auto_new.py


def parse_args():
    p = argparse.ArgumentParser(description="Simulasi auto_new.py tanpa radio (RSSI palsu).")
    p.add_argument("--peak-az", type=float, default=120.0,
                   help="Azimuth sinyal terbaik, derajat ke KIRI dari home (default 120)")
    p.add_argument("--peak-elev", type=float, default=2.5,
                   help="Elevasi terbaik dalam detik aktuator dari bawah (default 2.5)")
    p.add_argument("--peak-rssi", type=float, default=-65.0,
                   help="RSSI di titik terbaik dalam dBm (default -65)")
    p.add_argument("--slope-az", type=float, default=0.6,
                   help="Penurunan RSSI per derajat azimuth (dB/derajat, default 0.6)")
    p.add_argument("--slope-elev", type=float, default=3.0,
                   help="Penurunan RSSI per detik elevasi (dB/detik, default 3)")
    p.add_argument("--noise", type=float, default=0.5,
                   help="Jitter acak RSSI +/- dB (default 0.5)")
    p.add_argument("--no-link", action="store_true",
                   help="RSSI selalu -1 (SS terbaca tapi belum link ke BS)")
    p.add_argument("--snmp-down", action="store_true",
                   help="snmpget selalu timeout (radio tidak terjangkau) -> uji abort di awal")
    p.add_argument("--drop-after", type=float, default=None,
                   help="snmpget mulai timeout N detik setelah motor pertama bergerak -> uji connection lost")
    p.add_argument("--freq-entries", type=int, default=0,
                   help="Jumlah entri frekuensi palsu yang cocok tx_freq_ss (default 0 = tanpa filter)")
    return p.parse_args()


# =========================
# Model posisi antena
# =========================
class AntennaSim:
    def __init__(self, deg_per_sec, args):
        self.deg_per_sec = deg_per_sec
        self.args = args
        self.lock = threading.Lock()
        self.az = 0.0
        self.elev = ACTUATOR_CALIBRATE / 2.0
        self.on_since = {}          # pin -> waktu mulai HIGH
        self.first_move_time = None

    def _apply(self, pin, dt):
        if pin == PIN_MAIN:
            self.az -= self.deg_per_sec * dt
        elif pin == PIN_ALT:
            self.az += self.deg_per_sec * dt
        elif pin == PIN_UP:
            self.elev += dt
        elif pin == PIN_DOWN:
            self.elev -= dt
        self.az %= 360.0
        self.elev = min(max(self.elev, 0.0), float(ACTUATOR_CALIBRATE))

    def _flush(self, now):
        # Integrasikan gerakan yang sedang berlangsung sampai 'now'
        for pin, t0 in self.on_since.items():
            self._apply(pin, now - t0)
            self.on_since[pin] = now

    def set_pin(self, pin, val):
        with self.lock:
            now = time.time()
            self._flush(now)
            was_on = pin in self.on_since
            if val and not was_on:
                self.on_since[pin] = now
                if self.first_move_time is None:
                    self.first_move_time = now
                print(f"[SIM] {_pin_name(pin)} ON   az={self.az:6.1f}°  elev={self.elev:4.2f}s")
            elif not val and was_on:
                del self.on_since[pin]
                print(f"[SIM] {_pin_name(pin)} OFF  az={self.az:6.1f}°  elev={self.elev:4.2f}s")

    def home_seen(self):
        # GPIO13 LOW terbaca: kalau motor kanan sedang jalan, anggap posisi home (az=0)
        with self.lock:
            if PIN_MAIN in self.on_since:
                self._flush(time.time())
                if self.az != 0.0:
                    print(f"[SIM] Home tercapai -> az di-reset 0 (sebelumnya {self.az:.1f}°)")
                self.az = 0.0

    def rssi_dbm(self):
        a = self.args
        with self.lock:
            self._flush(time.time())
            az, elev = self.az, self.elev
        d_az = abs((az - a.peak_az + 180.0) % 360.0 - 180.0)
        r = a.peak_rssi - a.slope_az * d_az - a.slope_elev * abs(elev - a.peak_elev)
        r += random.uniform(-a.noise, a.noise)
        if r < -94:
            return -1  # di luar jangkauan -> belum link
        return int(round(r))

    def snmp_alive(self):
        a = self.args
        if a.snmp_down:
            return False
        if a.drop_after is not None and self.first_move_time is not None:
            return (time.time() - self.first_move_time) < a.drop_after
        return True


def _pin_name(pin):
    return {PIN_MAIN: "RIGHT(21)", PIN_ALT: "LEFT(20)", PIN_UP: "UP(10)", PIN_DOWN: "DOWN(9)"}.get(pin, str(pin))


# =========================
# Patch gpiod: bungkus line motor & home
# =========================
class _LineProxy:
    def __init__(self, line, pin, sim):
        self._line, self._pin, self._sim = line, pin, sim

    def set_value(self, val):
        self._line.set_value(val)
        self._sim.set_pin(self._pin, val)

    def get_value(self):
        v = self._line.get_value()
        if self._pin == PIN_HOME and v == 0:
            self._sim.home_seen()
        return v

    def __getattr__(self, name):
        return getattr(self._line, name)


def patch_gpiod(sim):
    import gpiod
    real_chip_cls = gpiod.Chip

    class SimChip:
        def __init__(self, *a, **kw):
            self._chip = real_chip_cls(*a, **kw)

        def get_line(self, pin):
            line = self._chip.get_line(pin)
            if pin in (PIN_MAIN, PIN_ALT, PIN_UP, PIN_DOWN, PIN_HOME):
                return _LineProxy(line, pin, sim)
            return line

        def __getattr__(self, name):
            return getattr(self._chip, name)

    gpiod.Chip = SimChip


# =========================
# Patch SNMP
# =========================
def patch_snmp(sim, args, tx_freq_ss):
    real_getoutput = subprocess.getoutput

    def fake_getoutput(cmd, *a, **kw):
        if isinstance(cmd, str) and cmd.strip().startswith("snmpget"):
            if not sim.snmp_alive():
                time.sleep(1)
                return "Timeout: No Response from simulated radio"
            r = -1 if args.no_link else sim.rssi_dbm()
            # auto_new.get_rssi membagi 100 -> kirim dalam centi-dBm
            return f"SNMPv2-SMI::enterprises.1807.113.2.11.1.2.1.1 = INTEGER: {r * 100}"
        return real_getoutput(cmd, *a, **kw)

    subprocess.getoutput = fake_getoutput

    import snmp_filter_integration as sfi
    entries = list(tx_freq_ss)[:max(args.freq_entries, 0)]

    def fake_snmpwalk(host, community, oid, port=161):
        if not sim.snmp_alive():
            return []
        return [(f"{oid}.{i + 1}", str(freq)) for i, freq in enumerate(entries)]

    def fake_snmpset(host, community, oid, value, data_type='i', port=161, verbose=False):
        return sim.snmp_alive()

    sfi.run_snmpwalk = fake_snmpwalk
    sfi.run_snmpset = fake_snmpset


def load_cfg():
    try:
        with open(os.path.join(PROJECT_DIR, "config.json")) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def main():
    args = parse_args()

    # auto_new.py memakai path relatif (config.json, led_sequence.py)
    os.chdir(PROJECT_DIR)
    sys.path.insert(0, PROJECT_DIR)

    cfg = load_cfg()
    deg_per_sec = 360.0 / float(cfg.get("360_in_sec", 68))
    tx_freq_ss = cfg.get("tx_freq_ss", [10507500, 10514500, 10521500, 10528500, 10535500, 10542500])

    sim = AntennaSim(deg_per_sec, args)
    patch_gpiod(sim)
    patch_snmp(sim, args, tx_freq_ss)

    print("=" * 60)
    print("SIMULASI auto_new.py - RSSI dari radio PALSU, GPIO ASLI")
    print(f"  peak az={args.peak_az}° elev={args.peak_elev}s rssi={args.peak_rssi} dBm")
    print(f"  no_link={args.no_link} snmp_down={args.snmp_down} drop_after={args.drop_after} "
          f"freq_entries={args.freq_entries}")
    print("  Pastikan auto.service sudah di-stop (GPIO motor dipakai bersama).")
    print("  Tekan tombol Start (GPIO5) untuk mulai, CTRL+C untuk keluar.")
    print("=" * 60)

    runpy.run_path(os.path.join(PROJECT_DIR, "auto_new.py"), run_name="__main__")


if __name__ == "__main__":
    main()
