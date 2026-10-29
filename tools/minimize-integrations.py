#!/usr/bin/python3
"""Keep unchanged desktop companion packages at their supported Ubuntu baseline.

Run only before applying our code-only patches to the exact supported source.
Never relax NetworkManager/libnm locks: Relay changes shared profile validation.
"""
import argparse
from pathlib import Path

BASELINES = {'applet': '1.36.0-4ubuntu1', 'settings': '1:51.0-1ubuntu1'}


def minimize_control(control, kind, source_version):
    baseline = BASELINES[kind]
    if source_version != baseline:
        raise ValueError(f'{kind} dependency minimization supports only {baseline}')
    if kind == 'applet':
        old = 'nm-connection-editor (= ${binary:Version})'
        new = f'nm-connection-editor (>= {baseline}), nm-connection-editor (<< 1.37~)'
        expected = 2  # Applet and transitional package both invoke the stock editor.
    else:
        old = 'gnome-control-center-data (>= ${source:Version})'
        new = f'gnome-control-center-data (>= {baseline})'
        expected = 1  # Existing next-upstream upper bound remains in place.
    if control.count(old) != expected:
        raise ValueError('Unexpected dependency template; review upstream compatibility before rebuilding')
    return control.replace(old, new)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('kind', choices=BASELINES)
    parser.add_argument('source_version')
    parser.add_argument('control', type=Path)
    args = parser.parse_args()
    args.control.write_text(minimize_control(args.control.read_text(), args.kind, args.source_version))


if __name__ == '__main__':
    main()
