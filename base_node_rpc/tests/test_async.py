# coding: utf-8
import base_node_rpc.ser_async as bnra

import asyncio


#: .. versionadded:: 0.47
def test_run_from_running_loop():
    async def foo():
        return bnra.available_devices()

    loop = bnra.new_file_event_loop()

    print(loop.run_until_complete(foo()))
    loop.stop()
    loop.close()


#: .. versionadded:: 0.47
def test_run_from_default_loop():
    print(bnra.available_devices())
