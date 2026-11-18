"""
TCP Reservation Server.

Speaks a simple line-based protocol with clients (one thread per connection),
forwards RESERVE / RELEASE decisions to the SDN controller's REST API, and
expires reservations when their duration runs out.

Protocol (one request per line, one JSON reply per line):
    RESERVE <dst_ip> <mbps> <duration_s>   -> {"status":"OK","id":1,...} | {"status":"ERR","reason":"..."}
    RELEASE <id>
    LIST
    STATUS
    STATS                                   -> utilization + accepted/rejected counters
    QUIT

The source IP of a reservation is the IP of the connecting client.

Run (in the root namespace, after the topology is up):  python3 reservation_server.py
"""
import ipaddress
import json
import socket
import threading
import time
import urllib.error
import urllib.request

HOST, PORT = '0.0.0.0', 5000
CONTROLLER = 'http://127.0.0.1:8080'

lock = threading.Lock()
reservations = {}   # id -> {src, dst, mbps, expires}


def controller(method, path, body=None):
    """Call the controller REST API; always returns a dict."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(CONTROLLER + path, data=data, method=method,
                                 headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:      # 409 = rejected by controller
        return json.loads(e.read())
    except Exception:
        return {'status': 'ERR', 'reason': 'CONTROLLER_UNAVAILABLE'}


def err(reason):
    return {'status': 'ERR', 'reason': reason}


def handle_reserve(args, client_ip):
    if len(args) != 3:
        return err('USAGE: RESERVE <dst_ip> <mbps> <duration_s>')
    try:
        dst = str(ipaddress.ip_address(args[0]))
        mbps, duration = float(args[1]), float(args[2])
    except ValueError:
        return err('INVALID: bad argument')
    if duration <= 0:
        return err('INVALID: duration must be > 0')

    reply = controller('POST', '/reservations', {'src': client_ip, 'dst': dst, 'mbps': mbps})
    if reply.get('status') == 'OK':
        with lock:
            reservations[reply['id']] = {'src': client_ip, 'dst': dst, 'mbps': mbps,
                                         'expires': time.time() + duration}
        reply['duration'] = duration
    return reply


def handle_release(args, client_ip):
    if len(args) != 1 or not args[0].isdigit():
        return err('USAGE: RELEASE <id>')
    rid = int(args[0])
    with lock:
        r = reservations.get(rid)
        if r is None:
            return err('NOT_FOUND: id=%d' % rid)
        if r['src'] != client_ip:
            return err('DENIED: reservation belongs to another client')
        del reservations[rid]
    return controller('DELETE', '/reservations/%d' % rid)


def handle_list():
    now = time.time()
    with lock:
        items = [{'id': i, 'src': r['src'], 'dst': r['dst'], 'mbps': r['mbps'],
                  'remaining': round(r['expires'] - now, 1)} for i, r in reservations.items()]
    return {'status': 'OK', 'reservations': items}


def handle_command(line, client_ip):
    parts = line.split()
    if not parts:
        return err('EMPTY_COMMAND')
    cmd, args = parts[0].upper(), parts[1:]
    if cmd == 'RESERVE':
        return handle_reserve(args, client_ip)
    if cmd == 'RELEASE':
        return handle_release(args, client_ip)
    if cmd == 'LIST':
        return handle_list()
    if cmd == 'STATUS':
        return controller('GET', '/status')
    if cmd == 'STATS':
        return controller('GET', '/stats')
    return err('UNKNOWN_COMMAND: ' + cmd)


def serve_client(conn, addr):
    conn.settimeout(60)
    try:
        with conn, conn.makefile('r') as rfile:
            for line in rfile:
                if line.strip().upper() == 'QUIT':
                    break
                reply = handle_command(line, addr[0])
                conn.sendall((json.dumps(reply) + '\n').encode())
    except (socket.timeout, ConnectionError):
        pass


def expiry_loop():
    while True:
        time.sleep(1)
        now = time.time()
        with lock:
            expired = [i for i, r in reservations.items() if r['expires'] <= now]
            for i in expired:
                del reservations[i]
        for i in expired:
            print('[expiry] reservation %d expired' % i, flush=True)
            controller('DELETE', '/reservations/%d' % i)


def main():
    threading.Thread(target=expiry_loop, daemon=True).start()
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((HOST, PORT))
    srv.listen(16)
    print('Reservation server listening on %s:%d' % (HOST, PORT), flush=True)
    while True:
        conn, addr = srv.accept()
        threading.Thread(target=serve_client, args=(conn, addr), daemon=True).start()


if __name__ == '__main__':
    main()
