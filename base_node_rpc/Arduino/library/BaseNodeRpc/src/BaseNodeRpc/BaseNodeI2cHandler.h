#ifndef ___BASE_NODE_I2C_HANDLER__H___
#define ___BASE_NODE_I2C_HANDLER__H___

#include "I2cHandler.h"
#include <CArrayDefs.h>


template <typename Handler>
class BaseNodeI2cHandler {
public:
  typedef Handler handler_type;
  handler_type i2c_handler_;

  uint32_t max_i2c_payload_size() {
    return (I2C_PACKET_SIZE
            - 3 * sizeof(uint8_t)  // Frame boundary
            - sizeof(uint16_t)  // UUID
            - sizeof(uint16_t));  // Payload length
  }

  UInt8Array i2c_request(uint8_t address, UInt8Array data) {
    return i2c_handler_.request(address, data);
  }

  void i2c_packet_reset() { i2c_handler_.packet_reset(); }
};

#endif  // #ifndef ___BASE_NODE_I2C_HANDLER__H___
