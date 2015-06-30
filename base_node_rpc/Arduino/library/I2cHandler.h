#include "NadaMQ.h"
#include <BaseNodeI2c.h>


namespace base_node_rpc {


inline void i2c_write_packet(uint8_t address, UInt8Array data) {
  /*
   * Write packet with `data` array contents as payload to specified I2C
   * address.
   *
   * Notes
   * -----
   *
   *  - Packet is sent as multiple transmissions on the I2C bus.
   *  - Target I2C address is prepended to each transmission to allow the
   *    target to identify the source of the message.
   *  - The payload is sent in chunks as required (i.e., payloads greater
   *    than the Wire library buffer size are supported). */
  FixedPacket to_send;
  to_send.type(Packet::packet_type::DATA);
  to_send.reset_buffer(data.length, data.data);
  to_send.payload_length_ = data.length;

  // Set the CRC checksum of the packet based on the contents of the payload.
  to_send.compute_crc();

  stream_byte_type startflag[] = "|||";
  const uint8_t source_addr = (TWAR & 0x0FE) >> 1;
  uint8_t type_ = static_cast<uint8_t>(to_send.type());

  // Send the packet header.
  Wire.beginTransmission(address);
  serialize_any(Wire, source_addr);
  Wire.write(startflag, 3);
  serialize_any(Wire, to_send.iuid_);
  serialize_any(Wire, type_);
  serialize_any(Wire, static_cast<uint16_t>(to_send.payload_length_));
  Wire.endTransmission();

  while (to_send.payload_length_ > 0) {
    uint16_t length = ((to_send.payload_length_ > TWI_BUFFER_LENGTH - 1)
                       ? TWI_BUFFER_LENGTH - 1 : to_send.payload_length_);

    /*  Send the next chunk of the payload up to the buffer size of the Wire
     *  library (actually one less, since the first byte is used to label the
     *  source address of the message). */
    Wire.beginTransmission(address);
    serialize_any(Wire, source_addr);
    Wire.write((stream_byte_type*)to_send.payload_buffer_,
                length);
    Wire.endTransmission();

    to_send.payload_buffer_ += length;
    to_send.payload_length_ -= length;
  }

  // Send CRC of packet.
  Wire.beginTransmission(address);
  serialize_any(Wire, source_addr);
  serialize_any(Wire, to_send.crc_);
  Wire.endTransmission();
}



template <size_t PacketSize, uint32_t TIMEOUT_MS=5000>
class I2cHandler {
public:
  typedef PacketParser<FixedPacket> parser_t;
  typedef i2c_buffer::I2cReceiver<parser_t> receiver_t;
  uint8_t packet_buffer_[PacketSize];
  FixedPacket i2c_packet_;
  parser_t i2c_parser_;
  receiver_t receiver_;

  I2cHandler()
    : i2c_packet_(PacketSize, &packet_buffer_[0]), i2c_parser_(&i2c_packet_),
      receiver_(i2c_parser_) {}

  uint32_t max_payload_size() {
    return (PacketSize
            - 3 * sizeof(uint8_t)  // Frame boundary
            - sizeof(uint16_t)  // UUID
            - sizeof(uint16_t)  // Payload length
            - sizeof(uint16_t));  // CRC
  }
  void packet_reset() { receiver_.reset(); }
  uint8_t packet_ready() { return receiver_.packet_ready(); }
  uint8_t packet_error() { return receiver_.packet_error(); }
  UInt8Array packet_read() {
    UInt8Array output = receiver_.packet_read();
    output.length = (output.length > max_payload_size() ? max_payload_size()
                     : output.length);
    return output;
  }

  uint8_t receiver_source_address() { return receiver_.source_addr_; }
  void set_receiver_source_address(uint8_t addr) {
    receiver_.source_addr_ = addr;
  }

  template <typename CommandProcessor>
  UInt8Array process_packet(CommandProcessor &command_processor) {
    UInt8Array result;
    if (packet_ready()) {
      // Process request packet using command processor.
      result = process_packet_with_processor(*receiver_.parser_.packet_,
                                             command_processor);
      if (result.data == NULL) { result.length = 0; }
      // Write response packet.
      i2c_write_packet(receiver_.source_addr_, result);
      // Reset packet state since request may have overwritten packet buffer.
      packet_reset();
    } else {
      result.data = NULL;
      result.length = 0;
    }
    return result;
  }

  UInt8Array request(uint8_t address, UInt8Array data) {
    packet_reset();
    i2c_write_packet(address, data);
    uint32_t start_time = millis();
    while (TIMEOUT_MS > (millis() - start_time) && !packet_ready()) {}
    UInt8Array result = packet_read();
    // Reset packet state to prepare for incoming requests on I2C interface.
    packet_reset();
    return result;
  }

  i2c_buffer::I2cReceiver<parser_t> &receiver() { return receiver_; }
};


} // namespace base_node_rpc
