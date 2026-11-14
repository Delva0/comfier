"""OpenSSH ProxyCommand using the user's existing local HTTP proxy.

No proxy or network configuration is changed. SSH remains end-to-end encrypted.
"""
import argparse
import os
import selectors
import socket
import sys


def connect(host, port, timeout=8):
    connection = socket.create_connection(('127.0.0.1', 10808), timeout=timeout)
    connection.settimeout(timeout)
    authority = f'{host}:{port}'
    request = f'CONNECT {authority} HTTP/1.1\r\nHost: {authority}\r\n\r\n'
    connection.sendall(request.encode('ascii'))
    response = bytearray()
    while not response.endswith(b'\r\n\r\n'):
        chunk = connection.recv(1)
        if not chunk or len(response) >= 8192:
            connection.close()
            raise OSError('Incomplete local proxy response')
        response.extend(chunk)
    if response.split(b'\r\n', 1)[0].split()[1] != b'200':
        connection.close()
        raise OSError('Existing local proxy declined CONNECT')
    return connection


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('host')
    parser.add_argument('port', type=int)
    args = parser.parse_args()
    connection = connect(args.host, args.port)
    connection.settimeout(None)
    selector = selectors.DefaultSelector()
    selector.register(connection, selectors.EVENT_READ, 'remote')
    selector.register(sys.stdin.buffer, selectors.EVENT_READ, 'local')
    while True:
        for selected, _ in selector.select():
            if selected.data == 'remote':
                data = connection.recv(65536)
                if not data:
                    raise SystemExit(0)
                sys.stdout.buffer.write(data)
                sys.stdout.buffer.flush()
            else:
                data = os.read(sys.stdin.fileno(), 65536)
                if not data:
                    connection.shutdown(socket.SHUT_WR)
                    selector.unregister(sys.stdin.buffer)
                else:
                    connection.sendall(data)
