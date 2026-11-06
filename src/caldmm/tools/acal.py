"""Periodic ACAL scheduler for an HP 3458A.

Runs an ACAL cycle every *interval* hours and logs the internal temperature
and calibration constant 72 (the DCV gain) before and after each one, to the
console and optionally to a semicolon-delimited CSV.

Usage:
  caldmm-acal --3458a dmm1:22 --interval 24 -o acal.csv
  caldmm-acal --3458a dmm1:1,22 --acal-type ALL --ambient 23.1
"""

import argparse
import os
import sys
import time
from datetime import datetime

from caldmm.lib.signals import setup_graceful_shutdown


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog='caldmm-acal',
        description='Periodic ACAL scheduler for HP 3458A',
    )
    p.add_argument('--3458a', dest='dmm_spec', required=True,
                   metavar='NAME:ADDR',
                   help="HP 3458A as name:gpib_addr (ADDR may be 'bus,pad')")
    p.add_argument('--bus', dest='gpib_bus', type=int, default=0,
                   help='GPIB bus number when ADDR has none (default: 0)')
    p.add_argument('-i', '--interval', type=float, default=24,
                   help='ACAL interval in hours (default: 24)')
    p.add_argument('--acal-type', default='DCV',
                   choices=['ALL', 'DCV', 'AC', 'OHMS'],
                   help='ACAL type (default: DCV)')
    p.add_argument('--settle', type=float, default=30,
                   help='Settle time before the first ACAL, in seconds '
                        '(default: 30)')
    p.add_argument('--ambient', type=float, default=None, metavar='CELSIUS',
                   help='Ambient temperature to record alongside each ACAL')
    p.add_argument('-o', '--csv', dest='csv_file', help='Output CSV file')
    return p


def _resolve_address(addr: str, default_bus: int) -> tuple[int, int]:
    from caldmm.cal.adapters import parse_gpib_address
    if ',' in addr or addr.lower().startswith('gpib'):
        return parse_gpib_address(addr)
    return default_bus, int(addr)


def run(args=None) -> None:
    parser = build_parser()
    args = parser.parse_args(args)

    from caldmm.cal.adapters import parse_instrument_spec

    name, addr = parse_instrument_spec(args.dmm_spec)
    bus, pad = _resolve_address(addr, args.gpib_bus)

    try:
        from caldmm.drivers.hp3458a import HP3458A, ACAL
        dev = HP3458A(gpib_address=pad, gpib_bus=bus, name=name)
    except Exception as e:
        print(f"Failed to connect to HP 3458A: {e}", file=sys.stderr)
        sys.exit(1)

    acal_type = ACAL[args.acal_type]

    csv_file = None
    if args.csv_file:
        csv_file = open(args.csv_file, 'a')
        if os.path.getsize(args.csv_file) == 0:
            csv_file.write("Time;Temp_Int;DCV_Gain_72;Ambient\n")

    def cleanup():
        if csv_file:
            csv_file.close()

    setup_graceful_shutdown(cleanup)

    calnum = dev.calnum()
    print(f"Date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Instrument: {name} (GPIB {bus},{pad})")
    print(f"Calnum: {int(calnum)}")
    if dev.has_internal_temp():
        print(f"Internal temp: {dev.get_temp():.1f}C")
    if args.ambient is not None:
        print(f"Ambient: {args.ambient:.1f}C (operator)")

    print(f"\nACAL type: {args.acal_type}, interval: {args.interval}h, "
          f"settle: {args.settle}s")
    print("Waiting for device to settle...")
    time.sleep(args.settle)

    acal_count = 0
    while True:
        acal_count += 1
        ts = datetime.now()

        temp_int = dev.get_temp() if dev.has_internal_temp() else None
        cal72_before = None
        try:
            cal72_before = float(dev.get_cal_const(72))
        except Exception:
            pass

        print(f"\n[ACAL #{acal_count}] {ts.isoformat()}")
        if temp_int is not None:
            print(f"  INT: {temp_int:.1f}C")

        dev.acal(acal_type)
        time.sleep(20)
        dev.display_off()

        end_ts = datetime.now()
        cal72_after = None
        try:
            cal72_after = float(dev.get_cal_const(72))
        except Exception:
            pass

        if cal72_before is not None and cal72_after is not None:
            drift_ppm = (cal72_after - cal72_before) / cal72_after * 1e6
            print(f"  CAL72: {cal72_after:.10E} (drift: {drift_ppm:.3f} ppm)")
        elif cal72_after is not None:
            print(f"  CAL72: {cal72_after:.10E}")

        if csv_file:
            parts = [
                end_ts.isoformat(),
                f'{temp_int:.1f}' if temp_int is not None else '',
                f'{cal72_after:.10E}' if cal72_after is not None else '',
                f'{args.ambient:.2f}' if args.ambient is not None else '',
            ]
            csv_file.write(';'.join(parts) + '\n')
            csv_file.flush()

        print(f"[ACAL #{acal_count}] Complete. Next in {args.interval}h")
        time.sleep(int(args.interval * 3600))


def main():
    run()


if __name__ == '__main__':
    main()
