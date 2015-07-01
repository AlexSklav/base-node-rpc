#ifndef ___BASE_NODE_SERIAL_HANDLER__H___
#define ___BASE_NODE_SERIAL_HANDLER__H___

#include "SerialHandler.h"


class BaseNodeSerialHandler {
public:
  typedef base_node_rpc::serial_handler_t handler_type;
  handler_type serial_handler_;
};

#endif  // #ifndef ___BASE_NODE_SERIAL_HANDLER__H___

