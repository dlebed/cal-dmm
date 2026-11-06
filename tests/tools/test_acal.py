"""Tests for caldmm.tools.acal CLI parser."""

import pytest

from caldmm.tools.acal import build_parser


class TestBuildParser:
    def test_requires_3458a(self):
        with pytest.raises(SystemExit):
            build_parser().parse_args([])

    def test_defaults(self):
        args = build_parser().parse_args(['--3458a', 'ref:22'])
        assert args.dmm_spec == 'ref:22'
        assert args.gpib_bus == 0
        assert args.interval == 24
        assert args.acal_type == 'DCV'
        assert args.settle == 30
        assert args.csv_file is None
        assert args.ambient is None

    def test_custom_interval(self):
        args = build_parser().parse_args(['--3458a', 'ref:22', '-i', '4'])
        assert args.interval == 4

    def test_acal_type_all(self):
        args = build_parser().parse_args(['--3458a', 'ref:22', '--acal-type', 'ALL'])
        assert args.acal_type == 'ALL'

    def test_invalid_acal_type(self):
        with pytest.raises(SystemExit):
            build_parser().parse_args(['--3458a', 'ref:22', '--acal-type', 'INVALID'])

    def test_csv_output(self):
        args = build_parser().parse_args(['--3458a', 'ref:22', '-o', 'acal.csv'])
        assert args.csv_file == 'acal.csv'

    def test_ambient(self):
        args = build_parser().parse_args(['--3458a', 'ref:22', '--ambient', '23.1'])
        assert args.ambient == pytest.approx(23.1)


class TestResolveAddress:
    def test_bare_pad_uses_the_default_bus(self):
        from caldmm.tools.acal import _resolve_address
        assert _resolve_address('22', 1) == (1, 22)

    def test_bus_pad_wins(self):
        from caldmm.tools.acal import _resolve_address
        assert _resolve_address('2,22', 0) == (2, 22)

    def test_settle_time(self):
        args = build_parser().parse_args(['--3458a', 'ref:22', '--settle', '120'])
        assert args.settle == 120
