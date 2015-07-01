#ifndef ___BASE_NODE_CONFIG__H___
#define ___BASE_NODE_CONFIG__H___


#include <Array.h>
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
};


#endif  // #ifndef ___BASE_NODE_CONFIG__H___
