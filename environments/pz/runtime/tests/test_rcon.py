import importlib.util
from pathlib import Path
import socket
import struct
import threading
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('game', Path(__file__).parents[1] / 'game/game.py')
game = importlib.util.module_from_spec(spec)
spec.loader.exec_module(game)

class RconTests(unittest.TestCase):
    def exercise(self, reject=False):
        listener = socket.socket()
        listener.bind(('127.0.0.1', 0))
        listener.listen()
        port = listener.getsockname()[1]
        received = []
        def serve():
            with listener, listener.accept()[0] as client:
                received.append(game.receive(client))
                packet = struct.pack('<iii', 10, -1 if reject else 1, 2) + b'\0\0'
                # The protocol can fragment any packet, including its length.
                for byte in packet:
                    client.sendall(bytes([byte]))
                if not reject:
                    received.append(game.receive(client))
                    payload = struct.pack('<ii', 2, 0) + b'Players connected (0):\0\0'
                    client.sendall(struct.pack('<i', len(payload)) + payload)
        thread = threading.Thread(target=serve)
        thread.start()
        try:
            with patch.dict(game.os.environ, {'RCON_HOST': '127.0.0.1', 'RCON_PORT': str(port), 'RCON_PASSWORD': 'test-only'}):
                if reject:
                    with self.assertRaises(PermissionError): game.rcon('players')
                else:
                    self.assertEqual(game.rcon('players'), 'Players connected (0):')
        finally:
            thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(received[0], (1, 3, 'test-only'))
        if not reject: self.assertEqual(received[1], (2, 2, 'players'))

    def test_fragmented_response(self): self.exercise()
    def test_wrong_password(self): self.exercise(reject=True)
    def test_truncated_packet(self):
        a, b = socket.socketpair()
        with a, b:
            a.sendall(struct.pack('<i', 10) + b'bad')
            a.shutdown(socket.SHUT_WR)
            with self.assertRaises(ConnectionError): game.receive(b)

if __name__ == '__main__': unittest.main()
