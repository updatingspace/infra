#!/usr/bin/env python3
"""Read-only HTTPS, authentication and complete Grafana asset acceptance."""
import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
import json
from pathlib import Path
import subprocess
from urllib.parse import urljoin, urlsplit

GRAFANA = 'https://grafana.updspace.com'


def grafana_assets(html):
    class Assets(HTMLParser):
        def __init__(self):
            super().__init__()
            self.assets = {}

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            path = attrs.get('src') if tag == 'script' else (
                attrs.get('href') if tag == 'link' and attrs.get('rel') == 'stylesheet' else None)
            if not path:
                return
            url = urljoin(GRAFANA + '/', path)
            # Never forward the operator's Basic credential to a third-party asset.
            if urlsplit(url).scheme != 'https' or urlsplit(url).netloc != urlsplit(GRAFANA).netloc:
                return
            self.assets[url] = 'script' if tag == 'script' else 'style'

    parser = Assets()
    parser.feed(html.decode())
    assert 'script' in parser.assets.values(), 'Grafana HTML contains no same-origin scripts'
    assert 'style' in parser.assets.values(), 'Grafana HTML contains no same-origin stylesheets'
    return parser.assets


def get(url, authorization=None, origin_ip=None):
    config = '' if authorization is None else 'header = "Authorization: ' + authorization + '"\n'
    extra = [] if not origin_ip else ['--resolve', urlsplit(url).hostname + ':443:' + origin_ip]
    result = subprocess.run(
        ['curl', '--compressed', '--silent', '--show-error', '--max-time', '30',
         '--config', '-', '--write-out', '\n%{http_code} %{content_type}', url] + extra,
        input=config.encode(), capture_output=True)
    assert result.returncode == 0, (url, 'incomplete download', result.returncode)
    body, metadata = result.stdout.rsplit(b'\n', 1)
    status, _, content_type = metadata.decode().partition(' ')
    return int(status), body, content_type.split(';', 1)[0].lower()


def verify_asset(url, kind, authorization, origin_ip):
    status, body, content_type = get(url, authorization, origin_ip)
    expected = {'text/javascript', 'application/javascript', 'application/x-javascript'} if kind == 'script' else {'text/css'}
    assert status == 200 and body and content_type in expected, (url, status, content_type)
    assert not body.lstrip().lower().startswith((b'<!doctype', b'<html')), (url, 'HTML instead of asset')
    return {'asset': urlsplit(url).path, 'bytes': len(body), 'contentType': content_type}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--origin-ip')
    args = parser.parse_args()
    credentials = json.loads(Path('/opt/updspace-infra/private/monitoring-credentials.json').read_text())
    auth = 'Basic ' + base64.b64encode((credentials['username'] + ':' + credentials['password']).encode()).decode()
    for domain, path in [('grafana', '/login'), ('prometheus', '/api/v1/query?query=up'), ('alerts', '/api/v2/status')]:
        url = 'https://' + domain + '.updspace.com' + path
        status, _, _ = get(url, origin_ip=args.origin_ip)
        assert status == 401, (domain, 'anonymous', status)
        wrong, _, _ = get(url, 'Basic bW9uaXRvcmluZzppbnZhbGlk', args.origin_ip)
        assert wrong == 401, (domain, 'wrong password', wrong)
        status, body, _ = get(url, auth, args.origin_ip)
        assert status == 200, (domain, 'authenticated', status)
        if domain == 'grafana':
            assets = grafana_assets(body)
            with ThreadPoolExecutor(max_workers=3) as pool:
                for result in pool.map(lambda item: verify_asset(*item, auth, args.origin_ip), assets.items()):
                    print(json.dumps(result), flush=True)
        if domain == 'prometheus':
            assert json.loads(body)['status'] == 'success'
        if domain == 'alerts':
            assert 'versionInfo' in json.loads(body)
        print(json.dumps({'host': domain + '.updspace.com', 'anonymous': 401, 'wrongPassword': 401,
                          'authenticated': 200, 'tlsVerified': True, 'originOverride': args.origin_ip}), flush=True)
    status, _, _ = get(GRAFANA + '/api/user', auth, args.origin_ip)
    assert status == 401, ('Grafana own login unexpectedly bypassed', status)
    for domain, path in [('status', '/api/status-page/updspace'), ('pz-admin', '/api/health')]:
        status, _, _ = get('https://' + domain + '.updspace.com' + path, origin_ip=args.origin_ip)
        assert status == 200, (domain, status)
        print(json.dumps({'existingHost': domain + '.updspace.com', 'http': status,
                          'tlsVerified': True, 'originOverride': args.origin_ip}))


if __name__ == '__main__':
    main()
