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
        // Write response packet.
        receiver_.write_f_(result);
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
