#ifndef ___BASE_NODE_SPI__H___
#define ___BASE_NODE_SPI__H___


#include <SPI.h>


class BaseNodeSpi {
public:
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
};

#endif  // #ifndef ___BASE_NODE_SPI__H___
