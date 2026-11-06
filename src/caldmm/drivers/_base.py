"""Base classes for DMM-type instrument drivers."""

import sys
from abc import ABC, abstractmethod
from threading import Event, Thread, Lock
from queue import Queue, Empty, Full
from datetime import datetime
from typing import Optional


class MultimeterBase(ABC):
    """Abstract base for all DMM-type instruments.

    Subclasses must implement the low-level I/O and measurement methods.
    Instrument-specific features (ACAL, AC filters, etc.) stay on the
    concrete driver class.
    """

    _name: Optional[str]
    _lock: Lock

    #: Reading the instrument returns to mean "no valid reading". 9.9e37 on
    #: HP/Agilent, 1e35 on the Wavetek 1281. A single such sample poisons an
    #: average and wrecks a plot's autoscaling, so every consumer must filter
    #: on the *driver's own* value rather than on a hard-coded constant.
    #: Overridden per driver; the default is the HP/Agilent one.
    overload_sentinel: float = 9.9e37

    def __init__(self, *, name: Optional[str] = None):
        self._name = name
        self._lock = Lock()

    # -- Identity --

    @property
    def name(self) -> Optional[str]:
        return self._name

    @name.setter
    def name(self, name: str) -> None:
        self._name = name

    @property
    def model_id(self) -> str:
        """Model key, e.g. ``hp3458a`` — *not* the operator-chosen name.

        Keys the specification registry (``caldmm/cal/data/<model_id>.yaml``).
        The default is the class name lower-cased, which is what every current
        driver wants; override it when a class name and a spec file have to
        differ.
        """
        return type(self).__name__.lower()

    # -- Low-level I/O (transport-specific) --

    @abstractmethod
    def cmd(self, cmd_str: str) -> None:
        """Send a command to the instrument (no response expected)."""

    @abstractmethod
    def query(self, cmd_str: str) -> str:
        """Send a command and return the response string."""

    @abstractmethod
    def read_raw_str(self) -> str:
        """Read a raw string from the instrument."""

    # -- Measurement --

    @abstractmethod
    def read_single_sample(self) -> float:
        """Take one measurement and return the value as a float."""

    # -- Configuration --

    @abstractmethod
    def set_func(self, func, max_input: float = -1) -> None:
        """Set the measurement function and optionally the range."""

    @property
    @abstractmethod
    def nplc(self) -> float:
        """Integration time in power line cycles."""

    @nplc.setter
    @abstractmethod
    def nplc(self, value: float) -> None: ...

    @property
    @abstractmethod
    def delay(self) -> float:
        """Trigger delay in seconds."""

    @delay.setter
    @abstractmethod
    def delay(self, value: float) -> None: ...

    # -- Display --

    @abstractmethod
    def display_on(self) -> None: ...

    @abstractmethod
    def display_off(self) -> None: ...

    # -- Optional capabilities (override to enable) --

    def has_internal_temp(self) -> bool:
        return False

    def get_temp(self) -> float:
        raise NotImplementedError

    def has_acal(self) -> bool:
        return False

    def acal(self, **kwargs) -> None:
        raise NotImplementedError

    def has_burst(self) -> bool:
        """True if the driver can capture N consecutive samples into the
        instrument's internal memory and bulk-fetch them in one round-trip.
        Drivers without internal storage return False; callers should fall
        back to a host-side loop over read_single_sample().
        """
        return False

    def burst_capture(self, n: int, *, timeout_sec: Optional[float] = None) -> list[float]:
        """Capture N consecutive samples back-to-back and return them.

        Default raises NotImplementedError. Drivers that override this
        must also override has_burst(). has_burst() need not be a constant
        True: a driver whose instrument cannot burst in *some* configuration
        answers False there so that read_samples() falls back to the
        per-sample loop below -- see HP3458A and offset-compensated ohms.
        """
        raise NotImplementedError(
            f'{type(self).__name__} does not support burst capture')

    def read_samples(self, n: int) -> list[float]:
        """Return N samples, routing through burst_capture() when available.

        An overloaded sample comes back as overload_sentinel rather than
        raising: the caller is averaging N readings and needs to count and
        exclude the bad ones, and one raised OverflowError would throw away
        the whole capture. read_single_sample() keeps its raise-on-overload
        contract for single-shot callers.
        """
        if n <= 0:
            raise ValueError(f'n must be >= 1, got {n}')
        if self.has_burst():
            return list(self.burst_capture(n))
        out: list[float] = []
        for _ in range(n):
            try:
                out.append(self.read_single_sample())
            except OverflowError:
                out.append(self.overload_sentinel)
        return out


class WorkerBase:
    """Background polling thread for any MultimeterBase instrument.

    Continuously calls read_single_sample() and enqueues (value, timestamp)
    tuples. Handles overload and queue-full conditions gracefully.

    Read errors back off exponentially (ERROR_BACKOFF_MIN .. ERROR_BACKOFF_MAX)
    and an unchanged error message is printed only every
    ERROR_REPEAT_REPORT occurrences. Without this, an error that can never
    recover (a dead socket's EPIPE) spins the loop and fills the disk with
    log lines.
    """

    ERROR_BACKOFF_MIN = 0.1
    ERROR_BACKOFF_MAX = 5.0
    ERROR_REPEAT_REPORT = 100

    def __init__(self, dev: MultimeterBase, maxsize: int = 0):
        self._dev = dev
        self._queue: Queue = Queue(maxsize=maxsize)
        self._thread: Optional[Thread] = None
        self._enabled: bool = False
        self._stop = Event()

    @property
    def device(self) -> MultimeterBase:
        return self._dev

    def start_polling(self) -> None:
        self._enabled = True
        self._stop.clear()
        self._thread = Thread(target=self._worker, daemon=True)
        self._thread.start()

    def stop_polling(self, timeout: Optional[float] = None) -> None:
        self._enabled = False
        self._stop.set()
        if self._thread:
            self._thread.join(timeout)
            self._thread = None

    def sample_get(self, block: bool = True, timeout: Optional[float] = None) -> tuple[float, datetime]:
        return self._queue.get(block=block, timeout=timeout)

    def sample_get_last(self, block: bool = True, timeout: Optional[float] = None) -> tuple[float, datetime]:
        while self._queue.qsize() > 1:
            try:
                self._queue.get_nowait()
            except Empty:
                break
        return self._queue.get(block=block, timeout=timeout)

    def sample_get_value(self, block: bool = True, timeout: Optional[float] = None) -> float:
        return self.sample_get(block=block, timeout=timeout)[0]

    def qsize(self) -> int:
        return self._queue.qsize()

    def sample_clear(self) -> None:
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except Empty:
                break

    def _worker(self) -> None:
        dev_name = self._dev.name or self._dev.__class__.__name__
        errors = 0          # consecutive errors
        last_msg = None
        repeats = 0         # occurrences of last_msg in the current run
        while self._enabled:
            try:
                reading = self._dev.read_single_sample()
                timestamp = datetime.now()
                if errors:
                    print(f'{dev_name} Worker: recovered after {errors} errors',
                          file=sys.stderr)
                    errors, last_msg, repeats = 0, None, 0
                self._queue.put((reading, timestamp), block=False)
            except OverflowError as e:
                print(f'{dev_name} Worker: overload: {e}', file=sys.stderr)
            except Full:
                pass
            except Exception as e:
                errors += 1
                msg = str(e)
                if msg != last_msg:
                    if repeats > 1:
                        print(f'{dev_name} Worker: previous error repeated '
                              f'{repeats} times', file=sys.stderr)
                    print(f'{dev_name} Worker: error: {msg}', file=sys.stderr)
                    last_msg, repeats = msg, 1
                else:
                    repeats += 1
                    if repeats % self.ERROR_REPEAT_REPORT == 0:
                        print(f'{dev_name} Worker: error: {msg} '
                              f'(repeated {repeats} times)', file=sys.stderr)
                delay = min(self.ERROR_BACKOFF_MIN * 2 ** min(errors - 1, 16),
                            self.ERROR_BACKOFF_MAX)
                self._stop.wait(delay)
