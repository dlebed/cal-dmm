"""A scriptable in-memory GPIB transport for driver tests.

Deliberately *not* a ``MagicMock``. The root ``conftest.py`` stubs the ``Gpib``
module with a ``MagicMock`` so driver modules import without linux-gpib
installed, but a ``MagicMock.query()`` returns another ``MagicMock``, which
``float()`` and ``int()`` reject. Tests that need *behaviour* want a real
fake with real strings.

:class:`FakeTransport` structurally implements
:class:`caldmm.drivers.fluke5700a.Transport`, so it works for anything built
on that seam.

Scripting
---------

``responses`` maps a command string to what the instrument answers:

* ``str``  — the same single-line response every time,
* ``list`` — a **queue of successive responses**, one consumed per query.
  Each element is a ``str`` (one line), a ``list[str]`` (a multi-line block
  response, all lines queued at once) or an exception instance/class
  (raised when the line is read),
* ``callable`` — called with the command, returns one of the above.

Examples::

    FakeTransport({"FAULT?": ["3111", "0"],          # two successive answers
                   "*IDN?": "FLUKE,5720A,5248000,1.0+B+*"})

    FakeTransport({'CAL_RPT? CAL': [['"', "line 1", "line 2", '"']]})

Reading with nothing queued raises ``read_error`` (``TimeoutError`` by
default), which models a GPIB read timeout.
"""


class FakeTransport:
    """Scriptable fake implementing the ``Transport`` protocol."""

    def __init__(self, responses=None, *, stb: int = 0,
                 read_error=None):
        self.responses = dict(responses or {})
        #: Ordered list of every command written (the brief's ``log``).
        self.log: "list[str]" = []
        #: Ordered list of *every* interaction, so tests can assert the
        #: relative order of set_timeout / clear / write / read.
        self.events: "list[tuple]" = []
        #: Every timeout requested, in order (seconds).
        self.timeouts: "list[float]" = []
        self.cleared = 0
        self.closed = False
        self.stb = stb
        self.serial_polls = 0
        #: Raised by ``read()`` when no response line is queued.
        self.read_error = read_error or TimeoutError("fake GPIB read timeout")
        self._pending: "list" = []
        self._consumed: "dict[str, int]" = {}

    # -- Transport protocol --

    def write(self, cmd: str) -> None:
        self.log.append(cmd)
        self.events.append(("write", cmd))
        self._queue_response(cmd)

    def read(self) -> str:
        if not self._pending:
            self.events.append(("read", None))
            raise self.read_error
        item = self._pending.pop(0)
        if isinstance(item, BaseException):
            self.events.append(("read", item))
            raise item
        if isinstance(item, type) and issubclass(item, BaseException):
            self.events.append(("read", item))
            raise item()
        self.events.append(("read", item))
        return item.strip()

    def query(self, cmd: str) -> str:
        self.write(cmd)
        return self.read()

    def serial_poll(self) -> int:
        self.serial_polls += 1
        self.events.append(("serial_poll", self.stb))
        return self.stb

    def set_timeout(self, seconds: float) -> None:
        self.timeouts.append(seconds)
        self.events.append(("set_timeout", seconds))

    def local(self) -> None:
        self.events.append(("local", None))

    def clear(self) -> None:
        self.cleared += 1
        self.events.append(("clear", None))

    def close(self) -> None:
        self.closed = True
        self.events.append(("close", None))

    # -- scripting --

    def script(self, cmd: str, entry) -> None:
        """(Re)script *cmd*, resetting any partly consumed response queue.

        ``transport.responses[cmd] = [...]`` alone is not enough once the
        command has already been queried (during ``open()``, say): the
        consumed-index for that command survives and the new queue would be
        entered part-way through.
        """
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


def stateful_isr(transport, *, base=None):
    """An ``ISR?`` answer that follows ``OPER`` and ``STBY``.

    A constant ``ISR?`` describes a calibrator whose output never changes
    state, which is fine until something *verifies* a state change. It is not
    fine for ``Fluke5700A.standby()``, which reads the OPER bit back after
    ``STBY`` precisely because the command is ignored when the calibrator is
    not in remote ([OM Table 5-2, p.190], fault 2213) -- against a constant
    ``ISR?`` with OPER set, a correct driver raises "the output is still on".

    So: OPER set after an ``OPER``, clear after a ``STBY``, and set before
    either has been written (the state an opened session inherits). REMOTE and
    SETTLED are always set; pass *base* to say otherwise.

    Script it after the transport exists, because it reads that transport's
    own log::

        transport.script("ISR?", stateful_isr(transport))
    """
    from caldmm.drivers.fluke5700a import ISR_OPER, ISR_REMOTE, ISR_SETTLED

    if base is None:
        base = ISR_REMOTE | ISR_SETTLED

    def answer(cmd: str) -> str:
        for entry in reversed(transport.log):
            if entry == "OPER":
                return str(base | ISR_OPER)
            if entry == "STBY":
                return str(base)
        return str(base | ISR_OPER)

    return answer
