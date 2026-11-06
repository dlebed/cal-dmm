"""Tests for caldmm.drivers.fluke5700a (A1: transport, parsers, session;
A2: output control, settling and the offline envelope).

Offline only — no hardware, no linux-gpib. Behavioural tests use
:class:`tests.drivers.fake_gpib.FakeTransport` rather than the root
``conftest.py`` ``MagicMock`` stub, which cannot return parseable strings.
"""

import inspect
from unittest.mock import patch

import pytest

from caldmm.drivers import fluke5700a as f57
from caldmm.drivers.fluke5700a import (
    CUR_POSTS,
    FAULT_CODES,
    Fluke5700A,
    Fluke5700ACommError,
    Fluke5700AError,
    Fluke5700AProtocolError,
    CalibratorFault,
    GpibTransport,
    ISR_OPER,
    ISR_REMOTE,
    ISR_RLOCK,
    ISR_SETTLED,
    STATE_NAMES,
    STATE_TRIPPED,
    Transport,
    _tcode_for_seconds,
    decode_esr,
    decode_isr,
    decode_stb,
    expected_range_id,
    expected_settle_seconds,
    explain_fault,
    parse_idn,
    parse_opt,
    parse_out,
    parse_state,
    parse_uncert,
    read_block,
    standby_verify_timeout,
    validate_output,
)

from tests.drivers.fake_gpib import FakeTransport, stateful_isr


GOOD_IDN = "FLUKE,5720A,5248000,1.0+B+*"


def default_responses(*, idn=GOOD_IDN, isr=None, faults=None,
                      opt='"5725A Attached"'):
    """Responses that let :meth:`Fluke5700A.open` complete successfully."""
    return {
        "FAULT?": list(faults) if faults is not None else ["0"],
        "*IDN?": idn,
        "*OPT?": opt,
        "ISR?": str(ISR_REMOTE) if isr is None else str(isr),
    }


def open_calibrator(**kwargs):
    """Return ``(driver, transport)`` for an opened session."""
    transport = FakeTransport(default_responses(**{
        k: v for k, v in kwargs.items()
        if k in ("idn", "isr", "faults", "opt")}))
    driver_kwargs = {k: v for k, v in kwargs.items()
                     if k not in ("idn", "isr", "faults", "opt")}
    return Fluke5700A(transport=transport, **driver_kwargs), transport


# ── T-codes ───────────────────────────────────────────────────────────

class TestTcodeForSeconds:
    @pytest.mark.parametrize("seconds,code", [
        (0.5, 11),
        (1, 11),
        (1.5, 12),
        (10, 13),
        (45, 15),
        (100, 15),
        (300, 16),
        (5000, 17),
    ])
    def test_boundaries(self, seconds, code):
        assert _tcode_for_seconds(seconds) == code

    def test_sub_second_codes(self):
        # 0.1 s is exactly T-code 9 (100 ms); 0.2 s needs T-code 10 (300 ms).
        assert _tcode_for_seconds(0.1) == 9
        assert _tcode_for_seconds(0.2) == 10

    def test_default_session_timeout_is_code_15(self):
        assert _tcode_for_seconds(f57.DEFAULT_TIMEOUT_SEC) == 15

    @pytest.mark.parametrize("seconds", [0, -1, -0.001])
    def test_non_positive_rejected(self, seconds):
        with pytest.raises(ValueError):
            _tcode_for_seconds(seconds)


class TestGpibTransport:
    def test_constructs_with_tcode_not_seconds(self):
        with patch.object(f57, "Gpib") as mock_gpib:
            GpibTransport(4, 0, timeout=45.0)
        mock_gpib.Gpib.assert_called_once_with(0, 4, timeout=15)

    def test_read_decodes_and_strips_bytes(self):
        with patch.object(f57, "Gpib") as mock_gpib:
            transport = GpibTransport()
            mock_gpib.Gpib.return_value.read.return_value = b"  1.0E+01,V,0\n"
            assert transport.read() == "1.0E+01,V,0"

    def test_set_timeout_converts_to_tcode(self):
        with patch.object(f57, "Gpib") as mock_gpib:
            transport = GpibTransport()
            transport.set_timeout(2.0)
        mock_gpib.Gpib.return_value.timeout.assert_called_once_with(12)


TRANSPORT_MEMBERS = ("write", "read", "query", "serial_poll", "set_timeout",
                     "clear", "close")


def test_fake_transport_satisfies_protocol():
    # runtime_checkable only compares method *names*, so check the signatures
    # too -- a fake with the right names and the wrong arity would otherwise
    # pass here and fail only deep inside a driver test.
    assert isinstance(FakeTransport(), Transport)
    assert sorted(TRANSPORT_MEMBERS) == sorted(
        name for name in vars(Transport)
        if not name.startswith("_") and callable(getattr(Transport, name)))


@pytest.mark.parametrize("member", TRANSPORT_MEMBERS)
def test_fake_transport_signatures_match_protocol(member):
    def params(func):
        return [(p.name, p.kind) for p in
                inspect.signature(func).parameters.values()][1:]   # drop self

    assert params(getattr(FakeTransport, member)) == params(
        getattr(Transport, member))


@pytest.mark.parametrize("member", TRANSPORT_MEMBERS)
def test_gpib_transport_signatures_match_protocol(member):
    def params(func):
        return [(p.name, p.kind) for p in
                inspect.signature(func).parameters.values()][1:]

    assert params(getattr(GpibTransport, member)) == params(
        getattr(Transport, member))


# ── exceptions ────────────────────────────────────────────────────────

class TestCalibratorFault:
    def test_single_code_rendering(self):
        exc = CalibratorFault([(3111, "A15: HV +DC High Voltage Output Fault")])
        assert str(exc) == (
            "5700A fault 3111: A15: HV +DC High Voltage Output Fault")

    def test_multiple_codes_all_listed(self):
        exc = CalibratorFault([(3111, "A15: HV +DC High Voltage Output Fault"),
                               (2213, "Remote Only")])
        text = str(exc)
        assert "3111" in text and "2213" in text
        assert "Remote Only" in text
        assert text.startswith("5700A faults (2):")

    def test_codes_attribute_preserved_in_order(self):
        exc = CalibratorFault([(1, "a"), (2, "b")])
        assert exc.codes == [(1, "a"), (2, "b")]

    def test_is_a_driver_error(self):
        assert isinstance(CalibratorFault([]), Fluke5700AError)


def test_protocol_error_carries_the_text():
    exc = Fluke5700AProtocolError("bad", "garbage,response")
    assert exc.response == "garbage,response"


# ── parsers ───────────────────────────────────────────────────────────

class TestParseOut:
    def test_dc_volts(self):
        assert parse_out("-1.520000E+01,V,0.000000E+00") == (-15.2, "V", 0.0)

    def test_bare_zero_frequency(self):
        assert parse_out("1.256983E+01,V,0") == (12.56983, "V", 0.0)

    def test_ac_current(self):
        amp, unit, freq = parse_out("1.883E-01,A,4.42E+02")
        assert (amp, unit, freq) == (0.1883, "A", 442.0)

    def test_ohms(self):
        assert parse_out("1.9E+06,OHM,0") == (1.9e6, "OHM", 0.0)

    def test_dbm_unit_accepted(self):
        assert parse_out("1.0E+01,DBM,1.0E+04")[1] == "DBM"

    @pytest.mark.parametrize("resp", [
        "1.0E+01,V",                  # too few fields
        "1.0E+01,V,0,extra",          # too many fields
        "not-a-number,V,0",           # bad amplitude
        "1.0E+01,V,later",            # bad frequency
        "",                           # empty
    ])
    def test_malformed_raises_protocol_error(self, resp):
        with pytest.raises(Fluke5700AProtocolError):
            parse_out(resp)

    def test_protocol_error_carries_the_response(self):
        with pytest.raises(Fluke5700AProtocolError) as info:
            parse_out("garbage")
        assert info.value.response == "garbage"


class TestParseUncert:
    def test_ppm(self):
        assert parse_uncert("8.5E+00,PPM,365") == (8.5, "PPM", 365)

    def test_no_spec_sentinel_returns_none(self):
        # guide §9 UNCERT?: -1.0 means "no specification available".
        value, units, days = parse_uncert("-1.0,PPM,365")
        assert value is None
        assert (units, days) == ("PPM", 365)

    def test_negative_uncertainty_other_than_sentinel_kept(self):
        assert parse_uncert("-2.0,PPM,90")[0] == -2.0

    def test_units_not_restricted_to_an_enum(self):
        # gap G22: the unit list in the manual ends in "etc.".
        assert parse_uncert("1.0E-06,SOMETHING,24")[1] == "SOMETHING"

    def test_exponential_interval_accepted(self):
        assert parse_uncert("8.5E+00,PPM,3.65E+02")[2] == 365

    @pytest.mark.parametrize("resp", [
        "8.5E+00,PPM",
        "eight,PPM,365",
        "8.5E+00,PPM,many",
        "8.5E+00,PPM,36.5",           # non-integral interval
        "",
    ])
    def test_malformed_raises_protocol_error(self, resp):
        with pytest.raises(Fluke5700AProtocolError):
            parse_uncert(resp)


class TestParseState:
    def test_operating(self):
        assert parse_state('0,"Operating"') == (0, "Operating")

    def test_tripped(self):
        code, substate = parse_state("18,Analog output tripped to dormant")
        assert code == STATE_TRIPPED
        assert substate == "Analog output tripped to dormant"

    def test_substate_may_be_absent(self):
        assert parse_state("19") == (19, "")

    def test_substate_may_contain_commas(self):
        assert parse_state("5,Calibration, +dc reference") == (
            5, "Calibration, +dc reference")

    @pytest.mark.parametrize("resp", ["nineteen,foo", "", ","])
    def test_malformed_raises_protocol_error(self, resp):
        with pytest.raises(Fluke5700AProtocolError):
            parse_state(resp)


class TestStateNames:
    def test_codes_0_to_19_present(self):
        assert sorted(STATE_NAMES) == list(range(20))

    def test_trip_state(self):
        assert STATE_TRIPPED == 18
        assert STATE_NAMES[18] == "Analog output tripped to dormant"

    def test_spot_checks(self):
        assert STATE_NAMES[0] == "Operating"
        assert STATE_NAMES[3] == "Calibration check"
        assert STATE_NAMES[13] == "Range calibration"
        assert STATE_NAMES[19] == "Starting up"


class TestParseIdn:
    def test_real_example(self):
        ident = parse_idn(GOOD_IDN)
        assert ident.manufacturer == "FLUKE"
        assert ident.model == "5720A"
        assert ident.serial == "5248000"
        assert ident.firmware == "1.0+B+*"
        assert ident.raw == GOOD_IDN

    def test_asterisk_means_no_amplifier(self):
        assert parse_idn(GOOD_IDN).has_amplifier is False

    def test_amplifier_firmware_detected(self):
        assert parse_idn("FLUKE,5700A,1234567,1.0+B+C").has_amplifier is True

    def test_firmware_fields_split(self):
        assert parse_idn(GOOD_IDN).firmware_fields == ["1.0", "B", "*"]

    @pytest.mark.parametrize("resp", ["FLUKE,5720A,5248000", ""])
    def test_malformed_raises_protocol_error(self, resp):
        with pytest.raises(Fluke5700AProtocolError):
            parse_idn(resp)


class TestParseOpt:
    def test_two_options(self):
        assert parse_opt('"5725A Attached","5215A Attached"') == [
            "5725A Attached", "5215A Attached"]

    def test_empty_response_is_no_options(self):
        # gap G23: the empty case is undefined; treat it as "none".
        assert parse_opt("") == []
        assert parse_opt('""') == []


# ── status decoders ───────────────────────────────────────────────────

class TestDecodeIsr:
    def test_remote_only(self):
        flags = decode_isr(2048)
        assert flags["REMOTE"] is True
        assert sum(flags.values()) == 1

    def test_settled_and_operating(self):
        flags = decode_isr(ISR_SETTLED | ISR_OPER)
        assert flags["SETTLED"] is True and flags["OPER"] is True
        assert flags["REMOTE"] is False

    def test_decimal_16_is_rcomp_not_rlock(self):
        # guide §13 item 14: the OM's own ISR? example mislabels 16 as RLOCK.
        flags = decode_isr(16)
        assert flags["RCOMP"] is True
        assert flags["RLOCK"] is False

    def test_bits_14_and_15_are_never_reported(self):
        assert len(decode_isr(0)) == 14
        assert set(decode_isr(0)) == {
            "RPTBUSY", "SETTLED", "REMOTE", "WBND", "SCALE", "OFFSET",
            "PLOCK", "PSHFT", "RLOCK", "RCOMP", "BOOST", "EXSENS",
            "EXGARD", "OPER"}

    def test_all_documented_bits_set(self):
        assert all(decode_isr(0x3FFF).values())

    def test_out_of_range_raises_protocol_error(self):
        with pytest.raises(Fluke5700AProtocolError):
            decode_isr(1 << 16)
        with pytest.raises(Fluke5700AProtocolError):
            decode_isr(-1)


class TestDecodeEsr:
    def test_guide_ese_140_example(self):
        # guide §8.3: *ESE 140 enables bits 2 (QYE), 3 (DDE) and 7 (PON).
        flags = decode_esr(140)
        assert flags["QYE"] and flags["DDE"] and flags["PON"]
        assert not flags["CME"] and not flags["EXE"] and not flags["OPC"]

    def test_opc(self):
        assert decode_esr(1)["OPC"] is True

    def test_bits_1_and_6_are_never_reported(self):
        assert set(decode_esr(0)) == {"PON", "CME", "EXE", "DDE", "QYE", "OPC"}

    def test_out_of_range_raises_protocol_error(self):
        with pytest.raises(Fluke5700AProtocolError):
            decode_esr(256)


class TestDecodeStb:
    def test_guide_stb_72_example(self):
        # guide §9 *STB?: 72 = bit 3 (EAV) + bit 6 (MSS).
        flags = decode_stb(72)
        assert flags["EAV"] is True
        assert flags["RQS_MSS"] is True
        assert sum(flags.values()) == 2

    def test_decimal_values_from_guide(self):
        assert decode_stb(4)["ISCB"] is True
        assert decode_stb(8)["EAV"] is True
        assert decode_stb(16)["MAV"] is True
        assert decode_stb(32)["ESB"] is True
        assert decode_stb(64)["RQS_MSS"] is True

    def test_bits_0_1_7_are_never_reported(self):
        assert set(decode_stb(0)) == {"RQS_MSS", "ESB", "MAV", "EAV", "ISCB"}

    def test_out_of_range_raises_protocol_error(self):
        with pytest.raises(Fluke5700AProtocolError):
            decode_stb(1 << 8)

    def test_non_integer_raises_protocol_error(self):
        with pytest.raises(Fluke5700AProtocolError):
            decode_stb("72")


# ── fault-code table ──────────────────────────────────────────────────

class TestFaultCodes:
    def test_a14_a15_diagnostic_range_complete(self):
        # guide §12.1, 3100-Level. Codes 3121 and 3134 are absent from
        # [OM Appendix A] itself.
        expected = set(range(3100, 3141)) - {3121, 3134}
        assert expected <= set(FAULT_CODES)

    def test_a14_spot_checks(self):
        assert FAULT_CODES[3111] == "A15: HV +DC High Voltage Output Fault"
        assert FAULT_CODES[3100] == "A14: 8255 Control Word"
        assert FAULT_CODES[3135] == "Assembly A14 Not Responding"
        assert FAULT_CODES[3140] == "A14/A15/A16: 2.2A AC Range Abs. Value"

    @pytest.mark.parametrize("code", [
        210, 222, 224, 227, 235, 812, 818, 819, 820, 838, 839, 845, 847,
        848, 850, 851, 852, 855, 860, 1301, 1302, 1303, 2002, 2004, 2005,
        2208, 2211, 2213, 2217,
    ])
    def test_driver_behaviour_codes_present(self, code):
        assert FAULT_CODES[code]

    def test_typos_transcribed_verbatim(self):
        # guide §13 item 31: match on the code, never the text.
        assert FAULT_CODES[831] == "Cannot Use 2-ire Comp Now"
        assert FAULT_CODES[2222] == "Invlaid Hexadecimal Block"
        assert FAULT_CODES[2202] == FAULT_CODES[2229]

    def test_explain_fault_offline(self):
        assert explain_fault(2213) == "Remote Only"
        assert "unknown" in explain_fault(999999).lower()

    def test_trip_codes_are_the_guide_11_7_set(self):
        assert f57.TRIP_FAULT_CODES == {
            210, 222, 224, 227, 235, 1301, 1302, 2004, 2005}
        assert f57.TRIP_FAULT_CODES <= set(FAULT_CODES)


# ── block reader (gap G1) ─────────────────────────────────────────────

class TestReadBlock:
    def test_terminates_on_quote_line(self):
        transport = FakeTransport(
            {"CAL_RPT? CAL": [['"', "line 1", "line 2", '"']]})
        transport.write("CAL_RPT? CAL")
        assert read_block(transport) == ["line 1", "line 2"]

    def test_terminates_on_quote_line_without_opening_delimiter(self):
        transport = FakeTransport({"CAL_SLST?": [["a", "b", '"']]})
        transport.write("CAL_SLST?")
        assert read_block(transport) == ["a", "b"]

    def test_timeout_after_at_least_one_line_ends_the_block(self):
        transport = FakeTransport({"CAL_SLST?": [["a", "b"]]})
        transport.write("CAL_SLST?")
        assert read_block(transport, line_timeout=0.1) == ["a", "b"]

    def test_timeout_with_zero_lines_raises_comm_error(self):
        transport = FakeTransport()
        transport.write("CAL_SLST?")
        with pytest.raises(Fluke5700ACommError):
            read_block(transport, line_timeout=0.1)

    def test_opening_quote_then_timeout_raises_comm_error(self):
        transport = FakeTransport({"CAL_SLST?": [['"']]})
        transport.write("CAL_SLST?")
        with pytest.raises(Fluke5700ACommError):
            read_block(transport, line_timeout=0.1)

    def test_stop_predicate_is_injectable(self):
        transport = FakeTransport({"X?": [["a", "END", "b"]]})
        transport.write("X?")
        assert read_block(transport, is_end=lambda ln: ln == "END") == ["a"]

    def test_line_timeout_is_applied_and_restored(self):
        transport = FakeTransport({"X?": [["a", '"']]})
        transport.write("X?")
        read_block(transport, line_timeout=0.5, restore_timeout=45.0)
        assert transport.timeouts == [0.5, 45.0]

    def test_max_lines_raises_rather_than_truncating(self):
        # A truncated CAL_CLST? must never be mistaken for a complete one.
        transport = FakeTransport({"X?": [["a"] * 50]})
        transport.write("X?")
        with pytest.raises(Fluke5700AProtocolError) as info:
            read_block(transport, max_lines=10)
        assert "max_lines=10" in str(info.value)

    def test_max_lines_exactly_reached_is_not_an_error(self):
        transport = FakeTransport({"X?": [["a"] * 10 + ['"']]})
        transport.write("X?")
        assert len(read_block(transport, max_lines=10)) == 10

    def test_driver_error_from_transport_is_not_end_of_block(self):
        # A3 must pass the raw transport, not a reader that wraps timeouts
        # into driver exceptions: those are re-raised, not treated as EOB.
        transport = FakeTransport(
            {"X?": [["a", Fluke5700ACommError("wrapped")]]})
        transport.write("X?")
        with pytest.raises(Fluke5700ACommError):
            read_block(transport)

    def test_timeout_is_not_restored_by_default(self):
        # Documented sharp edge; Fluke5700A.read_block() is the safe form.
        transport = FakeTransport({"X?": [["a", '"']]})
        transport.write("X?")
        read_block(transport, line_timeout=0.5)
        assert transport.timeouts == [0.5]


class _ChunkedTransport:
    """The framing a real 5700A uses (gap G1, settled on hardware).

    EOI is asserted **once, at the end of the block**, so linux-gpib returns
    fixed-size chunks with embedded newlines rather than lines. Reproduced
    here because it is the shape that made ``CAL_RPT?`` silently return an
    empty block: the opening ``"`` arrives glued to the front of chunk 0.
    """

    def __init__(self, text: str, chunk_size: int = 512):
        self._chunks = [text[i:i + chunk_size]
                        for i in range(0, len(text), chunk_size)] or [""]
        self._index = 0
        self.timeouts: "list[float]" = []

    def read_raw(self) -> str:
        if self._index >= len(self._chunks):
            raise TimeoutError("no more chunks")     # what EOI+idle looks like
        chunk = self._chunks[self._index]
        self._index += 1
        return chunk

    def read(self) -> str:                            # pragma: no cover
        raise AssertionError("read_block must use read_raw when it exists")

    def set_timeout(self, seconds: float) -> None:
        self.timeouts.append(seconds)


class TestReadBlockChunkedFraming:
    """Regression cover for the framing the 5700A actually uses."""

    REPORT = ('"\n\n    FLUKE CORPORATION      S/N 1234567\n'
              '    DC VOLTAGE OUTPUT SHIFTS\n'
              '     2.2V +FS   0.0000001  V  -1.46 ppm\n\n\n"\n')

    def test_a_block_split_across_chunks_is_reassembled(self):
        # chunk_size=16 splits mid-line many times over, which is exactly what
        # 512-byte chunks do to an 80-column report.
        transport = _ChunkedTransport(self.REPORT, chunk_size=16)
        assert read_block(transport) == [
            "",
            "    FLUKE CORPORATION      S/N 1234567",
            "    DC VOLTAGE OUTPUT SHIFTS",
            "     2.2V +FS   0.0000001  V  -1.46 ppm",
            "", ""]

    def test_the_opening_quote_glued_to_chunk_zero_is_not_the_end(self):
        # The original per-line loop returned [] here: chunk 0 starts with a
        # quote, the end predicate matched it, and an empty report looked like
        # a successful read.
        transport = _ChunkedTransport(self.REPORT, chunk_size=512)
        assert len(read_block(transport)) == 6

    def test_one_whole_block_in_a_single_chunk_still_ends_on_the_quote(self):
        transport = _ChunkedTransport('"\nalpha\nbeta\n"\n', chunk_size=4096)
        assert read_block(transport) == ["alpha", "beta"]

    def test_a_chunked_block_that_never_closes_ends_on_the_timeout(self):
        transport = _ChunkedTransport('"\nalpha\nbeta\n', chunk_size=8)
        assert read_block(transport, line_timeout=0.1) == ["alpha", "beta"]


class TestDriverReadBlock:
    def test_writes_the_query_then_reads_the_block(self):
        cal, transport = open_calibrator()
        transport.responses["CAL_RPT? CAL"] = [['"', "line 1", "line 2", '"']]
        transport.log.clear()
        assert cal.read_block("CAL_RPT? CAL") == ["line 1", "line 2"]
        assert transport.log == ["CAL_RPT? CAL"]

    def test_restores_the_session_timeout(self):
        cal, transport = open_calibrator()
        transport.responses["CAL_RPT? CAL"] = [["a", '"']]
        transport.timeouts.clear()
        cal.read_block("CAL_RPT? CAL")
        assert transport.timeouts == [f57.DEFAULT_BLOCK_LINE_TIMEOUT_SEC, 45.0]

    def test_restores_the_session_timeout_after_a_failure(self):
        cal, transport = open_calibrator(timeout=120.0)
        transport.timeouts.clear()
        with pytest.raises(Fluke5700ACommError):
            cal.read_block("CAL_RPT? CAL")     # nothing scripted -> timeout
        assert transport.timeouts[-1] == 120.0

    def test_keywords_are_forwarded(self):
        cal, transport = open_calibrator()
        transport.responses["X?"] = [["a", "END", "b"]]
        assert cal.read_block("X?", is_end=lambda ln: ln == "END") == ["a"]

    def test_restore_timeout_can_be_overridden(self):
        cal, transport = open_calibrator()
        transport.responses["X?"] = [["a", '"']]
        transport.timeouts.clear()
        cal.read_block("X?", restore_timeout=10.0)
        assert transport.timeouts[-1] == 10.0


# ── drain_faults (guide §8.5, §8.6) ───────────────────────────────────

class TestDrainFaults:
    def test_stops_at_zero_and_collects_in_order(self):
        cal, transport = open_calibrator(
            faults=["0", "3111", "2213", "0"])
        transport.responses["EXPLAIN? 3111"] = (
            '"A15: HV +DC High Voltage Output Fault"')
        transport.responses["EXPLAIN? 2213"] = "REMOTE ONLY"
        assert cal.drain_faults() == [
            (3111, "A15: HV +DC High Voltage Output Fault"),
            (2213, "REMOTE ONLY"),
        ]

    def test_empty_queue_returns_empty_list(self):
        cal, _ = open_calibrator(faults=["0", "0"])
        assert cal.drain_faults() == []

    def test_terminates_at_the_bound_without_raising(self):
        cal, transport = open_calibrator()
        transport.responses["FAULT?"] = "3111"          # never returns 0
        transport.responses["EXPLAIN? 3111"] = "A15: HV +DC ..."
        codes = cal.drain_faults(max_iterations=5)
        assert len(codes) == 5
        assert all(code == 3111 for code, _ in codes)

    def test_falls_back_to_offline_table_when_explain_fails(self):
        cal, _ = open_calibrator(faults=["0", "3111", "0"])
        # No EXPLAIN? response scripted -> read timeout -> offline lookup.
        assert cal.drain_faults() == [
            (3111, "A15: HV +DC High Voltage Output Fault")]

    def test_falls_back_when_explain_returns_only_quotes(self):
        cal, transport = open_calibrator(faults=["0", "2213", "0"])
        transport.responses["EXPLAIN? 2213"] = '""'
        assert cal.drain_faults() == [(2213, "Remote Only")]

    def test_explain_failure_latches_the_offline_fallback(self):
        # A unit that answers FAULT? but not EXPLAIN? must not be re-asked
        # once per fault at the full session timeout -- that reads as a hang.
        cal, transport = open_calibrator(
            faults=["0", "3111", "3135", "2213", "0"])
        transport.responses["EXPLAIN? 3135"] = "should never be asked"
        transport.responses["EXPLAIN? 2213"] = "should never be asked"
        transport.log.clear()
        codes = cal.drain_faults()          # EXPLAIN? 3111 is unscripted
        assert codes == [
            (3111, "A15: HV +DC High Voltage Output Fault"),
            (3135, "Assembly A14 Not Responding"),
            (2213, "Remote Only"),
        ]
        explains = [cmd for cmd in transport.log if cmd.startswith("EXPLAIN?")]
        assert explains == ["EXPLAIN? 3111"]

    def test_explain_keeps_being_used_while_it_works(self):
        cal, transport = open_calibrator(faults=["0", "3111", "2213", "0"])
        transport.responses["EXPLAIN? 3111"] = "HV OUTPUT FAULT"
        transport.responses["EXPLAIN? 2213"] = "REMOTE ONLY"
        transport.log.clear()
        cal.drain_faults()
        explains = [cmd for cmd in transport.log if cmd.startswith("EXPLAIN?")]
        assert explains == ["EXPLAIN? 3111", "EXPLAIN? 2213"]

    def test_does_not_raise_calibrator_fault(self):
        cal, transport = open_calibrator(faults=["0", "3111", "0"])
        transport.responses["EXPLAIN? 3111"] = "boom"
        # drain_faults reports; it must return the codes, never raise
        # CalibratorFault -- the raising wrapper belongs with the output API.
        assert cal.drain_faults() == [(3111, "boom")]


# ── session open (guide §14.1) ────────────────────────────────────────

class TestOpen:
    def test_exact_command_sequence(self):
        cal, transport = open_calibrator()
        assert transport.log == [
            "*CLS", "FAULT?", "*IDN?", "*OPT?", "*RST", "STBY", "ISR?"]
        assert cal.identity.model == "5720A"

    def test_full_event_order_including_clear_and_timeout(self):
        _, transport = open_calibrator()
        writes_and_setup = [event for event in transport.events
                            if event[0] in ("set_timeout", "clear", "write")]
        assert writes_and_setup == [
            ("set_timeout", 45.0),
            ("clear", None),
            ("write", "*CLS"),
            ("write", "FAULT?"),
            ("write", "*IDN?"),
            ("write", "*OPT?"),
            ("write", "*RST"),
            ("write", "STBY"),
            ("write", "ISR?"),
        ]

    def test_srq_enables_sre_8_last(self):
        _, transport = open_calibrator(srq=True)
        assert transport.log[-1] == "*SRE 8"

    def test_srq_off_by_default(self):
        _, transport = open_calibrator()
        assert "*SRE 8" not in transport.log

    def test_custom_timeout_is_applied(self):
        _, transport = open_calibrator(timeout=120.0)
        assert transport.timeouts[0] == 120.0

    def test_options_parsed(self):
        cal, _ = open_calibrator()
        assert cal.options == ["5725A Attached"]

    def test_blank_options_response_tolerated(self):
        # gap G23: the no-options case is undefined and may read back empty.
        cal, _ = open_calibrator(opt="")
        assert cal.options == []

    def test_options_timeout_is_not_swallowed(self):
        # A real timeout must not be mistaken for "no options": the late
        # *OPT? response would then be read as the ISR? answer.
        transport = FakeTransport(default_responses())
        del transport.responses["*OPT?"]
        with pytest.raises(Fluke5700ACommError):
            Fluke5700A(transport=transport)

    def test_startup_faults_recorded(self):
        cal, transport = open_calibrator(faults=["3111", "0"])
        assert cal.startup_faults == [
            (3111, "A15: HV +DC High Voltage Output Fault")]

    def test_name_stored_verbatim(self):
        cal, _ = open_calibrator(name="5720a-A")
        assert cal.name == "5720a-A"

    def test_default_gpib_address_is_4(self):
        with patch.object(f57, "Gpib") as mock_gpib:
            mock_gpib.Gpib.return_value.read.side_effect = (
                RuntimeError("no hardware"))
            with pytest.raises(Fluke5700ACommError):
                Fluke5700A()
        assert mock_gpib.Gpib.call_args.args == (0, 4)

    def test_raises_when_idn_is_not_a_fluke(self):
        with pytest.raises(Fluke5700AProtocolError) as info:
            open_calibrator(idn="HEWLETT-PACKARD,3458A,2823A12345,9,8")
        assert "not a Fluke" in str(info.value)

    def test_raises_when_a_fluke_idn_is_malformed(self):
        with pytest.raises(Fluke5700AProtocolError):
            open_calibrator(idn="FLUKE,5720A")

    def test_raises_when_remote_bit_is_clear(self):
        with pytest.raises(Fluke5700AError) as info:
            open_calibrator(isr=ISR_SETTLED)      # everything but REMOTE
        message = str(info.value)
        assert "Remote" in message and "REN" in message and "2213" in message

    def test_remote_check_accepts_other_bits_set(self):
        cal, _ = open_calibrator(isr=ISR_REMOTE | ISR_SETTLED | ISR_OPER)
        assert cal.identity.serial == "5248000"

    def test_dead_link_raises_comm_error(self):
        transport = FakeTransport()               # nothing scripted at all
        with pytest.raises(Fluke5700ACommError):
            Fluke5700A(transport=transport)

    def test_failed_open_closes_the_transport_without_shutdown_commands(self):
        transport = FakeTransport(default_responses(
            idn="HEWLETT-PACKARD,3458A,0,9,8"))
        with pytest.raises(Fluke5700AProtocolError):
            Fluke5700A(transport=transport)
        assert transport.closed is True
        # The wrong instrument must not be sent the §14.4 shutdown sequence.
        assert "STBY" not in transport.log


# ── session close (guide §14.4) ───────────────────────────────────────

class TestClose:
    def test_command_sequence(self):
        cal, transport = open_calibrator(faults=["0", "0"])
        transport.log.clear()
        cal.close()
        assert transport.log == ["STBY", "FAULT?", "*CLS"]
        assert transport.closed is True

    def test_still_sends_stby_and_closes_when_drain_raises(self):
        cal, transport = open_calibrator()
        transport.log.clear()
        # FAULT? queue is exhausted after open -> the drain read times out.
        cal.close()
        assert "STBY" in transport.log
        assert "*CLS" in transport.log
        assert transport.closed is True

    def test_still_closes_when_stby_raises(self):
        cal, transport = open_calibrator(faults=["0", "0"])

        def explode(_cmd):
            raise RuntimeError("bus wedged")

        transport.write = explode
        cal.close()
        assert transport.closed is True

    def test_shutdown_faults_recorded_not_raised(self):
        cal, transport = open_calibrator(faults=["0", "1303", "0"])
        cal.close()
        assert cal.shutdown_faults == [
            (1303, "DC Zero Calibration is Needed (Every 30 Days)")]

    def test_context_manager_closes(self):
        transport = FakeTransport(default_responses(faults=["0", "0"]))
        with Fluke5700A(transport=transport) as cal:
            assert cal.identity.model == "5720A"
        assert transport.closed is True

    def test_context_manager_closes_on_exception(self):
        transport = FakeTransport(default_responses(faults=["0", "0"]))
        with pytest.raises(ZeroDivisionError):
            with Fluke5700A(transport=transport):
                raise ZeroDivisionError
        assert transport.closed is True


# ── low-level I/O wrappers ────────────────────────────────────────────

class TestLowLevelIo:
    def test_query_strips_response(self):
        cal, transport = open_calibrator()
        transport.responses["OUT?"] = "  1.0E+00,V,0  "
        assert cal.query("OUT?") == "1.0E+00,V,0"

    def test_empty_response_raises_comm_error(self):
        cal, transport = open_calibrator()
        transport.responses["OUT?"] = "   "
        with pytest.raises(Fluke5700ACommError):
            cal.query("OUT?")

    def test_query_int(self):
        cal, transport = open_calibrator()
        transport.responses["ISCR?"] = "8"
        assert cal.query_int("ISCR?") == 8

    def test_query_int_rejects_non_integer(self):
        cal, transport = open_calibrator()
        transport.responses["ISCR?"] = "eight"
        with pytest.raises(Fluke5700AProtocolError):
            cal.query_int("ISCR?")

    def test_serial_poll_returns_stb(self):
        cal, transport = open_calibrator()
        transport.stb = 72
        assert decode_stb(cal.serial_poll())["EAV"] is True

    def test_write_failure_becomes_comm_error(self):
        cal, transport = open_calibrator()

        def explode(_cmd):
            raise RuntimeError("bus wedged")

        transport.write = explode
        with pytest.raises(Fluke5700ACommError):
            cal.cmd("*CLS")


# ══════════════════════════════════════════════════════════════════════
# Task A2 — output control, settling, offline envelope
# ══════════════════════════════════════════════════════════════════════

class FakeClock:
    """Virtual monotonic clock. Sleeps advance it and are logged.

    Shares the transport's ``events`` list so a test can assert the relative
    order of sleeping and querying -- which is the whole point of the 60 ms
    settle guard.
    """

    def __init__(self, events):
        self.now = 0.0
        self.events = events

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds
        self.events.append(("sleep", seconds))


def slept_before_first(events, command):
    """Total virtual sleep before the first write of *command*."""
    total = 0.0
    for kind, payload in events:
        if kind == "write" and payload == command:
            return total
        if kind == "sleep":
            total += payload
    raise AssertionError(f"{command!r} was never written: {events!r}")


# ── set_output (guide §7.3, §14.2) ────────────────────────────────────

class TestSetOutput:
    def test_dc_emits_one_out_with_amplitude_and_zero_hz(self):
        cal, transport = open_calibrator(faults=["0", "0"])
        transport.log.clear()
        cal.set_output(10.0, "V")
        assert transport.log == [
            "STBY",
            "ISR?",                                    # RLOCK check
            "OUT 1.0000000000E+01 V, 0.000000E+00 HZ",
            "FAULT?",
        ]
        assert len([c for c in transport.log if c.startswith("OUT")]) == 1

    def test_dc_command_carries_no_multiplier_prefix(self):
        # guide §3.6: 'MA' alone is milliamps but 'MAOHM'/'MAHZ' are mega.
        cal, transport = open_calibrator(faults=["0", "0"])
        transport.log.clear()
        cal.set_output(1.883e-1, "A")
        out = [c for c in transport.log if c.startswith("OUT")][0]
        assert out == "OUT 1.8830000000E-01 A, 0.000000E+00 HZ"
        assert " MA" not in out and " UA" not in out and " K" not in out

    def test_negative_dc_amplitude(self):
        cal, transport = open_calibrator(faults=["0", "0"])
        transport.log.clear()
        cal.set_output(-15.2, "V")
        assert "OUT -1.5200000000E+01 V, 0.000000E+00 HZ" in transport.log

    def test_ac_emits_one_out_with_both_fields(self):
        cal, transport = open_calibrator(faults=["0", "0"])
        transport.log.clear()
        cal.set_output(1.0, "V", 1000.0)
        assert transport.log == [
            "STBY",
            "ISR?",
            "OUT 1.0000000000E+00 V, 1.000000E+03 HZ",
            "FAULT?",
        ]

    def test_ohms_emits_no_hz_token_at_all(self):
        cal, transport = open_calibrator(faults=["0", "0"])
        transport.log.clear()
        cal.set_output(10e3, "OHM")
        assert transport.log == [
            "STBY", "ISR?", "OUT 1.0000000000E+04 OHM", "FAULT?"]
        assert not any("HZ" in cmd for cmd in transport.log)

    def test_ohms_with_a_frequency_raises_and_emits_nothing(self):
        # fault 812 "Cannot Set Frequency With Ohms" -- never even attempted.
        cal, transport = open_calibrator(faults=["0", "0"])
        transport.log.clear()
        with pytest.raises(ValueError) as info:
            cal.set_output(100.0, "OHM", 50.0)
        assert "812" in str(info.value)
        assert transport.log == []

    def test_range_lock_cleared_before_the_out_not_after(self):
        # guide §7.3 worked case 2: RANGELCK OFF *after* the OUT faults.
        cal, transport = open_calibrator(faults=["0", "0"])
        transport.script("ISR?", str(ISR_REMOTE | ISR_RLOCK))
        transport.log.clear()
        cal.set_output(10.0, "V")
        assert transport.log == [
            "STBY",
            "ISR?",
            "RANGELCK OFF",
            "OUT 1.0000000000E+01 V, 0.000000E+00 HZ",
            "FAULT?",
        ]
        assert (transport.log.index("RANGELCK OFF")
                < transport.log.index("OUT 1.0000000000E+01 V, "
                                      "0.000000E+00 HZ"))

    def test_range_lock_off_not_sent_when_the_range_is_unlocked(self):
        cal, transport = open_calibrator(faults=["0", "0"])
        transport.log.clear()
        cal.set_output(10.0, "V")
        assert "RANGELCK OFF" not in transport.log

    def test_envelope_violation_raises_before_any_io(self):
        cal, transport = open_calibrator(faults=["0", "0"])
        transport.log.clear()
        with pytest.raises(ValueError) as info:
            cal.set_output(5000.0, "V")
        assert "1100" in str(info.value)
        assert transport.log == []

    def test_every_violation_is_listed(self):
        cal, _ = open_calibrator(faults=["0", "0"], opt="")
        with pytest.raises(ValueError) as info:
            cal.set_output(5000.0, "OHM", 60.0)
        message = str(info.value)
        assert "812" in message and "catalogue" in message

    def test_fault_after_out_raises_calibrator_fault(self):
        cal, transport = open_calibrator(faults=["0", "812", "0"])
        transport.log.clear()
        with pytest.raises(CalibratorFault) as info:
            cal.set_output(10.0, "V")
        assert info.value.codes[0][0] == 812
        assert any(c.startswith("OUT") for c in transport.log)

    def test_does_not_energize(self):
        # Sourcing and energizing are separate steps (guide §14.2).
        cal, transport = open_calibrator(faults=["0", "0"])
        transport.log.clear()
        cal.set_output(1000.0, "V")
        assert "OPER" not in transport.log

    def test_unit_is_case_insensitive(self):
        cal, transport = open_calibrator(faults=["0", "0"])
        transport.log.clear()
        cal.set_output(10e3, "ohm")
        assert "OUT 1.0000000000E+04 OHM" in transport.log


# ── operate / standby (guide §8.6, §11.7) ─────────────────────────────

class TestOperate:
    def test_drains_before_oper_and_reads_isr_after(self):
        cal, transport = open_calibrator(faults=["0", "0"])
        transport.script("ISR?", str(ISR_REMOTE | ISR_OPER))
        transport.log.clear()
        cal.operate(settle=False)
        assert transport.log == ["FAULT?", "OPER", "ISR?"]

    def test_pending_fault_raises_and_oper_is_never_written(self):
        cal, transport = open_calibrator(faults=["0", "3111", "0"])
        transport.log.clear()
        with pytest.raises(CalibratorFault) as info:
            cal.operate(settle=False)
        assert info.value.codes[0][0] == 3111
        assert "OPER" not in transport.log

    def test_raises_when_oper_bit_is_clear_after_oper(self):
        # The trip case (guide §11.7). Without this check a driver records a
        # meter reading zero volts as a 100 % error.
        cal, transport = open_calibrator(faults=["0", "0", "1301", "0"])
        transport.script("ISR?", str(ISR_REMOTE))          # OPER clear
        transport.log.clear()
        with pytest.raises(Fluke5700AError) as info:
            cal.operate(settle=False)
        message = str(info.value)
        assert "OPER" in message and "1301" in message
        assert not isinstance(info.value, CalibratorFault)
        assert "STBY" in transport.log

    def test_settles_before_checking_the_oper_bit(self, monkeypatch):
        cal, transport = open_calibrator(faults=["0", "0"])
        clock = FakeClock(transport.events)
        monkeypatch.setattr(f57, "time", clock)
        transport.script("ISR?", [str(ISR_REMOTE),
                                  str(ISR_REMOTE | ISR_SETTLED),
                                  str(ISR_REMOTE | ISR_SETTLED | ISR_OPER)])
        transport.log.clear()
        cal.operate()
        assert transport.log == ["FAULT?", "OPER", "ISR?", "ISR?", "ISR?"]

    def test_settle_timeout_drops_the_output_to_standby(self, monkeypatch):
        cal, transport = open_calibrator(faults=["0", "0"])
        clock = FakeClock(transport.events)
        monkeypatch.setattr(f57, "time", clock)
        transport.script("ISR?", str(ISR_REMOTE))          # never settles
        transport.log.clear()
        with pytest.raises(TimeoutError):
            cal.operate(timeout=1.0)
        # STBY, then the M47 verification read that proves it took effect.
        assert transport.log[-2:] == ["STBY", "ISR?"]
        assert cal.standby_error is None


class TestStandby:
    def test_writes_stby_and_verifies_the_oper_bit(self):
        # M47: STBY is "ignored if not in remote" ([OM Table 5-2, p.190]), so
        # "the write did not raise" is not evidence the output came off.
        cal, transport = open_calibrator()
        transport.log.clear()
        cal.standby()
        assert transport.log == ["STBY", "ISR?"]

    def test_is_safe_to_call_redundantly(self):
        cal, transport = open_calibrator()
        transport.log.clear()
        cal.standby()
        cal.standby()
        assert transport.log == ["STBY", "ISR?", "STBY", "ISR?"]

    def test_does_not_raise_on_a_queued_fault(self):
        # It is the error-path method: a fault in the queue must not stop it.
        cal, transport = open_calibrator(faults=["0", "3111", "0"])
        transport.log.clear()
        cal.standby()
        assert transport.log == ["STBY", "ISR?"]

    def test_does_not_drain_the_fault_queue(self):
        cal, transport = open_calibrator(faults=["0", "3111", "0"])
        transport.log.clear()
        cal.standby()
        assert "FAULT?" not in transport.log

    def test_tolerates_a_stale_oper_bit_and_polls(self, monkeypatch):
        # STBY is overlapped: the OPER bit can still read set on the first
        # ISR? after it. Polling is what separates "not yet" from "never".
        cal, transport = open_calibrator()
        clock = FakeClock(transport.events)
        monkeypatch.setattr(f57, "time", clock)
        transport.script("ISR?", [str(ISR_REMOTE | ISR_OPER),
                                  str(ISR_REMOTE)])
        transport.log.clear()
        cal.standby()
        assert transport.log == ["STBY", "ISR?", "ISR?"]

    def test_raises_when_the_output_stays_on(self, monkeypatch):
        cal, transport = open_calibrator()
        clock = FakeClock(transport.events)
        monkeypatch.setattr(f57, "time", clock)
        transport.script("ISR?", str(ISR_REMOTE | ISR_OPER))   # never comes off
        transport.log.clear()
        with pytest.raises(Fluke5700AError) as info:
            cal.standby()
        message = str(info.value)
        assert "OPER" in message and "still" in message.lower()

    def test_raises_when_the_calibrator_has_left_remote(self):
        # The operator's "Go To Local Control" softkey ([OM Table 5-5,
        # p.188]): STBY is then ignored and logs fault 2213, so a silent
        # success here would leave up to 1100 V live at the UUT.
        cal, transport = open_calibrator()
        transport.script("ISR?", "0")            # OPER clear, REMOTE clear
        transport.log.clear()
        with pytest.raises(Fluke5700AError) as info:
            cal.standby()
        message = str(info.value)
        assert "2213" in message and "remote" in message.lower()

    def test_safe_standby_reports_rather_than_swallowing(self, monkeypatch,
                                                         capsys):
        cal, transport = open_calibrator()
        clock = FakeClock(transport.events)
        monkeypatch.setattr(f57, "time", clock)
        transport.script("ISR?", str(ISR_REMOTE | ISR_OPER))
        cal._safe_standby()                      # must not raise ...
        assert cal.standby_error is not None     # ... and must not be silent
        assert "OPER" in str(cal.standby_error)
        assert "may still be live" in capsys.readouterr().err

    def test_safe_standby_clears_the_error_when_it_succeeds(self, monkeypatch):
        cal, transport = open_calibrator()
        clock = FakeClock(transport.events)
        monkeypatch.setattr(f57, "time", clock)
        transport.script("ISR?", str(ISR_REMOTE | ISR_OPER))
        cal._safe_standby()
        assert cal.standby_error is not None
        transport.script("ISR?", str(ISR_REMOTE))
        cal._safe_standby()
        assert cal.standby_error is None


# ── the standby deadline scales with what was energized ───────────────

class TestStandbyVerifyTimeout:
    """The pure sizing function. **No I/O.**"""

    def test_low_voltage_keeps_the_original_one_second(self):
        # Nothing about sub-22 V behaviour may change: a 0.1 V point that has
        # not come off in a second has not come off.
        for volts in (0.0, 0.1, 10.0, 21.999):
            assert (standby_verify_timeout(volts)
                    == f57.STANDBY_VERIFY_TIMEOUT_SEC == 1.0)

    def test_the_threshold_is_the_shared_22_v_line(self):
        assert f57.HIGH_VOLTAGE_V == 22.0
        assert standby_verify_timeout(f57.HIGH_VOLTAGE_V) == \
            f57.STANDBY_VERIFY_TIMEOUT_SEC

    def test_full_scale_and_beyond_take_the_ceiling(self):
        for volts in (f57.STANDBY_VERIFY_FULL_SCALE_V, 1100.0, 5000.0):
            assert (standby_verify_timeout(volts)
                    == f57.STANDBY_VERIFY_TIMEOUT_MAX_SEC == 5.0)

    def test_a_kilovolt_gets_several_seconds(self):
        # The point the bench run actually ended on.
        assert 4.0 < standby_verify_timeout(-1000.0) < 5.0

    def test_polarity_does_not_matter(self):
        assert standby_verify_timeout(-1000.0) == standby_verify_timeout(1000.0)

    def test_monotone_in_magnitude(self):
        volts = [0.0, 1.0, 22.0, 100.0, 220.0, 1000.0, 1100.0, 2200.0]
        sized = [standby_verify_timeout(v) for v in volts]
        assert sized == sorted(sized)

    @pytest.mark.parametrize("unknown", [None, float("nan"), float("inf")])
    def test_unknown_takes_the_ceiling_not_the_floor(self, unknown):
        # Deliberate asymmetry: the deadline is only ever *reached* when the
        # output did not come off, so an over-long one costs seconds on the
        # failure path and a too-short one costs a false "assume it is live".
        assert (standby_verify_timeout(unknown)
                == f57.STANDBY_VERIFY_TIMEOUT_MAX_SEC)


#: How far a virtual-clock elapsed time may miss its deadline: the poll loop
#: only tests the clock after a sleep, and 0.05 s accumulated a hundred times
#: does not land exactly on 5.0.
SLACK = f57.STANDBY_POLL_INTERVAL_SEC


def energize(volts, unit="V", *, drains=3, hz=0.0):
    """``(cal, transport)`` with *volts* staged and energized.

    ``open()`` sees the output off, so the session starts from a known zero;
    from then on ``ISR?`` answers a constant OPER-set word, so the output
    never comes off and :meth:`standby` runs its deadline out -- the
    stuck-output case.
    """
    cal, transport = open_calibrator(faults=["0"] * drains,
                                     isr=ISR_REMOTE | ISR_SETTLED)
    transport.script("ISR?", str(ISR_REMOTE | ISR_SETTLED | ISR_OPER))
    cal.set_output(volts, unit, hz)
    cal.operate()
    return cal, transport


class TestStandbyDeadlineScales:
    """The SIZED deadline.  The 10 s minimum window (STANDBY_RETRY_MIN_SEC)
    is set to 0 here so the sizing stays visible; TestStandbyRetryMinimum
    pins the minimum itself."""

    @pytest.fixture(autouse=True)
    def _no_minimum(self, monkeypatch):
        monkeypatch.setattr(f57, "STANDBY_RETRY_MIN_SEC", 0.0)

    def test_a_kilovolt_point_gets_the_longer_deadline(self, monkeypatch):
        # The bench failure: a 15-point dc run ending on -1000 V was declared
        # "the output may still be live" with every point measured.
        cal, transport = energize(-1000.0)
        clock = FakeClock(transport.events)
        monkeypatch.setattr(f57, "time", clock)
        with pytest.raises(Fluke5700AError) as info:
            cal.standby()
        assert clock.now > 4.0
        message = str(info.value)
        assert "4.6 s after STBY" in message
        # The message says what it sized itself for, so an operator reading it
        # at 3 a.m. can tell a short deadline from a stuck output.
        assert "the deadline for an output of 1000 V" in message

    def test_a_low_voltage_point_still_gets_exactly_one_second(self,
                                                              monkeypatch):
        cal, transport = energize(0.1)
        clock = FakeClock(transport.events)
        monkeypatch.setattr(f57, "time", clock)
        with pytest.raises(Fluke5700AError) as info:
            cal.standby()
        assert clock.now == pytest.approx(1.0, abs=SLACK)
        assert "1.0 s after STBY" in str(info.value)

    def test_a_genuinely_stuck_output_still_raises(self, monkeypatch):
        # The whole point of the check survives the longer window: sized, not
        # weakened.
        cal, transport = energize(1000.0)
        clock = FakeClock(transport.events)
        monkeypatch.setattr(f57, "time", clock)
        with pytest.raises(Fluke5700AError) as info:
            cal.standby()
        message = str(info.value)
        assert "OPER" in message and "must be assumed live" in message

    def test_the_remote_bit_check_is_untouched(self, monkeypatch):
        # OPER clear, REMOTE clear: still fault 2213, still immediately.
        cal, transport = energize(1000.0)
        clock = FakeClock(transport.events)
        monkeypatch.setattr(f57, "time", clock)
        transport.script("ISR?", "0")
        with pytest.raises(Fluke5700AError) as info:
            cal.standby()
        assert "2213" in str(info.value)
        assert clock.now == 0.0                  # no waiting: OPER was clear

    def test_a_zero_staged_after_a_kilovolt_keeps_the_long_deadline(
            self, monkeypatch):
        # The regression that made the bug reachable at all: the engine's
        # teardown programs 0 V *while the kilovolt is still coming down*
        # (adapters.zero_output, M52) and only then calls standby().
        cal, transport = energize(-1000.0, drains=4)
        cal.set_output(0.0, "V")                 # the M52 zero, still live
        clock = FakeClock(transport.events)
        monkeypatch.setattr(f57, "time", clock)
        with pytest.raises(Fluke5700AError):
            cal.standby()
        assert clock.now > 4.0

    def test_ohms_keeps_the_floor(self, monkeypatch):
        # Ohms puts neither a large voltage nor a current on the terminals.
        for amplitude, unit in ((1000.0, "OHM"), (1.0e6, "OHM")):
            cal, transport = energize(amplitude, unit)
            clock = FakeClock(transport.events)
            monkeypatch.setattr(f57, "time", clock)
            with pytest.raises(Fluke5700AError):
                cal.standby()
            assert clock.now == pytest.approx(1.0, abs=SLACK), (amplitude, unit)

    @pytest.mark.parametrize("amps", [0.001, 0.1, 0.22, 1.0, -1.0, 2.2])
    def test_every_current_takes_the_ceiling(self, monkeypatch, amps):
        # 2026-11-01 ACI: 0.22 A @ 15 Hz on the 220 mA range was still OPER
        # 1.0 s after STBY, so the 220 mA range is not trusted to the floor.
        # 2026-08-29 (990 mA) and 2026-11-01 (after -1 A): OPER was still set
        # 1.0 s after STBY and clear moments later -- de-energizing ~1 A takes
        # longer than a second on this unit.  The voltage-derived deadline
        # gave every current output the floor.
        cal, transport = energize(amps, "A")
        clock = FakeClock(transport.events)
        monkeypatch.setattr(f57, "time", clock)
        with pytest.raises(Fluke5700AError) as info:
            cal.standby()
        assert clock.now == pytest.approx(
            f57.STANDBY_VERIFY_TIMEOUT_MAX_SEC, abs=SLACK)
        assert f"{abs(amps):g} A" in str(info.value)

    def test_a_high_current_that_comes_off_late_is_not_an_error(
            self, monkeypatch):
        # The healthy path the bug broke: OPER clears after 2 s.
        cal, transport = energize(1.0, "A")
        clock = FakeClock(transport.events)
        monkeypatch.setattr(f57, "time", clock)
        transport.script(
            "ISR?",
            lambda _cmd: str(ISR_REMOTE | (ISR_OPER if clock.now < 2.0 else 0)))
        cal.standby()
        assert 2.0 <= clock.now < 2.0 + 2 * SLACK

    @pytest.mark.parametrize("hz", [10.0, 15.0, 50.0])
    def test_low_frequency_ac_takes_the_ceiling(self, monkeypatch, hz):
        # 2026-11-02 34401A adjustment: after 10 V at 10 Hz OPER was still
        # set 1.0 s after STBY and clear moments later -- the floor declared
        # a healthy run "output must be assumed live" with 7 of 11 ACV
        # constants written.
        cal, transport = energize(10.0, "V", hz=hz)
        clock = FakeClock(transport.events)
        monkeypatch.setattr(f57, "time", clock)
        with pytest.raises(Fluke5700AError) as info:
            cal.standby()
        assert clock.now == pytest.approx(
            f57.STANDBY_VERIFY_TIMEOUT_MAX_SEC, abs=SLACK)
        assert "below 100 Hz" in str(info.value)

    @pytest.mark.parametrize("hz", [1.0e3, 5.0e4])
    def test_audio_and_higher_ac_keeps_the_floor(self, monkeypatch, hz):
        cal, transport = energize(10.0, "V", hz=hz)
        clock = FakeClock(transport.events)
        monkeypatch.setattr(f57, "time", clock)
        with pytest.raises(Fluke5700AError):
            cal.standby()
        assert clock.now == pytest.approx(1.0, abs=SLACK)

    def test_a_verified_standby_returns_low_frequency_to_the_floor(
            self, monkeypatch):
        cal, transport = energize(10.0, "V", hz=10.0)
        transport.script("ISR?", stateful_isr(transport))
        cal.standby()
        clock = FakeClock(transport.events)
        monkeypatch.setattr(f57, "time", clock)
        transport.script("ISR?", str(ISR_REMOTE | ISR_OPER))
        with pytest.raises(Fluke5700AError):
            cal.standby()
        assert clock.now == pytest.approx(1.0, abs=SLACK)

    def test_the_pure_function_honours_low_frequency(self):
        assert standby_verify_timeout(10.0, low_frequency=True) == \
            f57.STANDBY_VERIFY_TIMEOUT_MAX_SEC
        assert standby_verify_timeout(10.0) == f57.STANDBY_VERIFY_TIMEOUT_SEC
        assert standby_verify_timeout(0.0, low_frequency=True) == \
            f57.STANDBY_VERIFY_TIMEOUT_SEC

    def test_a_verified_standby_returns_a_current_to_the_floor(
            self, monkeypatch):
        cal, transport = energize(2.0, "A")
        transport.script("ISR?", stateful_isr(transport))
        cal.standby()
        clock = FakeClock(transport.events)
        monkeypatch.setattr(f57, "time", clock)
        transport.script("ISR?", str(ISR_REMOTE | ISR_OPER))
        with pytest.raises(Fluke5700AError):
            cal.standby()
        assert clock.now == pytest.approx(1.0, abs=SLACK)

    def test_a_verified_standby_returns_the_next_one_to_the_floor(
            self, monkeypatch):
        cal, transport = energize(1000.0)
        transport.script("ISR?", stateful_isr(transport))
        cal.standby()                            # proves the output came off
        clock = FakeClock(transport.events)
        monkeypatch.setattr(f57, "time", clock)
        transport.script("ISR?", str(ISR_REMOTE | ISR_OPER))
        with pytest.raises(Fluke5700AError):
            cal.standby()
        assert clock.now == pytest.approx(1.0, abs=SLACK)

    def test_an_unopened_setpoint_is_unknown_and_takes_the_ceiling(
            self, monkeypatch):
        # A session may open onto a calibrator the front panel left in
        # OPERATE at full scale; nothing in ISR? says at what magnitude.
        cal, transport = open_calibrator(isr=ISR_REMOTE | ISR_OPER)
        assert cal._live_volts is None           # open() saw OPER still set
        clock = FakeClock(transport.events)
        monkeypatch.setattr(f57, "time", clock)
        with pytest.raises(Fluke5700AError) as info:
            cal.standby()
        assert clock.now == pytest.approx(
            f57.STANDBY_VERIFY_TIMEOUT_MAX_SEC, abs=SLACK)
        assert "an unknown magnitude" in str(info.value)

    def test_an_incr_edit_makes_the_magnitude_unknown(self, monkeypatch):
        # INCR moves the output without naming the new value, so the driver
        # stops claiming to know it.
        cal, transport = energize(1.0, drains=4)
        cal.incr(0.001)
        clock = FakeClock(transport.events)
        monkeypatch.setattr(f57, "time", clock)
        with pytest.raises(Fluke5700AError):
            cal.standby()
        assert clock.now == pytest.approx(
            f57.STANDBY_VERIFY_TIMEOUT_MAX_SEC, abs=SLACK)

    def test_cal_rng_stages_its_own_magnitude(self, monkeypatch):
        # The §14.8 recipe is cal_rng(range, magnitude) then operate(), with
        # no OUT in between; a stale setpoint would size a kilovolt teardown
        # as though it were the last 10 V point.
        cal, transport = open_calibrator(
            faults=["0", "0", "0"],
            isr=ISR_REMOTE | ISR_SETTLED | ISR_OPER)
        cal.cal_rng("DC1100V", 1000.0)
        cal.operate()
        clock = FakeClock(transport.events)
        monkeypatch.setattr(f57, "time", clock)
        with pytest.raises(Fluke5700AError):
            cal.standby()
        assert clock.now > 4.0


# ── CUR_POST (H02 / H04; [OM Table 5-2, p.176]) ───────────────────────

class TestCurrentPost:
    def test_the_token_list_is_the_manual_s(self):
        # [OM 1996 p.176]: NORMAL (OUTPUT HI), AUX (AUX CURRENT OUTPUT),
        # IB5725 (the 5725A binding posts).
        assert CUR_POSTS == ("NORMAL", "AUX", "IB5725")

    def test_query_returns_the_selected_post(self):
        cal, transport = open_calibrator()
        transport.script("CUR_POST?", "AUX")
        transport.log.clear()
        assert cal.cur_post() == "AUX"
        assert transport.log == ["CUR_POST?"]

    def test_query_is_unquoted_and_upper_cased(self):
        cal, transport = open_calibrator()
        transport.script("CUR_POST?", '"normal"')
        assert cal.cur_post() == "NORMAL"

    def test_set_writes_the_token_and_checks_the_fault_queue(self):
        cal, transport = open_calibrator(faults=["0", "0"])
        transport.log.clear()
        cal.set_cur_post("aux")
        assert transport.log == ["CUR_POST AUX", "FAULT?"]

    def test_set_refuses_a_post_the_calibrator_does_not_have(self):
        cal, transport = open_calibrator()
        transport.log.clear()
        with pytest.raises(ValueError, match="CUR_POST"):
            cal.set_cur_post("REAR")
        assert transport.log == []

    def test_an_ignored_cur_post_raises(self):
        # "Sequential command, ignored if not in remote" ([OM p.176]) — the
        # fault queue is the only evidence, so it is read.
        cal, transport = open_calibrator(faults=["0", "2213", "0"])
        transport.script("EXPLAIN? 2213", "Not In Remote")
        transport.log.clear()
        with pytest.raises(CalibratorFault) as info:
            cal.set_cur_post("NORMAL")
        assert info.value.codes[0][0] == 2213


# ── wait_settled (guide §8.4) ─────────────────────────────────────────

class TestWaitSettled:
    def test_guard_elapses_before_the_first_isr_read(self, monkeypatch):
        # Polling inside the 60 ms window can read a stale SETTLED=1 from the
        # *previous* setpoint and return instantly (guide §8.4).
        cal, transport = open_calibrator()
        clock = FakeClock(transport.events)
        monkeypatch.setattr(f57, "time", clock)
        transport.script("ISR?", str(ISR_REMOTE | ISR_SETTLED))
        transport.events.clear()
        cal.wait_settled()
        # The literal, not f57.SETTLE_STATUS_LATENCY_SEC: comparing the
        # constant against itself would stay green if the guard were dropped
        # to 1 ms, which is exactly the stale-SETTLED bug of guide §8.4.
        assert slept_before_first(transport.events, "ISR?") >= 0.06

    def test_returns_when_bit_12_sets(self, monkeypatch):
        cal, transport = open_calibrator()
        clock = FakeClock(transport.events)
        monkeypatch.setattr(f57, "time", clock)
        transport.script("ISR?", [str(ISR_REMOTE), str(ISR_REMOTE),
                                  str(ISR_REMOTE | ISR_SETTLED)])
        transport.log.clear()
        cal.wait_settled()
        assert transport.log == ["ISR?", "ISR?", "ISR?"]

    def test_polls_at_poll_interval(self, monkeypatch):
        cal, transport = open_calibrator()
        clock = FakeClock(transport.events)
        monkeypatch.setattr(f57, "time", clock)
        transport.script("ISR?", [str(ISR_REMOTE),
                                  str(ISR_REMOTE | ISR_SETTLED)])
        cal.wait_settled(poll_interval=0.25)
        assert clock.now == pytest.approx(
            f57.SETTLE_STATUS_LATENCY_SEC + 0.25)

    def test_raises_timeout_error_when_it_never_settles(self, monkeypatch):
        cal, transport = open_calibrator()
        clock = FakeClock(transport.events)
        monkeypatch.setattr(f57, "time", clock)
        transport.script("ISR?", str(ISR_REMOTE))
        with pytest.raises(TimeoutError) as info:
            cal.wait_settled(timeout=1.0)
        assert "SETTLED" in str(info.value)
        assert clock.now >= 1.0

    def test_does_not_use_a_blocking_opc_read(self, monkeypatch):
        # A blocking *OPC? holds the GPIB controller for up to 45 s and
        # starves a co-resident free-running HP 3458A on the same bus.
        cal, transport = open_calibrator()
        clock = FakeClock(transport.events)
        monkeypatch.setattr(f57, "time", clock)
        transport.script("ISR?", str(ISR_REMOTE | ISR_SETTLED))
        transport.log.clear()
        cal.wait_settled()
        assert "*OPC?" not in transport.log
        assert "*WAI" not in transport.log

    def test_default_timeout_is_floored_at_45_s(self):
        cal, _ = open_calibrator()
        assert cal.default_settle_timeout() == 45.0

    def test_last_setpoint_drives_the_settle_estimate(self):
        # The estimate itself, not the timeout: every §11.5 figure is <= 12 s
        # and the margin is 3, so max(45, 3 x estimate) is *always* the 45 s
        # floor -- asserting the timeout could not detect a driver that never
        # recorded an estimate at all.
        cal, _ = open_calibrator(faults=["0", "0"])
        cal.set_output(10.0, "V")
        assert cal._settle_estimate == 4.0      # 3 s dc + 1 s range/polarity

    def test_settle_estimate_tracks_the_function(self):
        cal, _ = open_calibrator(faults=["0", "0", "0"])
        cal.set_output(1.0, "V", 15.0)          # ac volts below 20 Hz
        assert cal._settle_estimate == 8.0      # 7 s + 1 s range change
        cal.set_output(10e3, "OHM")
        assert cal._settle_estimate == 12.0     # not tabulated: worst case


# ── readback (guide §9, §11.4) ────────────────────────────────────────

class TestReadback:
    def test_out_actual(self):
        cal, transport = open_calibrator()
        transport.script("OUT?", "-1.520000E+01,V,0.000000E+00")
        assert cal.out_actual() == (-15.2, "V", 0.0)

    def test_out_actual_returns_the_characterized_resistance(self):
        # guide §11.4 item 2: the error reference is THIS value, never the
        # requested nominal.
        cal, transport = open_calibrator()
        transport.script("OUT?", "1.0000842E+04,OHM,0")
        amplitude, unit, frequency = cal.out_actual()
        assert (amplitude, unit, frequency) == (10000.842, "OHM", 0.0)

    def test_adj_actual(self):
        cal, transport = open_calibrator()
        transport.script("ADJOUT?", "1.256983E+01,V,0")
        assert cal.adj_actual() == (12.56983, "V", 0.0)
        assert "ADJOUT?" in transport.log

    def test_vout_actual_expresses_a_dbm_output_in_volts(self):
        # guide VOUT? example [OM p.5-41]: 10 dBm at 10 kHz on the main
        # output. The dBm reference is 600 ohm there and 50 ohm on wideband,
        # so the instrument's own conversion is the only trustworthy one.
        cal, transport = open_calibrator()
        transport.script("VOUT?", "2.4494897E+00,V,1.0000E+04")
        assert cal.vout_actual() == (2.4494897, "V", 10000.0)
        assert "VOUT?" in transport.log

    def test_dbmout_actual(self):
        # guide DBMOUT? example [OM p.5-26].
        cal, transport = open_calibrator()
        transport.script("DBMOUT?", "28.345,DBM,442")
        assert cal.dbmout_actual() == (28.345, "DBM", 442.0)
        assert "DBMOUT?" in transport.log

    def test_uncert(self):
        cal, transport = open_calibrator()
        transport.script("UNCERT?", "8.5E+00,PPM,365")
        assert cal.uncert() == (8.5, "PPM", 365)

    def test_uncert_no_spec_sentinel(self):
        cal, transport = open_calibrator()
        transport.script("UNCERT?", "-1.0,PPM,365")
        assert cal.uncert()[0] is None

    def test_range_id(self):
        cal, transport = open_calibrator()
        transport.script("RANGE?", '"DC220V"')
        assert cal.range_id() == "DC220V"


# ── modifier state ────────────────────────────────────────────────────

class TestModifiers:
    def test_range_lock_on_and_off(self):
        cal, transport = open_calibrator()
        transport.log.clear()
        cal.range_lock(True)
        cal.range_lock(False)
        assert transport.log == ["RANGELCK ON", "RANGELCK OFF"]

    def test_is_range_locked_reads_isr(self):
        cal, transport = open_calibrator()
        transport.script("ISR?", str(ISR_REMOTE | ISR_RLOCK))
        transport.log.clear()
        assert cal.is_range_locked() is True
        assert transport.log == ["ISR?"]

    def test_is_range_locked_prefers_the_instrument_over_cached_state(self):
        # The front panel can lock the range behind the driver's back.
        cal, transport = open_calibrator()
        cal.range_lock(False)
        transport.script("ISR?", str(ISR_REMOTE | ISR_RLOCK))
        assert cal.is_range_locked() is True

    @pytest.mark.parametrize("readback", [
        "1.9000420E+04,OHM,0",      # characterized *above* the nominal
        "1.8999600E+04,OHM,0",      # characterized below the nominal
        "1.9E+04,OHM,0",            # the exact nominal hardware never returns
    ])
    def test_rcomp_on_allowed_at_a_characterized_19k(self, readback):
        # OUT? in ohms returns the characterized value (guide §11.4 item 2),
        # which sits above nominal about half the time. Comparing it against
        # a nominal 19 kOhm limit would reject a legal setpoint at random.
        cal, transport = open_calibrator()
        transport.script("OUT?", readback)
        transport.log.clear()
        cal.rcomp(True)
        assert transport.log == ["OUT?", "RCOMP ON"]

    @pytest.mark.parametrize("readback", [
        "1.0000842E+04,OHM,0",      # characterized 10 kOhm
        "1.9E+03,OHM,0",
        "0.0E+00,OHM,0",            # the short
    ])
    def test_rcomp_on_allowed_below_19k(self, readback):
        cal, transport = open_calibrator()
        transport.script("OUT?", readback)
        transport.log.clear()
        cal.rcomp(True)
        assert transport.log[-1] == "RCOMP ON"

    @pytest.mark.parametrize("readback", [
        "1.0E+05,OHM,0",
        "1.9000420E+05,OHM,0",      # characterized 190 kOhm
    ])
    def test_rcomp_above_19k_raises_value_error(self, readback):
        cal, transport = open_calibrator()
        transport.script("OUT?", readback)
        transport.log.clear()
        with pytest.raises(ValueError) as info:
            cal.rcomp(True)
        assert "836" in str(info.value)
        assert not any(cmd.startswith("RCOMP") for cmd in transport.log)

    def test_rcomp_rejects_a_readback_far_from_any_catalogue_value(self):
        cal, transport = open_calibrator()
        transport.script("OUT?", "5.0E+03,OHM,0")
        transport.log.clear()
        with pytest.raises(ValueError) as info:
            cal.rcomp(True)
        assert "catalogue" in str(info.value)
        assert not any(cmd.startswith("RCOMP") for cmd in transport.log)

    def test_rcomp_outside_the_ohms_function_raises(self):
        cal, transport = open_calibrator()
        transport.script("OUT?", "1.0E+01,V,0")
        with pytest.raises(ValueError):
            cal.rcomp(True)

    def test_rcomp_off_is_always_allowed(self):
        cal, transport = open_calibrator()
        transport.log.clear()
        cal.rcomp(False)
        assert transport.log == ["RCOMP OFF"]

    def test_ext_sense(self):
        cal, transport = open_calibrator()
        transport.log.clear()
        cal.ext_sense(True)
        cal.ext_sense(False)
        assert transport.log == ["EXTSENSE ON", "EXTSENSE OFF"]

    def test_ext_guard(self):
        cal, transport = open_calibrator()
        transport.log.clear()
        cal.ext_guard(True)
        cal.ext_guard(False)
        assert transport.log == ["EXTGUARD ON", "EXTGUARD OFF"]


# ── offline envelope (guide §11.1-§11.3, §18.1) ───────────────────────

VALID_OUTPUTS = [
    (10.0, "V", 0.0, {}),                              # dc volts
    (-10.0, "V", 0.0, {}),
    (0.0, "V", 0.0, {}),
    (1100.0, "V", 0.0, {}),
    (100.0, "V", 0.0, {"range_id": "DC1100V"}),      # exactly the minimum
    (1.0, "V", 1000.0, {}),                            # ac volts
    (2.2e-4, "V", 1000.0, {}),                         # 10 % of 2.2 mV
    (250.0, "V", 30.0, {}),                            # at the 15-50 Hz cap
    (220.0, "V", 100e3, {}),                           # exactly 2.2E7 V*Hz
    (1.0, "V", 1.1999e6, {}),                          # the hard ceiling
    (1.0, "A", 0.0, {}),                               # dc current
    (0.0, "A", 0.0, {}),
    (9e-6, "A", 1000.0, {}),                           # the 220 uA minimum
    (1.0, "A", 1000.0, {}),                            # ac current
    (0.0, "OHM", 0.0, {}),                             # the short
    (10e3, "OHM", 0.0, {}),
    (1.9e6, "OHM", 0.0, {}),
    (100e6, "OHM", 0.0, {}),
]

# (amplitude, unit, frequency, kwargs, substring the violation must contain)
# The substring matters: without it the test cannot tell a correct complaint
# from an accidental one raised for the wrong reason.
INVALID_OUTPUTS = [
    (50.0, "V", 0.0, {"range_id": "DC1100V"},
     "below the 100 V minimum output of the DC1100V range"),
    (1200.0, "V", 0.0, {}, "exceeds the 1100 V dc-voltage maximum"),
    (1e-4, "V", 1000.0, {},
     "minimum output (10 % of range) of the AC2_2MV range"),
    (0.5, "V", 1000.0, {"range_id": "AC22V"},
     "minimum output (10 % of range) of the AC22V range"),
    (300.0, "V", 30.0, {}, "exceeds the 250 V limit of the AC1100V range"),
    (300.0, "V", 5000.0, {}, "outside the 15-1000 Hz limits of the AC1100V"),
    (100.0, "V", 1e6, {}, "V*Hz exceeds the 2.200E+07 V*Hz envelope"),
    (1.0, "V", 5.0, {}, "below the 10 Hz ac-voltage minimum"),
    (1.0, "V", 2e6, {}, "above the 1.1999e+06 Hz ac-voltage maximum"),
    (-1.0, "V", 1000.0, {}, "ac amplitude must be a positive rms value"),
    (5e3, "OHM", 0.0, {}, "is not one of the 18 catalogue resistances"),
    (100.0, "OHM", 50.0, {}, "cannot be set in the ohms function (fault 812)"),
    (5e-6, "A", 1000.0, {},
     "below the 9e-06 A minimum output of the AC220UA range"),
    (11.0, "A", 0.0, {}, "exceeds the 2.2 A dc-current maximum"),
    (11.0, "A", 1000.0, {}, "exceeds the 2.2 A ac-current maximum"),
    (3.0, "A", 0.0, {}, "exceeds the 2.2 A dc-current maximum"),
    # Amplifier-only points: the envelope data knows them, but the driver
    # cannot engage a 5725A (BOOST/BTYPE unimplemented), so declaring the
    # amplifier in options must not turn them into a promise.
    (11.0, "A", 0.0, {"options": ("5725A Attached",)},
     "does not implement BOOST"),
    (11.0, "A", 1000.0, {"options": ("5725A Attached",)},
     "does not implement BOOST"),
    (500.0, "V", 10e3, {"options": ("5725A Attached",)},
     "does not implement BOOST"),
    (1.0, "A", 15.0, {}, "outside the 20-10000 Hz limits of the AC2_2A"),
    (1.0, "A", 20e3, {}, "above the 10000 Hz ac-current maximum"),
    (10.0, "X", 0.0, {}, "unknown unit 'X'"),
]


class TestValidateOutput:
    @pytest.mark.parametrize("amplitude,unit,frequency,kwargs", VALID_OUTPUTS)
    def test_valid(self, amplitude, unit, frequency, kwargs):
        assert validate_output(amplitude, unit, frequency, **kwargs) == []

    @pytest.mark.parametrize("amplitude,unit,frequency,kwargs,expected",
                             INVALID_OUTPUTS)
    def test_invalid(self, amplitude, unit, frequency, kwargs, expected):
        problems = validate_output(amplitude, unit, frequency, **kwargs)
        assert problems
        assert any(expected in problem for problem in problems), problems

    def test_returns_every_violation_not_just_the_first(self):
        problems = validate_output(2000.0, "V", 2e6)
        assert len(problems) >= 3          # magnitude, frequency and V*Hz

    def test_1100v_minimum_is_only_reachable_with_a_pinned_range(self):
        # Autoranging never selects the 1100 V range for 50 V.
        assert validate_output(50.0, "V") == []
        assert validate_output(50.0, "V", range_id="DC1100V")

    def test_five725a_never_makes_a_point_sourceable(self):
        # Without the option the complaint is about the base envelope; with
        # it, about the driver's own missing BOOST support. Neither passes.
        assert validate_output(11.0, "A", 1000.0)
        assert validate_output(11.0, "A", 1000.0,
                               options=("5725A Attached",))

    def test_non_finite_rejected(self):
        assert validate_output(float("nan"), "V")
        assert validate_output(1.0, "V", float("inf"))

    def test_negative_frequency_rejected(self):
        assert validate_output(1.0, "V", -100.0)

    def test_all_catalogue_resistances_accepted(self):
        for value, _ in f57.RESISTANCE_VALUES:
            assert validate_output(value, "OHM") == [], value

    def test_unknown_unit_message_warns_about_multipliers(self):
        assert "MAOHM" in " ".join(validate_output(1.0, "MV"))


class TestUnrecognizedRangeId:
    """A pinned range that is not recognized must never read as "no pin".

    Falling back to autoranging silently drops the per-range limits the
    caller asked to be checked -- and, worse, restores the 5725A suppression
    that a pinned range is supposed to block.
    """

    @pytest.mark.parametrize("amplitude,unit,frequency,range_id", [
        (50.0, "V", 0.0, "DC1100"),          # typo: the 100 V minimum vanishes
        (50.0, "V", 0.0, "DC1100W"),
        (1.0, "V", 1000.0, "AC1100"),        # typo
        (1.0, "A", 0.0, "DC220"),
        (1.0, "A", 1000.0, "AC2_2"),
        (10.0, "V", 0.0, "AC22V"),           # ac range pinned on a dc request
        (1.0, "V", 1000.0, "DC220V"),        # dc range pinned on an ac request
        (10e3, "OHM", 0.0, "DC1100V"),       # wrong function entirely
    ])
    def test_rejected_rather_than_ignored(self, amplitude, unit, frequency,
                                          range_id):
        problems = validate_output(amplitude, unit, frequency,
                                   range_id=range_id)
        assert any("unknown range identifier" in p for p in problems), problems

    def test_a_typo_cannot_re_enable_5725a_suppression(self):
        # The pinned-range complaint must not vanish because the pin was
        # misspelled: 'AC1100' is not 'AC1100V'.
        problems = validate_output(500.0, "V", 10e3,
                                   options=("5725A Attached",),
                                   range_id="AC1100")
        assert any("unknown range identifier" in p for p in problems), problems

    def test_ohms_pin_naming_a_different_value_is_a_contradiction(self):
        # In the resistance function the range *is* the value.
        problems = validate_output(10e3, "OHM", range_id="OHM10")
        assert any("does not match the requested" in p for p in problems)

    def test_recognized_range_ids_still_work(self):
        assert validate_output(100.0, "V", range_id="DC1100V") == []
        assert validate_output(1.0, "V", 1000.0, range_id="AC2_2V") == []
        assert validate_output(1.0, "A", range_id="DC2_2A") == []
        assert validate_output(1.0, "A", 1000.0, range_id="AC2_2A") == []
        assert validate_output(10e3, "OHM", range_id="OHM10K") == []

    def test_no_range_id_still_autoranges(self):
        assert validate_output(50.0, "V") == []


class TestFive725aRefusal:
    """Amplifier-only points are refused, never promised.

    The envelope tables know the 5725A's corner, but engaging the amplifier
    takes ``BOOST``/``BTYPE`` (guide §5) and this driver implements neither —
    so a request only the amplifier covers must fail offline validation with
    a message naming *that*, instead of passing and then faulting 801/841 at
    the instrument.
    """

    def test_negative_rms_complaint_is_never_replaced(self):
        problems = validate_output(-500.0, "V", 1000.0,
                                   options=("5725A Attached",))
        assert any("positive rms value" in problem for problem in problems)

    def test_negative_rms_is_rejected_identically_with_and_without_5725a(self):
        assert (validate_output(-500.0, "V", 1000.0)
                == validate_output(-500.0, "V", 1000.0,
                                   options=("5725A Attached",)))

    def test_set_output_never_emits_a_negative_ac_amplitude(self):
        # The default *OPT? in these tests is "5725A Attached", so this path
        # stays live even for a unit that declares the amplifier.
        cal, transport = open_calibrator(faults=["0", "0"])
        assert cal.options == ["5725A Attached"]
        transport.log.clear()
        with pytest.raises(ValueError):
            cal.set_output(-500.0, "V", 1000.0)
        assert transport.log == []

    def test_a_pinned_range_keeps_its_own_complaint(self):
        # Naming a range is the caller asserting a base-instrument range, not
        # the amplifier: 500 V is over the AC22V range whatever is attached.
        problems = validate_output(500.0, "V", 1000.0,
                                   options=("AC22V", "5725A Attached"),
                                   range_id="AC22V")
        assert any("full scale of the AC22V range" in p for p in problems)

    def test_an_amplifier_point_is_refused_with_the_boost_message(self):
        # 500 V at 10 kHz is outside the base AC1100V window (15 Hz - 1 kHz)
        # but inside the 5725A's (20-1100 V, 40 Hz - 30 kHz): it used to be
        # accepted, and then faulted at the instrument.
        assert validate_output(500.0, "V", 10e3)
        problems = validate_output(500.0, "V", 10e3,
                                   options=("5725A Attached",))
        assert any("does not implement BOOST" in p for p in problems), problems

    def test_set_output_refuses_an_amplifier_point_offline(self):
        cal, transport = open_calibrator(faults=["0", "0"])
        assert cal.options == ["5725A Attached"]
        transport.log.clear()
        with pytest.raises(ValueError, match="BOOST"):
            cal.set_output(11.0, "A")
        assert transport.log == []

    def test_the_5725a_dc_current_ceiling_message_still_exists(self):
        # Beyond even the amplifier: the complaint is about 11 A, not BOOST.
        problems = validate_output(15.0, "A",
                                   options=("5725A Attached",))
        assert any("5725A dc-current maximum" in p for p in problems)


class TestExpectedRangeId:
    @pytest.mark.parametrize("amplitude,unit,frequency,expected", [
        (0.1, "V", 0.0, "DC220MV"),
        (10.0, "V", 0.0, "DC11V"),
        (500.0, "V", 0.0, "DC1100V"),
        (1.0, "V", 1000.0, "AC2_2V"),
        (500.0, "V", 100.0, "AC1100V"),
        (1e-3, "A", 0.0, "DC2_2MA"),
        (1.0, "A", 1000.0, "AC2_2A"),
        (1.9e3, "OHM", 0.0, "OHM1_9K"),
        (0.0, "OHM", 0.0, "OHM0"),
        (5e3, "OHM", 0.0, None),
        (5000.0, "V", 0.0, None),
    ])
    def test_prediction(self, amplitude, unit, frequency, expected):
        assert expected_range_id(amplitude, unit, frequency) == expected

    def test_amplifier_range_needs_the_option(self):
        assert expected_range_id(11.0, "A") is None
        assert expected_range_id(
            11.0, "A", options=("5725A Attached",)) == "DC5725A"
        assert expected_range_id(
            500.0, "V", 10e3, options=("5725A Attached",)) == "AC5725V"


# ── settling times (guide §11.5) ──────────────────────────────────────

class TestExpectedSettleSeconds:
    @pytest.mark.parametrize("unit,frequency,kwargs,expected", [
        # DC volts: 3 s; +1 s for range or polarity change; +1 s at 1100 V.
        ("V", 0.0, {}, 3.0),
        ("V", 0.0, {"range_change": True}, 4.0),
        ("V", 0.0, {"polarity_change": True}, 4.0),
        ("V", 0.0, {"range_change": True, "polarity_change": True}, 4.0),
        ("V", 0.0, {"range_id": "DC1100V"}, 4.0),
        ("V", 0.0, {"range_change": True, "range_id": "DC1100V"}, 5.0),
        # DC current: 1 s uA/mA, 3 s at 2.2 A, 6 s at the 5725A 11 A range.
        ("A", 0.0, {"range_id": "DC220UA"}, 1.0),
        ("A", 0.0, {"range_id": "DC220MA"}, 1.0),
        ("A", 0.0, {"range_id": "DC2_2A"}, 3.0),
        ("A", 0.0, {"range_id": "DC5725A"}, 6.0),
        ("A", 0.0, {}, 3.0),                    # no range: worst base figure
        ("A", 0.0, {"range_id": "DC22MA", "polarity_change": True}, 2.0),
        # AC current: 5 s; 6 s on the 5725A 11 A range; +1 s range change.
        ("A", 1000.0, {}, 5.0),
        ("A", 1000.0, {"range_id": "AC5725A"}, 6.0),
        ("A", 1000.0, {"range_change": True}, 6.0),
        # AC volts by frequency band.
        ("V", 10.0, {}, 7.0),
        ("V", 19.99, {}, 7.0),
        ("V", 20.0, {}, 7.0),                   # band absent from the manual
        ("V", 100.0, {}, 7.0),                  # band absent from the manual
        ("V", 119.99, {}, 7.0),                 # band absent from the manual
        ("V", 120.0, {}, 5.0),
        ("V", 120e3, {}, 5.0),
        ("V", 120e3 + 1.0, {}, 2.0),
        ("V", 1e6, {}, 2.0),
        ("V", 1000.0, {"range_change": True}, 6.0),
        ("V", 1000.0, {"range_id": "AC1100V"}, 7.0),
        ("V", 15.0, {"range_change": True, "range_id": "AC1100V"}, 10.0),
        # Worst case = 7 + 1 + 4 = 12 s (guide §11.5).
        ("V", 15.0, {"range_change": True, "range_id": "AC5725V"}, 12.0),
        # Wideband.
        ("V", 100.0, {"range_id": "WB110MV"}, 7.0),
        ("V", 120.0, {"range_id": "WB110MV"}, 5.0),
        ("V", 1.2e6, {"range_id": "WB3_5V"}, 5.0),
        ("V", 2e6, {"range_id": "WB3_5V"}, 0.5),
        # Resistance is not tabulated: the overall worst case.
        ("OHM", 0.0, {}, 12.0),
    ])
    def test_boundaries(self, unit, frequency, kwargs, expected):
        assert expected_settle_seconds(unit, frequency, **kwargs) == expected

    def test_twenty_to_120_hz_band_uses_the_7_s_worst_case(self):
        # Not covered by [OM p.1-27]; guide §11.5 / §18.4.
        for frequency in (20.0, 50.0, 60.0, 100.0, 119.9):
            assert expected_settle_seconds("V", frequency) == 7.0

    def test_only_the_range_moves_a_settling_number(self):
        # A unit that merely *has* a wideband module still settles as
        # standard ac volts while it is sourcing standard ac volts: 200 kHz
        # is 2 s on the ac-volts table but 5 s on the wideband one.
        assert expected_settle_seconds("V", 200e3, range_id="AC2_2V") == 2.0
        assert expected_settle_seconds("V", 200e3, range_id="WB110MV") == 5.0

    @pytest.mark.parametrize("options", [
        ("5725A Attached",),
        ("Wideband Option",),
    ])
    def test_capability_strings_cannot_reach_the_settling_numbers(self,
                                                                  options):
        # Structural, not documentary: there is no `options` parameter, so a
        # capability string cannot move a settling number even by accident.
        with pytest.raises(TypeError):
            expected_settle_seconds("V", 200e3, options=options)

    @pytest.mark.parametrize("unit", ["MV", "DBM", "X", ""])
    def test_unknown_unit_raises(self, unit):
        # DBM included deliberately: guide gap G20 records that no
        # "OUT ... DBM" example exists in either manual, so the syntax is
        # unverified and validate_output rejects it. Half-supporting it here
        # would leave an untested path to a live output.
        with pytest.raises(ValueError):
            expected_settle_seconds(unit, 0.0)


# ══════════════════════════════════════════════════════════════════════
# Task A3 — long-term commands, calibration, diagnostics
# ══════════════════════════════════════════════════════════════════════

from caldmm.drivers.fluke5700a import (           # noqa: E402
    CAL_ZERO_TIMEOUT_SEC,
    CalibrationSwitchError,
    ESR_DDE,
    ESR_EXE,
    ESR_OPC,
    RangeShift,
    STATE_DIAG_HALTED,
    STATE_UNKNOWN,
    ShiftPoint,
    cal_rng_unit,
    parse_clock,
    parse_constant_list,
    parse_pud,
    parse_range_shift,
    parse_shift_list,
    validate_cal_rng,
)


def opened(**kwargs):
    """An open session whose fault queue answers ``0`` indefinitely.

    ``open_calibrator``'s ``FAULT?`` queue is a one-shot list; every A3
    method drains, so the tests want a queue that stays empty unless the
    test says otherwise.
    """
    cal, transport = open_calibrator(**kwargs)
    transport.script("FAULT?", "0")
    transport.log.clear()
    transport.timeouts.clear()
    return cal, transport


def long_term_clock(monkeypatch, transport):
    """Install the virtual clock used by every long-term polling test."""
    clock = FakeClock(transport.events)
    monkeypatch.setattr(f57, "time", clock)
    return clock


# ── the long-term command runner (guide §7.4, §7.5) ───────────────────

class TestRunLongTerm:
    def test_accumulates_esr_across_polls(self, monkeypatch):
        # THE regression test for the destructive-read trap of gap §19.8
        # item 3. *ESR? is cleared by reading it, so EXE raised on poll 1 is
        # gone by the time OPC shows up on poll 3. A runner that tests bit 0
        # and discards the rest of the byte reports success on a calibration
        # that actually failed.
        cal, transport = opened()
        long_term_clock(monkeypatch, transport)
        transport.script("*ESR?", ["0", str(ESR_EXE), "0", str(ESR_OPC)])
        transport.script("STATE?", "9,Internal adjustment")
        transport.script("FAULT?", ["852", "0"])
        with pytest.raises(CalibratorFault) as info:
            cal._run_long_term("CAL_ADJ", timeout=60.0)
        assert info.value.codes == [(852, "Calibration Steps Out Of Order")]

    def test_stale_esr_bits_are_cleared_before_the_command(self,
                                                           monkeypatch):
        # The ESR is sticky until something reads it (guide §8.3) and
        # drain_faults() empties the fault *queue* without touching it. An
        # EXE left by an earlier rejected command therefore survives, and
        # would be OR-ed into the next command's mask and reported against an
        # operation that actually succeeded.
        cal, transport = opened()
        long_term_clock(monkeypatch, transport)
        transport.script("*ESR?", [str(ESR_EXE), str(ESR_OPC)])
        transport.script("STATE?", "0,Operating")
        cal.cal_adj()                                  # must not raise
        assert transport.log[:2] == ["*ESR?", "CAL_ADJ;*OPC"]

    @pytest.mark.parametrize("cmd", ["OPER", "STBY", "OUT 10 V", ""])
    def test_refuses_a_command_that_is_not_long_term(self, cmd):
        # The runner arms an *OPC and polls for it; nothing else completes
        # that way, and a typo would poll until the timeout expired.
        cal, transport = opened()
        with pytest.raises(ValueError):
            cal._run_long_term(cmd, timeout=60.0)
        assert transport.log == []

    def test_a_failed_progress_poll_does_not_abandon_the_operation(
            self, monkeypatch):
        # STATE? is decoration; the *ESR? poll is the completion signal. On a
        # sick unit a garbled progress reply must not kill a 30-minute
        # CAL_ADJ.
        cal, transport = opened()
        long_term_clock(monkeypatch, transport)
        transport.script("*ESR?", ["0", "0", str(ESR_OPC)])
        transport.script("STATE?", ["not a state", "0,Operating"])
        seen = []
        cal._run_long_term("CAL_ADJ", timeout=600.0,
                           progress_cb=lambda c, s: seen.append(c))
        assert seen == [STATE_UNKNOWN, 0]

    def test_repeated_progress_poll_failures_are_bounded(self, monkeypatch):
        # ... but a genuinely dead link still has to surface.
        cal, transport = opened()
        long_term_clock(monkeypatch, transport)
        transport.script("*ESR?", "0")
        transport.script("STATE?", "not a state")
        with pytest.raises(Fluke5700AProtocolError):
            cal._run_long_term("CAL_ADJ", timeout=600.0, poll_interval=1.0)
        assert transport.log.count("STATE?") == 6      # 5 tolerated, 6th not

    def test_completes_quietly_when_only_opc_is_set(self, monkeypatch):
        cal, transport = opened()
        long_term_clock(monkeypatch, transport)
        transport.script("*ESR?", ["0", "0", "0", str(ESR_OPC)])
        transport.script("STATE?", "4,DC zeros")
        cal._run_long_term("CAL_ZERO", timeout=60.0)
        # One pre-clear read (guide §8.3) plus three polls.
        assert transport.log.count("*ESR?") == 4

    def test_uses_the_opc_form_not_a_blocking_opc_query(self, monkeypatch):
        # A blocking *OPC? read holds the GPIB controller for minutes and
        # starves every other instrument on the bus.
        cal, transport = opened()
        long_term_clock(monkeypatch, transport)
        transport.script("*ESR?", str(ESR_OPC))
        transport.script("STATE?", "0,Operating")
        cal._run_long_term("CAL_CHK", timeout=60.0)
        assert transport.log[:2] == ["*ESR?", "CAL_CHK;*OPC"]
        assert "*OPC?" not in transport.log
        assert "*WAI" not in transport.log

    def test_progress_cb_receives_parsed_state_codes(self, monkeypatch):
        cal, transport = opened()
        long_term_clock(monkeypatch, transport)
        transport.script("*ESR?", ["0", "0", "0", str(ESR_OPC)])
        transport.script("STATE?", ["4,Measuring zeros",
                                    "4,Computing constants",
                                    "0,Operating"])
        seen = []
        cal._run_long_term("CAL_ZERO", timeout=60.0,
                           progress_cb=lambda code, sub: seen.append(
                               (code, sub)))
        assert seen == [(4, "Measuring zeros"), (4, "Computing constants"),
                        (0, "Operating")]

    def test_timeout_raises_without_emitting_stby(self, monkeypatch):
        # There is no remote abort (gap G6): STBY during a long-term command
        # is itself rejected with fault 2002, so trying one would only add a
        # fault to the queue and would not de-energize anything.
        cal, transport = opened()
        long_term_clock(monkeypatch, transport)
        transport.script("*ESR?", "0")
        transport.script("STATE?", "1,Self diagnostics")
        with pytest.raises(TimeoutError) as info:
            cal._run_long_term("DIAG ALL", timeout=5.0, poll_interval=1.0)
        assert "STBY" not in transport.log
        assert "*CLS" not in transport.log
        assert "no remote abort" in str(info.value)
        assert "2002" in str(info.value)

    def test_timeout_message_does_not_claim_the_unit_is_idle(self,
                                                             monkeypatch):
        cal, transport = opened()
        long_term_clock(monkeypatch, transport)
        transport.script("*ESR?", "0")
        transport.script("STATE?", "9,Adjusting")
        with pytest.raises(TimeoutError) as info:
            cal._run_long_term("CAL_ADJ", timeout=3.0, poll_interval=1.0)
        message = str(info.value)
        assert "still running" in message
        assert "front-panel STBY" in message

    def test_never_sends_cls_while_opc_is_outstanding(self, monkeypatch):
        # guide §7.5: *CLS terminates a pending *OPC and deadlocks (2208).
        cal, transport = opened()
        long_term_clock(monkeypatch, transport)
        transport.script("*ESR?", ["0", "0", str(ESR_OPC)])
        transport.script("STATE?", "3,Calibration check")
        cal._run_long_term("CAL_CHK", timeout=60.0)
        assert "*CLS" not in transport.log

    def test_error_bits_with_an_empty_queue_still_raise(self, monkeypatch):
        cal, transport = opened()
        long_term_clock(monkeypatch, transport)
        transport.script("*ESR?", ["0", str(ESR_DDE | ESR_OPC)])
        transport.script("STATE?", "0,Operating")
        with pytest.raises(Fluke5700AError) as info:
            cal._run_long_term("CAL_ADJ", timeout=60.0)
        assert "DDE" in str(info.value)

    def test_stop_state_ends_the_wait_without_opc(self, monkeypatch):
        cal, transport = opened()
        long_term_clock(monkeypatch, transport)
        transport.script("*ESR?", "0")
        transport.script("STATE?", ["1,Testing", "2,Halted at fault"])
        cal._run_long_term("DIAG D5700", timeout=600.0,
                           stop_states=(STATE_DIAG_HALTED,))
        assert transport.log.count("STATE?") == 2

    def test_sleeps_the_poll_interval_between_polls(self, monkeypatch):
        cal, transport = opened()
        clock = long_term_clock(monkeypatch, transport)
        transport.script("*ESR?", ["0", "0", "0", str(ESR_OPC)])
        transport.script("STATE?", "9,Adjusting")
        cal._run_long_term("CAL_ADJ", timeout=600.0, poll_interval=2.5)
        assert clock.now == 5.0                       # two sleeps of 2.5 s

    def test_error_bits_are_not_raised_when_the_caller_opts_out(self,
                                                                monkeypatch):
        # diag() collects its faults rather than raising them.
        cal, transport = opened()
        long_term_clock(monkeypatch, transport)
        transport.script("*ESR?", ["0", str(ESR_DDE | ESR_OPC)])
        transport.script("STATE?", "1,Diagnostics")
        cal._run_long_term("DIAG D5700", timeout=60.0,
                           raise_on_error_bits=False)
        assert "FAULT?" not in transport.log


# ── diagnostics (guide §9 DIAG / DIAGFLT) ─────────────────────────────

class TestDiag:
    def test_default_target_is_the_calibrator_alone(self, monkeypatch):
        cal, transport = opened()
        long_term_clock(monkeypatch, transport)
        transport.script("*ESR?", str(ESR_OPC))
        transport.script("STATE?", "0,Operating")
        cal.diag()
        assert transport.log[:2] == ["*ESR?", "DIAG D5700;*OPC"]

    def test_returns_the_logged_faults_rather_than_raising(self,
                                                           monkeypatch):
        # The A14 case: 3100-level codes are the deliverable, not an error.
        cal, transport = opened()
        long_term_clock(monkeypatch, transport)
        transport.script("*ESR?", str(ESR_DDE | ESR_OPC))
        transport.script("STATE?", "1,High voltage")
        transport.script("FAULT?", ["3135", "3111", "0"])
        transport.script("EXPLAIN? 3135", '"Assembly A14 Not Responding"')
        transport.script("EXPLAIN? 3111",
                         '"A15: HV +DC High Voltage Output Fault"')
        assert cal.diag("ALL") == [
            (3135, "Assembly A14 Not Responding"),
            (3111, "A15: HV +DC High Voltage Output Fault"),
        ]

    def test_faults_decode_offline_when_explain_is_unwell(self, monkeypatch):
        cal, transport = opened()
        long_term_clock(monkeypatch, transport)
        transport.script("*ESR?", str(ESR_OPC))
        transport.script("STATE?", "1,High voltage")
        transport.script("FAULT?", ["3100", "0"])   # no EXPLAIN? scripted
        assert cal.diag("D5700") == [(3100, "A14: 8255 Control Word")]

    @pytest.mark.parametrize("target", ["ALL", "D5700", "DV5725", "DI5725",
                                        "CONT"])
    def test_every_running_target_is_accepted(self, target, monkeypatch):
        cal, transport = opened()
        long_term_clock(monkeypatch, transport)
        transport.script("*ESR?", str(ESR_OPC))
        transport.script("STATE?", "0,Operating")
        cal.diag(target)
        assert transport.log[1] == f"DIAG {target};*OPC"

    @pytest.mark.parametrize("target", ["D5700X", "SELFTEST", "", "HALT"])
    def test_unknown_target_is_rejected_before_any_io(self, target):
        cal, transport = opened()
        with pytest.raises(ValueError):
            cal.diag(target)
        assert transport.log == []

    def test_abort_is_not_waited_on(self):
        cal, transport = opened()
        assert cal.diag_abort() == []
        assert transport.log == ["DIAG ABORT", "FAULT?"]

    def test_halt_state_ends_the_wait_so_stepping_works(self, monkeypatch):
        # DIAGFLT HALT stops the diagnostic at a fault and waits. No *OPC
        # ever arrives, so a runner that only watches bit 0 would sit there
        # until the timeout and the console could never step.
        cal, transport = opened()
        long_term_clock(monkeypatch, transport)
        transport.script("*ESR?", "0")
        transport.script("STATE?", ["1,High voltage", "2,Halted at fault"])
        transport.script("FAULT?", ["3117", "0"])
        transport.script("EXPLAIN? 3117",
                         '"A14/A15: HV +DC Current Error Amp Fault"')
        assert cal.diag("D5700", timeout=600.0) == [
            (3117, "A14/A15: HV +DC Current Error Amp Fault")]

    def test_diag_cont_resumes(self, monkeypatch):
        cal, transport = opened()
        long_term_clock(monkeypatch, transport)
        transport.script("*ESR?", str(ESR_OPC))
        transport.script("STATE?", "1,High voltage")
        cal.diag_cont()
        assert transport.log[1] == "DIAG CONT;*OPC"

    def test_cont_does_not_return_on_the_state_it_started_in(self,
                                                             monkeypatch):
        # Resuming from a halt, the *starting* state is the stop state. If the
        # zero-delay first poll lands before the calibrator has updated
        # STATE?, honouring it there returns instantly with the run still
        # halted -- and the documented stepping loop spins forever.
        cal, transport = opened()
        long_term_clock(monkeypatch, transport)
        transport.script("*ESR?", "0")
        transport.script("STATE?", ["2,Halted at fault", "1,High voltage",
                                    "2,Halted at fault"])
        cal.diag_cont(timeout=600.0)
        assert transport.log.count("STATE?") == 3

    def test_the_documented_stepping_loop_terminates(self, monkeypatch):
        # Two faults, then a clean finish: the loop from diag()'s docstring
        # must make progress and stop.
        cal, transport = opened()
        long_term_clock(monkeypatch, transport)
        remaining = ["1,High voltage", "2,Halted at fault",
                     "2,Halted at fault", "1,High voltage",
                     "2,Halted at fault",
                     "2,Halted at fault", "1,High voltage"]
        transport.script(
            "STATE?", lambda cmd: remaining.pop(0) if remaining
            else "0,Operating")
        transport.script(
            "*ESR?", lambda cmd: "0" if remaining else str(ESR_OPC))
        cal.diag("D5700", timeout=600.0)
        steps = 0
        while cal.diag_halted():
            cal.diag_cont(timeout=600.0)
            steps += 1
            assert steps < 10, "stepping loop did not make progress"
        assert steps == 2
        assert transport.log.count("DIAG CONT;*OPC") == 2

    def test_diag_halted_reports_the_state_the_run_ended_on(self,
                                                            monkeypatch):
        # Read from what the run itself observed, not from a fresh STATE?
        # that could be answered from a different moment.
        cal, transport = opened()
        long_term_clock(monkeypatch, transport)
        transport.script("*ESR?", "0")
        transport.script("STATE?", ["1,High voltage", "2,Halted at fault"])
        cal.diag("D5700", timeout=600.0)
        transport.log.clear()
        assert cal.diag_halted() is True
        assert transport.log == []                     # no extra STATE?

    def test_diag_halted_is_false_before_any_diagnostic_has_run(self):
        cal, _ = opened()
        assert cal.diag_halted() is False

    def test_abort_clears_the_halted_flag(self, monkeypatch):
        cal, transport = opened()
        long_term_clock(monkeypatch, transport)
        transport.script("*ESR?", "0")
        transport.script("STATE?", ["1,High voltage", "2,Halted at fault"])
        cal.diag("D5700", timeout=600.0)
        assert cal.diag_halted() is True
        cal.diag_abort()
        assert cal.diag_halted() is False

    def test_state_parses_code_and_substate(self):
        cal, transport = opened()
        transport.script("STATE?", "18,Output tripped")
        assert cal.state() == (18, "Output tripped")


class TestDiagflt:
    def test_sets_the_mode(self):
        cal, transport = opened()
        cal.diagflt("HALT")
        assert transport.log == ["DIAGFLT HALT", "FAULT?"]

    @pytest.mark.parametrize("mode", ["STOP", "halted", "", "ALL"])
    def test_rejects_an_unknown_mode_before_any_io(self, mode):
        cal, transport = opened()
        with pytest.raises(ValueError):
            cal.diagflt(mode)
        assert transport.log == []

    def test_lower_case_is_accepted(self):
        cal, transport = opened()
        cal.diagflt("halt")
        assert transport.log[0] == "DIAGFLT HALT"

    def test_query_strips_quotes_and_uppercases(self):
        cal, transport = opened()
        transport.script("DIAGFLT?", '"abort"')
        assert cal.diagflt_query() == "ABORT"

    def test_temporarily_restores_the_previous_mode(self):
        # DIAGFLT lives in nonvolatile memory: leaving a unit in HALT would
        # confuse whoever next runs diagnostics from the front panel.
        cal, transport = opened()
        transport.script("DIAGFLT?", "ABORT")
        with cal.diagflt_temporarily("HALT") as previous:
            assert previous == "ABORT"
        assert transport.log == ["DIAGFLT?", "DIAGFLT HALT", "FAULT?",
                                 "DIAGFLT ABORT", "FAULT?"]

    def test_temporarily_restores_on_exception(self):
        cal, transport = opened()
        transport.script("DIAGFLT?", "CONT")
        with pytest.raises(RuntimeError):
            with cal.diagflt_temporarily("HALT"):
                raise RuntimeError("diagnostic blew up")
        assert transport.log[-2:] == ["DIAGFLT CONT", "FAULT?"]

    def test_temporarily_validates_before_touching_the_instrument(self):
        cal, transport = opened()
        transport.script("DIAGFLT?", "ABORT")
        with pytest.raises(ValueError):
            with cal.diagflt_temporarily("NONSENSE"):
                pass
        assert "DIAGFLT NONSENSE" not in transport.log


class TestSelfTest:
    def test_zero_means_passed(self):
        cal, transport = opened()
        transport.script("*TST?", "0")
        assert cal.self_test() is True

    def test_one_means_failed(self):
        cal, transport = opened()
        transport.script("*TST?", "1")
        assert cal.self_test() is False

    @pytest.mark.parametrize("response", ["2", "-1", "PASS"])
    def test_unexpected_response_is_a_protocol_error(self, response):
        cal, transport = opened()
        transport.script("*TST?", response)
        with pytest.raises(Fluke5700AProtocolError):
            cal.self_test()

    def test_read_timeout_is_raised_for_the_query_and_restored(self):
        # *TST? is blocking with no documented duration (gap G9); the session
        # timeout must come back afterwards or the next query breaks.
        cal, transport = opened()
        transport.script("*TST?", "0")
        cal.self_test(timeout=900.0)
        assert transport.timeouts == [900.0, 45.0]

    def test_restores_the_timeout_after_a_failed_read(self):
        cal, transport = opened()
        with pytest.raises(Fluke5700ACommError):
            cal.self_test(timeout=900.0)          # nothing scripted
        assert transport.timeouts[-1] == 45.0

    def test_default_timeout_is_actually_wired_through(self):
        cal, transport = opened()
        transport.script("*TST?", "0")
        cal.self_test()
        assert transport.timeouts == [600.0, 45.0]

    def test_leaves_the_4300_level_faults_in_the_queue(self):
        # The caller drains them: they are the *content* of a failure.
        cal, transport = opened()
        transport.script("*TST?", "1")
        cal.self_test()
        assert "FAULT?" not in transport.log

    def test_self_test_codes_decode_offline(self):
        assert explain_fault(4301) == "ROM Checksum"
        assert explain_fault(4311) == "Inguard CPU ROM Fault"


# ── the service gate and the fatal-fault list (guide §16) ─────────────

class TestServiceGate:
    @pytest.mark.parametrize("call", [
        lambda cal: cal.fatality(),
        lambda cal: cal.fatalclr(),
        lambda cal: cal.set_etime(4628000),
    ])
    def test_refused_without_the_service_flag(self, call):
        cal, transport = opened()
        with pytest.raises(Fluke5700AError) as info:
            call(cal)
        assert "service" in str(info.value).lower()
        assert transport.log == []

    def test_allowed_with_the_service_flag(self):
        cal, transport = opened(service=True)
        transport.script("FATALITY?", [["0517 1032 3135 A14 NOT RESPONDING",
                                        '"']])
        assert cal.fatality() == ["0517 1032 3135 A14 NOT RESPONDING"]

    def test_service_defaults_off(self):
        cal, _ = opened()
        assert cal.service is False


class TestFatality:
    def test_reads_a_block_and_restores_the_session_timeout(self):
        cal, transport = opened(service=True)
        transport.script("FATALITY?", [["0517 1032 3135 A14 NOT RESPONDING",
                                        "0518 0904 2004 FATAL FAULT", '"']])
        assert cal.fatality() == ["0517 1032 3135 A14 NOT RESPONDING",
                                  "0518 0904 2004 FATAL FAULT"]
        assert transport.timeouts[-1] == 45.0

    def test_a_silent_unit_raises_rather_than_reporting_an_empty_list(self):
        # An empty fatal-fault list and firmware that does not know the
        # command look identical from here (gap G31), and zero block lines is
        # also what a dead link looks like -- so it raises, and the caller
        # reads that as "no fatal-fault list available".
        cal, _ = opened(service=True)
        with pytest.raises(Fluke5700ACommError):
            cal.fatality()

    def test_fatalclr_uses_the_documented_prose_spelling_first(self):
        cal, transport = opened(service=True)
        cal.fatalclr()
        assert transport.log == ["FATALCLR", "FAULT?"]

    def test_fatalclr_falls_back_on_unknown_command(self):
        # A misspelled *command* faults with 2200; a misspelled *query* would
        # instead return nothing at all (gap G31).
        cal, transport = opened(service=True)
        transport.script("FAULT?", ["2200", "0", "0"])
        transport.script("EXPLAIN? 2200", '"Unknown Command"')
        cal.fatalclr()
        assert [c for c in transport.log if "CLR" in c] == ["FATALCLR",
                                                            "FATLCLR"]

    def test_fatalclr_raises_when_neither_spelling_is_known(self):
        cal, transport = opened(service=True)
        transport.script("FAULT?", ["2200", "0", "2200", "0"])
        transport.script("EXPLAIN? 2200", '"Unknown Command"')
        with pytest.raises(Fluke5700AError) as info:
            cal.fatalclr()
        assert "FATALCLR" in str(info.value) and "FATLCLR" in str(info.value)

    def test_fatalclr_reraises_a_real_fault(self):
        cal, transport = opened(service=True)
        transport.script("FAULT?", ["2217", "0"])
        transport.script("EXPLAIN? 2217", '"Command Reserved For Service"')
        with pytest.raises(CalibratorFault) as info:
            cal.fatalclr()
        assert info.value.codes == [(2217, "Command Reserved For Service")]


class TestOhmsRef:
    def test_uses_the_majority_spelling_first(self):
        cal, transport = opened()
        transport.script("OHMSREF? CHECK", "1.000021902360723E+00")
        assert cal.ohms_ref("CHECK") == pytest.approx(1.000021902360723)
        assert transport.log[0] == "OHMSREF? CHECK"

    def test_falls_back_when_the_primary_spelling_answers_nothing(self):
        # gap G31: an unknown query produces no response, so the probe can
        # only fail by timing out -- waiting for a fault would hang forever.
        cal, transport = opened()
        transport.script("OHMREF? CAL", "9.999870000E-01")
        assert cal.ohms_ref() == pytest.approx(0.999987)
        assert [c for c in transport.log if "REF?" in c] == ["OHMSREF? CAL",
                                                             "OHMREF? CAL"]

    def test_probes_with_a_short_timeout_and_restores_the_session_one(self):
        cal, transport = opened()
        transport.script("OHMREF? CAL", "9.999870000E-01")
        cal.ohms_ref()
        assert transport.timeouts[0] == 2.0
        assert transport.timeouts[-1] == 45.0

    def test_falls_back_on_an_unknown_command_fault(self):
        cal, transport = opened()
        transport.script("OHMSREF? CAL", "0")
        transport.script("FAULT?", ["2200", "0", "0"])
        transport.script("EXPLAIN? 2200", '"Unknown Command"')
        transport.script("OHMREF? CAL", "1.000004100E+00")
        assert cal.ohms_ref() == pytest.approx(1.0000041)

    def test_raises_when_neither_spelling_answers(self):
        cal, _ = opened()
        with pytest.raises(Fluke5700AError) as info:
            cal.ohms_ref()
        assert "revision H" in str(info.value)

    @pytest.mark.parametrize("group", ["ALL", "RAW", ""])
    def test_group_is_validated_before_any_io(self, group):
        cal, transport = opened()
        with pytest.raises(ValueError):
            cal.ohms_ref(group)
        assert transport.log == []


# ── calibration commands (guide §9, §14.6-§14.8) ──────────────────────

class TestCalTemp:
    def test_emits_the_temperature(self):
        cal, transport = opened()
        cal.cal_temp(23.5)
        assert transport.log == ["CAL_TEMP 23.5", "FAULT?"]

    def test_integral_temperature_is_not_padded(self):
        cal, transport = opened()
        cal.cal_temp(23.0)
        assert transport.log[0] == "CAL_TEMP 23"

    @pytest.mark.parametrize("celsius", [230.0, -10.0, 51.0, float("nan")])
    def test_refuses_a_temperature_outside_the_operating_range(self, celsius):
        # 230 for 23.0 is the typo that would otherwise silently poison a
        # whole calibration.
        cal, transport = opened()
        with pytest.raises(ValueError):
            cal.cal_temp(celsius)
        assert transport.log == []

    def test_query_accepts_check_but_not_zero(self):
        # CAL_TEMP? takes CHECK where CAL_DAYS?/CAL_DATE? take ZERO.
        cal, transport = opened()
        transport.script("CAL_TEMP? CHECK", "2.34000E+01")
        assert cal.cal_temp_query("CHECK") == pytest.approx(23.4)
        with pytest.raises(ValueError):
            cal.cal_temp_query("ZERO")


class TestCalLongTermCommands:
    def _ready(self, monkeypatch):
        cal, transport = opened()
        long_term_clock(monkeypatch, transport)
        transport.script("*ESR?", str(ESR_OPC))
        transport.script("STATE?", "0,Operating")
        return cal, transport

    def test_cal_zero(self, monkeypatch):
        cal, transport = self._ready(monkeypatch)
        cal.cal_zero()
        assert transport.log[1] == "CAL_ZERO;*OPC"

    @pytest.mark.parametrize("call,cmd,timeout", [
        (lambda cal: cal.cal_zero(), "CAL_ZERO", 420.0),
        # CAL_CHK gets its own, longer default: [OM §1-19] puts the check at
        # "about an hour", so the generic 30-minute long-term default would
        # expire while it was still running -- with no remote abort.
        (lambda cal: cal.cal_chk(), "CAL_CHK", 5400.0),
        (lambda cal: cal.cal_adj(), "CAL_ADJ", 1800.0),
        (lambda cal: cal.cal_ref(10.00013, "V"),
         "CAL_REF 1.0000130000E+01 V", 1800.0),
        (lambda cal: cal.diag("ALL"), "DIAG ALL", 1800.0),
    ])
    def test_default_timeouts_are_actually_wired_through(self, call, cmd,
                                                         timeout,
                                                         monkeypatch):
        # Asserting the constant's value proves nothing about its use.
        cal, _ = opened()
        seen = {}

        def fake(_self, command, **kwargs):
            seen["cmd"] = command
            seen.update(kwargs)
            return (0, "")

        monkeypatch.setattr(f57.Fluke5700A, "_run_long_term", fake)
        call(cal)
        assert seen["cmd"] == cmd
        assert seen["timeout"] == timeout

    def test_cal_zero_default_timeout_covers_the_documented_duration(self):
        # 2.5 min typical + 30 s with a 5725A; the brief's floor is 5 min.
        assert CAL_ZERO_TIMEOUT_SEC >= 300.0

    @pytest.mark.parametrize("method", ["cal_zero", "cal_chk", "cal_adj",
                                        "cal_ref"])
    def test_the_no_abort_hazard_is_documented_on_the_public_methods(
            self, method):
        # D1 and E1 authors read these, not the private runner.
        doc = getattr(Fluke5700A, method).__doc__
        assert "no remote abort" in doc
        assert "22 V" in doc

    def test_cal_chk(self, monkeypatch):
        cal, transport = self._ready(monkeypatch)
        cal.cal_chk()
        assert transport.log[1] == "CAL_CHK;*OPC"

    def test_cal_adj(self, monkeypatch):
        cal, transport = self._ready(monkeypatch)
        cal.cal_adj()
        assert transport.log[1] == "CAL_ADJ;*OPC"

    @pytest.mark.parametrize("value,unit,expected", [
        (10.00013, "V", "CAL_REF 1.0000130000E+01 V;*OPC"),
        (-10.00013, "V", "CAL_REF -1.0000130000E+01 V;*OPC"),
        (9.99987, "KOHM", "CAL_REF 9.9998700000E+00 KOHM;*OPC"),
        (1.00026, "OHM", "CAL_REF 1.0002600000E+00 OHM;*OPC"),
    ])
    def test_cal_ref_emits_the_four_artifact_steps(self, value, unit,
                                                   expected, monkeypatch):
        cal, transport = self._ready(monkeypatch)
        cal.cal_ref(value, unit)
        assert transport.log[1] == expected

    @pytest.mark.parametrize("unit", ["A", "HZ", "MOHM", ""])
    def test_cal_ref_rejects_a_unit_that_names_no_procedure(self, unit):
        cal, transport = opened()
        with pytest.raises(ValueError):
            cal.cal_ref(10.0, unit)
        assert transport.log == []

    def test_cal_ref_does_not_enforce_the_sequence(self, monkeypatch):
        # Ordering is the calibration procedure's job (fault 852 is the
        # instrument's own guard); the driver exposes one step.
        cal, transport = self._ready(monkeypatch)
        cal.cal_ref(1.00026, "OHM")           # step 4 sent first
        assert transport.log[1] == "CAL_REF 1.0002600000E+00 OHM;*OPC"

    def test_cal_adj_is_not_broken_by_a_preceding_block_read(self,
                                                             monkeypatch):
        # The failure this guards: read_block shortens the transport timeout
        # to 2 s, and if it is not restored the *ESR? polling that follows a
        # CAL_ADJ fails as a spurious comm error.
        cal, transport = opened()
        transport.script("CAL_RPT? CAL", [["report line", '"']])
        cal.cal_rpt("CAL")
        assert transport.timeouts == [2.0, 45.0]
        long_term_clock(monkeypatch, transport)
        transport.script("*ESR?", str(ESR_OPC))
        transport.script("STATE?", "0,Operating")
        cal.cal_adj()
        assert transport.timeouts == [2.0, 45.0]      # unchanged since


class TestCalStore:
    def test_emits_cal_store_with_a_barrier(self):
        cal, transport = opened()
        cal.cal_store()
        assert transport.log == ["CAL_STORE;*WAI", "FAULT?"]

    def test_fault_845_names_the_calibration_switch(self):
        cal, transport = opened()
        transport.script("FAULT?", ["845", "0"])
        transport.script("EXPLAIN? 845",
                         '"Cannot Store, Rear Switch In NORMAL"')
        with pytest.raises(CalibrationSwitchError) as info:
            cal.cal_store()
        message = str(info.value)
        assert "CALIBRATION switch" in message
        assert "NORMAL" in message
        assert "ENABLE" in message
        assert "nothing was stored" in message

    def test_switch_error_is_still_a_calibrator_fault(self):
        cal, transport = opened()
        transport.script("FAULT?", ["845", "0"])
        with pytest.raises(CalibratorFault):
            cal.cal_store()

    def test_fault_851_names_the_amplifiers_own_switch(self):
        cal, transport = opened()
        transport.script("FAULT?", ["851", "0"])
        with pytest.raises(CalibrationSwitchError) as info:
            cal.cal_store()
        assert "5725A" in str(info.value)

    def test_an_unrelated_fault_stays_a_plain_calibrator_fault(self):
        cal, transport = opened()
        transport.script("FAULT?", ["852", "0"])
        with pytest.raises(CalibratorFault) as info:
            cal.cal_store()
        assert not isinstance(info.value, CalibrationSwitchError)


class TestCalRng:
    def test_emits_range_magnitude_and_unit(self):
        cal, transport = opened()
        cal.cal_rng("DC2_2V", 2.0)
        assert transport.log == ["CAL_RNG DC2_2V, 2.0000000000E+00 V",
                                 "FAULT?"]

    def test_emits_a_frequency_when_given(self):
        cal, transport = opened()
        cal.cal_rng("AC22V", 10.0, 1000.0)
        assert transport.log[0] == (
            "CAL_RNG AC22V, 1.0000000000E+01 V, 1.000000E+03 HZ")

    def test_current_range_gets_amps_not_a_multiplier_prefix(self):
        cal, transport = opened()
        cal.cal_rng("DC220MA", 0.15)
        assert transport.log[0] == "CAL_RNG DC220MA, 1.5000000000E-01 A"

    @pytest.mark.parametrize("magnitude", [0.5, 0.98, 2.1, 2.2, 0.0, -0.5])
    def test_rejects_a_magnitude_outside_45_to_95_percent_offline(
            self, magnitude):
        # 45 % of the 2.2 V range is 0.99 V and 95 % is 2.09 V.
        cal, transport = opened()
        with pytest.raises(ValueError) as info:
            cal.cal_rng("DC2_2V", magnitude)
        assert "45%" in str(info.value) or "95%" in str(info.value)
        assert transport.log == []

    @pytest.mark.parametrize("magnitude", [0.99, 1.5, 2.09, -1.5])
    def test_accepts_a_magnitude_inside_the_window(self, magnitude):
        cal, transport = opened()
        cal.cal_rng("DC2_2V", magnitude)
        assert transport.log[0].startswith("CAL_RNG DC2_2V,")

    @pytest.mark.parametrize("range_id", ["DC2_2", "DC1100", "NOPE", "OHM5"])
    def test_rejects_an_unrecognized_range_identifier(self, range_id):
        cal, transport = opened()
        with pytest.raises(ValueError):
            cal.cal_rng(range_id, 1.0)
        assert transport.log == []

    def test_range_without_a_magnitude_is_rejected(self):
        cal, transport = opened()
        with pytest.raises(ValueError):
            cal.cal_rng("DC2_2V")
        assert transport.log == []

    @pytest.mark.parametrize("keyword", ["NULL", "STORE", "null"])
    def test_null_and_store_take_no_magnitude(self, keyword):
        cal, transport = opened()
        cal.cal_rng(keyword)
        assert transport.log[0] == f"CAL_RNG {keyword.upper()}"

    def test_null_with_a_magnitude_is_rejected(self):
        cal, transport = opened()
        with pytest.raises(ValueError):
            cal.cal_rng("NULL", 1.0)
        assert transport.log == []

    def test_store_reports_the_calibration_switch(self):
        cal, transport = opened()
        transport.script("FAULT?", ["845", "0"])
        with pytest.raises(CalibrationSwitchError):
            cal.cal_rng("STORE")

    def test_resistance_range_skips_the_percentage_check(self):
        # In the resistance function the range *is* a single value, so a
        # percentage of it is meaningless; faults 847/848 remain the
        # authority there.
        cal, transport = opened()
        cal.cal_rng("OHM10K", 10.0e3)
        assert transport.log[0] == "CAL_RNG OHM10K, 1.0000000000E+04 OHM"


class TestValidateCalRng:
    @pytest.mark.parametrize("range_id,magnitude,ok", [
        ("DC220MV", 0.1, True),        # 45.5 % of 220 mV
        ("DC220MV", 0.09, False),      # 40.9 %
        ("DC220MV", 0.21, False),      # 95.5 %
        ("AC1100V", 600.0, True),
        ("AC1100V", 1100.0, False),
        ("DC2_2A", 1.0, True),
        ("WB110MV", 0.06, True),
    ])
    def test_window(self, range_id, magnitude, ok):
        assert (validate_cal_rng(range_id, magnitude) == []) is ok

    def test_amplifier_ranges_are_recognized_but_unchecked(self):
        assert validate_cal_rng("DC5725A", 7.0) == []
        assert validate_cal_rng("AC5725V", 900.0) == []

    def test_unknown_identifier_is_a_violation(self):
        assert validate_cal_rng("DC1100", 500.0) != []

    @pytest.mark.parametrize("range_id,unit", [
        ("DC220MV", "V"), ("AC5725V", "V"), ("WB3_3MV", "V"),
        ("DC220UA", "A"), ("DC5725A", "A"), ("AC2_2A", "A"),
        ("OHM10K", "OHM"), ("OHM1_9M", "OHM"),
    ])
    def test_unit_is_derived_from_the_identifier(self, range_id, unit):
        assert cal_rng_unit(range_id) == unit


# ── calibration reports and constants ─────────────────────────────────

CAL_SHIFT_ROWS = [
    "2.20E-1,0.00E+00,1.76E-07,1.97E-07,8.98E-01,7.10E+00,1.26E+01",
    "-2.20E-1,0.00E+00,1.58E-07,1.38E-07,6.26E-01,4.95E+00,1.26E+01",
]


class TestCalReportReaders:
    def test_cal_rpt_returns_the_text_verbatim(self):
        cal, transport = opened()
        # Line by line, unparsed: the report grammar is gap G24, so it is
        # parsed once, elsewhere, against a real capture -- not twice.
        # (Leading indentation does not survive: the transport strips every
        # response line, here and on real GPIB alike.)
        transport.script("CAL_RPT? CAL",
                         [['"', "5700A CALIBRATION REPORT",
                           "DC220MV 2.20E-1 0.898 12.6", '"']])
        assert cal.cal_rpt("CAL") == ("5700A CALIBRATION REPORT\n"
                                      "DC220MV 2.20E-1 0.898 12.6")

    @pytest.mark.parametrize("group", ["PREV", "ALL", ""])
    def test_cal_rpt_group_is_validated(self, group):
        cal, transport = opened()
        with pytest.raises(ValueError):
            cal.cal_rpt(group)
        assert transport.log == []

    def test_cal_shift_parses_one_range(self):
        cal, transport = opened()
        transport.script("CAL_SHIFT? CAL, DC220MV",
                         [['"', "DC220MV,2"] + CAL_SHIFT_ROWS + ['"']])
        shift = cal.cal_shift("CAL", "DC220MV")
        assert shift.range_id == "DC220MV"
        assert len(shift.points) == 2
        assert shift.points[0].relative_shift_ppm == pytest.approx(0.898)
        assert shift.points[0].spec_ppm == pytest.approx(12.6)
        assert shift.points[1].magnitude == pytest.approx(-0.22)

    def test_cal_shift_rejects_an_unknown_range(self):
        cal, transport = opened()
        with pytest.raises(ValueError):
            cal.cal_shift("CAL", "DC220")
        assert transport.log == []

    def test_cal_slst_parses_every_range(self):
        cal, transport = opened()
        transport.script("CAL_SLST? CHECK", [[
            '"', "2",
            "DC220MV,2", *CAL_SHIFT_ROWS,
            "DC2_2V,1",
            "2.00E+00,0.00E+00,1.10E-06,2.30E-06,1.15E+00,9.10E+00,1.26E+01",
            '"']])
        ranges = cal.cal_slst("CHECK")
        assert [entry.range_id for entry in ranges] == ["DC220MV", "DC2_2V"]
        assert len(ranges[0].points) == 2 and len(ranges[1].points) == 1

    def test_cal_clst_returns_name_value_pairs(self):
        cal, transport = opened()
        transport.script("CAL_CLST? CAL", [[
            '"', "3", "D3P, 3.9817876E+02", "D3M, -3.9712004E+02",
            "KV6 6.5000010E+00", '"']])
        constants = cal.cal_clst("CAL")
        assert constants["D3P"] == pytest.approx(398.17876)
        assert constants["D3M"] == pytest.approx(-397.12004)
        assert constants["KV6"] == pytest.approx(6.500001)

    def test_cal_const_returns_a_float(self):
        cal, transport = opened()
        transport.script("CAL_CONST? CHECK, KV6", "6.5000010E+00")
        assert cal.cal_const("CHECK", "KV6") == pytest.approx(6.500001)
        assert transport.log[0] == "CAL_CONST? CHECK, KV6"

    def test_cal_const_needs_a_name(self):
        cal, transport = opened()
        with pytest.raises(ValueError):
            cal.cal_const("CAL", "  ")
        assert transport.log == []

    @pytest.mark.parametrize("method,group", [
        ("cal_slst", "PREV"),
        ("cal_clst", "RAW"),
    ])
    def test_group_selectors_differ_per_command(self, method, group):
        cal, transport = opened()
        with pytest.raises(ValueError):
            getattr(cal, method)(group)
        assert transport.log == []

    @pytest.mark.parametrize("method,args,command", [
        ("cal_rpt", ("CAL",), "CAL_RPT? CAL"),
        ("cal_slst", ("CAL",), "CAL_SLST? CAL"),
        ("cal_clst", ("CAL",), "CAL_CLST? CAL"),
    ])
    def test_block_readers_restore_the_session_timeout(self, method, args,
                                                       command):
        cal, transport = opened(timeout=120.0)
        # Nothing scripted: the block read fails, and the session timeout
        # still has to come back or every later query is broken.
        with pytest.raises(Fluke5700AError):
            getattr(cal, method)(*args)
        assert transport.timeouts[-1] == 120.0

    def test_fatality_restores_the_session_timeout_after_a_failure(self):
        cal, transport = opened(service=True, timeout=120.0)
        with pytest.raises(Fluke5700AError):
            cal.fatality()
        assert transport.timeouts[-1] == 120.0


class TestShiftParsers:
    def test_point_fields_are_in_order(self):
        shift = parse_range_shift(["DC220MV,1", CAL_SHIFT_ROWS[0]])
        assert shift.points[0] == ShiftPoint(
            magnitude=0.22, frequency_hz=0.0, offset=1.76e-07,
            absolute_shift=1.97e-07, relative_shift_ppm=0.898,
            percent_of_spec=7.10, spec_ppm=12.6)

    def test_truncated_range_block_raises(self):
        with pytest.raises(Fluke5700AProtocolError):
            parse_range_shift(["DC220MV,2", CAL_SHIFT_ROWS[0]])

    def test_wrong_field_count_raises(self):
        with pytest.raises(Fluke5700AProtocolError):
            parse_range_shift(["DC220MV,1", "2.20E-1,0.00E+00,1.76E-07"])

    def test_more_than_one_range_is_rejected_for_cal_shift(self):
        with pytest.raises(Fluke5700AProtocolError):
            parse_range_shift(["DC220MV,1", CAL_SHIFT_ROWS[0],
                               "DC2_2V,1", CAL_SHIFT_ROWS[1]])

    def test_slst_count_may_be_ranges_or_points(self):
        lines = ["DC220MV,2", *CAL_SHIFT_ROWS]
        assert len(parse_shift_list(["1"] + lines)) == 1     # ranges
        assert len(parse_shift_list(["2"] + lines)) == 1     # points

    def test_slst_count_mismatch_raises_rather_than_truncating(self):
        with pytest.raises(Fluke5700AProtocolError) as info:
            parse_shift_list(["49", "DC220MV,2", *CAL_SHIFT_ROWS])
        assert "truncated" in str(info.value)

    def test_range_shift_is_hashable_data(self):
        shift = parse_range_shift(["DC220MV,1", CAL_SHIFT_ROWS[0]])
        assert isinstance(shift, RangeShift)
        assert hash(shift.points)          # frozen dataclasses in a tuple


class TestConstantListParser:
    def test_accepts_both_separators(self):
        constants = parse_constant_list(
            ["2", "D3P, 3.9817876E+02", "KV6 6.5000010E+00"])
        assert constants == {"D3P": pytest.approx(398.17876),
                             "KV6": pytest.approx(6.500001)}

    def test_count_mismatch_raises(self):
        with pytest.raises(Fluke5700AProtocolError) as info:
            parse_constant_list(["424", "D3P, 3.9817876E+02"])
        assert "truncated" in str(info.value)

    def test_duplicate_name_raises(self):
        with pytest.raises(Fluke5700AProtocolError):
            parse_constant_list(
                ["2", "D3P, 3.9817876E+02", "D3P, 3.9817877E+02"])

    def test_malformed_row_raises(self):
        with pytest.raises(Fluke5700AProtocolError):
            parse_constant_list(["1", "D3P"])


class TestCalIntervalAndConfidence:
    def test_interval_query(self):
        cal, transport = opened()
        transport.script("CAL_INTV?", "3.65E+02")     # floats come back freely
        assert cal.cal_intv() == 365

    @pytest.mark.parametrize("days", [1, 90, 180, 365])
    def test_interval_accepts_the_documented_values(self, days):
        cal, transport = opened()
        cal.set_cal_intv(days)
        assert transport.log[0] == f"CAL_INTV {days}"

    @pytest.mark.parametrize("days", [0, 30, 366, 7])
    def test_interval_rejects_anything_else(self, days):
        cal, transport = opened()
        with pytest.raises(ValueError):
            cal.set_cal_intv(days)
        assert transport.log == []

    def test_confidence_query(self):
        cal, transport = opened()
        transport.script("CAL_CONF?", '"conf99"')
        assert cal.cal_conf() == "CONF99"

    @pytest.mark.parametrize("given,sent", [
        ("CONF95", "CAL_CONF CONF95"),
        ("CONF99", "CAL_CONF CONF99"),
        (95, "CAL_CONF CONF95"),
        ("99", "CAL_CONF CONF99"),
    ])
    def test_confidence_normalizes_to_the_om_spelling(self, given, sent):
        # guide §13 item 5: OM says CONF95/CONF99, RPR says 95/99; the OM
        # form matches what CAL_CONF? answers.
        cal, transport = opened()
        cal.set_cal_conf(given)
        assert transport.log[0] == sent

    @pytest.mark.parametrize("level", ["CONF90", "90", ""])
    def test_confidence_rejects_anything_else(self, level):
        cal, transport = opened()
        with pytest.raises(ValueError):
            cal.set_cal_conf(level)
        assert transport.log == []


class TestCalDatesAndDays:
    def test_days_since_activity(self):
        cal, transport = opened()
        transport.script("CAL_DAYS? ZERO", "17")
        assert cal.cal_days("ZERO") == 17

    def test_date_keeps_its_leading_zero(self):
        # gap G25: the field is typed (Integer) but is a zero-padded 6-digit
        # date -- 9 January 1988 comes back as 10988 in MDY.
        cal, transport = opened()
        transport.script("CAL_DATE? CAL", "10988")
        assert cal.cal_date("CAL") == "010988"

    @pytest.mark.parametrize("selector", ["CHECK", "ALL", ""])
    def test_date_and_days_reject_the_cal_temp_selectors(self, selector):
        cal, transport = opened()
        with pytest.raises(ValueError):
            cal.cal_days(selector)
        with pytest.raises(ValueError):
            cal.cal_date(selector)
        assert transport.log == []


# ── housekeeping (guide §14.11) ───────────────────────────────────────

class TestHousekeeping:
    def test_etime_and_ontime(self):
        cal, transport = opened()
        transport.script("ETIME?", "4628000")
        transport.script("ONTIME?", "42")
        assert cal.etime() == 4628000
        assert cal.ontime() == 42

    def test_warmup_gate_is_thirty_minutes(self):
        assert f57.WARMUP_MAX_MINUTES == 30

    def test_set_etime_is_validated(self):
        cal, transport = opened(service=True)
        cal.set_etime(4628000)
        assert transport.log[0] == "ETIME SET_TO, 4628000"
        with pytest.raises(ValueError):
            cal.set_etime(-1)

    def test_clock_keeps_leading_zeros(self):
        cal, transport = opened()
        transport.script("CLOCK?", "80000,090188")
        assert cal.clock() == ("080000", "090188")

    def test_set_clock_pads_and_reports_the_switch(self):
        cal, transport = opened()
        cal.set_clock(133700, "071788")
        assert transport.log[0] == "CLOCK 133700, 071788"
        transport.script("FAULT?", ["838", "0"])
        with pytest.raises(CalibrationSwitchError):
            cal.set_clock("080000", "101088")

    def test_datefmt_round_trip(self):
        cal, transport = opened()
        transport.script("DATEFMT?", '"MDY"')
        assert cal.datefmt() == "MDY"
        cal.set_datefmt("ymd")
        assert transport.log[-2] == "DATEFMT YMD"
        with pytest.raises(ValueError):
            cal.set_datefmt("ISO")

    def test_rpt_str_round_trip(self):
        cal, transport = opened()
        transport.script("RPT_STR?", '"Example lab, bench 2"')
        assert cal.rpt_str() == "Example lab, bench 2"
        cal.set_rpt_str("Example lab, bench 2")
        assert transport.log[-2] == 'RPT_STR "Example lab, bench 2"'

    def test_rpt_str_length_is_checked_offline(self):
        cal, transport = opened()
        with pytest.raises(ValueError):
            cal.set_rpt_str("x" * 41)
        assert cal.set_rpt_str("x" * 40) is None
        assert len([c for c in transport.log if c.startswith("RPT_STR")]) == 1

    def test_rpt_str_reports_the_calibration_switch(self):
        cal, transport = opened()
        transport.script("FAULT?", ["855", "0"])
        with pytest.raises(CalibrationSwitchError):
            cal.set_rpt_str("nope")

    def test_pud_round_trip(self):
        cal, transport = opened()
        cal.set_pud("FLUKE")
        assert transport.log[0] == "*PUD #15FLUKE"
        transport.script("*PUD?", "205FLUKE")
        assert cal.pud() == "FLUKE"

    def test_pud_length_and_content_are_checked_offline(self):
        cal, transport = opened()
        with pytest.raises(ValueError):
            cal.set_pud("x" * 64)
        with pytest.raises(ValueError):
            cal.set_pud("bad\nvalue")
        assert transport.log == []

    def test_pud_counted_prefix_grows_with_the_payload(self):
        cal, transport = opened()
        cal.set_pud("x" * 12)
        assert transport.log[0] == "*PUD #212" + "x" * 12

    def test_echo_is_a_link_test(self):
        cal, transport = opened()
        transport.script('ECHO? "123abc456"', '"123abc456"')
        assert cal.echo("123abc456") == "123abc456"

    def test_echo_rejects_an_unescapable_quote(self):
        cal, transport = opened()
        with pytest.raises(ValueError):
            cal.echo('say "hi"')
        assert transport.log == []

    def test_lrn_returns_the_string_verbatim(self):
        cal, transport = opened()
        saved = "OUT 1.0E+01 V,0.0E+00 HZ;OPER;EXTSENSE OFF"
        transport.script("*LRN?", saved)
        assert cal.lrn() == saved

    def test_there_is_no_blind_restore_helper(self):
        # *LRN? re-establishes Operate (guide §14.10), so replaying it can
        # energize the output at up to 1100 V. Returning the string is the
        # whole API on purpose.
        assert not hasattr(Fluke5700A, "restore")
        assert "re-establishes Operate" in Fluke5700A.lrn.__doc__


class TestClockAndPudParsers:
    @pytest.mark.parametrize("resp,expected", [
        ("150000,090188", ("150000", "090188")),
        ("80000,090188", ("080000", "090188")),
        ('"133700,071788"', ("133700", "071788")),
    ])
    def test_parse_clock(self, resp, expected):
        assert parse_clock(resp) == expected

    @pytest.mark.parametrize("resp", ["150000", "a,b", ""])
    def test_parse_clock_rejects_malformed(self, resp):
        with pytest.raises(Fluke5700AProtocolError):
            parse_clock(resp)

    @pytest.mark.parametrize("resp,expected", [
        ("#15FLUKE", "FLUKE"),
        ("205FLUKE", "FLUKE"),          # the manual's own example, no '#'
        ('"#15FLUKE"', "FLUKE"),
        ("", ""),
    ])
    def test_parse_pud(self, resp, expected):
        assert parse_pud(resp) == expected

    @pytest.mark.parametrize("resp", ["#1", "#19FLUKE", "#XY"])
    def test_parse_pud_rejects_malformed(self, resp):
        with pytest.raises(Fluke5700AProtocolError):
            parse_pud(resp)


# ── wideband output (Option 5700A-03, [MS5] change #1) ────────────────

from caldmm.drivers.fluke5700a import (           # noqa: E402
    ISR_WBND,
    WIDEBAND_FREQ_MAX_HZ,
    WIDEBAND_FREQ_MIN_HZ,
    WIDEBAND_RANGES,
)


class TestWidebandValidation:
    def test_a_wideband_point_is_accepted(self):
        assert validate_output(1.0, "V", 1e6, wideband=True) == []
        assert validate_output(3.5, "V", 30e6, wideband=True) == []
        assert validate_output(300e-6, "V", 10.0, wideband=True) == []

    def test_above_3_5V_is_refused(self):
        problems = validate_output(5.0, "V", 1e6, wideband=True)
        assert any("3.5 V wideband maximum" in p for p in problems)

    def test_below_300uV_is_refused(self):
        problems = validate_output(200e-6, "V", 1e6, wideband=True)
        assert any("wideband minimum output" in p for p in problems)

    def test_dc_is_refused(self):
        problems = validate_output(1.0, "V", 0.0, wideband=True)
        assert any("ac only" in p for p in problems)

    def test_above_30MHz_is_refused(self):
        problems = validate_output(1.0, "V", 50e6, wideband=True)
        assert any("30 MHz" in p or "3e+07" in p for p in problems)

    def test_the_volt_hertz_ceiling_does_not_apply(self):
        # 3.5 V * 30 MHz is 1.05e8 V*Hz -- far past the standard 2.2e7
        # envelope, and legal on the wideband output.
        assert validate_output(3.5, "V", 30e6, wideband=True) == []

    def test_non_volts_are_refused(self):
        problems = validate_output(1.0, "A", 1e3, wideband=True)
        assert any("WBAND OFF" in p for p in problems)

    def test_negative_rms_is_refused(self):
        problems = validate_output(-1.0, "V", 1e3, wideband=True)
        assert any("positive rms" in p for p in problems)

    def test_a_pinned_wideband_range_is_honoured(self):
        assert validate_output(1e-3, "V", 1e3, wideband=True,
                               range_id="WB1_1MV") == []
        problems = validate_output(2.0, "V", 1e3, wideband=True,
                                   range_id="WB1_1MV")
        assert any("full scale of the declared WB1_1MV" in p
                   for p in problems)

    def test_an_unknown_wideband_range_pin_is_refused(self):
        problems = validate_output(1e-3, "V", 1e3, wideband=True,
                                   range_id="WB2MV")
        assert any("unknown range identifier" in p for p in problems)

    def test_expected_range_id_wideband(self):
        assert expected_range_id(1e-3, "V", 1e3,
                                 wideband=True) == "WB1_1MV"
        assert expected_range_id(3.5, "V", 30e6, wideband=True) == "WB3_5V"
        assert expected_range_id(0.2, "V", 1e3, wideband=True) == "WB330MV"
        assert expected_range_id(5.0, "V", 1e3, wideband=True) is None
        assert expected_range_id(1.0, "V", 0.0, wideband=True) is None


class TestWidebandSourcing:
    def test_wband_on_sends_and_tracks(self):
        cal, transport = open_calibrator(faults=["0", "0", "0"])
        transport.log.clear()
        cal.wband(True)
        assert transport.log[0] == "WBAND ON"
        assert cal._wideband is True
        cal.wband(False)
        assert "WBAND OFF" in transport.log
        assert cal._wideband is False

    def test_wband_on_a_unit_without_the_module_raises(self):
        # Fault 800 (Wideband Module Needed): *OPT? has no documented
        # spelling for the module, so the instrument is the offline gate.
        cal, transport = open_calibrator(faults=["0", "0"])
        transport.script("FAULT?", ["800", "0"])
        with pytest.raises(CalibratorFault) as exc:
            cal.wband(True)
        assert [code for code, _ in exc.value.codes] == [800]
        assert cal._wideband is False

    def test_is_wideband_reads_isr(self):
        cal, transport = open_calibrator()
        transport.script("ISR?", str(ISR_REMOTE | ISR_WBND))
        assert cal.is_wideband() is True
        transport.script("ISR?", str(ISR_REMOTE))
        assert cal.is_wideband() is False

    def test_set_output_validates_the_wideband_envelope(self):
        cal, transport = open_calibrator(faults=["0", "0", "0", "0"])
        cal.wband(True)
        transport.log.clear()
        with pytest.raises(ValueError, match="wideband"):
            cal.set_output(5.0, "V", 1e6)     # legal on AC22V, not wideband
        assert transport.log == []
        cal.set_output(1.0, "V", 1e6)
        assert any(entry.startswith("OUT 1.0000000000E+00 V")
                   for entry in transport.log)

    def test_wideband_settle_estimate_uses_the_wideband_row(self):
        cal, transport = open_calibrator(
            faults=["0", "0", "0", "0", "0", "0", "0", "0"])
        cal.wband(True)
        cal.set_output(1.0, "V", 50.0)
        assert cal._settle_estimate == 7.0     # < 120 Hz
        cal.set_output(1.0, "V", 1e5)
        assert cal._settle_estimate == 5.0     # 120 Hz - 1.2 MHz
        cal.set_output(1.0, "V", 10e6)
        assert cal._settle_estimate == 0.5     # above 1.2 MHz

    def test_wband_off_restores_standard_validation(self):
        cal, transport = open_calibrator(
            faults=["0", "0", "0", "0", "0", "0"])
        cal.wband(True)
        cal.wband(False)
        cal.set_output(10.0, "V")              # dc: illegal on wideband
        assert any(entry.startswith("OUT 1.0000000000E+01 V")
                   for entry in transport.log)


class TestWidebandCal:
    def _ready(self, monkeypatch):
        cal, transport = opened()
        long_term_clock(monkeypatch, transport)
        transport.script("*ESR?", str(ESR_OPC))
        transport.script("STATE?", "0,Operating")
        return cal, transport

    def test_pgain_and_ngain_are_long_term(self, monkeypatch):
        cal, transport = self._ready(monkeypatch)
        cal.cal_wbgain("PGAIN")
        assert transport.log[1] == "CAL_WBGAIN PGAIN;*OPC"

    def test_store_is_immediate_with_wai(self):
        cal, transport = opened()
        transport.log.clear()
        cal.cal_wbgain("STORE")
        assert transport.log[0] == "CAL_WBGAIN STORE;*WAI"

    def test_store_without_the_switch_raises_the_switch_error(self):
        cal, transport = opened()
        transport.script("FAULT?", ["845", "0"])
        transport.script("EXPLAIN? 845", '"Must Enable Cal Sw To Continue"')
        with pytest.raises(CalibrationSwitchError):
            cal.cal_wbgain("STORE")

    def test_an_unknown_step_is_refused_offline(self):
        cal, transport = opened()
        transport.log.clear()
        with pytest.raises(ValueError):
            cal.cal_wbgain("GAIN")
        assert transport.log == []

    def test_wbflat_steps_are_immediate(self):
        cal, transport = opened()
        transport.log.clear()
        cal.cal_wbflat("START")
        cal.cal_wbflat("NULL")
        assert transport.log[0] == "CAL_WBFLAT START"
        assert "CAL_WBFLAT NULL" in transport.log

    def test_wbflat_faults_raise(self):
        # 843: Wideband Flatness Cal Is Not Active.
        cal, transport = opened()
        transport.script("FAULT?", ["843", "0"])
        transport.script("EXPLAIN? 843",
                         '"Wideband Flatness Cal Is Not Active"')
        with pytest.raises(CalibratorFault):
            cal.cal_wbflat("NULL")


# ── output editing and entry limits ───────────────────────────────────

class TestOutputEditing:
    def test_incr_magnitude(self):
        cal, transport = open_calibrator(faults=["0", "0"])
        transport.log.clear()
        cal.incr(-1e-5)
        assert transport.log[0] == "INCR -1.0000000000E-05"

    def test_incr_frequency(self):
        cal, transport = open_calibrator(faults=["0", "0"])
        transport.log.clear()
        cal.incr(1.0, "HZ")
        assert transport.log[0] == "INCR 1.0000000000E+00 HZ"

    def test_incr_refuses_other_units_offline(self):
        cal, transport = open_calibrator()
        transport.log.clear()
        with pytest.raises(ValueError):
            cal.incr(1.0, "MV")
        with pytest.raises(ValueError):
            cal.incr(0.0)
        assert transport.log == []

    def test_incr_faults_raise(self):
        # 823: Cannot Adjust Frequency To <= 0 Hz.
        cal, transport = open_calibrator()
        transport.script("FAULT?", ["823", "0"])
        transport.script("EXPLAIN? 823",
                         '"Cannot Adjust Frequency To <= 0 Hz"')
        with pytest.raises(CalibratorFault):
            cal.incr(-100.0, "HZ")

    def test_newref(self):
        cal, transport = open_calibrator(faults=["0", "0"])
        transport.log.clear()
        cal.newref()
        assert transport.log[0] == "NEWREF"

    def test_mult(self):
        cal, transport = open_calibrator(faults=["0", "0"])
        transport.log.clear()
        cal.mult(1.9)
        assert transport.log[0] == "MULT 1.9000000000E+00"

    def test_mult_refuses_zero_offline(self):
        cal, transport = open_calibrator()
        transport.log.clear()
        with pytest.raises(ValueError):
            cal.mult(0.0)
        assert transport.log == []


class TestLimits:
    def test_set_limits_sends_both_parameters(self):
        cal, transport = open_calibrator(faults=["0", "0"])
        transport.log.clear()
        cal.set_limits(25.0, -25.0)
        assert transport.log[0] == \
            "LIMIT 2.5000000000E+01 V, -2.5000000000E+01 V"

    def test_set_limits_current(self):
        cal, transport = open_calibrator(faults=["0", "0"])
        transport.log.clear()
        cal.set_limits(1.8, -1.2, "A")
        assert transport.log[0] == \
            "LIMIT 1.8000000000E+00 A, -1.2000000000E+00 A"

    def test_wrong_polarity_is_refused_offline(self):
        # Fault 814's territory, caught before any I/O.
        cal, transport = open_calibrator()
        transport.log.clear()
        with pytest.raises(ValueError):
            cal.set_limits(-25.0, 25.0)
        assert transport.log == []

    def test_limits_query_parses_four_fields(self):
        cal, transport = open_calibrator()
        transport.script("LIMIT?", "2.20E+2,-1.0E+2,1.8E0,-1.2E0")
        assert cal.limits() == (220.0, -100.0, 1.8, -1.2)


class TestLatchedStatus:
    def test_iscr_reads_the_change_register(self):
        cal, transport = open_calibrator()
        transport.script("ISCR?", "8")
        assert cal.iscr() == 8
        assert "ISCR?" in transport.log

    def test_isce_write_and_read(self):
        cal, transport = open_calibrator()
        transport.script("ISCE?", "56")
        cal.isce(56)
        assert cal.isce_query() == 56
        assert "ISCE 56" in transport.log

    def test_isce_mask_is_range_checked(self):
        cal, transport = open_calibrator()
        transport.log.clear()
        with pytest.raises(ValueError):
            cal.isce(70000)
        assert transport.log == []


class TestStandbyRetryMinimum:
    """Operator ruling 2026-11-02: retry for at least 10 s."""

    def test_a_stuck_low_voltage_output_is_polled_for_ten_seconds(
            self, monkeypatch):
        cal, transport = energize(0.1)
        clock = FakeClock(transport.events)
        monkeypatch.setattr(f57, "time", clock)
        with pytest.raises(Fluke5700AError):
            cal.standby()
        assert clock.now == pytest.approx(10.0, abs=SLACK)

    def test_an_output_that_comes_off_after_three_seconds_is_fine(
            self, monkeypatch):
        cal, transport = energize(10.0, "V", hz=1.0e3)
        clock = FakeClock(transport.events)
        monkeypatch.setattr(f57, "time", clock)
        transport.script(
            "ISR?",
            lambda _cmd: str(ISR_REMOTE | (ISR_OPER if clock.now < 3.0 else 0)))
        cal.standby()
        assert 3.0 <= clock.now < 3.0 + 2 * SLACK
