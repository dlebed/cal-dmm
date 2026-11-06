"""RS-232C support for the Fluke 5700A (guide §2.5, [OM Chapter 6]).

Covers the :class:`SerialTransport` framing, the four behaviours that differ
from GPIB (Control/C for Device Clear, ``*STB?`` for the serial poll,
``REMOTE`` for REN, ``COMP`` for a parseable response style) and the session
work in :meth:`Fluke5700A.open` that arranges them.
"""

import inspect
from unittest.mock import patch

import pytest

from caldmm.drivers import fluke5700a as f57
from caldmm.drivers.fluke5700a import (
    DEFAULT_SERIAL_BAUD,
    DEFAULT_TIMEOUT_SEC,
    Fluke5700A,
    Fluke5700ACommError,
    Fluke5700AError,
    Fluke5700AProtocolError,
    ISR_REMOTE,
    SerialTransport,
    Transport,
    parse_sp_set,
)

from tests.drivers.fake_serial import FakeSerialPort


GOOD_IDN = "FLUKE,5720A,5248000,1.0+B+*"
FACTORY_SP_SET = "9600,TERM,XON,DBIT8,SBIT1,PNONE,CRLF"
COMP_SP_SET = "9600,COMP,XON,DBIT8,SBIT1,PNONE,CRLF"


def default_responses(*, idn=GOOD_IDN, isr=None, faults=None,
                      opt='"5725A Attached"', sp_set=COMP_SP_SET):
    """Responses that let a serial :meth:`Fluke5700A.open` complete."""
    return {
        "SP_SET?": sp_set,
        # A plain string answers every FAULT?, not just the drain in open():
        # a session that goes on to source needs the queue readable again.
        "FAULT?": list(faults) if faults is not None else "0",
        "*IDN?": idn,
        "*OPT?": opt,
        "ISR?": str(ISR_REMOTE) if isr is None else str(isr),
    }


def make_transport(responses=None, **kwargs):
    """Return ``(transport, port)`` with ``serial.Serial`` faked out."""
    port = FakeSerialPort(responses or {})
    with patch.object(f57, "serial") as mock_serial:
        mock_serial.Serial.return_value = port
        transport = SerialTransport("/dev/ttyUSB0", **kwargs)
    return transport, port


def open_calibrator(**kwargs):
    """Return ``(driver, port)`` for an opened serial session."""
    response_keys = ("idn", "isr", "faults", "opt", "sp_set")
    port = FakeSerialPort(default_responses(**{
        k: v for k, v in kwargs.items() if k in response_keys}))
    driver_kwargs = {k: v for k, v in kwargs.items()
                     if k not in response_keys}
    with patch.object(f57, "serial") as mock_serial:
        mock_serial.Serial.return_value = port
        driver = Fluke5700A(serial_port="/dev/ttyUSB0", **driver_kwargs)
    return driver, port


def commands(port):
    """Commands written, in order, terminators and Control/C removed."""
    return list(port.log)


# ── transport construction ────────────────────────────────────────────

class TestSerialTransportConstruction:
    def test_opens_the_port_with_the_factory_defaults(self):
        # [OM Table 6-1]: 9600 8N1, XON/XOFF. A port that disagrees with the
        # calibrator's own stall method drops bytes on a long write.
        port = FakeSerialPort()
        with patch.object(f57, "serial") as mock_serial:
            mock_serial.Serial.return_value = port
            SerialTransport("/dev/ttyUSB0")
        kwargs = mock_serial.Serial.call_args.kwargs
        assert kwargs["port"] == "/dev/ttyUSB0"
        assert kwargs["baudrate"] == DEFAULT_SERIAL_BAUD == 9600
        assert kwargs["timeout"] == DEFAULT_TIMEOUT_SEC
        assert kwargs["xonxoff"] is True
        assert kwargs["rtscts"] is False
        assert kwargs["bytesize"] is mock_serial.EIGHTBITS
        assert kwargs["parity"] is mock_serial.PARITY_NONE
        assert kwargs["stopbits"] is mock_serial.STOPBITS_ONE

    def test_baud_and_flow_control_are_overridable(self):
        port = FakeSerialPort()
        with patch.object(f57, "serial") as mock_serial:
            mock_serial.Serial.return_value = port
            SerialTransport("/dev/ttyS0", 19200, xonxoff=False, rtscts=True)
        kwargs = mock_serial.Serial.call_args.kwargs
        assert kwargs["baudrate"] == 19200
        assert (kwargs["xonxoff"], kwargs["rtscts"]) == (False, True)

    def test_declares_itself_serial(self):
        assert SerialTransport.is_serial is True
        assert f57.GpibTransport.is_serial is False


# ── framing ───────────────────────────────────────────────────────────

class TestSerialTransportFraming:
    def test_write_appends_exactly_one_terminator(self):
        # The input EOL is "CR or LF" (guide §2.5 item 2), so CR LF would be
        # two terminators and the second would delimit an empty command.
        transport, port = make_transport()
        transport.write("OUT 1.0E+01 V")
        assert port.raw_writes == [b"OUT 1.0E+01 V\n"]

    def test_the_terminator_is_overridable(self):
        transport, port = make_transport(eol="\r")
        transport.write("STBY")
        assert port.raw_writes == [b"STBY\r"]

    def test_read_decodes_and_strips(self):
        transport, port = make_transport({"OUT?": "1.0E+01,V,0"})
        assert transport.query("OUT?") == "1.0E+01,V,0"

    def test_a_timeout_raises_rather_than_returning_empty(self):
        # read_block() uses this exception as its end-of-block signal, and
        # query() reports a silent link as an error rather than as "".
        transport, _ = make_transport()
        with pytest.raises(TimeoutError):
            transport.read()

    def test_a_timeout_is_not_a_driver_error(self):
        # read_block() re-raises Fluke5700AError instead of treating it as
        # end-of-block, so the transport must not raise one.
        transport, _ = make_transport()
        with pytest.raises(TimeoutError) as excinfo:
            transport.read()
        assert not isinstance(excinfo.value, Fluke5700AError)

    def test_a_blank_line_is_not_a_timeout(self):
        # An option-less *OPT? may legitimately answer with a blank line
        # (gap G23); _query_raw() is written to accept that.
        transport, _ = make_transport({"*OPT?": ""})
        assert transport.query("*OPT?") == ""

    def test_query_drops_stale_input_before_asking(self):
        # Serial bytes arrive unbidden: a leftover line would otherwise be
        # returned as the answer to the next query.
        transport, port = make_transport({"ISR?": "2048"})
        transport.query("ISR?")
        assert port.events.index(("reset_input", None)) < port.events.index(
            ("write", "ISR?"))


# ── the four GPIB-isms that differ ────────────────────────────────────

class TestSerialTransportSubstitutions:
    def test_clear_sends_control_c(self):
        # Control/C performs the same function as DCL/SDC (guide §2.5 item 1).
        transport, port = make_transport()
        transport.clear()
        assert port.raw_writes == [b"\x03"]
        assert port.input_flushes == 1

    def test_serial_poll_reads_stb(self):
        # No hardware serial poll exists here; *STB? is the substitute, and
        # over serial bit 6 is MSS, so nothing is left latched (guide §8.2).
        transport, port = make_transport({"*STB?": "72"})
        assert transport.serial_poll() == 72
        assert commands(port) == ["*STB?"]

    def test_local_sends_the_serial_only_command(self):
        transport, port = make_transport()
        transport.local()
        assert commands(port) == ["LOCAL"]

    def test_remote_sends_the_serial_only_command(self):
        transport, port = make_transport()
        transport.remote()
        assert commands(port) == ["REMOTE"]

    def test_set_timeout_assigns_seconds_not_a_tcode(self):
        transport, port = make_transport()
        transport.set_timeout(2.0)
        assert port.timeout == 2.0

    def test_close_closes_the_port(self):
        transport, port = make_transport()
        transport.close()
        assert port.closed is True


TRANSPORT_MEMBERS = ("write", "read", "query", "serial_poll", "set_timeout",
                     "clear", "close")


def test_serial_transport_satisfies_the_protocol():
    transport, _ = make_transport()
    assert isinstance(transport, Transport)


@pytest.mark.parametrize("member", TRANSPORT_MEMBERS)
def test_serial_transport_signatures_match_protocol(member):
    def params(func):
        return [(p.name, p.kind) for p in
                inspect.signature(func).parameters.values()][1:]   # drop self

    assert params(getattr(SerialTransport, member)) == params(
        getattr(Transport, member))


# ── SP_SET? parser ────────────────────────────────────────────────────

class TestParseSpSet:
    def test_the_factory_default_string(self):
        setup = parse_sp_set(FACTORY_SP_SET)
        assert setup.baud == 9600
        assert setup.style == "TERM"
        assert setup.stall == "XON"
        assert (setup.data_bits, setup.stop_bits) == ("DBIT8", "SBIT1")
        assert setup.parity == "PNONE"
        assert setup.eol == "CRLF"

    def test_term_style_is_not_parseable(self):
        assert parse_sp_set(FACTORY_SP_SET).parseable is False
        assert parse_sp_set(COMP_SP_SET).parseable is True

    def test_only_a_cr_eol_is_unreadable(self):
        def eol(text):
            return parse_sp_set(
                f"9600,COMP,XON,DBIT8,SBIT1,PNONE,{text}").readable_lines

        assert eol("CRLF") is True
        assert eol("LF") is True
        assert eol("CR") is False

    def test_fields_are_case_folded(self):
        assert parse_sp_set(COMP_SP_SET.lower()).style == "COMP"

    def test_a_malformed_response_blames_term_style(self):
        # The overwhelmingly likely cause, and the one with a fix attached.
        with pytest.raises(Fluke5700AProtocolError) as excinfo:
            parse_sp_set("Baud rate is 9600, responses are for a terminal")
        assert "TERM" in str(excinfo.value)
        assert excinfo.value.response.startswith("Baud rate")


# ── session ───────────────────────────────────────────────────────────

class TestSerialOpen:
    def test_remote_and_comp_precede_the_first_parsed_response(self):
        # REMOTE because there is no REN line; SP_SET COMP because the
        # factory response style is prose. Both must land before *IDN?.
        _, port = open_calibrator()
        sent = commands(port)
        assert sent[:4] == ["REMOTE", "SP_SET COMP", "SP_SET?", "*CLS"]
        assert sent.index("SP_SET COMP") < sent.index("*IDN?")

    def test_comp_is_sent_unconditionally_not_conditionally(self):
        # Reading SP_SET? first to decide would mean parsing the very prose
        # being escaped from, so the write comes first even when the port is
        # already COMP.
        _, port = open_calibrator(sp_set=COMP_SP_SET)
        assert commands(port).index("SP_SET COMP") < commands(port).index(
            "SP_SET?")

    def test_the_port_configuration_is_recorded(self):
        driver, _ = open_calibrator()
        assert driver.serial_setup.style == "COMP"
        assert driver.serial_setup.stall == "XON"

    def test_a_port_still_in_term_style_is_refused(self):
        with pytest.raises(Fluke5700AProtocolError) as excinfo:
            open_calibrator(sp_set=FACTORY_SP_SET)
        message = str(excinfo.value)
        assert "TERM" in message
        assert "Remote" in message and "COMPUTER" in message

    def test_a_cr_only_eol_is_refused_with_the_fix(self):
        with pytest.raises(Fluke5700AError) as excinfo:
            open_calibrator(sp_set="9600,COMP,XON,DBIT8,SBIT1,PNONE,CR")
        assert "CRLF" in str(excinfo.value)

    def test_the_not_in_remote_error_does_not_mention_ren(self):
        # There is no REN line over serial; advising the operator to assert
        # one sends them looking for a bus that is not there.
        with pytest.raises(Fluke5700AError) as excinfo:
            open_calibrator(isr=0)
        message = str(excinfo.value)
        assert "REN" not in message
        assert "REMOTE" in message and "RS-232C Port Setup" in message

    def test_the_link_is_released_when_open_fails(self):
        port = FakeSerialPort(default_responses(idn="HP,3458A,0,0"))
        with patch.object(f57, "serial") as mock_serial:
            mock_serial.Serial.return_value = port
            with pytest.raises(Fluke5700AProtocolError):
                Fluke5700A(serial_port="/dev/ttyUSB0")
        assert port.closed is True

    def test_srq_is_refused_before_the_port_is_opened(self):
        # No SRQ capability exists over serial (guide §2.5 item 5). Refusing
        # after construction would leak an open file descriptor.
        with patch.object(f57, "serial") as mock_serial:
            with pytest.raises(ValueError) as excinfo:
                Fluke5700A(serial_port="/dev/ttyUSB0", srq=True)
            mock_serial.Serial.assert_not_called()
        assert "SRQ" in str(excinfo.value)

    def test_srq_is_refused_on_an_injected_serial_transport_too(self):
        transport, _ = make_transport()
        with pytest.raises(ValueError):
            Fluke5700A(transport=transport, srq=True)

    def test_the_session_reports_which_interface_it_is_on(self):
        driver, _ = open_calibrator()
        assert driver.is_serial is True


class TestSerialClose:
    def test_close_returns_the_front_panel_to_local(self):
        driver, port = open_calibrator()
        driver.close()
        assert commands(port)[-1] == "LOCAL"
        assert port.closed is True


class TestSerialSessionBehaviour:
    def test_a_block_response_reads_to_its_end(self):
        # The end-of-block signal is a read timeout, which on this transport
        # means readline() returning b"" — the reason read() must raise.
        driver, port = open_calibrator()
        port.script("CAL_CLST?", [['"', "DCV 10V 1.0", "DCV 1V 0.9", '"']])
        assert driver.read_block("CAL_CLST?") == ["DCV 10V 1.0", "DCV 1V 0.9"]

    def test_an_unanswered_query_is_a_comm_error(self):
        driver, port = open_calibrator()
        port.script("ONTIME?", [])
        with pytest.raises(Fluke5700ACommError):
            driver.query("ONTIME?")

    def test_output_control_is_unchanged_by_the_interface(self):
        driver, port = open_calibrator()
        driver.set_output(10.0, "V")
        assert "OUT 1.0000000000E+01 V, 0.000000E+00 HZ" in commands(port)


class TestGpibIsUnaffected:
    def test_a_gpib_session_sends_no_serial_only_commands(self):
        # REMOTE, LOCAL and LOCKOUT are fault 2211 over GPIB (guide §7.3),
        # and SP_SET COMP would be a pointless NVRAM write there.
        from tests.drivers.fake_gpib import FakeTransport

        transport = FakeTransport({
            "FAULT?": ["0"], "*IDN?": GOOD_IDN, "*OPT?": "",
            "ISR?": str(ISR_REMOTE)})
        driver = Fluke5700A(transport=transport)
        assert driver.is_serial is False
        assert driver.serial_setup is None
        assert not [cmd for cmd in transport.log
                    if cmd.startswith(("REMOTE", "SP_SET"))]

    def test_the_gpib_not_in_remote_error_still_explains_ren(self):
        from tests.drivers.fake_gpib import FakeTransport

        transport = FakeTransport({
            "FAULT?": ["0"], "*IDN?": GOOD_IDN, "*OPT?": "", "ISR?": "0"})
        with pytest.raises(Fluke5700AError) as excinfo:
            Fluke5700A(transport=transport)
        assert "REN" in str(excinfo.value)
