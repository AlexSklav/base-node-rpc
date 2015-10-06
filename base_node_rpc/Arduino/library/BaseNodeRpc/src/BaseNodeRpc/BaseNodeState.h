#ifndef ___BASE_NODE_STATE__H___
#define ___BASE_NODE_STATE__H___


#include <CArrayDefs.h>
#include <pb.h>


template <typename StateMessage>
class BaseNodeState {
public:
  typedef StateMessage message_type;
  StateMessage state_;

  BaseNodeState(const pb_field_t *fields) : state_(fields) {}

  void reset_state() { state_.reset(); }
  UInt8Array serialize_state() { return state_.serialize(); }
  uint8_t update_state(UInt8Array serialized) {
    return state_.update(serialized);
  }
};


#endif  // #ifndef ___BASE_NODE_STATE__H___
