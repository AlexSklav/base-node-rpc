#ifndef ____REMOTE_I2C_COMMAND__H___
#define ____REMOTE_I2C_COMMAND__H___


struct BlockingI2cRead {
  static const int8_t CREATED              =  10;
  static const int8_t QUERY_STARTED        =  20;
  static const int8_t SEND_FAILED          = -10;
  static const int8_t QUERY_LENGTH_FAILED  = -20;
  static const int8_t QUERY_ERROR          = -30;
  static const int8_t INVALID_BUFFER_SIZE  = -40;
  static const int8_t RESPONSE_SIZE_ERROR  = -50;
  static const int8_t RESPONSE_EMPTY_ERROR = -60;
  static const int8_t QUERY_COMPLETE       =  30;
  int8_t ERROR_CODE_;

  BlockingI2cRead() : ERROR_CODE_(CREATED) {}

  UInt8Array operator() (uint8_t address, UInt8Array response) {
    bool status = false;
    uint8_t i2c_count = 0;

    /* Request response size. */
    response.length = Wire.requestFrom(address, (uint8_t)1);
    i2c_count = Wire.read();

    /* Request actual response. */
    response.length = Wire.requestFrom(address, (uint8_t)i2c_count);

    // Slave may send less than requested
    for (int i = 0; i < i2c_count; i++) {
      // receive a byte as character
      response.data[i] = Wire.read();
    }

    return response;
  }
};


#endif  // ifndef ____REMOTE_I2C_COMMAND__H___
