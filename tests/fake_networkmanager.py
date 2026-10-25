"""Private-bus NetworkManager/kernel simulation. Never opens the system bus or runs iw."""
import json
from pathlib import Path
import sys
import gi
gi.require_version('Gio', '2.0')
from gi.repository import Gio, GLib

NM='org.freedesktop.NetworkManager'
ROOT='/org/freedesktop/NetworkManager'
DEV=NM+'.Device'
ACTIVE=NM+'.Connection.Active'
PROFILE=NM+'.Settings.Connection'
TEST='io.github.relayforge.TestNetworkManager'
DIRECTORY=Path(sys.argv[1])
CAPTURE=json.loads((DIRECTORY/'capture.json').read_text())
BUS=Gio.DBusConnection.new_for_address_sync(__import__('os').environ['DBUS_SESSION_BUS_ADDRESS'],
    Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,None,None)
CONTROL=lambda:json.loads((DIRECTORY/'control.json').read_text())
state={'events':[],'interfaces':{},'profiles':{},'active':{},'foreign_deletes':0,'parent_writes':0}
registrations=[]
if (DIRECTORY/'trace.json').exists():
    state['interfaces']=json.loads((DIRECTORY/'trace.json').read_text()).get('interfaces',{})


def event(kind,**values):
    state['events'].append(dict(kind=kind,**values))
    # No SSID, PSK or full settings in traces.
    public={k:v for k,v in state.items() if k not in ('profiles','active')}
    public['profile_ids']=list(state['profiles'])
    (DIRECTORY/'trace.json').write_text(json.dumps(public))


def register(path,xml):
    for interface in Gio.DBusNodeInfo.new_for_xml(xml).interfaces:
        registrations.append(BUS.register_object(path,interface,dispatch,None,None))


def device_xml():
    return '''<node><interface name="org.freedesktop.DBus.Properties"><method name="Get"><arg type="s" direction="in"/><arg type="s" direction="in"/><arg type="v" direction="out"/></method><method name="Set"><arg type="s" direction="in"/><arg type="s" direction="in"/><arg type="v" direction="in"/></method></interface></node>'''


def remove_profile(path):
    profile=state['profiles'].pop(path,None)
    if profile:
        if profile.get('native'):
            state['interfaces'].pop(profile['settings']['connection']['interface-name'],None)
        event('profile-deleted',uuid=profile['settings']['connection']['uuid'])
    state['active'].pop(path.replace('/Settings/','/ActiveConnection/'),None)


ACCESS_POINTS={ROOT+'/AccessPoint/1':dict(ssid=b'Test upstream',frequency=2412,strength=60),
                ROOT+'/AccessPoint/2':dict(ssid=b'Test upstream',frequency=2437,strength=80),
                ROOT+'/AccessPoint/3':dict(ssid=b'Test upstream',frequency=5745,strength=70),
                ROOT+'/AccessPoint/4':dict(ssid=b'Other network',frequency=2437,strength=90)}


def station_frequency(control):
    if control.get('band5'):return 5745
    if control.get('roamed') or control.get('upstream_changed'):return 2437
    return 2412


def property_value(path,interface,name):
    control=CONTROL()
    if path in ACCESS_POINTS:
        point=ACCESS_POINTS[path]
        return {'Ssid':GLib.Variant('ay',point['ssid']),'Frequency':GLib.Variant('u',point['frequency']),
                'Strength':GLib.Variant('y',point['strength']),'HwAddress':GLib.Variant('s','aa:bb:cc:dd:ee:01')}[name]
    if path==ROOT and name=='Capabilities':
        return GLib.Variant('au',[0x7001] if control.get('native') else [])
    if path==ROOT+'/Devices/1':
        return {'State':GLib.Variant('u',30 if control.get('upstream_missing') else 100),'ActiveConnection':GLib.Variant('o',ROOT+'/ActiveConnection/1'),
                'WirelessCapabilities':GLib.Variant('u',0 if control.get('no_ap') else 0x40),
                'AccessPoints':GLib.Variant('ao',[path for path in ACCESS_POINTS if not control.get('no_scan')])}[name]
    if path==ROOT+'/ActiveConnection/1':
        return {'State':GLib.Variant('u',2),'Uuid':GLib.Variant('s','22222222-2222-4222-a222-222222222222' if control.get('upstream_changed') else '11111111-1111-4111-a111-111111111111')}[name]
    if '/Devices/' in path:
        return GLib.Variant('u',20 if control.get('unavailable') else 30)
    if '/ActiveConnection/' in path:
        record=state['active'][path]
        return {'State':GLib.Variant('u',4 if control.get('fail_activation') or control.get('disconnected') else 1 if control.get('hang_activation') else 2),
                'Connection':GLib.Variant('o',record)}[name]
    raise ValueError('Unexpected property')


def dispatch(_bus,sender,path,interface,method,parameters,invocation):
    try:
        control=CONTROL()
        args=parameters.unpack()
        if interface=='org.freedesktop.DBus.Properties':
            if method=='Get':
                invocation.return_value(GLib.Variant('(v)',(property_value(path,*args),)))
            else:
                if path==ROOT+'/Devices/1':
                    state['parent_writes']+=1
                    raise ValueError('Must never modify parent')
                event('child-property',property=args[1],value=args[2])
                invocation.return_value(GLib.Variant('()',()))
        elif interface==TEST:
            request=json.loads(args[0]);iface=request.get('interface')
            if method=='Radio':
                result=dict(CAPTURE)
                result['link']=result['link'].replace('2412',str(station_frequency(control)))
            elif method=='Identity':
                if iface==CAPTURE['wifi_iface']:
                    result=dict(ifindex=8 if control.get('parent_replaced') else 7,mac='02:11:22:33:44:55',phy=0)
                else:
                    result=state['interfaces'].get(iface)
                    if result and control.get('tamper_interface'):
                        result=dict(result,ifindex=result['ifindex']+1,mac='02:99:99:99:99:99')
            elif method=='Interfaces':
                result=[dict(name=CAPTURE['wifi_iface'],phy=0,kind='managed'),dict(name='',phy=0,kind='P2P-device')]
                result += [dict(name=name,phy=0,kind='AP') for name in state['interfaces']]
                if control.get('sibling'):
                    result.append(dict(name='foreign0',phy=0,kind='AP'))
            elif method=='Routes':
                result=['192.168.12.0/24'] if control.get('subnet_collision') else ['192.0.2.0/24']
            elif method=='Create':
                assert iface not in state['interfaces']
                state['interfaces'][iface]=dict(ifindex=101,mac=request['mac'],phy=request['phy'])
                event('interface-created',interface=iface)
                if control.get('fail_create_after'):
                    raise ValueError('Ambiguous kernel creation failure')
                result=None
            elif method=='Delete':
                if control.get('cleanup_fails'):
                    raise ValueError('Simulated deletion failure')
                state['interfaces'].pop(iface,None)
                event('interface-deleted',interface=iface)
                result=None
            elif method=='RestoreMode':
                control['unavailable']=False
                (DIRECTORY/'control.json').write_text(json.dumps(control))
                event('mode-restored',interface=iface)
                result=None
            elif method=='VerifyAp':
                if control.get('bad_ap'):
                    raise ValueError('Wrong AP channel')
                assert iface in state['interfaces'] and request['frequency']==station_frequency(control)
                result=None
            elif method=='Clients':
                result=[dict(mac='02:aa:bb:cc:dd:ee',ip='192.0.2.2',hostname='')] if control.get('client') else []
            elif method=='EmitRadio':
                # A kill switch is announced by NetworkManager, not by the service.
                BUS.emit_signal(None,ROOT,'org.freedesktop.DBus.Properties','PropertiesChanged',
                                GLib.Variant('(sa{sv}as)',(NM,{'WirelessEnabled':GLib.Variant('b',bool(request.get('enabled')))},[])))
                result=None
            else:
                raise ValueError('Unknown test operation')
            reply=GLib.Variant('(s)',(json.dumps(result),))
            if method=='Radio':
                event('radio-read')
                foreign_probe=bool(state['profiles']) and all(record['sender']!=sender for record in state['profiles'].values())
                if control.get('hold_radio') or (control.get('hold_post_start_probe') and foreign_probe):
                    event('radio-held')
                    def finish():
                        fields=CONTROL()
                        if fields.get('hold_radio') or (fields.get('hold_post_start_probe') and foreign_probe):return True
                        invocation.return_value(reply);return False
                    GLib.timeout_add(20,finish)
                    return
            invocation.return_value(reply)
        elif interface=='org.freedesktop.NetworkManager.Device.Wireless' and method=='RequestScan':
            event('scan-requested')
            if control.get('fail_scan'):
                raise ValueError('Simulated scan refusal')
            invocation.return_value(GLib.Variant('()',()))
        elif method=='GetConnectionByUuid':
            invocation.return_value(GLib.Variant('(o)',(ROOT+'/Settings/1',)))
        elif method=='ActivateConnection':
            connection,device,specific=args
            event('band-activate',device=device,specific=specific)
            if control.get('switch_fails'):
                raise ValueError('Simulated band switch failure')
            if specific in ACCESS_POINTS:
                frequency=ACCESS_POINTS[specific]['frequency']
                control['roamed']=frequency==2437
                control['band5']=frequency==5745
                if control.get('roam_back'):
                    control['roamed']=False
                    control['band5']=False
                (DIRECTORY/'control.json').write_text(json.dumps(control))
            elif control.get('roam_back'):
                control['roamed']=False
                control['band5']=False
                (DIRECTORY/'control.json').write_text(json.dumps(control))
            invocation.return_value(GLib.Variant('(o)',(ROOT+'/ActiveConnection/1',)))
        elif method=='GetDeviceByIpIface':
            iface=args[0]
            if iface==CAPTURE['wifi_iface']:
                value=ROOT+'/Devices/1'
            elif iface in state['interfaces']:
                value=ROOT+'/Devices/2'
            else:
                raise ValueError('No such device')
            invocation.return_value(GLib.Variant('(o)',(value,)))
        elif method=='ListConnections':
            invocation.return_value(GLib.Variant('(ao)',([ROOT+'/Settings/1',*state['profiles']],)))
        elif method=='GetSettings':
            if path==ROOT+'/Settings/1':
                settings={'connection':{'uuid':GLib.Variant('s','11111111-1111-4111-a111-111111111111'),'id':GLib.Variant('s','Untouched upstream')},
                          '802-11-wireless':{'mode':GLib.Variant('s','infrastructure')}}
            else:
                record=state['profiles'][path]
                settings={section:{key:record['variant'].lookup_value(section,None).lookup_value(key,None)
                                   for key in fields if key!='psk'} for section,fields in record['settings'].items()}
                if control.get('tamper_profile'):
                    settings['user']['data']=GLib.Variant('a{ss}',{'io.github.relayforge.owner':'foreign-owner'})
            invocation.return_value(GLib.Variant('(a{sa{sv}})',(settings,)))
        elif method=='Delete':
            if path==ROOT+'/Settings/1':
                state['foreign_deletes']+=1
                raise ValueError('Must not delete upstream')
            if control.get('cleanup_fails'):
                raise ValueError('Simulated profile deletion failure')
            remove_profile(path)
            invocation.return_value(GLib.Variant('()',()))
        elif method=='AddAndActivateConnection2':
            settings,device,specific,options=args
            if settings['802-11-wireless'].get('mode')!='ap':
                # Volatile band-constrained activation of the upstream station.
                assert settings['802-11-wireless']['band'] in ('a','bg')
                assert settings['connection']['id']=='RelayForge band switch'
                event('band-pin',band=settings['802-11-wireless']['band'])
                if specific in ACCESS_POINTS:
                    frequency=ACCESS_POINTS[specific]['frequency']
                    control['roamed']=frequency==2437
                    control['band5']=frequency==5745
                    (DIRECTORY/'control.json').write_text(json.dumps(control))
                invocation.return_value(GLib.Variant('(ooa{sv})',(ROOT+'/Settings/1',ROOT+'/ActiveConnection/1',{})))
                return
            assert options=={'persist':'volatile','bind-activation':'dbus-client'}
            assert settings['connection']['autoconnect'] is False
            assert settings['802-11-wireless']['mode']=='ap'
            assert settings['802-11-wireless']['channel']==(6 if control.get('roamed') else 1)
            # A psk-flags value makes NetworkManager discard the supplied PSK and
            # ask an agent for it, so the profile must not carry one.
            assert 'psk-flags' not in settings['802-11-wireless-security']
            assert settings['802-11-wireless-security']['proto']==['rsn']
            assert settings['802-11-wireless-security']['pairwise']==['ccmp']
            assert settings['802-11-wireless-security']['group']==['ccmp']
            assert settings['ipv4']['address-data']==[{'address':'192.168.12.1','prefix':24}]
            assert settings['ipv4']['never-default'] is True
            assert settings['ipv6']['method']=='disabled'
            assert settings['connection']['interface-name']!=CAPTURE['wifi_iface']
            assert bool(settings['user']['data']['io.github.relayforge.owner'])
            if control.get('deny_activation'):
                raise ValueError('Simulated activation denial with credentials that must not leak')
            variant=parameters.get_child_value(0)
            iface=settings['connection']['interface-name']
            native=control.get('native',False)
            assert device==('/' if native else ROOT+'/Devices/2')
            if native:
                assert settings['user']['data']['org.freedesktop.NetworkManager.wifi-relay.parent']==CAPTURE['wifi_iface']
                state['interfaces'][iface]=dict(ifindex=102,mac='02:77:88:99:aa:bb',phy=0)
            path=ROOT+'/Settings/2';active=ROOT+'/ActiveConnection/2'
            state['profiles'][path]=dict(settings=settings,variant=variant,sender=sender,native=native)
            state['active'][active]=path
            event('activation-request',native=native,uuid=settings['connection']['uuid'],secrets_in_memory_only=True)
            if control.get('change_during_activation'):
                control['upstream_changed']=True
                (DIRECTORY/'control.json').write_text(json.dumps(control))
            reply=GLib.Variant('(ooa{sv})',(path,active,{}))
            if control.get('delay_reply'):
                GLib.timeout_add(5000,lambda:(invocation.return_value(reply),False)[1])
            else:
                invocation.return_value(reply)
        elif method=='DeactivateConnection':
            event('deactivate-owned')
            if control.get('cleanup_fails'):
                raise ValueError('Simulated deactivate failure')
            path=state['active'].get(args[0])
            if path and not control.get('retain_profile'):
                remove_profile(path)
            invocation.return_value(GLib.Variant('()',()))
        else:
            raise ValueError('Unexpected method')
    except Exception:
        invocation.return_dbus_error(NM+'.Failed','Simulated NetworkManager failure')


def owner_changed(_bus,_sender,_path,_interface,_signal,params):
    name,old,new=params.unpack()
    if old and not new:
        for path,record in list(state['profiles'].items()):
            if record['sender']==name and not CONTROL().get('retain_profile') and not CONTROL().get('cleanup_fails'):
                remove_profile(path)

register(ROOT,'''<node><interface name="org.freedesktop.NetworkManager">
<method name="GetDeviceByIpIface"><arg type="s" direction="in"/><arg type="o" direction="out"/></method>
<method name="ActivateConnection"><arg type="o" direction="in"/><arg type="o" direction="in"/><arg type="o" direction="in"/><arg type="o" direction="out"/></method>
<method name="AddAndActivateConnection2"><arg type="a{sa{sv}}" direction="in"/><arg type="o" direction="in"/><arg type="o" direction="in"/><arg type="a{sv}" direction="in"/><arg type="o" direction="out"/><arg type="o" direction="out"/><arg type="a{sv}" direction="out"/></method>
<method name="DeactivateConnection"><arg type="o" direction="in"/></method></interface></node>''')
register(ROOT,device_xml())
register(ROOT+'/Settings','''<node><interface name="org.freedesktop.NetworkManager.Settings"><method name="ListConnections"><arg type="ao" direction="out"/></method><method name="GetConnectionByUuid"><arg type="s" direction="in"/><arg type="o" direction="out"/></method></interface></node>''')
register(ROOT+'/Devices/1','''<node><interface name="org.freedesktop.NetworkManager.Device.Wireless"><method name="RequestScan"><arg type="a{sv}" direction="in"/></method></interface></node>''')
for index in ACCESS_POINTS:
    register(index,device_xml())
profile_xml='''<node><interface name="org.freedesktop.NetworkManager.Settings.Connection"><method name="GetSettings"><arg type="a{sa{sv}}" direction="out"/></method><method name="Delete"/></interface></node>'''
for number in (1,2):
    register(ROOT+f'/Settings/{number}',profile_xml)
    register(ROOT+f'/Devices/{number}',device_xml())
    register(ROOT+f'/ActiveConnection/{number}',device_xml())
register(ROOT,'<node><interface name="'+TEST+'">'+''.join('<method name="'+name+'"><arg type="s" direction="in"/><arg type="s" direction="out"/></method>' for name in ('Radio','Identity','Interfaces','Routes','Create','Delete','RestoreMode','VerifyAp','Clients','EmitRadio'))+'</interface></node>')
BUS.signal_subscribe('org.freedesktop.DBus','org.freedesktop.DBus','NameOwnerChanged','/org/freedesktop/DBus',None,Gio.DBusSignalFlags.NONE,owner_changed)
BUS.call_sync('org.freedesktop.DBus','/org/freedesktop/DBus','org.freedesktop.DBus','RequestName',GLib.Variant('(su)',(NM,4)),None,Gio.DBusCallFlags.NONE,5000,None)
event('ready')
print('ready',flush=True)
GLib.MainLoop().run()
