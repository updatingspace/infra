#!/usr/bin/env python3
"""Check the owned VM's public Steam query endpoint without joining the game.

Run from outside the VM, only after the game is ready. Sends A2S_INFO to the
fixed 51.250.40.253:16261 endpoint, with at most one challenge reply (2 outbound
datagrams total) and a 3-second overall network deadline. No player queries,
login, credentials, server response text or raw packets are printed.

Success confirms a structurally valid A2S_INFO response on UDP 16261 only.
It does not establish that a player can join or that UDP 16262 is reachable.
Timeout is inconclusive. Split responses are reported but not reassembled.

Protocol evidence:
https://projectzomboid.com/blog/news/2022/09/upstairs-downstairs/
https://steamcommunity.com/discussions/forum/14/2974028351344359625/
Wire format also matches panel-src/server/routes/serverFinder.js in this repo.
"""
import argparse
import json
import socket
import time


ENDPOINT = ("51.250.40.253", 16261)
TIMEOUT_SECONDS = 3.0
QUERY = b"\xff\xff\xff\xff\x54Source Engine Query\x00"
HEADER = b"\xff\xff\xff\xff"


def valid_info(packet):
    """Validate Source A2S_INFO framing without retaining server text."""
    if len(packet) < 6 or packet[:5] != HEADER + b"I":
        return False
    offset = 6  # response type and one protocol byte

    def skip(size):
        nonlocal offset
        offset += size
        if offset > len(packet):
            raise ValueError("truncated")

    def string():
        nonlocal offset
        offset = packet.index(b"\x00", offset) + 1

    try:
        for _ in range(4):  # name, map, folder, game
            string()
        fixed = offset
        skip(9)  # appID, players, capacity, bots, type, OS, visibility, VAC
        if packet[fixed + 5] not in b"dlp" or packet[fixed + 6] not in b"lwmo":
            return False
        if packet[fixed + 7] not in (0, 1) or packet[fixed + 8] not in (0, 1):
            return False
        string()  # version
        if offset < len(packet):
            flags = packet[offset]
            skip(1)
            if flags & ~0xF1:
                return False
            if flags & 0x80:
                skip(2)  # game port
            if flags & 0x10:
                skip(8)  # Steam ID
            if flags & 0x40:
                skip(2)  # SourceTV port
                string()
            if flags & 0x20:
                string()  # tags
            if flags & 0x01:
                skip(8)  # game ID
        return offset == len(packet)
    except ValueError:
        return False


def probe():
    report = {
        "ok": False,
        "endpoint": "51.250.40.253:16261/udp",
        "scope": "steam_query_only",
        "udp_16262": "not_tested",
        "sent_packets": 0,
        "response_types": [],
        "status": "unconfirmed",
    }
    deadline = time.monotonic() + TIMEOUT_SECONDS

    def remaining(sock):
        seconds = deadline - time.monotonic()
        if seconds <= 0:
            raise TimeoutError
        sock.settimeout(seconds)

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            remaining(sock)
            # This sets the UDP peer/filter locally; it sends no handshake.
            sock.connect(ENDPOINT)
            request = QUERY
            for attempt in range(2):
                remaining(sock)
                sock.send(request)
                report["sent_packets"] += 1
                remaining(sock)
                packet = sock.recv(65535)
                if packet[:4] == b"\xfe\xff\xff\xff":
                    report["response_types"].append("split_response")
                    report["status"] = "unsupported_split_response"
                    break
                if packet[:5] == HEADER + b"A" and len(packet) == 9:
                    report["response_types"].append("S2C_CHALLENGE")
                    if attempt == 0:
                        request = QUERY + packet[5:9]
                        continue
                    report["status"] = "repeated_challenge"
                    break
                if valid_info(packet):
                    report["response_types"].append("A2S_INFO")
                    report["status"] = "confirmed_a2s_info"
                    report["ok"] = True
                else:
                    report["response_types"].append("unrecognized_or_malformed")
                    report["status"] = "unconfirmed_response"
                break
    except TimeoutError:
        report["status"] = "timeout_inconclusive"
    except OSError:
        report["status"] = "network_error_unconfirmed"
    return report


def main():
    argparse.ArgumentParser(description=__doc__).parse_args()
    report = probe()
    print(json.dumps(report, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
