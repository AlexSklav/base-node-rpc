#include "Arduino.h"
#include "EEPROM.h"
#include "SPI.h"
#include "Wire.h"
#include "Memory.h"
#include "ArduinoRpc.h"
#include "nanopb.h"
#include "NadaMQ.h"
#include "CArrayDefs.h"
#include "RPCBuffer.h"
#include "NodeCommandProcessor.h"
#include "BaseNodeRpc.h"
#include "Node.h"


base_node_rpc::Node node_obj;
base_node_rpc::CommandProcessor<base_node_rpc::Node>
  command_processor(node_obj);


void i2c_receive_event(int byte_count) { node_obj.i2c_handler_.receiver()(byte_count); }


void setup() {
  /* ..versionchanged:: 0.31
   *     Notify device identifier after setup is completed.
   */
  node_obj.begin();
  Wire.onReceive(i2c_receive_event);
#if defined(DEVICE_ID_RESPONSE)
  // Notify device identifier
  node_obj.serial_handler_.write_device_id_response();
#endif
}


void loop() {
#ifndef DISABLE_SERIAL
  /* Parse all new bytes that are available.  If the parsed bytes result in a
   * completed packet, pass the complete packet to the command-processor to
   * process the request.
   *
   * .. versionchanged:: v0.32
   *     Poll for available serial data instead of using `serialEvent`
   *     callback.  Seems that this is necessary to support Arduino micro.
   */
  if (Serial.available()) {
    node_obj.serial_handler_.receiver()(Serial.available());

    if (node_obj.serial_handler_.packet_ready()) {
        node_obj.serial_handler_.process_packet(command_processor);
    }
  }
#endif  // #ifndef DISABLE_SERIAL
  if (node_obj.i2c_handler_.packet_ready()) {
    node_obj.i2c_handler_.process_packet(command_processor);
  }
}
