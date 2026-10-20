#!/usr/bin/env python3
import os,subprocess
from pathlib import Path
SOURCE=Path('/opt/notify-bridge/source')
units={
 'notify-bridge-vpn':('VPN subscription refresh and Telegram egress failover','vpn.py','1min','240s'),
 'notify-bridge-kuma-reconcile':('Replay missed Kuma events to notify-bridge','reconcile-kuma.py','1min','120s'),
 'notify-bridge-health':('Export notification maintenance state','host-health.py','1min','15s'),
 'notify-bridge-backup':('Consistent snapshot of notify-bridge SQLite','backup.py','1d','60s'),
}
for name,(description,script,interval,timeout) in units.items():
    service=f'''[Unit]
Description={description}
After=k3s.service network-online.target
[Service]
Type=oneshot
ExecStart=/usr/bin/python3 {SOURCE/script}
TimeoutStartSec={timeout}
UMask=0077
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=yes
ReadWritePaths=/opt/notify-bridge/private /srv/notify-bridge
MemoryMax=160M
CPUQuota=30%
'''
    timer=f'''[Unit]
Description=Schedule {description}
[Timer]
OnBootSec=90s
OnUnitActiveSec={interval}
AccuracySec=5s
Persistent=true
[Install]
WantedBy=timers.target
'''
    (Path('/etc/systemd/system')/(name+'.service')).write_text(service)
    (Path('/etc/systemd/system')/(name+'.timer')).write_text(timer)
subprocess.run(['systemctl','daemon-reload'],check=True)
for name in units:subprocess.run(['systemctl','enable','--now',name+'.timer'],check=True)
print('maintenance timers installed')
