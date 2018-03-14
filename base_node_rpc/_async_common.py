from __future__ import absolute_import
from nadamq.NadaMq import cPacket, PACKET_TYPES


ID_REQUEST = cPacket(type_=PACKET_TYPES.ID_REQUEST).tostring()


class ParseError(Exception):
    pass
