#ifndef ___BASE_NODE_SERIAL_HANDLER__H___
#define ___BASE_NODE_SERIAL_HANDLER__H___

#include "SerialHandler.h"


class BaseNodeSerialHandler {
public:
  typedef base_node_rpc::serial_handler_t handler_type;
  handler_type serial_handler_;

  uint32_t max_serial_payload_size() {
    return (PACKET_SIZE
            - 3 * sizeof(uint8_t)  // Frame boundary
            - sizeof(uint16_t)  // UUID
            - sizeof(uint16_t));  // Payload length
  }
};

#endif  // #ifndef ___BASE_NODE_SERIAL_HANDLER__H___

