#!/usr/bin/env python3
"""Read-only HTTPS, authentication and complete Grafana asset acceptance."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
import json
from pathlib import Path
import subprocess
from urllib.parse import parse_qs, urljoin, urlsplit

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
            # Cookies must never be forwarded to a third-party asset.
            if urlsplit(url).scheme != 'https' or urlsplit(url).netloc != urlsplit(GRAFANA).netloc:
                return
            self.assets[url] = 'script' if tag == 'script' else 'style'

    parser = Assets()
    parser.feed(html.decode())
    assert 'script' in parser.assets.values(), 'Grafana HTML contains no same-origin scripts'
    assert 'style' in parser.assets.values(), 'Grafana HTML contains no same-origin stylesheets'
    return parser.assets


def get(url, cookie_file=None, origin_ip=None, headers=()):
    config = ''.join('header = ' + json.dumps(value) + '\n' for value in headers)
    extra = [] if not origin_ip else ['--resolve', urlsplit(url).hostname + ':443:' + origin_ip]
    if cookie_file:
        extra += ['--cookie', str(cookie_file)]
    result = subprocess.run(
        ['curl', '--compressed', '--silent', '--show-error', '--max-time', '30',
         '--config', '-', '--write-out', '\n%{http_code}\n%{content_type}\n%{redirect_url}', url] + extra,
        input=config.encode(), capture_output=True)
    assert result.returncode == 0, (urlsplit(url).hostname, 'incomplete download', result.returncode)
    body, status, content_type, redirect = result.stdout.rsplit(b'\n', 3)
    return int(status), body, content_type.decode().split(';', 1)[0].lower(), redirect.decode()


def verify_asset(url, kind, cookie_file, origin_ip):
    status, body, content_type, _ = get(url, cookie_file, origin_ip)
    expected = {'text/javascript', 'application/javascript', 'application/x-javascript'} if kind == 'script' else {'text/css'}
    assert status == 200 and body and content_type in expected, (url, status, content_type)
    assert not body.lstrip().lower().startswith((b'<!doctype', b'<html')), (url, 'HTML instead of asset')
    return {'asset': urlsplit(url).path, 'bytes': len(body), 'contentType': content_type}


def verify_login_start(host, origin_ip=None):
    status, _, _, redirect = get('https://' + host + '/oauth2/start?rd=/', origin_ip=origin_ip)
    target = urlsplit(redirect)
    query = parse_qs(target.query)
    assert status == 302 and target.scheme == 'https' and target.netloc == 'id.updspace.com' and target.path == '/oauth/authorize', (host, 'unexpected identity redirect')
    assert query.get('client_id') == ['observability']
    assert query.get('redirect_uri') == ['https://' + host + '/oauth2/callback']
    assert query.get('code_challenge_method') == ['S256'] and query.get('nonce')
    assert 'approval_prompt' not in query, 'ID rejects the legacy approval_prompt parameter'
    # Exercise the next hop too: a syntactically valid redirect can still fail in ID.
    status, _, _, login = get(redirect, origin_ip=origin_ip)
    assert status == 302 and urlsplit(login).path == '/login', (host, 'ID did not reach login', status)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--origin-ip')
    parser.add_argument('--cookie-file', type=Path, help='Private Netscape cookie file from an authorized operator session')
    args = parser.parse_args()
    if args.cookie_file:
        assert args.cookie_file.is_file() and args.cookie_file.stat().st_mode & 0o077 == 0, 'Cookie file must be private'
    for domain, path in [('grafana', '/login'), ('prometheus', '/api/v1/query?query=up'),
                         ('alerts', '/api/v2/status'), ('errors', '/'), ('status', '/dashboard')]:
        host = domain + '.updspace.com'
        url = 'https://' + host + path
        for headers in [(), ('X-Observability-Token: forged', 'X-Auth-Request-Access-Token: forged', 'Authorization: Bearer forged')]:
            status, _, _, redirect = get(url, origin_ip=args.origin_ip, headers=headers)
            assert status == 302 and redirect == 'https://' + host + '/oauth2/start?rd=/', (domain, 'anonymous/spoofed', status)
        verify_login_start(host, args.origin_ip)
        print(json.dumps({'host':host, 'anonymousAndSpoofed':302, 'idLoginReached':True, 'tlsVerified':True, 'originOverride':args.origin_ip}), flush=True)
        if args.cookie_file and domain in ('grafana', 'prometheus', 'alerts'):
            status, body, _, _ = get(url, args.cookie_file, args.origin_ip)
            assert status == 200, (domain, 'operator session', status)
            if domain == 'grafana':
                with ThreadPoolExecutor(max_workers=3) as pool:
                    for result in pool.map(lambda item: verify_asset(*item, args.cookie_file, args.origin_ip), grafana_assets(body).items()):
                        print(json.dumps(result), flush=True)
            elif domain == 'prometheus':
                assert json.loads(body)['status'] == 'success'
            else:
                assert 'versionInfo' in json.loads(body)
    for domain, path in [('status', '/status/updspace'), ('status', '/api/status-page/updspace'),
                         ('status', '/api/status-page/heartbeat/updspace'), ('pz-admin', '/api/health')]:
        status, _, _, _ = get('https://' + domain + '.updspace.com' + path, origin_ip=args.origin_ip)
        assert status == 200, (domain, path, status)
        print(json.dumps({'publicHost':domain + '.updspace.com', 'path':path, 'http':status}), flush=True)


if __name__ == '__main__':
    main()
