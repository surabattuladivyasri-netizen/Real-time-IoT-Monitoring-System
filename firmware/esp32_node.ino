#include <Wire.h>
#include <SPI.h>
#include <Ethernet.h>
#include <PubSubClient.h>
#include <vector>

// ================================================================= //
// --- SECTION 1: CORE CONFIGURATION (Pins, Network, MQTT) ---
// ================================================================= //

// --- Input Pins (GPIO) ---
const int inputPins[8] = {32, 33, 25, 26, 27, 14, 13, 15};

// --- W5500 Ethernet Pins ---
#define W5500_CS_PIN    4
#define SPI_SCK_PIN     18
#define SPI_MISO_PIN    19
#define SPI_MOSI_PIN    23

// --- Network Settings ---
byte mac[] = {0xDE, 0xAD, 0xBE, 0xEF, 0xFE, 0x01}; // Unique per board
IPAddress ip(192, 168, 1, 177);
IPAddress gateway(192, 168, 1, 1);
IPAddress subnet(255, 255, 255, 0);
IPAddress mqttBroker(192, 168, 1, 51); // ← CHANGE to your MQTT broker
const int mqttPort = 1883;

// --- MQTT & Ethernet Clients ---
EthernetClient ethClient;
PubSubClient client(ethClient);
String clientID = "1";   // <<< FIXED client ID

// ================================================================= //
// --- SECTION 2: DYNAMIC I2C DISCOVERY & DATA STRUCTURES ---
// ================================================================= //

// Structure to store MUX hierarchy and sensor data
struct MuxNode {
  uint8_t address;
  std::vector<std::pair<uint8_t, std::vector<uint8_t>>> channelDevices; // Channel -> device addresses
};

// Discovered device topology
std::vector<MuxNode> detectedMuxes;
std::vector<uint8_t> directSensors;
uint8_t mcp23008Address = 0; // Address of the relay expander, 0 if not found

// --- Sensor Data ---
float tempValues[8];
float humValues[8]; // Store humidity, even if not in JSON
unsigned long lastSensorRead = 0;
const long SENSOR_INTERVAL = 2000; // Read every 2 seconds

// ================================================================= //
// --- SECTION 3: SENSOR READING & I2C HELPER FUNCTIONS ---
// ================================================================= //

// --- Generic I2C MUX and Scan Functions ---
bool selectChannel(uint8_t muxAddr, uint8_t channel) {
  if (channel > 7) return false;
  Wire.beginTransmission(muxAddr);
  Wire.write(1 << channel);
  return (Wire.endTransmission() == 0);
}

void deselectChannels(uint8_t muxAddr) {
  Wire.beginTransmission(muxAddr);
  Wire.write(0x00);
  Wire.endTransmission();
}

std::vector<uint8_t> scanBus() {
  std::vector<uint8_t> addresses;
  for (uint8_t addr = 1; addr < 127; addr++) {
    Wire.beginTransmission(addr);
    if (Wire.endTransmission() == 0) {
      addresses.push_back(addr);
    }
  }
  return addresses;
}

// --- Sensor Identification Functions (Try each protocol) ---
// Method 1: MCP9808 (temperature only)
bool tryMCP9808(uint8_t sensorAddr, float& temp) {
  Wire.beginTransmission(sensorAddr);
  Wire.write(0x05); // Temp register
  if (Wire.endTransmission() != 0) return false;
  if (Wire.requestFrom(sensorAddr, (uint8_t)2) != 2) return false;
  
  uint16_t raw = (Wire.read() << 8) | Wire.read();
  if (raw == 0xFFFF) return false; // Invalid reading
  
  temp = (raw & 0x0FFF) / 16.0;
  if (raw & 0x1000) temp -= 256.0; // Handle negative temps
  
  return (temp > -55.0 && temp < 125.0);
}

// Method 2: SHT3x (temperature and humidity)
bool trySHT3x(uint8_t sensorAddr, float& temp, float& hum) {
  Wire.beginTransmission(sensorAddr);
  Wire.write(0x24); Wire.write(0x00); // High repeatability measurement
  if (Wire.endTransmission() != 0) return false;
  delay(20);

  if (Wire.requestFrom(sensorAddr, (uint8_t)6) != 6) return false;
  uint16_t tempRaw = (Wire.read() << 8) | Wire.read();
  Wire.read(); // Skip CRC
  uint16_t humRaw = (Wire.read() << 8) | Wire.read();
  Wire.read(); // Skip CRC

  temp = -45.0 + (175.0 * tempRaw / 65535.0);
  hum = 100.0 * humRaw / 65535.0;

  return (temp > -40.0 && temp < 125.0 && hum >= 0.0 && hum <= 100.0);
}

// Method 3: AHT10/AHT20 (temperature and humidity)
bool tryAHT20(uint8_t sensorAddr, float& temp, float& hum) {
  Wire.beginTransmission(sensorAddr);
  Wire.write(0xAC); Wire.write(0x33); Wire.write(0x00);
  if (Wire.endTransmission() != 0) return false;
  delay(80);

  if (Wire.requestFrom(sensorAddr, (uint8_t)6) != 6) return false;
  uint8_t data[6];
  for (int i = 0; i < 6; i++) data[i] = Wire.read();
  if (data[0] & 0x80) return false; // Check busy bit

  uint32_t humRaw = ((uint32_t)data[1] << 12) | ((uint32_t)data[2] << 4) | (data[3] >> 4);
  hum = (humRaw * 100.0) / 1048576.0;
  uint32_t tempRaw = ((uint32_t)(data[3] & 0x0F) << 16) | ((uint32_t)data[4] << 8) | data[5];
  temp = ((tempRaw * 200.0) / 1048576.0) - 50.0;
  
  return (temp > -40.0 && temp < 85.0 && hum >= 0.0 && hum <= 100.0);
}

// --- Main Function to Read Any Supported Sensor ---
bool readSensorData(uint8_t sensorAddr, float& temp, float& hum) {
  hum = NAN; // Default: no humidity
  if (tryMCP9808(sensorAddr, temp)) return true;
  if (trySHT3x(sensorAddr, temp, hum)) return true;
  if (tryAHT20(sensorAddr, temp, hum)) return true;
  // Add other try...() functions here if more sensors are supported
  return false;
}

// ================================================================= //
// --- SECTION 4: RELAY CONTROL & MQTT ---
// ================================================================= //

void setupMCP23008() {
  if (mcp23008Address == 0) return; // Don't setup if not found
  Wire.beginTransmission(mcp23008Address);
  Wire.write(0x00); // IODIR register
  Wire.write(0x00); // All pins as outputs
  if (Wire.endTransmission() == 0) {
    Serial.println("MCP23008 configured successfully.");
  } else {
    Serial.println("Failed to configure MCP23008.");
  }
}

void setRelayOutputs(uint8_t mask) {
  if (mcp23008Address == 0) return; // Don't control if not found
  Wire.beginTransmission(mcp23008Address);
  Wire.write(0x0A); // OLAT register
  Wire.write(mask);
  Wire.endTransmission();
}

String buildJsonStatus() {
  String json = "{\"client_id\":\"" + clientID + "\",\"gpio\":[";
  for (int i = 0; i < 8; i++) {
    json += (digitalRead(inputPins[i]) == LOW) ? "1" : "0";
    if (i < 7) json += ",";
  }
  json += "],\"temps\":[";
  for (int i = 0; i < 8; i++) {
    if (isnan(tempValues[i])) {
      json += "null";
    } else {
      json += String(tempValues[i], 2);
    }
    if (i < 7) json += ",";
  }
  json += "],\"humd\":[";
  for (int i = 0; i < 8; i++) {
    if (isnan(humValues[i])) {
      json += "null";
    } else {
      json += String(humValues[i], 2);
    }
    if (i < 7) json += ",";
  }
  json += "],\"timestamp\":" + String(millis()) + "}";
  return json;
}

bool connectMQTT() {
  if (client.connected()) return true;
  Serial.print("INFO: Connecting to MQTT...");
  if (client.connect(clientID.c_str())) {
    Serial.println("connected");
    return true;
  } else {
    Serial.print("failed, rc=");
    Serial.println(client.state());
    delay(2000); // Wait before retrying
    return false;
  }
}

// ================================================================= //
// --- SECTION 5: SETUP ---
// ================================================================= //

void setup() {
  Serial.begin(115200);
  while (!Serial) delay(10);
  Serial.println("\n\n--- System Initializing ---");

  // --- I2C Init ---
  Wire.begin(21, 22);
  Wire.setClock(400000);

  // --- Configure Inputs ---
  for (int i = 0; i < 8; i++) {
    pinMode(inputPins[i], INPUT_PULLUP);
  }

  // --- Dynamic I2C Device Discovery ---
  Serial.println("Scanning main I2C bus...");
  std::vector<uint8_t> mainDevices = scanBus();

  if (mainDevices.empty()) {
    Serial.println("WARNING: No I2C devices found on the main bus!");
  } else {
    Serial.print("Found devices at: ");
    for (auto addr : mainDevices) {
      Serial.print("0x"); Serial.print(addr, HEX); Serial.print(" ");
    }
    Serial.println();

    // Identify MUXes, Sensors, and the Relay Expander
    for (auto addr : mainDevices) {
      // Check for MCP23008 (Relay Expander)
      if (addr >= 0x20 && addr <= 0x27) {
        Wire.beginTransmission(addr);
        Wire.write(0x09); // Try to read GPIO register
        if (Wire.endTransmission() == 0 && Wire.requestFrom(addr, (uint8_t)1) == 1) {
           Serial.print("Found MCP23008 Relay Expander at 0x"); Serial.println(addr, HEX);
           mcp23008Address = addr;
           continue; // Move to next device
        }
      }

      // Check if it's a MUX
      bool isMux = false;
      selectChannel(addr, 0);
      std::vector<uint8_t> devicesOnChannel = scanBus();
      deselectChannels(addr);
      // A device is a MUX if scanning a channel reveals devices not on the main bus
      if (devicesOnChannel.size() > mainDevices.size()) {
          isMux = true;
      }

      if (isMux) {
        Serial.print("Found MUX at 0x"); Serial.println(addr, HEX);
        MuxNode muxNode;
        muxNode.address = addr;
        // Scan each channel of the MUX
        for (uint8_t ch = 0; ch < 8; ch++) {
          selectChannel(addr, ch);
          std::vector<uint8_t> channelDevs = scanBus();
          std::vector<uint8_t> newSensors;
          // Filter out devices that are visible on the main bus
          for (auto saddr : channelDevs) {
              bool isNew = true;
              for (auto paddr : mainDevices) {
                  if (saddr == paddr) { isNew = false; break; }
              }
              if (isNew) newSensors.push_back(saddr);
          }
          if (!newSensors.empty()) {
              muxNode.channelDevices.push_back({ch, newSensors});
          }
          deselectChannels(addr);
        }
        detectedMuxes.push_back(muxNode);
      } else {
        // If not a MUX and not the relay expander, assume it's a direct sensor
        if (addr != mcp23008Address) {
            Serial.print("Found Direct Sensor at 0x"); Serial.println(addr, HEX);
            directSensors.push_back(addr);
        }
      }
    }
  }

  // --- Setup MCP23008 (Relays) ---
  setupMCP23008();
  setRelayOutputs(0x00); // All relays OFF

  // --- Ethernet Init ---
  SPI.begin(SPI_SCK_PIN, SPI_MISO_PIN, SPI_MOSI_PIN, W5500_CS_PIN);
  Ethernet.init(W5500_CS_PIN);
  Ethernet.begin(mac, ip, gateway, subnet);
  delay(1000);
  Serial.print("Ethernet IP Address: ");
  Serial.println(Ethernet.localIP());

  // --- MQTT Setup ---
  client.setServer(mqttBroker, mqttPort);

  Serial.println("--- System Ready. ClientID: " + clientID + " ---");
}


// ================================================================= //
// --- SECTION 6: LOOP ---
// ================================================================= //

void loop() {
  // --- Handle MQTT Connection ---
  if (!client.connected()) {
    connectMQTT();
  }
  client.loop();

  // --- FAST: Read inputs & control relays ---
  uint8_t relayMask = 0;
  for (int i = 0; i < 8; i++) {
    if (digitalRead(inputPins[i]) == LOW) { // LOW = active
      relayMask |= (1 << i);
    }
  }
  setRelayOutputs(relayMask);

  // --- NON-BLOCKING: Read sensors & publish periodically ---
  if (millis() - lastSensorRead >= SENSOR_INTERVAL) {
    lastSensorRead = millis();

    // Clear previous readings
    for (int i = 0; i < 8; i++) {
        tempValues[i] = NAN;
        humValues[i] = NAN;
    }

    // --- Populate sensor data based on discovered topology ---
    if (!detectedMuxes.empty()) {
      // Priority 1: Use the first detected MUX
      MuxNode& mux = detectedMuxes[0];
      for (const auto& ch_pair : mux.channelDevices) {
        uint8_t channel = ch_pair.first;
        if (channel < 8 && !ch_pair.second.empty()) {
          uint8_t sensorAddr = ch_pair.second[0]; // Use first sensor on the channel
          selectChannel(mux.address, channel);
          readSensorData(sensorAddr, tempValues[channel], humValues[channel]);
          deselectChannels(mux.address);
        }
      }
    } else if (!directSensors.empty()) {
      // Priority 2: Use the first detected direct sensor
      readSensorData(directSensors[0], tempValues[0], humValues[0]);
    }

    // --- Build and publish JSON ---
    String json = buildJsonStatus();
    if (client.connected()) {
      if(client.publish("device/status", json.c_str())) {
        Serial.println("MQTT Publish Success: " + json);
      } else {
        Serial.println("MQTT Publish FAILED");
      }
    } else {
      Serial.println("WARN: MQTT Not Connected. Payload not sent: " + json);
    }
  }
}