"""
Reservation client: sends one command to the reservation server and prints the reply.

Examples (from a Mininet host):
    python3 client.py RESERVE 10.0.0.3 4 60     # reserve 4 Mbps to 10.0.0.3 for 60 s
    python3 client.py RELEASE 1
    python3 client.py LIST
    python3 client.py STATUS
"""
import argparse
import socket
import sys


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--server', default='10.0.0.100')
    ap.add_argument('--port', type=int, default=5000)
    ap.add_argument('command', nargs='+')
    args = ap.parse_args()

    try:
        with socket.create_connection((args.server, args.port), timeout=5) as s:
            s.sendall((' '.join(args.command) + '\n').encode())
            reply = s.makefile('r').readline().strip()
            s.sendall(b'QUIT\n')
    except OSError as e:
        print('{"status":"ERR","reason":"CONNECTION_FAILED: %s"}' % e)
        sys.exit(1)
    print(reply)


if __name__ == '__main__':
    main()
