import time
from datetime import datetime
from Queue import Queue
from threading import Thread

import pandas as pd
import numpy as np
from nadamq.NadaMq import cPacketParser, PACKET_TYPES


class PacketQueueManager(object):
    def __init__(self, high_water_mark=None):
        self._packet_parser = cPacketParser()
        packet_types = ['data', 'ack', 'stream']
        self.packet_queues = pd.Series([Queue() for t in packet_types], index=packet_types)
        self.high_water_mark = high_water_mark

    def parse_available(self, stream):
        data = stream.read()

        packets = []

        for c in data:
            result = self._packet_parser.parse(np.fromstring(c, dtype='uint8'))
            if result != False:
                packet_str = np.fromstring(result.tostring(), dtype='uint8')
                packets.append((datetime.now(), cPacketParser().parse(packet_str)))
                self._packet_parser.reset()
            elif self._packet_parser.error:
                self._packet_parser.reset()

        for t, p in packets:
            if p.type_ == PACKET_TYPES.DATA and not self.queue_full('data'):
                self.packet_queues['data'].put((t, p))
            elif p.type_ == PACKET_TYPES.ACK and not self.queue_full('ack'):
                self.packet_queues.ack.put((t, p))
            elif ((p.type_ == PACKET_TYPES.STREAM) and
                  (not self.queue_full('stream'))):
                self.packet_queues.stream.put((t, p))

    def queue_full(self, name):
        return ((self.high_water_mark is not None) and
                (self.packet_queues[name].qsize() >= self.high_water_mark))


class SerialStream(object):
    def __init__(self, serial_device):
        self.serial_device = serial_device

    def read(self):
        return self.serial_device.read(self.serial_device.inWaiting())


class FakeStream(object):
    def __init__(self, messages):
        self.messages = messages

    def read(self):
        if self.messages:
            return self.messages.pop(0)
        else:
            return ''


class PacketWatcher(Thread):
    def __init__(self, stream, delay_seconds=.01, high_water_mark=None):
        self.message_parser = PacketQueueManager(high_water_mark)
        self.stream = stream
        self.enabled = False
        self._terminated = False
        self.delay_seconds = delay_seconds
        super(PacketWatcher, self).__init__()
        self.daemon = True

    def run(self):
        while True:
            if self._terminated:
                break
            elif self.enabled:
                self.parse_available()
            time.sleep(self.delay_seconds)

    def parse_available(self):
        self.message_parser.parse_available(self.stream)

    @property
    def queues(self):
        return self.message_parser.packet_queues

    def terminate(self):
        self._terminated = True
        self.delay_seconds = 0
        self.join()

    def __del__(self):
        self.terminate()
