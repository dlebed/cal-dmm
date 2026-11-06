"""A scriptable in-memory serial port for driver tests.

The counterpart to :mod:`tests.drivers.fake_gpib` one layer down: that module
fakes a whole :class:`~caldmm.drivers.fluke5700a.Transport`, this one fakes
the ``serial.Serial`` object *underneath*
:class:`~caldmm.drivers.fluke5700a.SerialTransport`, so the transport's own
framing, terminators and Control/C handling are exercised rather than stubbed
past.

Scripting matches :class:`~tests.drivers.fake_gpib.FakeTransport`: a command
maps to a ``str`` (the same answer every time), a ``list`` (a queue of
successive answers, each a ``str`` or a ``list[str]`` block), or a callable.
The command key is the command *without* its terminator.

Nothing queued means ``readline()`` returns ``b""``, which is what pyserial
does on a timeout and what the transport turns into ``TimeoutError``.

    port = FakeSerialPort({"*IDN?": "FLUKE,5720A,5248000,1.0+B+*"})
    transport = SerialTransport("/dev/null", port=port)   # via patched Serial
"""


class FakeSerialPort:
    """Scriptable stand-in for ``serial.Serial``."""

    def __init__(self, responses=None, *, timeout: float = 45.0,
                 eol: str = "\r\n"):
        self.responses = dict(responses or {})
        #: Every command written, terminators stripped (the ``log``).
        self.log: "list[str]" = []
        #: Every byte string written, verbatim — terminators included.
        self.raw_writes: "list[bytes]" = []
        #: Ordered interactions, so tests can assert relative order.
        self.events: "list[tuple]" = []
        #: Every timeout assigned, in order (seconds).
        self.timeouts: "list[float]" = [timeout]
        self.input_flushes = 0
        self.flushes = 0
        self.closed = False
        #: Line terminator the fake instrument puts on its responses.
        self.eol = eol
        self._timeout = timeout
        self._pending: "list[str]" = []
        self._consumed: "dict[str, int]" = {}

    # -- the pyserial surface SerialTransport uses --

    @property
    def timeout(self):
        return self._timeout

    @timeout.setter
    def timeout(self, seconds):
        self._timeout = seconds
        self.timeouts.append(seconds)
        self.events.append(("set_timeout", seconds))

    def write(self, data: bytes) -> int:
        self.raw_writes.append(data)
        text = data.decode("ascii")
        if text == "\x03":
            self.events.append(("dcl", None))
            return len(data)
        cmd = text.strip()
        self.log.append(cmd)
        self.events.append(("write", cmd))
        self._queue_response(cmd)
        return len(data)

    def flush(self) -> None:
        self.flushes += 1

    def readline(self) -> bytes:
        if not self._pending:
            self.events.append(("read", None))
            return b""                      # pyserial's timeout return
        line = self._pending.pop(0)
        self.events.append(("read", line))
        return (line + self.eol).encode("ascii")

    def read_until(self, expected: bytes = b"\n") -> bytes:
        """Like :meth:`readline`, for drivers that frame on another byte.

        A queued line comes back with :attr:`eol` appended whatever
        *expected* is, so a driver framing on the wrong byte gets a read
        that does not end in what it expected, not a silent success.
        """
        if not self._pending:
            self.events.append(("read", None))
            return b""                      # pyserial's timeout return
        line = self._pending.pop(0)
        self.events.append(("read", line))
        return (line + self.eol).encode("ascii")

    def reset_input_buffer(self) -> None:
        self.input_flushes += 1
        self.events.append(("reset_input", None))
        self._pending.clear()

    def close(self) -> None:
        self.closed = True
        self.events.append(("close", None))

    # -- scripting --

    def script(self, cmd: str, entry) -> None:
        """(Re)script *cmd*, resetting any partly consumed response queue."""
        self.responses[cmd] = entry
        self._consumed.pop(cmd, None)

    def _queue_response(self, cmd: str) -> None:
        entry = self.responses.get(cmd)
        if entry is None:
            return
        if callable(entry) and not isinstance(entry, type):
            entry = entry(cmd)
        if isinstance(entry, list):
            index = self._consumed.get(cmd, 0)
            if index >= len(entry):
                return                      # queue exhausted -> read timeout
            self._consumed[cmd] = index + 1
            entry = entry[index]
        if isinstance(entry, list):
            self._pending.extend(entry)     # multi-line block response
        else:
            self._pending.append(entry)
