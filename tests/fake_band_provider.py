"""Optional private-bus reassociation fixture; no system bus or network commands."""
import time
from gi.repository import GLib

NM='org.freedesktop.NetworkManager'
ROOT='/org/freedesktop/NetworkManager'
DEV=ROOT+'/Devices/1'
PROFILE=ROOT+'/Settings/1'
SSID=b'Test upstream'
UUID='11111111-1111-4111-a111-111111111111'


class BandProvider:
    def __init__(self, control, event, register, state):
        self.control,self.event,self.state=control,event,state
        self.ap=1
        self.active=ROOT+'/ActiveConnection/1'
        self.counter=20
        self.scan=1
        self.requests=0
        self.activations={self.active:1}
        for index in range(1,5):
            register(ROOT+f'/AccessPoint/{index}', '<node><interface name="org.freedesktop.DBus.Properties"><method name="Get"><arg type="s" direction="in"/><arg type="s" direction="in"/><arg type="v" direction="out"/></method></interface></node>')
        register(DEV, '<node><interface name="org.freedesktop.NetworkManager.Device.Wireless"><method name="RequestScan"><arg type="a{sv}" direction="in"/></method><method name="GetAllAccessPoints"><arg type="ao" direction="out"/></method></interface></node>')
        self.register=register

    def ap_values(self,index):
        c=self.control()
        frequencies={1:5745,2:2412,3:2437,4:5180}
        return dict(Ssid=GLib.Variant('ay',b'Foreign network' if c.get('foreign_ssid') and index!=1 else SSID),
                    HwAddress=GLib.Variant('s','ff:00:00:00:00:00' if c.get('bad_bssid') and index==2 else f'02:aa:bb:cc:dd:{index:02x}'),
                    Frequency=GLib.Variant('u',frequencies[index]),
                    Strength=GLib.Variant('y',{1:60,2:80,3:40,4:100}[index]),
                    LastSeen=GLib.Variant('i',-1 if c.get('stale_scan') and index!=1 else int(time.clock_gettime(time.CLOCK_BOOTTIME))),
                    Mode=GLib.Variant('u',2))

    def property(self,path,interface,name):
        c=self.control()
        if '/AccessPoint/' in path:
            return self.ap_values(int(path.rsplit('/',1)[1]))[name]
        if path==DEV:
            if name=='ActiveConnection':
                return GLib.Variant('o',ROOT+'/ActiveConnection/999' if c.get('foreign_active') else self.active)
            if name=='ActiveAccessPoint':return GLib.Variant('o',ROOT+f"/AccessPoint/{3 if c.get('foreign_ap') else self.ap}")
            if name=='LastScan':return GLib.Variant('x',self.scan)
        if path in self.activations:
            return {'Connection':GLib.Variant('o',PROFILE),'Uuid':GLib.Variant('s',UUID),
                    'State':GLib.Variant('u',1 if c.get('hang_switch') and self.ap!=1 else 2)}[name]
        return None

    def settings(self):
        c=self.control()
        result={'connection':{'uuid':GLib.Variant('s',UUID),'id':GLib.Variant('s','Administrator upstream'),
                              'type':GLib.Variant('s','802-11-wireless'),
                              'autoconnect':GLib.Variant('b',not c.get('profile_changed'))},
                '802-11-wireless':{'mode':GLib.Variant('s','infrastructure'),'ssid':GLib.Variant('ay',SSID)}}
        if c.get('pinned_band'):result['802-11-wireless']['band']=GLib.Variant('s','a')
        return result

    def radio(self,capture):
        result=dict(capture)
        index=1 if self.control().get('wrong_association') else self.ap
        values=self.ap_values(index)
        result['link']=f"Connected to {values['HwAddress'].unpack()} (on demo0)\n\tSSID: Test upstream\n\tfreq: {values['Frequency'].unpack()}\n"
        return result

    def dispatch(self,sender,path,interface,method,args,invocation):
        c=self.control()
        if method=='GetSettings' and path==PROFILE:
            invocation.return_value(GLib.Variant('(a{sa{sv}})',(self.settings(),)));return True
        if method=='RequestScan':
            if not c.get('scan_stuck'):self.scan+=1
            self.event('band-scan')
            invocation.return_value(GLib.Variant('()',()));return True
        if method=='GetAllAccessPoints':
            invocation.return_value(GLib.Variant('(ao)',([ROOT+f'/AccessPoint/{i}' for i in range(1,5)],)));return True
        if method=='ActivateConnection':
            profile,device,point=args
            assert profile==PROFILE and device==DEV
            self.requests+=1
            index=int(point.rsplit('/',1)[1])
            self.event('band-activation',request=self.requests,point=index)
            if index==1 and c.get('rollback_fails'):raise ValueError('Simulated restoration failure')
            self.counter+=1
            self.active=ROOT+f'/ActiveConnection/{self.counter}'
            self.ap=index
            self.activations[self.active]=index
            self.register(self.active,'<node><interface name="org.freedesktop.DBus.Properties"><method name="Get"><arg type="s" direction="in"/><arg type="s" direction="in"/><arg type="v" direction="out"/></method></interface></node>')
            reply=GLib.Variant('(o)',(ROOT+'/ActiveConnection/1' if c.get('reused_active_reply') else self.active,))
            if c.get('ambiguous_activation') and index!=1:
                raise ValueError('Ambiguous activation after accepted request')
            if c.get('hold_activation_reply') and index!=1:
                def finish():
                    if self.control().get('hold_activation_reply'):return True
                    invocation.return_value(reply);return False
                GLib.timeout_add(20,finish)
                return True
            invocation.return_value(reply);return True
        return False
