#!/usr/bin/env python3
"""Run repository tests in separate processes so component imports stay isolated."""
from pathlib import Path
import os
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def main():
    required = ('age', 'age-keygen', 'zstd', 'rsync', 'java', 'node')
    missing = [name for name in required if not shutil.which(name)]
    if missing:
        sys.exit('Missing test tools: ' + ', '.join(missing))
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
    directories = sorted({path.parent for path in ROOT.rglob('test_*.py') if '.git' not in path.parts})
    for directory in directories:
        print('Testing ' + str(directory.relative_to(ROOT)), flush=True)
        subprocess.run([sys.executable, '-m', 'unittest', 'discover', '-s', str(directory), '-p', 'test_*.py', '-v'],
                       cwd=ROOT, env=env, check=True)
    telemetry = ROOT / 'environments/pz/kubernetes/panel-telemetry'
    subprocess.run(['node', '--test', str(telemetry / 'test-provider.mjs'),
                    str(telemetry / 'test-server-transform.mjs'), str(telemetry / 'client-overlay.test.mjs')],
                   cwd=ROOT, check=True)


if __name__ == '__main__':
    main()
