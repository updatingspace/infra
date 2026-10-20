#!/usr/bin/env python3
"""Export only numeric maintenance state to a read-only bridge volume."""
import json,os,time
from pathlib import Path
private=Path('/opt/notify-bridge/private');public=Path('/srv/notify-bridge/status')
public.mkdir(parents=True,exist_ok=True,mode=0o755)
def load(name):
    try:return json.loads((private/name).read_text())
    except (OSError,ValueError):return {}
vpn=load('vpn-state.json');kuma=load('kuma-cursor.json')
state={'host_status_timestamp_seconds':int(time.time()),'vpn_last_refresh_timestamp_seconds':vpn.get('last_refresh',0),'vpn_last_probe_timestamp_seconds':vpn.get('last_probe_success',0),'kuma_reconcile_last_success_timestamp_seconds':kuma.get('last_success',0),'id_backup_resume_pending':int(Path('/srv/backups/updspace-id/resume-state.json').exists())}
temp=public/'health.tmp';temp.write_text(json.dumps(state));temp.chmod(0o644);os.replace(temp,public/'health.json')
