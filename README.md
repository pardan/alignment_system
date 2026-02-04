# STARS-105 Alignment System

An automated antenna alignment system for radio frequency (RF) communication, built for Raspberry Pi. This system provides automatic antenna positioning, signal strength monitoring, and web-based configuration interface.

## 🌟 Features

- **Automatic Antenna Alignment**: Serpentine scanning algorithm to find optimal signal position
- **RSSI Monitoring**: Real-time signal strength measurement via SNMP
- **LED Status Indicators**: Visual feedback system with 9 LEDs showing signal quality
- **Web-Based Configuration**: Flask web interface for system settings
- **Network Management**: DHCP/Static IP configuration support
- **Manual Control**: GPIO-based manual up/down movement controls
- **SNMP Filter Integration**: Dynamic frequency filter testing and optimization
- **System Monitoring**: CPU, RAM, and uptime tracking
- **Service Management**: Systemd integration for background services

## 📋 System Requirements

### Hardware
- Raspberry Pi (3B+ or 4 recommended)
- GPIO-compatible rotator/actuator for horizontal movement
- GPIO-compatible actuator for vertical movement
- 9-LED status indicator panel
- Push buttons for control (Start, Manual Up, Manual Down, Abort)
- Home sensor (limit switch)
- Radio device with SNMP support

### Software
- Raspberry Pi OS (Linux)
- Python 3.7+
- Flask web framework
- gpiod library for GPIO control
- SNMP tools (snmpwalk, snmpget, snmpset)
- Systemd service manager
- NetworkManager (nmcli)

## 🚀 Installation

### 1. Clone or Copy Files
```bash
# Navigate to project directory
cd /path/to/project
```

### 2. Install Python Dependencies
```bash
pip3 install flask gpiod
```

### 3. Install SNMP Tools
```bash
sudo apt-get update
sudo apt-get install snmp snmpd
```

### 4. Configure Systemd Services

Create service files for automatic startup:

**monitor.service** (for RSSI monitoring and LED control):
```ini
[Unit]
Description=RSSI Monitor Service
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/path/to/project
ExecStart=/usr/bin/python3 /path/to/project/monitor.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

**auto.service** (for automatic alignment):
```ini
[Unit]
Description=Auto Alignment Service
After=network.target monitor.service

[Service]
Type=simple
User=root
WorkingDirectory=/path/to/project
ExecStart=/usr/bin/python3 /path/to/project/auto_new.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

**web.service** (for web interface):
```ini
[Unit]
Description=Web Configuration Service
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/path/to/project
ExecStart=/usr/bin/python3 /path/to/project/app.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Install the services:
```bash
sudo cp monitor.service /etc/systemd/system/
sudo cp auto.service /etc/systemd/system/
sudo cp web.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable monitor.service
sudo systemctl enable auto.service
sudo systemctl enable web.service
```

### 5. Configure GPIO Permissions
Ensure the user running the services has GPIO access:
```bash
sudo usermod -a -G gpio $USER
```

## 🔧 Configuration

### Configuration File (config.json)

The system uses a JSON configuration file with the following parameters:

```json
{
  "target_rssi": -80,
  "IP_RADIO": "172.20.25.5",
  "SNMP_PORT": 161,
  "SNMP_COMMUNITY": "public",
  "OID_RSSI": "1.3.6.1.4.1.1807.113.2.11.1.2.1.1",
  "degrees_per_step": 6.0,
  "settle_sec": 5,
  "iteration_actuator": 3,
  "actuator_speed": 0.5,
  "max_try": 1,
  "360_in_sec": 68
}
```

### Configuration Parameters

| Parameter | Description | Default | Range |
|-----------|-------------|---------|-------|
| `target_rssi` | Target RSSI value (dBm) to achieve | -80 | -100 to -50 |
| `IP_RADIO` | Radio device IP address | 172.20.25.5 | Valid IP |
| `SNMP_PORT` | SNMP port number | 161 | 1-65535 |
| `SNMP_COMMUNITY` | SNMP community string | "public" | Any string |
| `OID_RSSI` | SNMP OID for RSSI reading | 1.3.6.1.4.1.1807.113.2.11.1.2.1.1 | Valid OID |
| `degrees_per_step` | Horizontal rotation per step (degrees) | 6.0 | 1.0-30.0 |
| `settle_sec` | Wait time after each step (seconds) | 5 | 1-30 |
| `iteration_actuator` | Vertical refinement iterations | 3 | 1-10 |
| `actuator_speed` | Vertical movement speed (seconds) | 0.5 | 0.1-5.0 |
| `max_try` | Maximum alignment attempts | 1 | 1-3 |
| `360_in_sec` | Time for full 360° rotation (seconds) | 68 | 30-120 |

### GPIO Pin Configuration

The system uses the following GPIO pins (gpiochip0):

| Function | Pin Number | Direction | Description |
|----------|------------|-----------|-------------|
| Horizontal Right | 21 | Output | Move rotator right |
| Horizontal Left | 20 | Output | Move rotator left |
| Vertical Up | 10 | Output | Move actuator up |
| Vertical Down | 9 | Output | Move actuator down |
| Start Button | 5 | Input | Start alignment (active LOW) |
| Home Sensor | 13 | Input | Home position sensor (active LOW) |
| Manual Up | 2 | Input | Manual up button (active LOW) |
| Manual Down | 3 | Input | Manual down button (active LOW) |
| Abort | 6 | Input | Abort signal (active LOW) |

### LED Pin Configuration

9 LEDs for signal strength indication:

| LED | Pin | Color | RSSI Range |
|-----|-----|-------|------------|
| 1 | 24 | Red | -94 dBm |
| 2 | 23 | Yellow | -93 to -82 dBm |
| 3 | 22 | Yellow | -81 to -80 dBm |
| 4 | 27 | Yellow | -79 to -78 dBm |
| 5 | 18 | Yellow | -77 to -76 dBm |
| 6 | 4 | Green | -75 to -74 dBm |
| 7 | 26 | Green | -73 to -72 dBm |
| 8 | 16 | Green | -71 to -70 dBm |
| 9 | 17 | Green | -69 to -1 dBm |

## 📖 Usage

### Starting the System

Start all services:
```bash
sudo systemctl start monitor.service
sudo systemctl start auto.service
sudo systemctl start web.service
```

Check service status:
```bash
sudo systemctl status monitor.service
sudo systemctl status auto.service
sudo systemctl status web.service
```

### Web Interface

Access the web interface at:
```
http://<raspberry-pi-ip>:5000
```

The web interface provides three main sections:

1. **Radio Settings**: Configure alignment parameters
2. **Network Settings**: Manage network configuration (DHCP/Static)
3. **System Status**: View real-time system information and RSSI

### Manual Controls

- **Start Button**: Triggers automatic alignment sequence
- **Manual Up/Down**: Manually control vertical actuator movement
- **Abort**: Stops current alignment operation

### Automatic Alignment Process

1. **Calibration Phase**:
   - Move actuator down for calibration
   - Rotate to home position (rightmost)
   - Move actuator up to starting position

2. **Serpentine Scan**:
   - Horizontal sweep with RSSI measurement
   - SNMP filter testing at each position
   - Vertical refinement at best position
   - Repeat until target RSSI or max attempts reached

3. **Completion**:
   - Return to best position found
   - Enable optimal SNMP filter
   - Resume monitoring mode

## 📁 Project Structure

```
.
├── app.py                      # Flask web application
├── auto_new.py                 # Main alignment control logic
├── monitor.py                  # RSSI monitoring and LED control
├── led_control.py              # LED control functions
├── led_sequence.py             # LED sequence for alignment
├── gpio_control_sequence.py    # GPIO control utilities
├── snmp_filter_integration.py  # SNMP filter management
├── config.json                 # System configuration
├── templates/
│   └── index.html             # Web interface template
├── static/
│   └── tailwind.js            # Tailwind CSS framework
└── README.md                  # This file
```

## 🔍 Component Descriptions

### app.py
Flask web application providing REST API and web interface:
- `/api/config` - Radio configuration (GET/POST)
- `/api/network` - Network configuration (GET/POST)
- `/api/rssi` - Current RSSI value (GET)
- `/api/system` - System status (GET)
- `/` - Web interface

### auto_new.py
Main alignment control system:
- GPIO control for rotator and actuator
- Serpentine scanning algorithm
- SNMP filter integration
- Manual button handling
- Service management

### monitor.py
RSSI monitoring service:
- Continuous RSSI polling via SNMP
- LED status indication
- Blinking patterns for connection status

### snmp_filter_integration.py
SNMP filter management:
- Discover and test frequency filters
- Enable optimal filter based on RSSI
- Calibration mode support

### led_control.py & led_sequence.py
LED control modules:
- Signal strength visualization
- Alignment sequence indication
- Status feedback

## 🛠️ Troubleshooting

### Services Not Starting
```bash
# Check service logs
sudo journalctl -u monitor.service -f
sudo journalctl -u auto.service -f
sudo journalctl -u web.service -f
```

### GPIO Access Issues
```bash
# Check GPIO permissions
ls -l /dev/gpiochip0

# Add user to gpio group
sudo usermod -a -G gpio $USER
```

### SNMP Connection Issues
```bash
# Test SNMP connection
snmpget -v 2c -c public <radio-ip>:161 <OID>

# Check SNMP service status
sudo systemctl status snmpd
```

### Web Interface Not Accessible
```bash
# Check if Flask is running
sudo systemctl status web.service

# Check firewall
sudo ufw status
sudo ufw allow 5000
```

### RSSI Always Returns -1
- Check radio device connectivity
- Verify SNMP OID is correct
- Ensure radio device is powered on
- Check network connectivity to radio

## 🔒 Security Considerations

- Change default SNMP community string from "public"
- Use secure authentication for web interface in production
- Restrict web interface access to trusted networks
- Regularly update system packages
- Monitor service logs for suspicious activity

## 📝 License

© 2025 Starcom. All rights reserved.

## 🤝 Support

For technical support or questions, please contact the development team.

---

**Version**: 1.0.0  
**Last Updated**: 2025-02-04  
**Platform**: Raspberry Pi OS  
**Python Version**: 3.7+
