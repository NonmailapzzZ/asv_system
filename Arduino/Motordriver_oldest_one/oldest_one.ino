/*
 * motor_controller.ino
 * Arduino MEGA 2560 #1 — คุมมอเตอร์
 * หน้าที่: รับสัญญาณจาก Pixhawk → ขับ BTS7960
 *
 * การเชื่อมต่อ:
 *   Pixhawk OUT L → Pin 20
 *   Pixhawk OUT R → Pin 21
 *   BTS7960 L PWM → Pin 5
 *   BTS7960 R PWM → Pin 4
 */

// --- Pins รับสัญญาณจาก Pixhawk ---
const int pixInL = 20;
const int pixInR = 21;

// --- Pins ขับ BTS7960 ---
const int pwmL = 5;
const int pwmR = 4;

// --- ค่าชดเชยมอเตอร์ขวา (ปรับค่านี้จนมอเตอร์สองข้างเท่ากัน) ---
const int RIGHT_PWM_OFFSET = 20;

// --- ตัวแปรอ่านค่าจาก Pixhawk ---
volatile long pixL_Value = 1500;
volatile long pixR_Value = 1500;
volatile long pixL_Start = 0;
volatile long pixR_Start = 0;

// ===================================================

void setup() {
  Serial.begin(115200);

  // Pixhawk inputs
  pinMode(pixInL, INPUT_PULLUP);
  pinMode(pixInR, INPUT_PULLUP);
  attachInterrupt(digitalPinToInterrupt(pixInL), calcPixL, CHANGE);
  attachInterrupt(digitalPinToInterrupt(pixInR), calcPixR, CHANGE);

  // Motor outputs
  pinMode(pwmL, OUTPUT);
  pinMode(pwmR, OUTPUT);
}

void loop() {
  long currentL, currentR;

  noInterrupts();
  currentL = pixL_Value;
  currentR = pixR_Value;
  interrupts();

  // อัปเดตมอเตอร์ทุก 20ms (ไม่ใช้ delay)
  static unsigned long lastMotorUpdate = 0;
  if (millis() - lastMotorUpdate >= 20) {
    lastMotorUpdate = millis();
    driveForward(currentL, pwmL, true);
    driveForward(currentR, pwmR, false);
  }
}

// --- ขับมอเตอร์ ---
void driveForward(long pixhawkUs, int pin, bool isLeft) {
  int pwmOut = 0;
  if (pixhawkUs > 1550) {
    pwmOut = map(pixhawkUs, 1550, 2000, 75, 160);
    if (!isLeft) pwmOut += RIGHT_PWM_OFFSET;
    pwmOut = constrain(pwmOut, 75, 160);
  }
  analogWrite(pin, pwmOut);
}

// --- Interrupts: Pixhawk ---
void calcPixL() {
  if (digitalRead(pixInL) == HIGH) pixL_Start = micros();
  else if (pixL_Start > 0) pixL_Value = micros() - pixL_Start;
}
void calcPixR() {
  if (digitalRead(pixInR) == HIGH) pixR_Start = micros();
  else if (pixR_Start > 0) pixR_Value = micros() - pixR_Start;
}
