"""Small private-session-bus NM property service; all devices are JSON fixtures."""
import json
import os
from pathlib import Path
import sys
import gi
gi.require_version('Gio','2.0')
from gi.repository import Gio,GLib
root=Path(sys.argv[1]);state_path=root/'network-state.json'
NM='org.freedesktop.NetworkManager';BASE='/org/freedesktop/NetworkManager'
bus=Gio.DBusConnection.new_for_address_sync(os.environ['DBUS_SESSION_BUS_ADDRESS'],Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT|Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,None,None)
registered=set();handles=[]
properties=Gio.DBusNodeInfo.new_for_xml('''<node><interface name="org.freedesktop.DBus.Properties"><method name="Get"><arg type="s" direction="in"/><arg type="s" direction="in"/><arg type="v" direction="out"/></method><method name="Set"><arg type="s" direction="in"/><arg type="s" direction="in"/><arg type="v" direction="in"/></method></interface></node>''').interfaces[0]

def dispatch(_bus,_sender,path,interface,method,params,reply):
    try:
        data=json.loads(state_path.read_text());control=json.loads((root/'nm-control.json').read_text());devices=data['nm']['devices']
        if method=='GetDevices':
            if control.get('fail_query'): raise ValueError('query denied')
            if control.get('hang_query'):
                GLib.timeout_add(5000,lambda:(reply.return_dbus_error(NM+'.Failed','Delayed query'),False)[1]);return
            for v in devices.values():
                if v['path'] not in registered:
                    handles.append(bus.register_object(v['path'],properties,dispatch,None,None));registered.add(v['path'])
            reply.return_value(GLib.Variant('(ao)',([v['path'] for v in devices.values()],)));return
        name,dev=next((name,v) for name,v in devices.items() if v['path']==path)
        args=params.unpack()
        if args[0]!=NM+'.Device': raise ValueError('wrong interface')
        if method=='Get':
            values={'Interface':GLib.Variant('s',name),'Managed':GLib.Variant('b',dev['managed']),
                    'State':GLib.Variant('u',dev['state']),'ActiveConnection':GLib.Variant('o',BASE+'/ActiveConnection/1' if dev['active'] else '/')}
            reply.return_value(GLib.Variant('(v)',(values[args[1]],)));return
        if method!='Set' or args[1]!='Managed' or name=='demo0' or control.get('deny_set'): raise ValueError('set denied')
        dev['managed']=args[2];dev['state']=30 if args[2] else 10
        data.setdefault('calls',[]).append(['D-Bus',name,'Managed',str(args[2]).lower()])
        temporary=root/'network-state.dbus-new';temporary.write_text(json.dumps(data));temporary.chmod(0o600);temporary.replace(state_path)
        if control.get('lose_set'): raise ValueError('reply lost after committing')
        reply.return_value(GLib.Variant('()',()))
    except Exception:
        reply.return_dbus_error(NM+'.Failed','Simulated property failure')

info=Gio.DBusNodeInfo.new_for_xml('<node><interface name="'+NM+'"><method name="GetDevices"><arg type="ao" direction="out"/></method></interface></node>').interfaces[0]
handles.append(bus.register_object(BASE,info,dispatch,None,None))
bus.call_sync('org.freedesktop.DBus','/org/freedesktop/DBus','org.freedesktop.DBus','RequestName',GLib.Variant('(su)',(NM,4)),None,Gio.DBusCallFlags.NONE,5000,None)
print('ready',flush=True)
GLib.MainLoop().run()
