"""Private-bus service-manager fixture. Executes only the staged Rust demo."""
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import gi
gi.require_version('Gio','2.0')
from gi.repository import Gio,GLib

root=Path(sys.argv[1]);base=root.parent
name='org.freedesktop.systemd1';path='/org/freedesktop/systemd1';interface=name+'.Manager'
units=['wifi-hotspot-daemon.service','relayforge.service','relayforge-native.service','relayforge-create-ap.service']
paths={u:path+'/unit/u'+str(i) for i,u in enumerate(units)}
bus=Gio.DBusConnection.new_for_address_sync(os.environ['DBUS_SESSION_BUS_ADDRESS'],Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT|Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,None,None)
process=None;active=None;lock=None;failed=set();events=[];jobs=0
def control():
    try:return json.loads((base/'manager-control.json').read_text())
    except FileNotFoundError:return {}
def record(method,args):
    events.append([method,args]);temporary=base/'manager-events.tmp'
    temporary.write_text(json.dumps(events));temporary.replace(base/'manager-events.json')
def maskpath(unit):return root/'run/systemd/system'/unit
def unitfile(unit):return root/'usr/lib/systemd/system'/unit
def localunit(unit):return root/'etc/systemd/system'/unit
def masks():return [u for u in units if maskpath(u).is_symlink()]
def directory(path,mode=0o755):
    current=root
    for part in path.relative_to(root).parts:
        current=current/part;current.mkdir(mode=0o755,exist_ok=True);current.chmod(0o755)
    path.chmod(mode)
def launch(unit):
    global process,active,lock
    if process and process.poll() is None:raise RuntimeError('Already running')
    if process:process.communicate(timeout=5)
    if unit in masks():raise RuntimeError('Masked unit')
    runtime=root/'run/relayforge';directory(runtime,0o700)
    handle=os.open(runtime/'.lock',os.O_CREAT|os.O_RDWR,0o600);os.fchmod(handle,0o600)
    lock=os.fdopen(handle,'r+');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    binary=root/'usr/lib/relayforge/relayforge-service'
    mode='--demo-maintenance' if (root/'var/lib/relayforge-deploy/live.json').exists() else '--demo'
    if control().get('unhealthy'):mode='--demo'
    process=subprocess.Popen([str(binary),mode],stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
    active=unit;failed.discard(unit)
def stop(unit):
    global process,active,lock
    if active==unit and process:
        if process.poll() is None:process.terminate()
        process.communicate(timeout=5);process=None;active=None
        if lock:lock.close();lock=None
    failed.discard(unit)
def rows():
    global lock
    if process and process.poll() is not None:
        failed.add(active)
        if lock:lock.close();lock=None
    return [(u,u,'loaded' if unitfile(u).exists() or u in masks() else 'not-found',
             'failed' if u in failed else 'active' if active==u else 'inactive',
             'running' if active==u else 'dead','',paths[u],1 if control().get('job') and active==u else 0,'','/') for u in units]
def reply(invocation,signature,values):invocation.return_value(GLib.Variant(signature,values))
def dispatch(_bus,_sender,obj,iface,method,params,invocation):
    global jobs
    args=params.unpack()
    try:
        if method in control().get('fail',[]):raise RuntimeError('Injected manager failure')
        if method=='ListUnitsByPatterns':
            assert args==([],units);reply(invocation,'(a(ssssssouso))',(rows(),));return
        if method=='GetUnitFileState':
            u=args[0];value='masked-runtime' if u in masks() else control().get('unit_states',{}).get(u,'disabled' if unitfile(u).exists() or localunit(u).exists() else 'not-found')
            reply(invocation,'(s)',(value,));return
        if method in ['GetUnit','LoadUnit']:
            u=args[0]
            if not unitfile(u).exists() and not localunit(u).exists() and u not in masks():
                invocation.return_dbus_error(name+'.NoSuchUnit','Not found');return
            reply(invocation,'(o)',(paths[u],));return
        if method=='GetUnitByPID':
            if not process or args[0]!=process.pid:raise RuntimeError('Foreign process')
            reply(invocation,'(o)',(paths[active],));return
        if method=='Get':
            u=next(u for u in units if paths[u]==obj);requested,key=args
            if key=='MainPID':value=GLib.Variant('u',os.getpid() if control().get('bad_pid') else process.pid if process and active==u else 0)
            elif key=='FragmentPath':value=GLib.Variant('s','/etc/systemd/system/'+u if localunit(u).exists() else '/dev/null' if u in masks() else '/usr/lib/systemd/system/'+u)
            elif key=='DropInPaths':value=GLib.Variant('as',control().get('overrides',[]))
            else:raise RuntimeError('Unexpected property')
            reply(invocation,'(v)',(value,));return
        record(method,args)
        if method=='MaskUnitFiles':
            selected,runtime,force=args;assert runtime and not force
            changes=[]
            for u in selected:
                assert u in units
                target=maskpath(u);directory(target.parent)
                if not target.is_symlink():target.symlink_to('/dev/null');changes.append(('symlink','/run/systemd/system/'+u,'/dev/null'))
            reply(invocation,'(a(sss))',(changes,))
        elif method=='UnmaskUnitFiles':
            selected,runtime=args;assert runtime
            for u in selected:
                assert u in units
                target=maskpath(u)
                if target.is_symlink():
                    assert os.readlink(target)=='/dev/null';target.unlink()
            reply(invocation,'(a(sss))',([],))
        elif method=='Reload':reply(invocation,'()',())
        elif method in ['StopUnit','StartUnit']:
            u,mode=args;assert u in units and mode=='fail'
            if method=='StopUnit':stop(u)
            else:launch(u)
            jobs+=1
            if control().get('lost_reply')==method:raise RuntimeError('Side effect happened before lost reply')
            reply(invocation,'(o)',(path+'/job/j'+str(jobs),))
        else:raise RuntimeError('Unexpected manager method')
    except Exception:
        invocation.return_dbus_error(name+'.Failed','Manager fixture rejected operation')

xml='''<node><interface name="org.freedesktop.systemd1.Manager">
<method name="ListUnitsByPatterns"><arg type="as" direction="in"/><arg type="as" direction="in"/><arg type="a(ssssssouso)" direction="out"/></method>
<method name="GetUnitFileState"><arg type="s" direction="in"/><arg type="s" direction="out"/></method>
<method name="GetUnit"><arg type="s" direction="in"/><arg type="o" direction="out"/></method>
<method name="LoadUnit"><arg type="s" direction="in"/><arg type="o" direction="out"/></method>
<method name="GetUnitByPID"><arg type="u" direction="in"/><arg type="o" direction="out"/></method>
<method name="MaskUnitFiles"><arg type="as" direction="in"/><arg type="b" direction="in"/><arg type="b" direction="in"/><arg type="a(sss)" direction="out"/></method>
<method name="UnmaskUnitFiles"><arg type="as" direction="in"/><arg type="b" direction="in"/><arg type="a(sss)" direction="out"/></method>
<method name="Reload"/>
<method name="StartUnit"><arg type="s" direction="in"/><arg type="s" direction="in"/><arg type="o" direction="out"/></method>
<method name="StopUnit"><arg type="s" direction="in"/><arg type="s" direction="in"/><arg type="o" direction="out"/></method>
</interface></node>'''
for info in Gio.DBusNodeInfo.new_for_xml(xml).interfaces:bus.register_object(path,info,dispatch,None,None)
props=Gio.DBusNodeInfo.new_for_xml('''<node><interface name="org.freedesktop.DBus.Properties"><method name="Get"><arg type="s" direction="in"/><arg type="s" direction="in"/><arg type="v" direction="out"/></method></interface></node>''').interfaces[0]
for obj in paths.values():bus.register_object(obj,props,dispatch,None,None)
bus.call_sync('org.freedesktop.DBus','/org/freedesktop/DBus','org.freedesktop.DBus','RequestName',GLib.Variant('(su)',(name,4)),None,Gio.DBusCallFlags.NONE,5000,None)
if (root/'usr/lib/relayforge/relayforge-service').exists() and not control().get('inactive'):
    launch('relayforge-create-ap.service' if unitfile('relayforge-create-ap.service').exists() else 'relayforge-native.service')
loop=GLib.MainLoop()
def shutdown(_signal,_frame):loop.quit()
signal.signal(signal.SIGTERM,shutdown)
print('ready',flush=True)
try:loop.run()
finally:
    if active:stop(active)
