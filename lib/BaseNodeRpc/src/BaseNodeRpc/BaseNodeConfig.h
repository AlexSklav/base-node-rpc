#ifndef ___BASE_NODE_CONFIG__H___
#define ___BASE_NODE_CONFIG__H___


#include <stdint.h>
#include <CArrayDefs.h>
#include <Wire.h>
#include <pb.h>


template <typename ConfigMessage, uint8_t Address=0>
class BaseNodeConfig {
public:
  typedef ConfigMessage message_type;
  ConfigMessage config_;

  BaseNodeConfig(const pb_msgdesc_t *fields) : config_(fields) {}

  void load_config() { config_.load(Address); }
  void save_config() { config_.save(Address); }
  void reset_config() { config_.reset(); }
  UInt8Array serialize_config() { return config_.serialize(); }
  uint8_t update_config(UInt8Array serialized) {
    return config_.update(serialized);
  }

  bool on_config_i2c_address_changed(uint32_t new_value) {
    // I2C addresses must be in the range 8-119, according to the
    // specification.
    if ((new_value > 0x07) && (new_value < 0x78)) {
      Wire.begin(static_cast<uint8_t>(new_value));
      return true;
    } else {
      // Invalid i2c address was specified. Start in master-only mode.
      Wire.begin();
      return false;
    }
  }

};


#endif  // #ifndef ___BASE_NODE_CONFIG__H___
