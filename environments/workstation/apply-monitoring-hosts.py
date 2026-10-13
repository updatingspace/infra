#!/usr/bin/env python3
"""Append only the explicitly approved home-monitoring override on this workstation."""
from pathlib import Path
import os
import tempfile


def updated_hosts(current, block):
    begin, end = '# BEGIN UpdSpace home monitoring', '# END UpdSpace home monitoring'
    if begin in current or end in current:
        if current.count(begin) != 1 or current.count(end) != 1:
            raise ValueError('Ambiguous managed hosts block')
        start = current.index(begin)
        stop = current.index(end, start)
        stop = current.find('\n', stop)
        stop = len(current) if stop < 0 else stop + 1
        return current[:start] + block + current[stop:]
    return current.rstrip('\n') + '\n\n' + block


def main():
    path = Path('/etc/hosts'); current = path.read_text()
    block = Path(__file__).with_name('hosts-monitoring.block').read_text()
    result = updated_hosts(current, block)
    if result == current:
        print('Monitoring host mapping already matches'); return
    info = path.stat()
    backup = Path('/etc/hosts.before-updspace-monitoring')
    with backup.open('x') as file:
        file.write(current); file.flush(); os.fsync(file.fileno())
    fd, tmp = tempfile.mkstemp(prefix='.hosts.updspace-', dir='/etc')
    try:
        with os.fdopen(fd,'w') as file:
            file.write(result); file.flush(); os.fsync(file.fileno())
            os.fchown(file.fileno(),info.st_uid,info.st_gid); os.fchmod(file.fileno(),info.st_mode & 0o777)
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)
    print('Five monitoring names now resolve to the home HTTPS edge; other entries preserved')


if __name__ == '__main__': main()
