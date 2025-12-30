import subprocess
import re
import smbus2
import time
import requests
import json
import threading
import queue
import socket
import sys

# ========= CONFIGURATION =========
# You will only need to edit the variables in this section.

# The I2C bus number your sensors are connected to. Usually 0 or 1 on a Raspberry Pi.
I2C_BUS = 0

# 🔴 ACTION REQUIRED: Set a permanent, unique ID for this client.
# This ID should match one of the keys in the `CLIENT_GPIO_PINS` dictionary on the server.
CLIENT_ID = "pi-lab"

# The path to the compiled C program that reads GPIO states.
# Ensure this file is in the same directory and is executable (`chmod +x gpio3`).
GPIO_EXECUTABLE = "./gpio3"

# How often (in seconds) the client sends data to the server.
SEND_INTERVAL = 0.1

# --- Server Discovery Configuration ---
BROADCAST_PORT = 9999
BROADCAST_MESSAGE = b'IOT_SERVER_DISCOVERY'
SERVER_UPDATE_ENDPOINT = "/update"

# --- Advanced Configuration (Usually no changes needed below this line) ---
TEMPERATURE_SENSOR_ADDRESSES = {
    0x18: "MCP9808" # Only supports MCP9808 temperature sensors
}
MUX_ADDRESS_RANGE = range(0x70, 0x78) # Standard I2C MUX addresses


# ========= UTILITIES =========
def get_local_ip():
    """Finds the client's local IP address to send to the server."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # Doesn't have to be reachable
        s.connect(("10.255.255.255", 1))
        IP = s.getsockname()[0]
    except Exception:
        IP = "N/A"
    finally:
        s.close()
    return IP

def find_server():
    """
    Sends a UDP broadcast message to find the server on the local network.
    Returns the server's IP and port as a tuple (ip, port).
    """
    print("[DISCOVERY] Searching for server...")
    server_address = None
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.settimeout(3) # Timeout for receiving a response
        
        # Send broadcast message
        sock.sendto(BROADCAST_MESSAGE, ('<broadcast>', BROADCAST_PORT))
        
        try:
            # Listen for a response
            data, addr = sock.recvfrom(1024)
            if data == b'DISCOVERY_ACK':
                server_address = (addr[0], 5000) # Assume default port 5000 from server
                print(f"[DISCOVERY] Server found at {server_address[0]}")
                return server_address
        except socket.timeout:
            print("[DISCOVERY] Server discovery failed.")
            return None
        except Exception as e:
            print(f"[DISCOVERY] An error occurred: {e}")
            return None

# ========= I2C HELPERS (Simplified) =========
def get_i2c_addresses():
    """Scans the I2C bus and returns a list of detected device addresses."""
    try:
        result = subprocess.run(['i2cdetect', '-y', str(I2C_BUS)], capture_output=True, text=True, check=True)
        addresses = []
        for line in result.stdout.splitlines():
            if re.match(r'^\s*[0-7][0-9a-fA-F]:\s*', line):
                parts = line.split(':')[1].strip().split()
                for addr in parts:
                    if addr != '--':
                        addresses.append(int(addr, 16))
        return addresses
    except Exception as e:
        print(f"[I2C] Detection error: {e}")
        return []

def read_mcp9808_temperature(bus, address):
    """Reads temperature from an MCP9808 sensor."""
    try:
        # Read the 16-bit temperature register
        raw = bus.read_word_data(address, 0x05)
        # Swap the bytes
        raw = ((raw & 0xFF) << 8) | (raw >> 8)
        # Process the temperature data according to the MCP9808 datasheet
        temp = (raw & 0x0FFF) / 16.0
        if raw & 0x1000: # Check for negative temperature
            temp -= 256
        return temp
    except Exception as e:
        print(f"[MCP9808] Read failed @ 0x{address:02X}: {e}")
        return None

def read_temperature(bus, address):
    """Generic function to read temperature based on sensor type."""
    if address in TEMPERATURE_SENSOR_ADDRESSES:
        return read_mcp9808_temperature(bus, address)
    return None

# ========= GPIO MONITOR (Updated to send 1/0) =========
def collect_gpio_statuses(gpio_queue):
    """Runs the C executable to monitor GPIO states and puts results in a queue."""
    try:
        process = subprocess.Popen(
            ['sudo', GPIO_EXECUTABLE],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1
        )
        print("[GPIO] Started GPIO monitoring executable")

        while True:
            line = process.stdout.readline().strip()
            if not line and process.poll() is not None:
                print("[GPIO] Executable terminated unexpectedly")
                break

            if line.startswith("GPIO"):
                statuses = []
                pins = []
                # Regex to find all instances of "GPIO <pin>: <STATE>" in a line
                matches = re.findall(r'GPIO (\d+): (HIGH|LOW)', line)
                for pin, state in matches:
                    # Convert the text state "HIGH" or "LOW" to a number (1 or 0)
                    status = 1 if state == "HIGH" else 0
                    pins.append(int(pin))
                    statuses.append(status)

                if statuses:
                    # Clear the queue to ensure only the latest data is present
                    while not gpio_queue.empty():
                        try:
                            gpio_queue.get_nowait()
                        except queue.Empty:
                            break
                    gpio_queue.put({'pins': pins, 'statuses': statuses})
    except Exception as e:
        print(f"[GPIO] Error running executable: {e}")
        # Put an empty list to signal an error state
        gpio_queue.put({'pins': [], 'statuses': []})

# ========= DATA AGGREGATION AND SENDING =========
def collect_all_temperature_data():
    """Scans all I2C devices, including those behind a MUX, and reads temperature."""
    data = {"i2c_devices": []}
    try:
        bus = smbus2.SMBus(I2C_BUS)
        all_addresses = get_i2c_addresses()
        mux_addresses = [addr for addr in all_addresses if addr in MUX_ADDRESS_RANGE]

        if mux_addresses:
            # Handle sensors connected via a TCA9548A MUX
            for mux_addr in mux_addresses:
                print(f"[MUX] Found at 0x{mux_addr:02X}")
                for channel in range(8): # Iterate through all 8 channels of the MUX
                    try:
                        bus.write_byte(mux_addr, 1 << channel) # Select channel
                        time.sleep(0.05)
                        # Check only for the MCP9808 address on the selected channel
                        if 0x18 in get_i2c_addresses():
                             temp = read_temperature(bus, 0x18)
                             if temp is not None:
                                 data["i2c_devices"].append({
                                     "channel": channel,
                                     "temperature": round(temp, 2)
                                 })
                    except Exception as e:
                        print(f"[MUX] Channel {channel} scan error: {e}")
                bus.write_byte(mux_addr, 0) # Deselect all channels
        else:
            # Handle directly connected sensors
            for addr in all_addresses:
                if addr in TEMPERATURE_SENSOR_ADDRESSES:
                    temp = read_temperature(bus, addr)
                    if temp is not None:
                        data["i2c_devices"].append({
                            "temperature": round(temp, 2)
                        })
        bus.close()
    except Exception as e:
        print(f"[I2C] Scan failed: {e}")
    return data

# ========= MAIN LOOP =========
def main():
    print("[SYSTEM] Starting Temperature and GPIO Monitor")
    gpio_queue = queue.Queue(maxsize=1)
    gpio_thread = threading.Thread(target=collect_gpio_statuses, args=(gpio_queue,), daemon=True)
    gpio_thread.start()

    server_address = None
    gpio_data = {'pins': [], 'statuses': []}
    discovery_count = 0

    while True:
        # Check if we need to search for the server
        if server_address is None and discovery_count % 30 == 0:
            server_address = find_server()
        discovery_count += 1
        
        # Watchdog to restart the GPIO thread if it dies
        if not gpio_thread.is_alive():
            print("[WATCHDOG] GPIO thread died. Restarting...")
            gpio_thread = threading.Thread(target=collect_gpio_statuses, args=(gpio_queue,), daemon=True)
            gpio_thread.start()

        # Collect data from sensors
        sensor_data = collect_all_temperature_data()
        time.sleep(0.05) # Small delay

        # Get the latest GPIO data from the queue
        try:
            gpio_data = gpio_queue.get_nowait()
        except queue.Empty:
            pass # It's okay if there's no new data yet

        # Assemble the final payload to send to the server
        payload = {
            "client_id": CLIENT_ID,
            "client_ip": get_local_ip(),
            "i2c_devices": sensor_data["i2c_devices"],
            "gpio_statuses": gpio_data['statuses'],
            "gpio_pins": gpio_data['pins']
        }

        print("[DATA] Sending to server:")
        # Print the payload without the "gpio_pins" key for a cleaner output
        payload_to_print = payload.copy()
        payload_to_print.pop('gpio_pins', None)
        print(json.dumps(payload_to_print, indent=2))
        
        # Only try to send data if a server address has been found
        if server_address:
            SERVER_URL = f"http://{server_address[0]}:{server_address[1]}{SERVER_UPDATE_ENDPOINT}"
            try:
                # Send the data as a JSON POST request
                res = requests.post(SERVER_URL, json=payload, timeout=5)
                print(f"[SERVER] Response: {res.status_code}")
            except Exception as e:
                print(f"[ERROR] Failed to send data to {SERVER_URL}: {e}")
                server_address = None # Reset server address on failure to trigger re-discovery

        # Wait before the next cycle
        time.sleep(SEND_INTERVAL)

if __name__ == "__main__":
    main()