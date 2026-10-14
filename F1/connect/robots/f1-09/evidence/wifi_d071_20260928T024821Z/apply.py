import json, os, shlex, subprocess
from pathlib import Path
from datetime import datetime, timezone

TARGET='65b8366a-f2da-4c8b-8445-5bf632c98f93'
FIELDS=['connection.id','connection.autoconnect','connection.autoconnect-priority','connection.autoconnect-retries','connection.interface-name','802-11-wireless.ssid','802-11-wireless.mode']
KEYS=['connection.autoconnect','connection.autoconnect-priority','connection.autoconnect-retries']

def nm(*args):
    return subprocess.check_output(['nmcli',*args],text=True,stderr=subprocess.PIPE).strip()

def snapshot():
    rows=[]
    for line in nm('-t','-f','UUID,TYPE','connection','show').splitlines():
        uid,kind=line.split(':',1)
        if kind!='802-11-wireless':continue
        vals=nm('-g',','.join(FIELDS),'connection','show','uuid',uid).splitlines()
        row=dict(uuid=uid,**dict(zip(FIELDS,vals)))
        if row['802-11-wireless.mode']=='infrastructure':rows.append(row)
    return rows

assert Path('/etc/machine-id').read_text().strip()=='90429b8a96e04ef79dbc12175b538115', 'wrong robot'
assert nm('-g','GENERAL.CON-UUID','device','show','wlan0')==TARGET, 'D071 is not currently active'
before=snapshot()
target=next(row for row in before if row['uuid']==TARGET)
assert target['802-11-wireless.ssid']=='HQ-W2-D071' and target['connection.interface-name']=='wlan0'
stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
backup=Path('/home/user/Bruno/connect/evidence')/('wifi_d071_'+stamp)
backup.mkdir(parents=True,exist_ok=False)
os.chmod(backup,0o700)
(backup/'before.json').write_text(json.dumps(before,ensure_ascii=False,indent=2)+'\n')
rollback=['#!/usr/bin/env bash','set -euo pipefail','# Restores only autoconnect settings, without disconnecting Wi-Fi.']
for row in before:
    args=['nmcli','connection','modify','uuid',row['uuid']]
    for key in KEYS:args.extend([key,row[key]])
    rollback.append(shlex.join(args))
(backup/'rollback.sh').write_text('\n'.join(rollback)+'\n')
os.chmod(backup/'rollback.sh',0o700)
changed=[]
try:
    nm('connection','modify','uuid',TARGET,'connection.autoconnect','yes','connection.autoconnect-priority','999','connection.autoconnect-retries','0')
    changed.append(target)
    for row in before:
        if row['uuid']==TARGET:continue
        nm('connection','modify','uuid',row['uuid'],'connection.autoconnect','no')
        changed.append(row)
    after=snapshot()
    assert {r['uuid'] for r in before}=={r['uuid'] for r in after}, 'profile list changed during update'
    enabled=[r for r in after if r['connection.autoconnect']=='yes']
    assert len(enabled)==1 and enabled[0]['uuid']==TARGET
    assert enabled[0]['connection.autoconnect-priority']=='999'
    assert enabled[0]['connection.autoconnect-retries']=='0'
    assert nm('-g','GENERAL.CON-UUID','device','show','wlan0')==TARGET
except Exception:
    errors=[]
    for row in reversed(changed):
        args=['connection','modify','uuid',row['uuid']]
        for key in KEYS:args.extend([key,row[key]])
        try:nm(*args)
        except Exception as exc:errors.append(str(exc))
    (backup/'rollback_errors.json').write_text(json.dumps(errors))
    raise
report={'observed_utc':datetime.now(timezone.utc).isoformat(),'backup':str(backup),'before':before,'after':after,'active':nm('-g','GENERAL.CONNECTION,IP4.ADDRESS','device','show','wlan0'),'other_profiles_autoconnect_disabled':len(after)-1,'profiles_deleted':0,'network_restart':False,'disconnect_test':False,'reboot_test':False}
(backup/'verification.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
print(json.dumps(report,ensure_ascii=False))
