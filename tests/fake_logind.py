"""Private-bus logind fixture with real delay-inhibitor descriptor lifetimes."""
import json
import os
from pathlib import Path
import sys
import gi
gi.require_version('Gio','2.0')
from gi.repository import Gio,GLib

root=Path(sys.argv[1]);root.mkdir(exist_ok=True)
name='org.freedesktop.login1';path='/org/freedesktop/login1';interface=name+'.Manager'
bus=Gio.DBusConnection.new_for_address_sync(os.environ['DBUS_SESSION_BUS_ADDRESS'],
    Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT|Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,None,None)
def control():
    try:return json.loads((root/'logind-control.json').read_text())
    except FileNotFoundError:return {}
state={'sleeping':bool(control().get('initial_sleeping')),'events':[]};readers=[]
def deferred(field,action):
    if not control().get(field):action();return
    record(field+'-held')
    def finish():
        if control().get(field):return True
        action();return False
    GLib.timeout_add(20,finish)
def record(kind):
    state['events'].append(kind)
    (root/'logind.json').write_text(json.dumps(state))
def dispatch(_bus,_sender,_path,iface,method,params,invocation):
    if method=='Inhibit':
        assert params.unpack()==('sleep','RelayForge','Clean up owned hotspot before sleep','delay')
        def complete():
            if control().get('deny_inhibit'):
                invocation.return_dbus_error(name+'.Denied','Fixture inhibitor denied');return
            read,write=os.pipe2(os.O_NONBLOCK|os.O_CLOEXEC);readers.append(read)
            descriptors=Gio.UnixFDList.new();index=descriptors.append(write);os.close(write)
            invocation.return_value_with_unix_fd_list(GLib.Variant('(h)',(index,)),descriptors)
            record('inhibit')
        deferred('hold_inhibit',complete)
    elif method=='Get':
        assert params.unpack()==(interface,'PreparingForSleep')
        def complete():invocation.return_value(GLib.Variant('(v)',(GLib.Variant('b',state['sleeping']),)))
        deferred('hold_property',complete)
    elif method=='SetSleep':
        state['sleeping']=params.unpack()[0]
        bus.emit_signal(None,path,interface,'PrepareForSleep',GLib.Variant('(b)',(state['sleeping'],)))
        invocation.return_value(GLib.Variant('()',()))
        record('sleep' if state['sleeping'] else 'wake')
    elif method=='Malformed':
        bus.emit_signal(None,path,interface,'PrepareForSleep',GLib.Variant('(s)',('invalid',)))
        invocation.return_value(GLib.Variant('()',()))
    else:
        invocation.return_dbus_error(name+'.Failed','Unknown fixture call')
xml='''<node><interface name="org.freedesktop.login1.Manager">
<method name="Inhibit"><arg type="s" direction="in"/><arg type="s" direction="in"/><arg type="s" direction="in"/><arg type="s" direction="in"/><arg type="h" direction="out"/></method>
<method name="SetSleep"><arg type="b" direction="in"/></method><method name="Malformed"/>
<signal name="PrepareForSleep"><arg type="b"/></signal></interface>
<interface name="org.freedesktop.DBus.Properties"><method name="Get"><arg type="s" direction="in"/><arg type="s" direction="in"/><arg type="v" direction="out"/></method></interface></node>'''
for info in Gio.DBusNodeInfo.new_for_xml(xml).interfaces:bus.register_object(path,info,dispatch,None,None)
bus.call_sync('org.freedesktop.DBus','/org/freedesktop/DBus','org.freedesktop.DBus','RequestName',GLib.Variant('(su)',(name,4)),None,Gio.DBusCallFlags.NONE,5000,None)
def reap():
    for descriptor in readers[:]:
        try:
            if os.read(descriptor,1)==b'':
                readers.remove(descriptor);os.close(descriptor);record('released')
        except BlockingIOError:pass
    return True
GLib.timeout_add(20,reap)
record('ready');print('ready',flush=True)
GLib.MainLoop().run()
