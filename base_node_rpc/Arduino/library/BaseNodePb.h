#ifndef ___BASE_NODE_PB__H___
#define ___BASE_NODE_PB__H___


#include <Wire.h>
#include "BaseBuffer.h"


/* Callback functions for slave device. */
extern void i2c_receive_event(int byte_count);
extern void i2c_request_event();


namespace base_node_rpc {

template <typename Obj, typename Fields>
inline UInt8Array serialize_to_array(Obj &obj, Fields &fields,
                                     UInt8Array output) {
  pb_ostream_t ostream = pb_ostream_from_buffer(output.data, output.length);
  bool ok = pb_encode(&ostream, fields, &obj);
  if (ok) {
    output.length = ostream.bytes_written;
  } else {
    output.length = 0;
    output.data = NULL;
  }
  return output;
}


template <typename Fields, typename Obj>
inline bool decode_from_array(UInt8Array input, Fields &fields, Obj &obj,
                              bool init_default=false) {
  pb_istream_t istream = pb_istream_from_buffer(input.data, input.length);
  if (init_default) {
    return pb_decode(&istream, fields, &obj);
  } else {
    return pb_decode_noinit(&istream, fields, &obj);
  }
}


} // namespace base_node_rpc


class BaseNodePb : public BufferIFace {
public:
  template <typename Obj, typename Fields>
  UInt8Array serialize_obj(Obj &obj, Fields &fields) {
    UInt8Array pb_buffer = get_buffer();
    pb_buffer = base_node_rpc::serialize_to_array(obj, fields, pb_buffer);
    return pb_buffer;
  }
  template <typename Obj, typename Fields>
  bool decode_obj_from_eeprom(uint16_t address, Obj &obj, Fields &fields) {
    UInt8Array pb_buffer = get_buffer();

    pb_buffer = base_node_rpc::eeprom_to_array(address, pb_buffer);
    bool ok;
    if (pb_buffer.data == NULL) {
      ok = false;
    } else {
      ok = base_node_rpc::decode_from_array(pb_buffer, fields, obj);
    }
    return ok;
  }
};

#endif  // #ifndef ___BASE_NODE_PB__H___
