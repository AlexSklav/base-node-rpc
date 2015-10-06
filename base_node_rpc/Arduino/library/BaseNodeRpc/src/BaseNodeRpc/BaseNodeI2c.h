#ifndef ___BASE_NODE_I2C__H___
#define ___BASE_NODE_I2C__H___


#include <Wire.h>
#include "CArrayDefs.h"
#include "BaseBuffer.h"

#define BROADCAST_ADDRESS 0x00


/* Callback functions for slave device. */
extern void i2c_receive_event(int byte_count);
extern void i2c_request_event();


class BaseNodeI2c : public BufferIFace {
public:
  void set_i2c_address(uint8_t address) { Wire.begin(address); }
  uint8_t i2c_address() { return (TWAR & 0x0FE) >> 1; }
  uint16_t i2c_buffer_size() { return TWI_BUFFER_LENGTH; }
  UInt8Array i2c_scan() {
    UInt8Array output = get_buffer();
    uint16_t count = 0;

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
    UInt8Array output = get_buffer();
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
  void i2c_enable_broadcast() { TWAR |= 0x01; }
  void i2c_disable_broadcast() { TWAR &= ~0x01; }
};
#endif  // #ifndef ___BASE_NODE_I2C__H___
