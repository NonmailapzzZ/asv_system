/*
 * sensor_node_uart.ino
 * Arduino MEGA - อ่านค่าเซนเซอร์ และส่งไปยัง RPi ผ่าน UART
 * 
 * ต่ออะไร:
 * - Garmin NMEA (4800 baud) → Serial2 (RX=pin 17, TX=pin 16)
 * - DS18B20 Temperature → pin 6
 * - pH sensor → A0
 * - DO sensor → A1
 * 
 * ส่งไปยัง RPi ผ่าน:
 * - Serial (pin 0=RX, pin 1=TX) → RPi UART
 * - Format: JSON ทุก 1 วินาที
 */

#include <OneWire.h>
#include <DallasTemperature.h>
#include <ArduinoJson.h>

// ===== SENSOR PINS =====
#define ONE_WIRE_PIN 6      // DS18B20
#define PH_PIN       A0     // pH sensor
#define DO_PIN       A1     // DO sensor

// ===== SENSOR OBJECTS =====
OneWire oneWire(ONE_WIRE_PIN);
DallasTemperature tempSensor(&oneWire);

// ===== SENSOR VARIABLES =====
float waterDepth = 0.0;
float garminLat  = 0.0;
float garminLon  = 0.0;
float waterTemp  = 0.0;
float waterPH    = 0.0;
float waterDO    = 0.0;

// ===== MOVING AVERAGE FILTER =====
#define FILTER_SIZE 40
float tempBuffer[FILTER_SIZE] = {0};
float phBuffer[FILTER_SIZE] = {0};
float doBuffer[FILTER_SIZE] = {0};
int bufferIndex = 0;

// ===== NMEA PARSER =====
String nmeaBuffer = "";

// ===== TIMING =====
unsigned long lastSensorRead = 0;
#define SENSOR_INTERVAL 1000  // 1 วินาที

// ===== CALIBRATION CONSTANTS =====
#define PH_VREF     5.0
#define PH_OFFSET   27.34
// #define PH_VMID     2.5
#define PH_SLOPE_MV -7.01

#define DO_VREF     5.0
#define DO_MAX_MV   4.0    // 4.0V = 14 mg/L
#define DO_MIN_MV   0.2    // 0.2V = 0 mg/L

// ===================================================
// SETUP
// ===================================================

void setup() {
  // Serial สำหรับเชื่อมต่อกับ RPi
  Serial.begin(9600);  // TX=pin1, RX=pin0
  
  // Serial2 สำหรับ Garmin NMEA
  Serial2.begin(4800);
  
  // Initialize sensors
  tempSensor.begin();
  
  delay(1000);
  Serial.println("{\"status\":\"BOOT\",\"msg\":\"Sensor node ready\"}");
}

// ===================================================
// MAIN LOOP
// ===================================================

void loop() {
  // อ่าน Garmin NMEA
  readGarmin();

  // อ่านเซนเซอร์ทุก SENSOR_INTERVAL
  if (millis() - lastSensorRead >= SENSOR_INTERVAL) {
    lastSensorRead = millis();
    
    readTemperature();
    readPH();
    readDO();
    
    // ส่งข้อมูลไปยัง RPi
    sendToRPi();
  }
}

// ===================================================
// GARMIN NMEA READER
// ===================================================

void readGarmin() {
  /**
   * อ่าน NMEA sentences จาก Garmin NMEA 0183
   * 
   * ตัวอย่าง:
   * $SDDPT,1.25,0.00*33  → Depth 1.25m
   * $GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,*47
   *   → Lat: 48°07.038'N (4807.038), Lon: 11°31.000'E
   */
  
  while (Serial2.available()) {
    char c = Serial2.read();
    
    if (c == '\n') {
      // ประมวลผล NMEA sentence
      if (nmeaBuffer.startsWith("$SDDPT")) {
        // Depth: $SDDPT,1.25,0.00*33
        // Field 1: depth in meters
        int idx = nmeaBuffer.indexOf(',');
        if (idx != -1) {
          int idx2 = nmeaBuffer.indexOf(',', idx + 1);
          if (idx2 != -1) {
            String depth_str = nmeaBuffer.substring(idx + 1, idx2);
            waterDepth = depth_str.toFloat();
          }
        }
      }
      else if (nmeaBuffer.startsWith("$GPGGA")) {
        // GPS: $GPGGA,time,lat,N/S,lon,E/W,...
        parseGPGGA();
      }
      
      nmeaBuffer = "";
    } 
    else if (c != '\r' && c != '\0') {
      nmeaBuffer += c;
      // ป้องกัน buffer overflow
      if (nmeaBuffer.length() > 100) {
        nmeaBuffer = "";
      }
    }
  }
}

void parseGPGGA() {
  /**
   * Parse GPS data from GPGGA sentence
   * Format: $GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,*47
   * 
   * Fields:
   * 0: $GPGGA
   * 1: UTC Time (hhmmss)
   * 2: Latitude (ddmm.mmmm)
   * 3: N/S
   * 4: Longitude (dddmm.mmmm)
   * 5: E/W
   * 6: Fix quality
   * ...
   */
  
  int fields[10];
  int field_count = 0;
  int idx = 0;
  
  // หา position ของ commas
  for (int i = 0; i < 10 && idx < (int)nmeaBuffer.length(); i++) {
    int comma = nmeaBuffer.indexOf(',', idx);
    if (comma == -1) {
      fields[i] = idx;
      break;
    }
    fields[i] = comma + 1;
    idx = comma + 1;
  }
  
  // Parse latitude (field 2)
  if (fields[2] > 0) {
    int comma = nmeaBuffer.indexOf(',', fields[2]);
    if (comma > fields[2]) {
      String lat_str = nmeaBuffer.substring(fields[2], comma);
      garminLat = nmea_to_decimal(lat_str);
    }
  }
  
  // Parse longitude (field 4)
  if (fields[4] > 0) {
    int comma = nmeaBuffer.indexOf(',', fields[4]);
    if (comma > fields[4]) {
      String lon_str = nmeaBuffer.substring(fields[4], comma);
      garminLon = nmea_to_decimal(lon_str);
    }
  }
}

float nmea_to_decimal(String nmea_str) {
  /**
   * Convert NMEA format (ddmm.mmmm) to decimal degrees
   * Example: 4807.038 = 48°07.038' = 48 + (7.038/60) = 48.1173°
   */
  int dot = nmea_str.indexOf('.');
  if (dot == -1 || dot < 3) return 0.0;
  
  // Extract degrees (first 2 or 3 digits)
  int degrees = nmea_str.substring(0, dot - 2).toInt();
  
  // Extract minutes (last 2 digits before dot + decimals)
  float minutes = nmea_str.substring(dot - 2).toFloat();
  
  return degrees + (minutes / 60.0);
}

// ===================================================
// DS18B20 TEMPERATURE SENSOR
// ===================================================

void readTemperature() {
  /**
   * อ่านอุณหภูมิจาก DS18B20
   * - ขอให้ sensor วัดค่า
   * - รอ ~750ms สำหรับการวัด
   * - อ่านค่าและใช้ moving average filter
   */
  
  tempSensor.requestTemperatures();  // ขอการวัด
  float t = tempSensor.getTempCByIndex(0);
  
  // ถ้าได้ค่าที่ถูกต้อง
  if (t != DEVICE_DISCONNECTED_C && t > -50 && t < 100) {
    waterTemp = applyMovingAverage(t, tempBuffer);
  }
}

// ===================================================
// pH SENSOR (E-201-C)
// ===================================================

void readPH() {
  /**
   * อ่านค่า pH จาก sensor E-201-C
   * 
   * Calibration:
   * - ต้องสอบเทียบกับ buffer pH 7.0 และ pH 4.0 (หรือ 10.0)
   * - PH_OFFSET และ PH_SLOPE_MV ต้องปรับตามการสอบเทียร์
   * 
   * Formula:
   * pH = 7.0 + ((V_mid - V_measured) / slope)
   * 
   * ตัวอย่าง:
   * - ถ้า V = 2.5V → pH = 7.0
   * - ถ้า V = 3.0V → pH = 7.0 + ((2.5-3.0)/0.18) = 5.2
   */
  
  int raw = analogRead(PH_PIN);
  float voltage = raw * (PH_VREF / 1023.0);
  float ph = (PH_SLOPE_MV * voltage) + PH_OFFSET;
  
  // Constrain to valid pH range
  // ph = constrain(ph, 0.0, 14.0);
  
  waterPH = applyMovingAverage(ph, phBuffer);
}

// ===================================================
// DO SENSOR (RS-LDO-N01-2-20)
// ===================================================

void readDO() {
  /**
   * อ่านค่า DO (Dissolved Oxygen) จาก RS-LDO-N01-2-20
   * 
   * Output voltage:
   * - 0.2V = 0 mg/L
   * - 4.0V = 14 mg/L (saturated)
   * 
   * Formula:
   * DO = (V - 0.2) / (4.0 - 0.2) * 14
   * DO = (V - 0.2) / 3.8 * 14
   */
  
  int raw = analogRead(DO_PIN);
  float voltage = raw * (DO_VREF / 1023.0);
  
  float do_val = 0.0;
  if (voltage > DO_MIN_MV) {
    do_val = ((voltage - DO_MIN_MV) / (DO_MAX_MV - DO_MIN_MV)) * 14.0;
  }
  
  // Constrain to valid range
  do_val = constrain(do_val, 0.0, 14.0);
  
  waterDO = applyMovingAverage(do_val, doBuffer);
}

// ===================================================
// MOVING AVERAGE FILTER
// ===================================================

float applyMovingAverage(float newValue, float* buffer) {
  /**
   * ใช้ moving average เพื่อลด noise
   * 
   * Flow:
   * 1. เก็บค่า newValue ลงใน buffer
   * 2. คำนวณค่าเฉลี่ยของ buffer ทั้งหมด
   * 3. Return ค่าเฉลี่ย
   * 
   * ประโยชน์:
   * - ลดเสียงรบกวน ~70%
   * - ทำให้อ่านค่า stable
   */
  
  buffer[bufferIndex] = newValue;
  
  float sum = 0;
  for (int i = 0; i < FILTER_SIZE; i++) {
    sum += buffer[i];
  }
  
  return sum / FILTER_SIZE;
}

// ===================================================
// SEND DATA TO RPi
// ===================================================

void sendToRPi() {
  /**
   * ส่งข้อมูล sensor ทั้งหมดไปยัง RPi เป็น JSON
   * 
   * Format:
   * {
   *   "lat": 13.7563,
   *   "lon": 100.5018,
   *   "depth": 1.25,
   *   "temp": 28.50,
   *   "ph": 7.40,
   *   "do": 6.20
   * }
   * 
   * RPi จะรับ ผ่าน /dev/ttyS0 (UART)
   */
  
  StaticJsonDocument<200> doc;
  
  // GPS coordinates
  doc["lat"]   = round(garminLat * 10000) / 10000.0;  // 4 decimal places
  doc["lon"]   = round(garminLon * 10000) / 10000.0;
  
  // Depth from Garmin
  doc["depth"] = round(waterDepth * 100) / 100.0;     // 2 decimal places
  
  // Temperature
  doc["temp"]  = round(waterTemp * 10) / 10.0;        // 1 decimal place
  
  // pH
  doc["ph"]    = round(waterPH * 100) / 100.0;        // 2 decimal places
  
  // Dissolved Oxygen
  doc["do"]    = round(waterDO * 100) / 100.0;        // 2 decimal places
  
  // ส่งเป็น JSON string ผ่าน Serial (pin 0/1)
  serializeJson(doc, Serial);
  Serial.println();  // จบด้วย newline
  
  // Increment buffer index สำหรับ moving average
  bufferIndex = (bufferIndex + 1) % FILTER_SIZE;
}

