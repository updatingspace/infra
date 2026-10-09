#!/usr/bin/env python3
"""Render the captured YC operation map for a private Caddy listener."""

import collections
import json
from pathlib import Path
import re
import sys


PORTS = {"api": 8081, "sessions": 8082, "mutations": 8083, "web": 3000}


def path_pattern(path):
    parts = re.split(r"(\{[a-zA-Z_]+\+?\})", path)
    return "".join(
        ".+" if part.endswith("+}") else "[^/]+" if part.startswith("{") else re.escape(part)
        for part in parts
    )


def groups(routes):
    grouped = collections.defaultdict(list)
    for route in routes:
        path = route["path"]
        rank = 2 if "+}" in path else 1 if "{" in path else 0
        if path == "/{file+}":
            rank = 3
        assert route["component"] in PORTS
        assert route["method"] in {"*", "GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"}
        grouped[(rank, route["component"], route["method"])].append(path)
    return sorted(grouped.items())


def destination(grouped, method, path):
    for (_, component, allowed), paths in grouped:
        if allowed in {"*", method} and any(re.fullmatch(path_pattern(p), path) for p in paths):
            return component
    return None


def render(routes):
    grouped = groups(routes)
    # Check every declared operation against the actual order, including placeholders.
    for route in routes:
        sample = re.sub(r"\{\w+\+\}", "sample/nested", route["path"])
        sample = re.sub(r"\{\w+\}", "sample", sample)
        methods = ["GET", "POST", "PATCH", "DELETE", "OPTIONS"] if route["method"] == "*" else [route["method"]]
        for method in methods:
            assert destination(grouped, method, sample) == route["component"], (method, sample)
    assert destination(grouped, "GET", "/api/v1/auth/data/exports/example/download") == "mutations"
    assert destination(grouped, "GET", "/api/v1/auth/sessions") == "sessions"
    assert destination(grouped, "GET", "/unknown-page") == "web"
    assert not re.fullmatch(path_pattern("/api/v1/auth/sessions/{sid}"), "/api/v1/auth/sessions/a/b")
    lines = [
        "{", "  admin off", "  auto_https off", "  servers {",
        "    trusted_proxies static 10.42.0.0/16 127.0.0.1/32",
        "    trusted_proxies_strict", "  }", "}", ":8089 {", "  route {",
    ]
    for index, ((rank, component, method), paths) in enumerate(grouped):
        lines.append(f"    @r{index} {{")
        if method != "*":
            lines.append(f"      method {method}")
        if rank == 0:
            lines.append("      path " + " ".join(sorted(paths)))
        else:
            pattern = "^(?:" + "|".join(path_pattern(p) for p in sorted(paths)) + ")$"
            lines.append(f"      path_regexp r{index} {json.dumps(pattern)}")
        lines.extend(["    }", f"    handle @r{index} {{", f"      reverse_proxy 127.0.0.1:{PORTS[component]} {{"])
        if component == "web":
            # The unchanged production web image pins its avatar CSP to the old S3 origin.
            lines.append('        header_down Content-Security-Policy "https://storage[.]yandexcloud[.]net" "https://storage.updspace.com"')
        lines.extend(["      }", "    }"])
    lines.extend(['    respond "Not found" 404', "  }", "}", ""])
    return "\n".join(lines)


if __name__ == "__main__":
    directory = Path(__file__).parent
    rendered = render(json.loads((directory / "routes.json").read_text()))
    output = directory / "Caddyfile.internal"
    if "--check" in sys.argv:
        assert output.read_text() == rendered, "Regenerate Caddyfile.internal"
        print("All captured operations and route precedence checks passed")
    else:
        output.write_text(rendered)
