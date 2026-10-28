# Simulasi Auto (tanpa radio)

`sim_auto.py` menjalankan `auto_new.py` **apa adanya** dengan RSSI dari radio/SS diganti nilai palsu.
GPIO tetap asli: motor, tombol Start, tombol manual, sensor home GPIO13, abort GPIO6, LED.
Gunanya untuk menguji pergerakan auto (kalibrasi, sweep, kembali ke posisi terbaik, vertical refine, abort) di Raspberry Pi tanpa perlu radio.

## Cara pakai (di Raspberry Pi)

```bash
sudo systemctl stop auto.service
cd /home/prodev2025/Documents/controllerRadio/production
sudo python3 simulation/sim_auto.py
```

Tekan tombol Start (GPIO5) untuk memulai run, CTRL+C untuk keluar. Setelah selesai:

```bash
sudo systemctl start auto.service
```

`auto.service` harus di-stop dulu karena pin motornya dipakai bersama.

## Model RSSI palsu

Posisi antena dihitung dari lamanya pin motor HIGH:

- **Azimuth**: derajat ke KIRI dari home. Motor RIGHT (GPIO21) mengurangi, LEFT (GPIO20) menambah, dengan kecepatan `360 / 360_in_sec` derajat per detik. Nilainya di-reset ke 0 saat GPIO13 terbaca LOW sambil motor kanan menyala.
- **Elevasi**: detik aktuator dari bawah. UP (GPIO10) menambah, DOWN (GPIO9) mengurangi, dibatasi 0–6 detik.

`RSSI = peak_rssi − slope_az × |Δaz| − slope_elev × |Δelev|`. Kalau hasilnya di bawah −94 dBm, nilainya dikirim sebagai `-1` (belum link).
Setiap perubahan motor tercetak di log, contoh: `[SIM] LEFT(20) OFF  az= 100.4°  elev=2.00s`.

## Opsi

| Opsi | Default | Kegunaan |
|---|---|---|
| `--peak-az` | 120 | Azimuth sinyal terbaik (derajat ke kiri dari home) |
| `--peak-elev` | 2.5 | Elevasi terbaik (detik aktuator) |
| `--peak-rssi` | -65 | RSSI di titik terbaik (dBm) |
| `--slope-az` | 0.6 | Penurunan dB per derajat azimuth |
| `--slope-elev` | 3.0 | Penurunan dB per detik elevasi |
| `--noise` | 0.5 | Jitter acak ± dB |
| `--no-link` | – | RSSI selalu -1 (SS terbaca, belum link ke BS) |
| `--snmp-down` | – | snmpget selalu timeout, untuk uji abort di awal run |
| `--drop-after N` | – | snmpget mati N detik setelah motor pertama bergerak, untuk uji connection lost |
| `--freq-entries N` | 0 | Jumlah entri frekuensi palsu yang cocok `tx_freq_ss`, untuk uji jalur filter frekuensi |

## Contoh skenario

```bash
# Target tercapai di tengah sweep LEFT
sudo python3 simulation/sim_auto.py --peak-az 120

# Target tidak tercapai -> sweep penuh lalu kembali ke posisi terbaik
sudo python3 simulation/sim_auto.py --peak-rssi -85

# Koneksi SNMP putus di tengah run -> abort
sudo python3 simulation/sim_auto.py --drop-after 30

# Dengan filter frekuensi (3 entri dicoba di setiap step)
sudo python3 simulation/sim_auto.py --freq-entries 3
```

## Catatan

- Tidak ada perintah `snmpget`, `snmpwalk`, atau `snmpset` yang dikirim ke radio.
- `systemctl stop/start monitor.service` dan `led_sequence.py` tetap dijalankan sungguhan, sama seperti `auto_new.py`.
- Sensor home tetap memakai GPIO13 asli. Untuk menguji fallback sensor rusak, buat GPIO13 LOW sebelum menekan Start.
