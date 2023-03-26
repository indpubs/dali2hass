from dali.command import Command
from dali.gear.general import EnableDeviceType
from dali.sequences import sleep as seq_sleep
from dali.sequences import progress as seq_progress
from dali.exceptions import CommunicationError, UnsupportedFrameTypeError
import dali.frame
import time
import socket
import struct
import voluptuous as vol


SCHEMA = vol.Schema({
    "driver": "daliserver",
    vol.Optional("hostname", default="localhost"): str,
    vol.Optional("port", default=55825): int,
})


def _wrap(command):
    response = yield command
    return response


class DaliServer:
    """Communicate with daliserver
    (https://github.com/onitake/daliserver)

    NB this requires daliserver commit
    90e34a0cd2945dc7a15681f11647e708f858521e or later.
    """

    def __init__(self, config):
        config = SCHEMA(config)
        self._target = (config["hostname"], config["port"])
        self._s = None

    def __enter__(self):
        self._s = socket.create_connection(self._target)
        return self

    def __exit__(self, *vpass):
        self._s.close()
        self._s = None

    def send(self, command, progress=None):
        s = self._s

        # If "command" is not a sequence, wrap it in a trivial sequence
        seq = _wrap(command) if isinstance(command, Command) else command

        response = None
        while True:
            try:
                cmd = seq.send(response)
            except StopIteration as r:
                return r.value

            if isinstance(cmd, seq_sleep):
                time.sleep(cmd.delay)
            elif isinstance(cmd, seq_progress):
                if progress:
                    progress(cmd)
            else:
                if cmd.devicetype != 0:
                    self._send(s, EnableDeviceType(cmd.devicetype))
                response = self._send(s, cmd)

    def _send(self, s, command):
        f = command.frame
        if len(f) != 16:
            raise UnsupportedFrameTypeError

        result = "\x02\xff\x00\x00"
        message = struct.pack("BB", 2, 0) + f.pack
        s.send(message)
        result = s.recv(4)
        if command.sendtwice:
            s.send(message)
            result = s.recv(4)

        ver, status, rval, pad = struct.unpack("BBBB", result)
        response = None

        if command.response:
            if status == 0:
                response = command.response(None)
            elif status == 1:
                response = command.response(dali.frame.BackwardFrame(rval))
            elif status == 255:
                # This is "failure" - daliserver seems to be reporting
                # this for a garbled response when several ballasts
                # reply.  It should be interpreted as "Yes".
                response = command.response(dali.frame.BackwardFrameError(255))
            else:
                raise CommunicationError("status was %d" % status)

        return response
