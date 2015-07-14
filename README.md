# base-node-rpc #

Base classes for Arduino RPC node/device.

 - A memory-efficient set of base classes providing an API to most of the
   Arduino API, including EEPROM access, raw I2C master-write/slave-request,
   etc.
 - Support for processing RPC command requests through either serial or I2C
   interface.
 - Utilizes Python (host) and C++ (device) code generation from the
   [`arduino_rpc`][1] package.


## C++ Base classes ##

The following classes may be used to form a basis for an Arduino RPC firmware
that may be interacted with through either a serial port or through I2C (the
same code path is used for buffers regardless of the source interface).

### BaseNode ###

The `BaseNode` class provides methods to identify key properties of a device
and exposes most of the Arduino API.  The `BaseNode` class is intended as a
base class, to be extended by combining with the remaining `Base*` classes.

### BaseNodeConfig ###

The `BaseNodeConfig` class provides methods to interact with a persistent
device configuration.  The main methods are:

 - `load_config`: Load a config from persistent storage (e.g., EEPROM).
 - `save_config`: Write a config to persistent storage.
 - `reset_config`: Reset in-memory config to default values.
 - `serialize_config`: Serialize in-memory config to a byte array.
 - `update_config`: Update in-memory config based on a serialized config.

Note that the `BaseNodeConfig` class is a template class, with two template
parameters:

 - `typename ConfigMessage`:
     * The configuration class to use.
     * Must have the methods: `load(address)`, `save(address)`, `reset()`,
       `serialize()`, and `update(serialized)`.
 - `uint8_t Address`: Address in persistent storage where config is stored.

__NB__ : Currently, the configuration class is assumed to be a
[Protocol Buffer][2] message type class, with Arduino code generated using the
[`nanopb`][3] library.  The `nanopb` library is also used for encoding/decoding
the messages.  The [`nanopb-helpers`][4] package provides a mechanism for
validating updates to a `nanopb` instance (see "Config validation").

#### Config validation ####

__TODO__ Add description of callback methods.


### BaseNodeEeprom ###

The `BaseNodeEeprom` class is a mixin to add methods for interfacing with
Arduino EEPROM.

The following methods are provided:

 - `update_eeprom_block(address, data)`: Write data byte array to EEPROM
   address.  Only write to the EEPROM if values have changed.
 - `read_eeprom_block(address, n)`: Read `n` bytes from EEPROM at address to
   bytes array.

### BaseNodeI2c ###

The `BaseNodeI2c` class is a mixin to add methods for interfacing with
Arduino I2C bus

The following methods are provided:

 - `set_i2c_address(address)`: Set I2C address.
 - `i2c_address`: Query I2C address.
 - `i2c_buffer_size`: Query TWI library buffer length.
 - `i2c_scan`: Return array of addresses found on I2C bus.
 - `i2c_available`: Number of bytes available in incoming I2C buffer.
 - `i2c_read_byte`: Read byte from I2C buffer.
 - `i2c_request_from(address, n_bytes_to_read)`: Request data from I2C slave.
 - `i2c_read(address, n_bytes_to_read)`: Request followed by read to array.
 - `i2c_write(address, data)`: Write byte array to address.

### BaseNodeSpi ###

The `BaseNodeSpi` class is a mixin to add methods for interfacing with the
Arduino SPI API.

The following methods are provided:

 - `set_spi_bit_order(order)`
 - `set_spi_clock_divider(divider)`
 - `set_spi_data_mode(mode)`
 - `spi_transfer(value)`

See the Arduino SPI API reference for details.

### BaseNodeState ###

The `BaseNodeState` class provides methods to interact with an in-memory device
state structure.  The main methods are:

 - `reset_state`: Reset in-memory state to default values.
 - `serialize_state`: Serialize in-memory state to a byte array.
 - `update_state`: Update in-memory state based on a serialized state.

Note that the `BaseNodeState` class is a template class, with the following
template parameter:

 - `typename StateMessage`:
     * The state class to use.
     * Must have the methods: `reset()`, `serialize()`, and
       `update(serialized)`.

__NB__ : Currently, the state class is assumed to be a
[Protocol Buffer][2] message type class, with Arduino code generated using the
[`nanopb`][3] library.  The `nanopb` library is also used for encoding/decoding
the messages.  The [`nanopb-helpers`][4] package provides a mechanism for
validating updates to a `nanopb` instance (see "State validation").

#### State validation ####

__TODO__ Add description of callback methods.


### BaseNodeI2cHandler ###

The `BaseNodeI2cHandler` class is a mixin to add methods for handling RPC
requests from the I2C interface.  The main methods are:


 - `max_i2c_payload_size`: Return maximum serialized command size supported by
   the I2C interface handler.
 - `i2c_request(address, data)`: Send a serialized command byte array to the
   specified I2C address and return serialized response array.

### BaseNodeSerialHandler ###

The `BaseNodeSerialHandler` class is a mixin to add methods for handling RPC
requests from the serial interface.  The main methods are:

 - `max_serial_payload_size`: Return maximum serialized command size supported
   by the serial interface handler.


[1]: http://github.com/wheeler-microfluidics/arduino_rpc.git
[2]: https://code.google.com/p/protobuf/
[3]: http://koti.kapsi.fi/jpa/nanopb/
[4]: https://github.com/wheeler-microfluidics/nanopb_helpers
