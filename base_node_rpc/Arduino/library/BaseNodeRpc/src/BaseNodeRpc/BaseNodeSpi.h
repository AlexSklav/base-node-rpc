#ifndef ___BASE_NODE_SPI__H___
#define ___BASE_NODE_SPI__H___


#include <SPI.h>
#include <CArrayDefs.h>


class BaseNodeSpi {
public:
  void spi_begin() { SPI.begin(); }
  void set_spi_bit_order(uint8_t order) {
#ifdef __SAM3X8E__
    SPI.setBitOrder((BitOrder)order);
#else
    SPI.setBitOrder(order);
#endif
  }
  void set_spi_clock_divider(uint8_t divider) { SPI.setClockDivider(divider); }
  void set_spi_data_mode(uint8_t mode) { SPI.setDataMode(mode); }
  uint8_t spi_transfer(uint8_t value) { return SPI.transfer(value); }
  void spi_begin_transaction(uint32_t clock, uint8_t bit_order,
                             uint8_t data_mode) {
    SPI.beginTransaction(SPISettings(clock, bit_order, data_mode));
  }
  void spi_end_transaction(void) { SPI.endTransaction(); }
  UInt8Array spi_transfer_array(uint8_t slave_select_pin, uint32_t clock,
                                uint8_t bit_order, uint8_t data_mode,
                                UInt8Array data) {
    SPI.beginTransaction(SPISettings(clock, bit_order, data_mode));
    digitalWrite(slave_select_pin, LOW);
    for (uint32_t i = 0; i < data.length; i++) {
        //SPI.transfer(data.data, data.length);
        SPI.transfer(data.data[i]);
    }
    digitalWrite(slave_select_pin, HIGH);
    SPI.endTransaction();
    return data;
  }
};

#endif  // #ifndef ___BASE_NODE_SPI__H___
