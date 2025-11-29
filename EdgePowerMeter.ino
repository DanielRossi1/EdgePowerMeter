#include "INA226.h"
#include <Wire.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>
#include "RTClib.h"

#define SCREEN_WIDTH 128  // OLED display width, in pixels
#define SCREEN_HEIGHT 32  // OLED display height, in pixels

#define OLED_RESET -1        // Reset pin # (or -1 if sharing Arduino reset pin)
#define SCREEN_ADDRESS 0x3C  ///< See datasheet for Address; 0x3D for 128x64, 0x3C for 128x32

#define INA226_INTERVAL 10
#define DISPLAY_INTERVAL 10

Adafruit_SSD1306 display(SCREEN_WIDTH, SCREEN_HEIGHT, &Wire, OLED_RESET);
INA226 ina226(0x40);
RTC_DS3231 rtc;

float shunt = 0.010;               /* shunt (Shunt Resistance in Ohms) */
float current_LSB_mA = 0.100;      /* current_LSB_mA (Current Least Significant Bit in milli Amperes) */
float current_zero_offset_mA = 0;  /* current_zero_offset_mA (Current Zero Offset in milli Amperes) */
uint16_t bus_V_scaling_e4 = 10000; /* bus_V_scaling_e4 (Bus Voltage Scaling Factor) */

struct Measurement {
  String timestamp;
  float voltage;
  float current;
  float power;
};

Measurement measurement;

void displayPower(float powerW) {
  display.clearDisplay();
  display.setTextSize(2);
  display.setTextColor(SSD1306_WHITE);
  display.setCursor(0, 0);
  display.print(F("Power"));
  display.setTextSize(2);
  display.setCursor(0, 20);
  display.print(powerW, 2);
  display.print(F(" W"));
  display.display();
}

void displayText(String msg1, String msg2) {
  display.clearDisplay();
  display.setTextSize(2);
  display.setTextColor(SSD1306_WHITE);
  display.setCursor(0, 0);
  display.print(msg1);
  display.setTextSize(2);
  display.setCursor(0, 20);
  display.print(msg2);
  display.display();
}

void setup_INA226(void) {
  if (!ina226.begin()) {
    Serial.println("Failed to find INA226 chip");
    while (1)
      delay(100);
    displayText("INA226", "setup");
    Serial.print(".");
  } else {
    displayText("INA226", "found");
    Serial.println("");
    Serial.println("INA226 Found!");
  }

  if (ina226.configure(shunt, current_LSB_mA, current_zero_offset_mA, bus_V_scaling_e4))
    Serial.println("\n***** Configuration Error! Chosen values outside range *****\n");


  ina226.setModeShuntBusContinuous();
  ina226.setAverage(INA226_16_SAMPLES);
}

void setup_display(void) {
  // SSD1306_SWITCHCAPVCC = generate display voltage from 3.3V internally
  if (!display.begin(SSD1306_SWITCHCAPVCC, SCREEN_ADDRESS)) {
    Serial.println(F("SSD1306 allocation failed"));
    for (;;)
      ; // forever loop
  }
  display.display();

  delay(2000); 
  display.clearDisplay();

  display.drawPixel(10, 10, SSD1306_WHITE);
}

Measurement readMeasurement() {
  DateTime now = rtc.now();
  char buf[20];
  snprintf(buf, sizeof(buf), "%04d-%02d-%02d %02d:%02d:%02d",
           now.year(), now.month(), now.day(),
           now.hour(), now.minute(), now.second());

  delay(1);
  Measurement m;
  m.timestamp = String(buf);
  m.voltage = ina226.getBusVoltage();
  m.current = ina226.getCurrent();
  m.power = ina226.getPower();
  delay(1);
  return m;
}

void printMeasurementCSV(const Measurement &m) {
  // Timestamp, Voltage[V], Current[A], Power[W]
  Serial.print(m.timestamp);
  Serial.print(",");
  Serial.print(m.voltage, 3);
  Serial.print(",");
  Serial.print(m.current, 3);
  Serial.print(",");
  Serial.println(m.power, 3);
}

void plotMeasurement(const Measurement &m) {
  Serial.print(m.voltage, 3);
  Serial.print(" ");
  Serial.print(m.current, 3);
  Serial.print(" ");
  Serial.println(m.power, 3);
}

unsigned long ina226_update = 0;
unsigned long display_update = 0;

void setup() {
  Serial.begin(115200);
  rtc.begin();

  setup_display();
  displayText("Display", "ready!");
  delay(500);

  setup_INA226();
  delay(500);

  ina226_update = millis();
  display_update = millis();
  Serial.println("Power Meter ready!");
}


void loop() {
  if (millis() - ina226_update >= INA226_INTERVAL) {
    measurement = readMeasurement();
    ina226_update = millis();
  }

  if (millis() - display_update >= DISPLAY_INTERVAL) {
    displayPower(measurement.power);
    plotMeasurement(measurement);

    display_update = millis();
  }
}
