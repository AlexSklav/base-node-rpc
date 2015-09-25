#ifndef ___NODE__H___
#define ___NODE__H___

#include <BaseNodeRpc/BaseNodeRpc.h>
#include <BaseNodeRpc/BaseNodeEeprom.h>
#include <BaseNodeRpc/BaseNodeI2c.h>
#include <BaseNodeRpc/BaseNodeSpi.h>
#include <BaseNodeRpc/BaseNodeSerialHandler.h>
#include <BaseNodeRpc/BaseNodeI2cHandler.h>
#include <BaseNodeRpc/I2cHandler.h>
#include <BaseNodeRpc/SerialHandler.h>

namespace base_node_rpc {

class Node :
  public BaseNode, public BaseNodeEeprom, public BaseNodeI2c,
  public BaseNodeSpi,
#ifndef DISABLE_SERIAL
  public BaseNodeSerialHandler,
#endif  // #ifndef DISABLE_SERIAL
  public BaseNodeI2cHandler<base_node_rpc::i2c_handler_t> {
public:
  uint8_t buffer_[128];
  Node() : BaseNode() {}
  UInt8Array get_buffer() { return UInt8Array(sizeof(buffer_), buffer_); }
  void begin() {
#if !defined(DISABLE_SERIAL)
    Serial.begin(115200);
#endif  // #ifndef DISABLE_SERIAL
    // Set i2c clock-rate to 400kHz.
    TWBR = 12;
  }
};

}  // namespace base_node_rpc

#endif  // #ifndef ___NODE__H___
