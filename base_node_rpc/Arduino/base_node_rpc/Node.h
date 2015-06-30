#ifndef ___NODE__H___
#define ___NODE__H___

#include <BaseNodeRpc.h>
#include <BaseNodeEeprom.h>
#include <BaseNodeI2c.h>
#include <BaseNodeSpi.h>


class Node :
  public BaseNode, public BaseNodeEeprom, public BaseNodeI2c,
  public BaseNodeSpi {
public:
  uint8_t output_buffer[128];
  Node() : BaseNode() {}
  virtual UInt8Array get_buffer() {
    UInt8Array output;
    output.data = output_buffer;
    output.length = sizeof(output_buffer);
    return output;
  }
  uint32_t test() { return 128; }
};


#endif  // #ifndef ___NODE__H___
