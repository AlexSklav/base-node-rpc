#ifndef ___BASE_NODE_EEPROM__H___
#define ___BASE_NODE_EEPROM__H___


#include <avr/eeprom.h>
#include "BaseBuffer.h"

#ifndef eeprom_update_block
/* Older versions of `avr-gcc` (like the one included with Arduino IDE 1.0.5)
 * do not include the `eeprom_update_block` function. Use `eeprom_write_block`
 * instead. */
#define eeprom_write_block eeprom_update_block
#endif


namespace base_node_rpc {

inline UInt8Array eeprom_to_array(uint16_t address, UInt8Array output) {
  uint16_t payload_size;

  eeprom_read_block((void*)&payload_size, (void*)address, sizeof(uint16_t));
  if (output.length < payload_size) {
    output.length = 0;
    output.data = NULL;
  } else {
    eeprom_read_block((void*)output.data, (void*)(address + 2), payload_size);
    output.length = payload_size;
  }
  return output;
}

} // namespace base_node_rpc


class BaseNodeEeprom : public BufferIFace {
public:
  void update_eeprom_block(uint16_t address, UInt8Array data) {
    cli();
    eeprom_update_block((void*)data.data, (void*)address, data.length);
    sei();
  }

  UInt8Array read_eeprom_block(uint16_t address, uint16_t n) {
    UInt8Array output = get_buffer();
    eeprom_read_block((void*)&output.data[0], (void*)address, n);
    output.length = n;
    return output;
  }
};


#endif  // #ifndef ___BASE_NODE_EEPROM__H___
