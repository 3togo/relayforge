"""Rust firewall transactions against a private simulator, never the real firewall."""
import contextlib
import fcntl
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import unittest
from test_ap_runtime import bench as runtime_bench,private
from test_dbus import ROOT


@contextlib.contextmanager
def bench(**fields):
    with runtime_bench() as b:
        b.fields.update(WIFI_IFACE='aptest0',VWIFI_IFACE='aptest0',NEW_MACADDR='02:00:00:00:00:08',DNS_PORT='5353')
        b.fields.update(fields)
        b.interface('aptest0',8,'02:00:00:00:00:08')
        b.save()
        b.statepath=b.root/'firewall-state.json'
        private(b.statepath,{})
        yield b


def state(b):return json.loads(b.statepath.read_text())
def update(b,**fields):
    data=state(b);data.update(fields);private(b.statepath,data)
def apply(b):
    for phase in ('nat','dns','dhcp'):b.invoke('firewall-add',None,phase)


def pinned_rules(b):
    """Execute only extracted iptables invocations with a printing Bash function."""
    lines=(ROOT/'vendor/wifi-relay/daemon/create_ap').read_text().splitlines()
    commands=[]
    index=0
    while index<len(lines):
        line=lines[index]
        if re.match(r'^\s*iptables ',line):
            command=line
            while command.rstrip().endswith('\\'):
                index+=1;command+='\n'+lines[index]
            if ' -I ' in command:commands.append(command)
        index+=1
    assignments='\n'.join(k+'='+shlex.quote(v) for k,v in b.fields.items())
    script='iptables() { printf "%s\\0" "$@"; printf "\\0"; }; die() { return 1; };\n'+assignments+'\n'+'\n'.join(commands)
    output=subprocess.run(['/bin/bash','-c',script],capture_output=True,check=True,timeout=3).stdout
    rules=[]
    for entry in output.split(b'\0\0'):
        if not entry:continue
        args=entry.decode().split('\0')
        assert args.pop(0)=='-w'
        table='filter'
        if args[0]=='-t':args.pop(0);table=args.pop(0)
        assert args.pop(0)=='-I'
        chain=args.pop(0)
        if '-p' in args and '--dport' in args:
            protocol=args[args.index('-p')+1]
            if not any(args[i:i+2]==['-m',protocol] for i in range(len(args)-1)):
                position=args.index('--dport');args[position:position]=['-m',protocol]
        rules.append(dict(table=table,chain=chain,args=args))
    return rules


class FirewallTests(unittest.TestCase):
    def test_all_rules_match_pinned_bash_without_executing_network_tools(self):
        with bench() as b:
            apply(b)
            self.assertEqual(state(b)['rules'],pinned_rules(b))
            self.assertEqual(state(b)['mutations'],8)
            self.assertNotIn('PASSPHRASE',b.statepath.read_text())

    def test_idempotency_and_exact_foreign_comment_preservation(self):
        with bench() as b:
            tag=b.fields['RULE_COMMENT']
            foreign=[dict(table='filter',chain='INPUT',args=['--comment',value,'-j','ACCEPT']) for value in ('foreign',tag+'-foreign',"x'$(touch ignored) "+tag)]
            update(b,rules=foreign)
            apply(b);apply(b)
            self.assertEqual(state(b)['mutations'],8)
            b.invoke('firewall-check',ok=False)
            b.invoke('firewall-remove');b.invoke('firewall-remove');b.invoke('firewall-check')
            self.assertEqual(state(b)['rules'],foreign)
            self.assertEqual(state(b)['mutations'],16)
            self.assertFalse(any('-F' in call or '-X' in call or '-P' in call for call in state(b)['calls']))

    def test_partial_insertions_retain_snapshot_and_cleanup_every_phase(self):
        for failure in range(1,9):
            with self.subTest(failure=failure),bench() as b:
                before=b.snapshot.read_bytes()
                update(b,fail_mutation=failure)
                results=[]
                for phase in ('nat','dns','dhcp'):
                    result=b.invoke('firewall-add',None,phase,ok=False if failure<=3 and phase=='nat' or 4<=failure<=7 and phase=='dns' or failure==8 and phase=='dhcp' else True)
                    results.append(result)
                    if result.returncode:break
                self.assertTrue(results[-1].returncode)
                self.assertEqual(len(state(b)['rules']),failure-1)
                self.assertEqual(b.snapshot.read_bytes(),before)
                update(b,fail_mutation=None)
                b.invoke('firewall-remove')
                self.assertFalse(state(b)['rules'])

    def test_lost_mutation_reply_is_recovered_by_observed_rule_presence(self):
        with bench() as b:
            update(b,lose_reply=2)
            b.invoke('firewall-add',None,'nat',ok=False)
            self.assertEqual(len(state(b)['rules']),2)
            update(b,lose_reply=3)
            b.invoke('firewall-remove')
            self.assertFalse(state(b)['rules'])
            apply(b)
            self.assertEqual(len(state(b)['rules']),8)

    def test_failed_delete_is_not_reported_as_success_and_retry_preserves_evidence(self):
        with bench() as b:
            apply(b);original=b.snapshot.read_bytes()
            update(b,fail_delete=True)
            b.invoke('firewall-remove',ok=False)
            self.assertEqual(len(state(b)['rules']),8)
            self.assertEqual(b.snapshot.read_bytes(),original)
            b.invoke('firewall-check',ok=False)
            update(b,fail_delete=False)
            b.invoke('firewall-remove');b.invoke('firewall-check')

    def test_known_duplicates_are_removed_without_adding_more(self):
        with bench() as b:
            apply(b)
            update(b,rules=state(b)['rules']*2)
            count=state(b)['mutations']
            b.invoke('firewall-add',None,'nat',ok=False)
            self.assertEqual(state(b)['mutations'],count)
            b.invoke('firewall-remove')
            self.assertFalse(state(b)['rules'])

    def test_unknown_exactly_tagged_rule_blocks_success_and_is_never_deleted(self):
        with bench() as b:
            unknown=dict(table='filter',chain='OUTPUT',args=['--comment',b.fields['RULE_COMMENT'],'-j','DROP'])
            update(b,rules=[unknown])
            b.invoke('firewall-add',None,'nat',ok=False)
            b.invoke('firewall-remove',ok=False)
            self.assertEqual(state(b)['rules'],[unknown])
            self.assertFalse(any('-D' in call or '-I' in call for call in state(b)['calls']))

    def test_failed_or_malformed_queries_cannot_prove_absence(self):
        for fields in ({'fail_list':True},{'fail_check':True},{'listing':"-A INPUT --comment 'truncated"},{'listing':'-A INPUT --comment'},{'listing':'unrecognized data'}):
            with self.subTest(fields=fields),bench() as b:
                update(b,**fields)
                b.invoke('firewall-add',None,'nat',ok=False)
                b.invoke('firewall-remove',ok=False)
                self.assertEqual(state(b)['mutations'],0)

    def test_mode_and_dns_disabled_phases_have_no_unrequested_rules(self):
        with bench(SHARE_METHOD='none',NO_DNS='1',DNS_PORT='0') as b:
            b.invoke('firewall-add',None,'nat',ok=False)
            b.invoke('firewall-add',None,'dns',ok=False)
            b.invoke('firewall-add',None,'unknown',ok=False)
            b.invoke('firewall-add',None,'dhcp')
            self.assertEqual(len(state(b)['rules']),1)
            b.invoke('firewall-remove')
            self.assertFalse(state(b)['rules'])

    def test_lock_blocks_overlapping_or_orphaned_transactions_until_released(self):
        with bench() as b:
            lock=b.directory/'.firewall.lock'
            with lock.open('wb') as handle:
                lock.chmod(0o600);fcntl.flock(handle,fcntl.LOCK_EX)
                result=b.invoke('firewall-add',None,'nat',ok=False)
                self.assertIn('still running',result.stderr)
                b.invoke('firewall-remove',ok=False)
                self.assertEqual(state(b).get('mutations',0),0)
                fcntl.flock(handle,fcntl.LOCK_UN)
            b.invoke('firewall-add',None,'nat')
            b.invoke('firewall-remove')

    def test_unsafe_lock_and_simulator_files_fail_before_changes(self):
        for kind in ('lock-link','lock-mode','state-link','state-hardlink'):
            with self.subTest(kind=kind),bench() as b:
                lock=b.directory/'.firewall.lock'
                if kind=='lock-link':lock.symlink_to(b.statepath)
                elif kind=='lock-mode':lock.write_text('');lock.chmod(0o644)
                elif kind=='state-link':
                    original=b.root/'original';b.statepath.rename(original);b.statepath.symlink_to(original)
                else:os.link(b.statepath,b.root/'other')
                b.invoke('firewall-add',None,'nat',ok=False)
                self.assertEqual(state(b).get('mutations',0),0)

    def test_dead_owner_and_reused_ap_cannot_insert_or_claim_idempotent_success(self):
        with bench() as b:
            apply(b)
            data=json.loads(b.snapshot.read_text());data['owner']['starttime']+='0';private(b.snapshot,data)
            before=state(b)['mutations']
            b.invoke('firewall-add',None,'nat',ok=False)
            self.assertEqual(state(b)['mutations'],before)
            b.invoke('firewall-remove')
        for change in ('index','mac','missing'):
            with self.subTest(change=change),bench() as b:
                apply(b);before=state(b)['mutations']
                if change=='missing':shutil.rmtree(b.root/'net/aptest0')
                else:b.interface('aptest0',99 if change=='index' else 8,'02:00:00:00:00:99' if change=='mac' else '02:00:00:00:00:08')
                b.invoke('firewall-add',None,'nat',ok=False)
                self.assertEqual(state(b)['mutations'],before)
                b.invoke('firewall-remove',ok=change=='missing')

    def test_rule_count_is_bounded_without_a_global_flush(self):
        with bench() as b:
            b.invoke('firewall-add',None,'dhcp')
            update(b,rules=state(b)['rules']*65)
            before=state(b)['mutations']
            b.invoke('firewall-remove',ok=False)
            self.assertEqual(len(state(b)['rules']),65)
            self.assertEqual(state(b)['mutations'],before)
