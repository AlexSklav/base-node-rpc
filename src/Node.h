#ifndef ___NODE__H___
#define ___NODE__H___

#include <CArrayDefs.h>
#include <BaseNodeRpc/BaseNode.h>
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
  public BaseNodeSpi
#ifndef DISABLE_SERIAL
  , public BaseNodeSerialHandler
#endif  // #ifndef DISABLE_SERIAL
  , public BaseNodeI2cHandler<base_node_rpc::i2c_handler_t> {
public:
  uint8_t buffer_[128];
  Node() : BaseNode() {}
  UInt8Array get_buffer() { return UInt8Array_init(sizeof(buffer_), buffer_); }
  void begin() {
    /* ..versionchanged:: 0.29.2
     *     Disable I2C on Teensy to avoid freezing.
     */
#if !defined(DISABLE_SERIAL)
#if F_CPU == 8000000L
    Serial.begin(57600);
#else
    Serial.begin(115200);
#endif  // #if F_CPU == 8000000L
#endif  // #ifndef DISABLE_SERIAL
#ifndef DISABLE_I2C
    // Set i2c clock-rate to 400kHz.
    TWBR = 12;
#endif
  }
};

}  // namespace base_node_rpc

#endif  // #ifndef ___NODE__H___
