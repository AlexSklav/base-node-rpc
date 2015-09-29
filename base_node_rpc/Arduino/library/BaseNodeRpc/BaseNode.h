#ifndef ___BASE_NODE__H___
#define ___BASE_NODE__H___

#include <stdint.h>
#include <utility/twi.h>
#include <pb_decode.h>
#include <pb_encode.h>
#include <CArrayDefs.h>
#include "Memory.h"
#include "BaseBuffer.h"
#include "RPCBuffer.h"


#ifndef P
#define P(str) (strcpy_P(p_buffer_, PSTR(str)), p_buffer_)
#endif

#ifdef BASE_NODE__BASE_NODE_SOFTWARE_VERSION
const char BASE_NODE_SOFTWARE_VERSION_[] PROGMEM =
  BASE_NODE__BASE_NODE_SOFTWARE_VERSION;
#else
const char BASE_NODE_SOFTWARE_VERSION_[] PROGMEM = "";
#endif
#ifdef BASE_NODE__NAME
const char NAME_[] PROGMEM = BASE_NODE__NAME;
#else
const char NAME_[] PROGMEM = "";
#endif  // #ifdef BASE_NODE__NAME
#ifdef BASE_NODE__MANUFACTURER
const char MANUFACTURER_[] PROGMEM = BASE_NODE__MANUFACTURER;
#else
const char MANUFACTURER_[] PROGMEM = "";
#endif
#ifdef BASE_NODE__SOFTWARE_VERSION
const char SOFTWARE_VERSION_[] PROGMEM = BASE_NODE__SOFTWARE_VERSION;
#else
const char SOFTWARE_VERSION_[] PROGMEM = "";
#endif
#ifdef BASE_NODE__URL
const char URL_[] PROGMEM = BASE_NODE__URL;
#else
const char URL_[] PROGMEM = "";
#endif


inline UInt8Array prog_string(const char* str, UInt8Array array) {
  strcpy_P((char *)array.data, str);
  array.length = strlen_P(str);
  return array;
}


class BaseNode : public BufferIFace {
  /* The `BaseNode` class provides methods to identify key properties of
   * a device and exposes most of the Arduino API. */
public:
  BaseNode() {}

  UInt8Array base_node_software_version() {
    return prog_string(BASE_NODE_SOFTWARE_VERSION_, get_buffer());
  }
  UInt8Array name() { return prog_string(NAME_, get_buffer()); }
  UInt8Array manufacturer() {
    return prog_string(MANUFACTURER_, get_buffer());
  }
  UInt8Array software_version() {
    return prog_string(SOFTWARE_VERSION_, get_buffer());
  }
  UInt8Array url() { return prog_string(URL_, get_buffer()); }

  uint32_t microseconds() { return micros(); }
  uint32_t milliseconds() { return millis(); }
  void delay_us(uint16_t us) { if (us > 0) { delayMicroseconds(us); } }
  void delay_ms(uint16_t ms) { if (ms > 0) { delay(ms); } }
  uint32_t ram_free() { return free_memory(); }

  void pin_mode(uint8_t pin, uint8_t mode) { return pinMode(pin, mode); }
  uint8_t digital_read(uint8_t pin) const { return digitalRead(pin); }
  void digital_write(uint8_t pin, uint8_t value) { digitalWrite(pin, value); }
  uint16_t analog_read(uint8_t pin) const { return analogRead(pin); }
  void analog_write(uint8_t pin, uint8_t value) { return analogWrite(pin, value); }

  uint16_t array_length(UInt8Array array) { return array.length; }
  UInt32Array echo_array(UInt32Array array) { return array; }
  UInt8Array str_echo(UInt8Array msg) { return msg; }
};


#endif  // #ifndef ___BASE_NODE__H___
