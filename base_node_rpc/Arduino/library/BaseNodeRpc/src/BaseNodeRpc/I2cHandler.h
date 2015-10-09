#ifndef ___I2C_HANDLER__H___
#define ___I2C_HANDLER__H___

#include <NadaMQ.h>
#include <Wire.h>
#include "BaseHandler.h"


namespace base_node_rpc {


struct i2c_write_packet {
  uint8_t address_;

  i2c_write_packet() : address_(0) {}

  void operator()(UInt8Array data, uint8_t type_=Packet::packet_type::DATA) {
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
    to_send.type(type_);
    to_send.reset_buffer(data.length, data.data);
    to_send.payload_length_ = data.length;

    // Set the CRC checksum of the packet based on the contents of the payload.
    to_send.compute_crc();

    stream_byte_type startflag[] = "|||";
    const uint8_t source_addr = (TWAR & 0x0FE) >> 1;

    // Send the packet header.
    Wire.beginTransmission(address_);
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
      Wire.beginTransmission(address_);
      serialize_any(Wire, source_addr);
      Wire.write((stream_byte_type*)to_send.payload_buffer_,
                  length);
      Wire.endTransmission();

      to_send.payload_buffer_ += length;
      to_send.payload_length_ -= length;
    }

    // Send CRC of packet.
    Wire.beginTransmission(address_);
    serialize_any(Wire, source_addr);
    serialize_any(Wire, to_send.crc_);
    Wire.endTransmission();
  }
};


template <typename Parser>
class I2cReceiver : public Receiver<Parser> {
public:
  /* Receiver has `write_f_` functor attribute, allowing stateful responses.
   *
   * In other words, it makes it possible for a response to be written to the
   * source of the original request through a call without providing an address
   * (the address is automatically set to the source of the request). */
  typedef Receiver<Parser> base_type;
  using base_type::parser_;

  i2c_write_packet write_f_;

  I2cReceiver(Parser &parser) : base_type(parser) { reset(); }

  virtual void reset() {
    base_type::reset();
    write_f_.address_ = 0;
  }

  void operator()(int16_t byte_count) {
    // Interpret first byte of each I2C message as source address of message.
    uint8_t source_addr = Wire.read();
    byte_count -= 1;
    /* Received message from a new source address.
     *
     * TODO
     * ====
     *
     * Strategies for dealing with this situation:
     *  1. Discard messages that do not match current source address until
     *     receiver is reset.
     *  2. Reset parser and start parsing data from new source.
     *  3. Maintain separate parser for each source address? */
    if (write_f_.address_ == 0) {
      write_f_.address_ = source_addr;
    }

    for (int i = 0; i < byte_count; i++) {
      uint8_t value = Wire.read();
      if (source_addr == write_f_.address_) { parser_.parse_byte(&value); }
    }
  }
};


template <typename Receiver_, size_t PacketSize, uint32_t TIMEOUT_MS=5000>
class I2cHandler : public Handler<Receiver_, PacketSize, TIMEOUT_MS> {
public:
  typedef Handler<Receiver_, PacketSize, TIMEOUT_MS> base_type;
  using base_type::packet_read;
  using base_type::packet_ready;
  using base_type::packet_reset;
  using base_type::receiver_;

  I2cHandler() : base_type() {}

  UInt8Array request(uint8_t address, UInt8Array data) {
    packet_reset();

    receiver_.write_f_.address_ = address;
    receiver_.write_f_(data);
    uint32_t start_time = millis();
    while (TIMEOUT_MS > (millis() - start_time) && !packet_ready()) {}
    UInt8Array result = packet_read();
    // Reset packet state to prepare for incoming requests on I2C interface.
    packet_reset();
    return result;
  }
};


typedef PacketParser<FixedPacket> parser_t;
typedef I2cReceiver<parser_t> i2c_receiver_t;
typedef I2cHandler<i2c_receiver_t, I2C_PACKET_SIZE> i2c_handler_t;

} // namespace base_node_rpc

#endif  // #ifndef ___I2C_HANDLER__H___
