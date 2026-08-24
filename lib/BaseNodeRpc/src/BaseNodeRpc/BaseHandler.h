#ifndef ___BASE_HANDLER__H___
#define ___BASE_HANDLER__H___

#include <string.h>

#include <Packet.h>


#if defined(DEVICE_ID_RESPONSE)
const char __DEVICE_ID_RESPONSE__[] PROGMEM = DEVICE_ID_RESPONSE;
#endif


namespace base_node_rpc {

template <typename Parser>
class Receiver {
public:
  typedef Parser parser_type;
  Parser &parser_;

  Receiver(Parser &parser) : parser_(parser) { reset(); }

  virtual void reset() {
    parser_.reset();
  }

  bool packet_ready() { return parser_.message_completed_; }
  uint8_t packet_error() {
    if (parser_.parse_error_) { return 'e'; }
    return 0;
  }
  virtual void operator()(int16_t byte_count) = 0;
};


/* Write a ``DATA`` response, echoing the ``IUID`` of the request packet so
 * that a driver can correlate each response with the request that produced it
 * (rather than relying on responses arriving strictly in order, which breaks
 * as soon as a request times out and is re-sent while the device is still
 * working on it).
 *
 * N.B. a request carrying ``IUID`` 0 -- i.e. one sent by any driver predating
 * this change -- echoes 0 back, so old drivers see byte-for-byte identical
 * responses.
 *
 * The overload pair below picks the 3-argument ``write_f_`` where the receiver
 * provides one (e.g. `serial_write_packet`) and falls back to the 1-argument
 * form otherwise (e.g. `i2c_write_packet`, where the response is addressed by
 * I2C address rather than by IUID).  ..versionadded:: 0.55.1 */
template <typename WriteF>
inline auto write_data_response(WriteF &write_f, UInt8Array data, uint16_t iuid,
                                int /* overload rank */)
    -> decltype(write_f(data, (uint8_t)Packet::packet_type::DATA, iuid)) {
  write_f(data, (uint8_t)Packet::packet_type::DATA, iuid);
}

template <typename WriteF>
inline void write_data_response(WriteF &write_f, UInt8Array data,
                                uint16_t /* iuid */, long /* overload rank */) {
  write_f(data);
}


template <typename Receiver_, size_t PacketSize, uint32_t TIMEOUT_MS=5000>
class Handler {
public:
  typedef Receiver_ receiver_t;
  typedef typename receiver_t::parser_type parser_t;
  uint8_t packet_buffer_[PacketSize];
  FixedPacket packet_;
  parser_t parser_;
  receiver_t receiver_;

  Handler()
    : packet_(PacketSize, &packet_buffer_[0]), parser_(&packet_),
      receiver_(parser_) {}

  uint32_t max_payload_size() {
    return (PacketSize
            - 3 * sizeof(uint8_t)  // Frame boundary
            - sizeof(uint16_t)  // UUID
            - sizeof(uint16_t)  // Payload length
            - sizeof(uint16_t));  // CRC
  }
  void packet_reset() { receiver_.reset(); }
  uint8_t packet_ready() {
    if (packet_error() != 0) {
      packet_reset();
      return false;
    }
    return receiver_.packet_ready();
  }
  uint8_t packet_error() { return receiver_.packet_error(); }
  UInt8Array packet_read() {
    UInt8Array output = packet_.payload();
    output.length = (output.length > max_payload_size() ? max_payload_size()
                     : output.length);
    return output;
  }

#if defined(DEVICE_ID_RESPONSE)
  void write_device_id_response(UInt8Array buffer) {
    /*
     * Write packet containing device identifier.
     *
     * ..versionadded:: 0.31
     *
     * Parameters
     * ----------
     * buffer : UInt8Array
     *     Buffer to temporarily store identifier string.
     */
    // Write response packet.
    strcpy_P((char *)buffer.data, __DEVICE_ID_RESPONSE__);
    buffer.length = strlen_P(__DEVICE_ID_RESPONSE__);
    receiver_.write_f_(buffer, Packet::packet_type::ID_RESPONSE);
  }

  void write_device_id_response() {
    /*
     * Write packet containing device identifier.
     *
     * Use packet buffer to temporarily store identifier string.
     *
     * ..versionadded:: 0.31
     */
    write_device_id_response(packet_.buffer());
  }
#endif

  template <typename CommandProcessor>
  UInt8Array process_packet(CommandProcessor &command_processor) {
    UInt8Array result = UInt8Array_init_default();

    if (packet_ready()) {
      if (packet_.type() == Packet::packet_type::DATA) {
        // Process request packet using command processor.
        result = process_packet_with_processor(packet_, command_processor);
        if (result.data == NULL) { result.length = 0; }
        // Write response packet, echoing the request IUID (see
        // `write_data_response` above).
        write_data_response(receiver_.write_f_, result, packet_.iuid_, 0);
#if defined(DEVICE_ID_RESPONSE)
      } else if (packet_.type() == Packet::packet_type::ID_REQUEST) {
        /* ID information was requested.
         *
         * ..versionadded:: 0.30
         *
         * ..versionchanged:: 0.31
         *     Use ``write_device_id_response`` to write device identifier.
         */
        write_device_id_response();
#endif
      }

      // Reset packet state since request may have overwritten packet buffer.
      packet_reset();
    } else {
      result.data = NULL;
      result.length = 0;
    }
    return result;
  }

  receiver_t &receiver() { return receiver_; }
};

}

#endif  // #ifndef ___BASE_HANDLER__H___
