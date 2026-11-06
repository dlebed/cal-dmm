"""Tests for caldmm.tools.interactive CLI parser."""

import pytest

from caldmm.tools.interactive import build_parser


class TestBuildParser:
    def test_requires_instrument(self):
        with pytest.raises(SystemExit):
            build_parser().parse_args([])

    def test_3458a_mode(self):
        args = build_parser().parse_args(['--3458a', '22'])
        assert args.hp3458a_addr == 22
        assert args.gpib_addr is None
        assert args.gpib_bus == 0
        assert args.display_on is False

    def test_gpib_mode(self):
        args = build_parser().parse_args(['--gpib', '15'])
        assert args.gpib_addr == 15
        assert args.hp3458a_addr is None

    def test_mutually_exclusive(self):
        with pytest.raises(SystemExit):
            build_parser().parse_args(['--3458a', '22', '--gpib', '15'])

    def test_custom_bus(self):
        args = build_parser().parse_args(['--gpib', '22', '-b', '1'])
        assert args.gpib_bus == 1

    def test_display_on(self):
        args = build_parser().parse_args(['--3458a', '22', '-d'])
        assert args.display_on is True
