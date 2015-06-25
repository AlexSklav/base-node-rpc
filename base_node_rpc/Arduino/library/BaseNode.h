#ifndef ___BASE_NODE__H___
#define ___BASE_NODE__H___

#include <stdint.h>
#include <avr/eeprom.h>
#include <SPI.h>
#include <utility/twi.h>
#include "Memory.h"
#include "Array.h"
#include "RPCBuffer.h"
#define BROADCAST_ADDRESS 0x00
#ifndef P
#define P(str) (strcpy_P(p_buffer_, PSTR(str)), p_buffer_)
#endif


#ifdef BASE_NODE__NAME
const char NAME_[] PROGMEM = BASE_NODE__NAME;
#else
const char NAME_[] PROGMEM = "";
#endif  // #ifdef BASE_NODE__NAME
#ifdef BASE_NODE__MANUFACTURER
const char MANUFACTURER_[] PROGMEM = BASE_NODE__MANUFACTURER;
#else
const char MANUFACTURER_[] PROGMEM = "";
#endif
#ifdef BASE_NODE__SOFTWARE_VERSION
const char SOFTWARE_VERSION_[] PROGMEM = BASE_NODE__SOFTWARE_VERSION;
#else
const char SOFTWARE_VERSION_[] PROGMEM = "";
#endif
#ifdef BASE_NODE__URL
const char URL_[] PROGMEM = BASE_NODE__URL;
#else
const char URL_[] PROGMEM = "";
#endif


/* Callback functions for slave device. */
extern void i2c_receive_event(int byte_count);
extern void i2c_request_event();


inline UInt8Array prog_string(const char* str, UInt8Array array) {
  strcpy_P((char *)array.data, str);
  array.length = strlen_P(str);
  return array;
}


class BaseNode {
public:
  static const uint16_t EEPROM__I2C_ADDRESS = 0x00;
  uint8_t i2c_address_;
  uint8_t output_buffer[128];

  BaseNode() {
    i2c_address_ = EEPROM.read(EEPROM__I2C_ADDRESS);
    Wire.begin(i2c_address_);
  }

  uint32_t microseconds() { return micros(); }
  uint32_t milliseconds() { return millis(); }
  void delay_us(uint16_t us) { if (us > 0) { delayMicroseconds(us); } }
  void delay_ms(uint16_t ms) { if (ms > 0) { delay(ms); } }
  uint32_t max_payload_size() {
    return (PACKET_SIZE
            - 3 * sizeof(uint8_t)  // Frame boundary
            - sizeof(uint16_t)  // UUID
            - sizeof(uint16_t));  // Payload length
  }
  uint32_t ram_free() { return free_memory(); }
  void pin_mode(uint8_t pin, uint8_t mode) { return pinMode(pin, mode); }
  uint8_t digital_read(uint8_t pin) const { return digitalRead(pin); }
  void digital_write(uint8_t pin, uint8_t value) { digitalWrite(pin, value); }
  uint16_t analog_read(uint8_t pin) const { return analogRead(pin); }
  void analog_write(uint8_t pin, uint8_t value) { return analogWrite(pin, value); }

  uint16_t array_length(UInt8Array array) { return array.length; }
  UInt32Array echo_array(UInt32Array array) { return array; }
  UInt8Array str_echo(UInt8Array msg) { return msg; }

  int i2c_address() const { return i2c_address_; }
  int set_i2c_address(uint8_t address) {
    i2c_address_ = address;
    Wire.begin(address);
    // Write the value to the appropriate byte of the EEPROM.
    // These values will remain there when the board is turned off.
    EEPROM.write(EEPROM__I2C_ADDRESS, i2c_address_);
    return address;
  }
  uint16_t i2c_buffer_size() { return TWI_BUFFER_LENGTH; }
  UInt8Array i2c_scan() {
    UInt8Array output = {sizeof(output_buffer), output_buffer};
    int count = 0;

    /* The I2C specification has reserved addresses in the ranges `0x1111XXX`
     * and `0x0000XXX`.  See [here][1] for more details.
     *
     * [1]: http://www.totalphase.com/support/articles/200349176-7-bit-8-bit-and-10-bit-I2C-Slave-Addressing */
    for (uint8_t i = 8; i < 120; i++) {
      if (count >= output.length) { break; }
      Wire.beginTransmission(i);
      if (Wire.endTransmission() == 0) {
        output.data[count++] = i;
        delay(1);  // maybe unneeded?
      }
    }
    output.length = count;
    return output;
  }
  int16_t i2c_available() { return Wire.available(); }
  int8_t i2c_read_byte() { return Wire.read(); }
  int8_t i2c_request_from(uint8_t address, uint8_t n_bytes_to_read) {
    return Wire.requestFrom(address, n_bytes_to_read);
  }
  UInt8Array i2c_read(uint8_t address, uint8_t n_bytes_to_read) {
    UInt8Array output = {sizeof(output_buffer), output_buffer};
    Wire.requestFrom(address, n_bytes_to_read);
    uint8_t n_bytes_read = 0;
    while (Wire.available()) {
      uint8_t value = Wire.read();
      if (n_bytes_read >= n_bytes_to_read) {
        break;
      }
      output.data[n_bytes_read++] = value;
    }
    output.length = n_bytes_read;
    return output;
  }
  void i2c_write(uint8_t address, UInt8Array data) {
    Wire.beginTransmission(address);
    Wire.write(data.data, data.length);
    Wire.endTransmission();
  }
  UInt8Array i2c_send_command(uint8_t address, UInt8Array payload) {
    i2c_write(address, payload);
    return i2c_command_read(address);
  }
  UInt8Array i2c_command_read(uint8_t address) {
    UInt8Array output = {sizeof(output_buffer), output_buffer};
    uint8_t i2c_count = 0;

    /* Request output size. */
    output.length = Wire.requestFrom(address, (uint8_t)1);
    i2c_count = Wire.read();

    /* Request actual output. */
    output.length = Wire.requestFrom(address, (uint8_t)i2c_count);

    // Slave may send less than requested
    for (int i = 0; i < i2c_count; i++) {
      // receive a byte as character
      output.data[i] = Wire.read();
    }
    return output;
  }

  void set_spi_bit_order(uint8_t order) {
#ifdef __SAM3X8E__
    SPI.setBitOrder((BitOrder)order);
#else
    SPI.setBitOrder(order);
#endif
  }
  void set_spi_clock_divider(uint8_t divider) { SPI.setClockDivider(divider); }
  void set_spi_data_mode(uint8_t mode) { SPI.setDataMode(mode); }
  uint8_t spi_transfer(uint8_t value) { return SPI.transfer(value); }

  void update_eeprom_block(uint16_t address, UInt8Array data) {
    eeprom_update_block((void*)data.data, (void*)address, data.length);
  }

  UInt8Array read_eeprom_block(uint16_t address, uint16_t n) {
    UInt8Array output = {sizeof(output_buffer), output_buffer};
    eeprom_read_block((void*)&output_buffer, (void*)address, n);
    output.length = n;
    return output;
  }

  UInt8Array name() {
    UInt8Array output = {sizeof(output_buffer), output_buffer};
    return prog_string(NAME_, output);
  }
  UInt8Array manufacturer() {
    UInt8Array output = {sizeof(output_buffer), output_buffer};
    return prog_string(MANUFACTURER_, output);
  }
  UInt8Array software_version() {
    UInt8Array output = {sizeof(output_buffer), output_buffer};
    return prog_string(SOFTWARE_VERSION_, output);
  }
  UInt8Array url() {
    UInt8Array output = {sizeof(output_buffer), output_buffer};
    return prog_string(URL_, output);
  }
};


#endif  // #ifndef ___BASE_NODE__H___
