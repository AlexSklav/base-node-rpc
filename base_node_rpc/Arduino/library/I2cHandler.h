#ifndef ___BASE_NODE__I2C_HANDLER__H___
#define ___BASE_NODE__I2C_HANDLER__H___

namespace base_node {

template <typename Packet>
struct I2cHandler {
  uint8_t processing_request;
  uint8_t response_size_sent;
  Packet request_packet;
  Packet response_packet;

  I2cHandler() : processing_request(false), response_size_sent(false) {}

  template <typename Processor>
  void process_available(Processor &command_processor) {
    if (processing_request) {
      UInt8Array result = process_packet_with_processor(request_packet,
                                                        command_processor);
      if (result.data == NULL) {
        /* There was an error encountered while processing the request. */
        response_packet.type(Packet::packet_type::NACK);
        response_packet.payload_length_ = 0;
      } else {
        response_packet.reset_buffer(result.length, result.data);
        response_packet.payload_length_ = result.length;
        response_packet.type(Packet::packet_type::DATA);
      }
      processing_request = false;
    }
  }

  void on_receive(int16_t byte_count) {
    processing_request = true;
    /* Record all bytes received on the i2c bus to a buffer.  The contents of
    * this buffer will be forwarded to the local serial-stream. */
    int i;
    for (i = 0; i < byte_count; i++) {
        request_packet.payload_buffer_[i] = Wire.read();
    }
    request_packet.payload_length_ = i;
    request_packet.type(Packet::packet_type::DATA);
  }

  void on_request() {
    uint8_t byte_count = (uint8_t)response_packet.payload_length_;
    /* There is a response from a previously received packet, so send it to the
    * master of the i2c bus. */
    if (!response_size_sent) {
      if (processing_request) {
        Wire.write(0xFF);
      } else {
        Wire.write(byte_count);
        response_size_sent = true;
      }
    } else {
      Wire.write(response_packet.payload_buffer_, byte_count);
      response_size_sent = false;
    }
  }
};

}  // namespace base_node

#endif  // ifndef ___BASE_NODE__I2C_HANDLER__H___
