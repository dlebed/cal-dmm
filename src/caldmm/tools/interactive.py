"""Interactive instrument console — replaces interactive.py and gpib-interactive.py.

Provides a REPL for sending SCPI/GPIB commands to instruments.

Usage:
  # HP 3458A interactive mode (uses driver for setup, then REPL)
  caldmm-interactive --3458a 22

  # Raw GPIB interactive mode (direct Gpib commands)
  caldmm-interactive --gpib 22

  # Custom GPIB bus
  caldmm-interactive --gpib 22 --bus 1
"""

import argparse
import sys

from caldmm.lib.signals import setup_graceful_shutdown


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog='caldmm-interactive',
        description='Interactive instrument console',
    )
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument('--3458a', dest='hp3458a_addr', type=int, default=None,
                       metavar='ADDR', help='HP 3458A GPIB address')
    group.add_argument('--gpib', dest='gpib_addr', type=int, default=None,
                       metavar='ADDR', help='Raw GPIB address')

    p.add_argument('-b', '--bus', dest='gpib_bus', type=int, default=0,
                   help='GPIB bus number (default: 0)')
    p.add_argument('-d', '--display-on', dest='display_on', action='store_true',
                   default=False, help='Keep display on (3458A mode)')
    return p


def _repl_3458a(addr: int, bus: int, display_on: bool) -> None:
    """Interactive REPL using the HP 3458A driver."""
    from caldmm.drivers.hp3458a import HP3458A

    dev = HP3458A(gpib_address=addr, gpib_bus=bus, display_en=display_on)
    setup_graceful_shutdown()

    print(f"HP 3458A interactive mode (GPIB {bus}:{addr})")
    print("  Send commands directly. Queries ending with '?' auto-read response.")
    print("  Prefix with '?' to read last response. Ctrl-C to exit.\n")

    while True:
        try:
            cmd_str = input("> ").strip()
        except EOFError:
            break

        if not cmd_str:
            continue

        if not cmd_str.startswith("?"):
            dev.cmd(cmd_str)

        if cmd_str.endswith("?") or cmd_str.startswith("?"):
            try:
                print(dev.read_raw_str())
            except Exception as e:
                print(f"Read error: {e}")


def _repl_gpib(addr: int, bus: int) -> None:
    """Interactive REPL using raw GPIB commands."""
    from caldmm.drivers import gpib as Gpib

    dev = Gpib.Gpib(bus, addr, timeout=10)
    setup_graceful_shutdown()

    print(f"Raw GPIB interactive mode ({bus}:{addr})")
    print("  Prefix with '?' to read response. Ctrl-C to exit.\n")

    while True:
        try:
            cmd_str = input("> ").strip()
        except EOFError:
            break

        if not cmd_str:
            continue

        if cmd_str.startswith("?"):
            try:
                print(dev.read().decode("ascii").strip())
            except Exception as e:
                print(f"Read error: {e}")
        else:
            dev.write(cmd_str)
            if cmd_str.endswith("?"):
                try:
                    print(dev.read().decode("ascii").strip())
                except Exception as e:
                    print(f"Read error: {e}")


def run(args=None) -> None:
    parser = build_parser()
    args = parser.parse_args(args)

    if args.hp3458a_addr is not None:
        _repl_3458a(args.hp3458a_addr, args.gpib_bus, args.display_on)
    elif args.gpib_addr is not None:
        _repl_gpib(args.gpib_addr, args.gpib_bus)


def main():
    run()


if __name__ == '__main__':
    main()
