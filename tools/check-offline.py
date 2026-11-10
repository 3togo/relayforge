#!/usr/bin/env python3
"""Run bounded offline build, protocol, desktop and packaging checks.

Does not install packages, start a real hotspot or modify network configuration.
"""
import argparse
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]


def run_check(name, command, output, environment, timeout):
    start = time.monotonic()
    log = output / (name + '.log')
    print(f'Running {name}', flush=True)
    with log.open('wb') as stream:
        process = subprocess.Popen(command, cwd=ROOT, env=environment, stdout=stream,
                                   stderr=subprocess.STDOUT, start_new_session=True)
        try:
            code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            code = 124
    text = log.read_text(errors='replace')
    result = {'exit_code': code, 'seconds': round(time.monotonic() - start, 2),
              'log': log.name, 'summary': [line for line in text.splitlines()
                                          if line.startswith(('Ran ', 'OK', 'FAILED', 'test result:'))]}
    print(f'{name}: {code}', flush=True)
    if code:
        print('\n'.join(text.splitlines()[-30:]), flush=True)
    return result


def source_manifest():
    excluded = {'.git', 'target', 'artifacts', '__pycache__'}
    result = {}
    for directory, directories, files in os.walk(ROOT):
        directories[:] = sorted(name for name in directories if name not in excluded)
        for name in sorted(files):
            path = Path(directory) / name
            if path.is_file() and not path.is_symlink() and path.suffix != '.pyc':
                result[str(path.relative_to(ROOT))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def candidate_target(base, files):
    # Cargo uses file timestamps when deciding whether to rebuild. Keep caches
    # for different source contents separate even when an archive preserves old
    # timestamps, rather than trusting a previously built workspace's artifacts.
    identity = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
    return base / 'offline-checks' / identity, identity


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, default=ROOT / 'artifacts/validation')
    parser.add_argument('--jobs', type=int, default=4)
    parser.add_argument('--with-applet', action='store_true',
                        help='Also compile/run the GTK3/Jansson applet harness, optimized and sanitized')
    parser.add_argument('--python-pattern', action='append',
                        help='Focused Python module glob; repeat to select modules (default: all)')
    args = parser.parse_args()
    if args.jobs < 1 or args.jobs > 256:
        parser.error('--jobs must be between 1 and 256')
    patterns = args.python_pattern or ['test_*.py']
    modules = [path for path in sorted((ROOT / 'tests').glob('test_*.py'))
               if any(fnmatch.fnmatchcase(path.name, pattern) for pattern in patterns)]
    if not modules:
        parser.error('--python-pattern did not select any test modules')
    args.output_dir.mkdir(parents=True, exist_ok=True)
    output = Path(tempfile.mkdtemp(prefix='offline-', dir=args.output_dir))
    environment = os.environ.copy()
    environment.update(GIO_USE_VFS='local', NO_AT_BRIDGE='1')
    target = Path(environment.get('CARGO_TARGET_DIR', ROOT / 'target'))
    if not target.is_absolute():
        target = ROOT / target
    files = source_manifest()
    target, build_identity = candidate_target(target.resolve(), files)
    environment['CARGO_TARGET_DIR'] = str(target)
    for variable, name in (('RELAYFORGE_BINARY', 'relayforge-service'),
                           ('RELAYFORGE_CONFIG_ORACLE', 'examples/config_oracle'),
                           ('RELAYFORGE_RADIO_ORACLE', 'examples/radio_oracle'),
                           ('RELAYFORGE_AP_CHILD', 'examples/ap_child_fixture')):
        environment[variable] = str(target / 'release' / name)
    results = {}
    report = {'source_files': files, 'build_source_id': build_identity, 'checks': results,
              'python_patterns': patterns, 'full_python_suite': args.python_pattern is None,
              'os_release': Path('/etc/os-release').read_text(),
              'architecture': os.uname().machine, 'python': sys.version,
              'limits': 'Offline fixtures, private buses and package assembly only; no host activation or installation.'}

    def check(name, command, timeout=150, env=None):
        result = run_check(name, command, output, env or environment, timeout)
        results[name] = result
        (output / 'validation.json').write_text(json.dumps(report, indent=2) + '\n')
        return result['exit_code'] == 0

    # Deployment fixtures deliberately reject oversized executables. Use the
    # production-shaped release build rather than an unstripped debug ELF.
    built = check('build', ['cargo', 'build', '--workspace', '--all-targets', '--release',
                            '--locked', '--offline', f'-j{args.jobs}'], 600)
    if built:
        check('rust-tests', ['cargo', 'test', '--workspace', '--release', '--locked',
                             '--offline', f'-j{args.jobs}'], 300)
        for path in modules:
            check(path.stem, ['dbus-run-session', '--', sys.executable, '-m', 'unittest',
                              'discover', '-s', 'tests', '-p', path.name, '-v'],
                  180 if path.stem in ('test_suspend', 'test_sleep_audit') else 150)
    check('desktop-contracts', [sys.executable, 'tools/check-desktop.py'], 90)
    check('vendor-provenance', [sys.executable, 'tools/check-vendor.py'])
    check('integration-provenance', [sys.executable, 'tools/check-integrations.py'])
    if args.with_applet:
        optimized = dict(environment, OPT_LEVEL='2')
        check('applet-optimized', ['bash', 'integration/nm-applet/test.sh'], env=optimized)
        sanitized = dict(environment, OPT_LEVEL='1', SANITIZE='1')
        check('applet-sanitized', ['bash', 'integration/nm-applet/test.sh'], env=sanitized)
    print(output)
    raise SystemExit(0 if built and all(item['exit_code'] == 0 for item in results.values()) else 1)


if __name__ == '__main__':
    main()
