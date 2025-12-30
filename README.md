# 🌡️ Real-time Hygrothermal & Event Monitoring System

A sophisticated, multi-tier Industrial IoT (IIoT) platform designed for real-time environmental tracking and hardware event logging. The system features dynamic sensor discovery, automated alarm duration processing, and a professional web-based dashboard.

---

## 📺 System Demo
[Click here to watch the full system demonstration video](./docs/system_demo.mp4)

---
## 🌟 Key Features

- **Dynamic I2C Scanning:** The ESP32 node automatically scans the bus to identify and interface with sensors like the **MCP9808**, **SHT3x**, or **AHT20** without manual configuration.
- **Automatic Server Discovery:** Clients utilize **UDP broadcasting** to find the server's IP address on the local network automatically, eliminating the need for hardcoded IPs.
- **Intelligent Alarm Processing:** A background worker monitors database entries to calculate precise Start and End times for hardware events.
- **Data Portability:** Integrated feature to export historical readings and alarm logs to **Excel (.xlsx)** for professional reporting.

---

## 🏗️ System Architecture

The project is divided into four main layers to ensure scalability and reliability:

1.  **The Edge (ESP32):** Collects sensor data and handles local relay control via an MCP23008 for immediate hardware response.
2.  **The Gateway (NanoPi):** Monitors local GPIO pins using a high-performance C binary and sends data via HTTP POST.
3.  **The Middleware (Node-RED):** Hosts the MQTT broker (Aedes) and manages the logic for inserting raw data into the PostgreSQL database.
4.  **The Server (Flask):** Serves the web UI, manages client configurations via `config.json`, and performs background data analysis.

---

## 🛠️ Tech Stack

- **Firmware:** C++ (Arduino IDE)
- **Backend:** Python (Flask), SQLAlchemy, Pandas
- **Frontend:** HTML5, CSS3 (Jinja2 Templates), Plotly.js
- **Database:** PostgreSQL
- **Communication:** MQTT, UDP Broadcast, HTTP REST

---

## 🚀 Getting Started

### 1. Installation

Clone the repository and install the required Python packages:

````bash
pip install -r requirements.txt
````

### 2. Configuration

*   **Database:** Create a database named `iotdb` in PostgreSQL.
*   **Node-RED:** Import the `node_red_flows.json` file from the `/middleware` folder into your Node-RED instance.
*   **Server:** Ensure your database credentials in `app.py` match your local PostgreSQL setup.

### 3. Running the Project

Start the Flask dashboard:

```bash
python server/app.py
```
---


## 🔌 Hardware Setup

### ESP32
* **Ethernet**: Connect the **W5500 Ethernet module** (CS: Pin 4).
* **Sensors**: Connect **I2C sensors** (MCP9808, SHT3x, or AHT20) to Pins 21 (SDA) and 22 (SCL).
* **Relays**: Local relay control is managed via an **MCP23008** on the same I2C bus.

### NanoPi (Gateway)
* **Execution**: Ensure the `gpio3` binary exists in the `gateway` folder.
* **Permissions**: Run the following command to allow the Python script to execute the binary:
  ```bash
  chmod +x gateway/gpio3

## 🔌 Hardware Interface Setup (Gateway)

The **Gateway (NanoPi)** uses a high-performance C binary to monitor GPIO pins in real-time. This binary is called by the Python client to ensure minimal latency.

### 1. GPIO Monitoring Code (`gpio3.c`)
The following C code utilizes the `wiringPi` library to scan pins 0, 2, 3, 7, 12, 13, 14, 15, and 16.

### 2. Compilation Instructions
To generate the executable required by `nanopi_client.py`, run the following commands on your NanoPi:

```bash
# Navigate to the gateway folder
cd gateway

# Compile the C source code
gcc -o gpio3 gpio3.c -lwiringPi

# Grant execution permissions
chmod +x gpio3
```