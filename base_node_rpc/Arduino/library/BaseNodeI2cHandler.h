#ifndef ___BASE_NODE_I2C_HANDLER__H___
#define ___BASE_NODE_I2C_HANDLER__H___

#include "I2cHandler.h"
#include <Array.h>


class BaseNodeI2cHandler {
public:
  typedef base_node_rpc::i2c_handler_t handler_type;
  handler_type i2c_handler_;

  UInt8Array i2c_request(uint8_t address, UInt8Array data) {
    return i2c_handler_.request(address, data);
  }
};

#endif  // #ifndef ___BASE_NODE_I2C_HANDLER__H___
