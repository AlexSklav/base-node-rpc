#ifndef ___BASE_BUFFER__H___
#define ___BASE_BUFFER__H___

#include "CArrayDefs.h"

struct BufferIFace {
  virtual UInt8Array get_buffer() = 0;
};


#endif  // #ifndef ___BASE_BUFFER__H___
