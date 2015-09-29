#ifndef ___BASE_NODE_CONFIG__H___
#define ___BASE_NODE_CONFIG__H___


#include <CArrayDefs.h>
#include <Wire.h>
#include <pb.h>


template <typename ConfigMessage, uint8_t Address=0>
class BaseNodeConfig {
public:
  typedef ConfigMessage message_type;
  ConfigMessage config_;

  BaseNodeConfig(const pb_field_t *fields) : config_(fields) {}

  void load_config() { config_.load(Address); }
  void save_config() { config_.save(Address); }
  void reset_config() { config_.reset(); }
  UInt8Array serialize_config() { return config_.serialize(); }
  uint8_t update_config(UInt8Array serialized) {
    return config_.update(serialized);
  }

  bool on_config_serial_number_changed(uint32_t new_value) {
    // Serial number must be greater than zero.
    return (new_value > 0);
  }

  bool on_config_baud_rate_changed(uint32_t new_value) {
    // Only certain baud rates are valid.
    switch (new_value) {
      case 300:
      case 600:
      case 1200:
      case 2400:
      case 4800:
      case 9600:
      case 14400:
      case 19200:
      case 28800:
      case 31250:
      case 38400:
      case 57600:
      case 115200:
        return true;
      default:
        return false;
    };
  }

  bool on_config_i2c_address_changed(uint32_t new_value) {
    // I2C addresses must be in the range 10-120, according to the
    // specification.
    if ((new_value > 0x07) && (new_value < 0x78)) {
#ifdef __AVR__
      Wire.begin(static_cast<uint8_t>(new_value));
#endif  // #ifdef __AVR__
      return true;
    } else {
      // Invalid i2c address was specified.
      return false;
    }
  }
};


#endif  // #ifndef ___BASE_NODE_CONFIG__H___
