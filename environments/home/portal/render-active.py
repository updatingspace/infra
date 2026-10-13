#!/usr/bin/env python3
"""Render the declared Portal rollout, with a dormant override for data restore."""
import argparse
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).parent
spec = importlib.util.spec_from_file_location('portal_base', ROOT / 'render-portal.py')
base = importlib.util.module_from_spec(spec)
spec.loader.exec_module(base)


def render(dormant=False):
    state = json.loads((ROOT / 'rollout.json').read_text())
    manifest = base.render()
    frontend = json.loads((ROOT / 'frontend.json').read_text())
    manifest['items'].extend(frontend['items'])
    job_names = {x['metadata']['name'] for x in manifest['items'] if x['kind'] == 'CronJob'}
    if state['replicas'] not in (0, 1) or not isinstance(state['enabled_jobs'], list) or set(state['enabled_jobs']) - job_names:
        raise ValueError('Invalid rollout state')
    if not state['replicas'] and state['enabled_jobs']:
        raise ValueError('Cannot run jobs while applications are dormant')
    for item in manifest['items']:
        if item['kind'] == 'Deployment':
            item['spec']['replicas'] = 0 if dormant else state['replicas']
        elif item['kind'] == 'CronJob':
            item['spec']['suspend'] = dormant or item['metadata']['name'] not in state['enabled_jobs']
            if item['metadata']['name'] == 'portal-outbox':
                item['spec']['schedule'] = '*/15 * * * *'
    return manifest


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dormant', action='store_true')
    print(json.dumps(render(parser.parse_args().dormant), indent=2))
