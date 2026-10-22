"""Companion to the Bash simulator. Writes only beneath RELAYFORGE_TEST_STATE."""
import json
import os
from pathlib import Path
import shutil
import sys

ROOT=Path(os.environ['RELAYFORGE_TEST_STATE']).resolve()
ACTION=sys.argv[1]
TARGET=Path(sys.argv[2]).resolve()
if not TARGET.is_relative_to(ROOT):
    raise SystemExit('Refusing fixture path outside its temporary directory')
CONTROL=lambda:json.loads((ROOT/'control.json').read_text())


def private(path,value,mode=0o600):
    path.write_text(json.dumps(value) if not isinstance(value,str) else value)
    path.chmod(mode)


def event(kind,**fields):
    path=ROOT/'trace.json'
    trace=json.loads(path.read_text()) if path.exists() else []
    trace.append(dict(kind=kind,**fields))
    temporary=path.with_suffix('.new')
    private(temporary,trace)
    temporary.replace(path)


def directory(marker):
    for candidate in (ROOT/'engine').glob('create_ap.*.conf.*'):
        snapshot=candidate/'recovery.json'
        if snapshot.exists() and json.loads(snapshot.read_text())['fields']['DAEMON_PIDFILE']==str(marker):
            return candidate
    return None


def cleanup(path):
    if CONTROL().get('fail_cleanup'):
        raise SystemExit(1)
    if path:
        snapshot=json.loads((path/'recovery.json').read_text())
        marker=Path(snapshot['fields']['DAEMON_PIDFILE'])
        marker.unlink(missing_ok=True)
        Path(str(marker)+'.ready').unlink(missing_ok=True)
        shutil.rmtree(path)
        event('cleaned',directory=path.name)


if ACTION=='init':
    control=CONTROL()
    if control.get('fail_before_pid'):
        raise SystemExit(1)
    marker=TARGET
    pid=int(sys.argv[3])
    config=dict(line.split('=',1) for line in Path(sys.argv[4]).read_text().splitlines())
    engine=ROOT/'engine'
    engine.mkdir(mode=0o700,exist_ok=True)
    path=engine/f'create_ap.{config["WIFI_IFACE"]}.conf.ABCDEFGH'
    path.mkdir(mode=0o755)
    start=Path(f'/proc/{pid}/stat').read_text().rsplit(')',1)[1].split()[19]
    private(path/'pid',str(pid)+'\n',0o444)
    private(path/'recovery.json',dict(owner=dict(pid=pid,starttime=start),
        fields=dict(DAEMON_PIDFILE=str(marker),CONFDIR=str(path),WIFI_IFACE=config['WIFI_IFACE'],VWIFI_IFACE='apfake0'),
        interfaces={'apfake0':dict(ifindex=101,mac='02:77:88:99:aa:bb')}))
    private(marker,str(pid)+'\n')
    event('launched',pid=pid,config_mode=oct(Path(sys.argv[4]).stat().st_mode&0o777),argv_has_password=False)
    if control.get('foreign'):
        foreign=engine/f'create_ap.{config["WIFI_IFACE"]}.conf.FOREIGN1'
        foreign.mkdir(mode=0o755,exist_ok=True)
        private(foreign/'recovery.json',dict(owner=dict(pid=1,starttime='1'),fields=dict(DAEMON_PIDFILE=str(ROOT/'foreign.pid'),CONFDIR=str(foreign)),interfaces={}))
    if control.get('fail_after_resources'):
        raise SystemExit(1)
elif ACTION=='tick':
    control=CONTROL()
    if control.get('flood'):
        try:
            sys.stdout.write('credential-must-not-leak\n'*60000)
            sys.stdout.flush()
        except BrokenPipeError:
            pass
        raise SystemExit(1)
    if not control.get('no_ready'):
        ready=Path(str(TARGET)+'.ready')
        if not ready.exists():
            private(ready,str(int(sys.argv[3])+(1 if control.get('wrong_ready') else 0))+'\n')
            event('ready')
elif ACTION=='should-stop':
    raise SystemExit(1 if CONTROL().get('ignore_stop') else 0)
elif ACTION=='stop':
    cleanup(directory(TARGET))
elif ACTION=='recover':
    event('recover-request',directory=TARGET.name)
    cleanup(TARGET)
elif ACTION=='observe':
    if directory(TARGET) is None:
        raise SystemExit(1)
    control=CONTROL()
    print(json.dumps(dict(interface='apfake0',frequency=2437 if control.get('bad_channel') else 2412,
                         clients=[dict(mac='02:aa:bb:cc:dd:ee',ip='192.0.2.2',hostname='Client')] if control.get('client') else [])))
else:
    raise SystemExit(2)
