"""Helpers shared by test_d2.py and evaluate.py."""
import json
import re
import time
import urllib.request

from topology import DIR

CONTROLLER = 'http://127.0.0.1:8080'


def request(host, *args, quiet=False):
    """Run client.py on a Mininet host and parse the JSON reply."""
    cmd = ' '.join(str(a) for a in args)
    out = host.cmd('python3 %s/client.py %s' % (DIR, cmd)).strip().splitlines()
    out = out[-1] if out else '{}'
    if not quiet:
        print('  %s> %s\n      -> %s' % (host.name, cmd, out))
    try:
        return json.loads(out)
    except ValueError:
        return {'status': 'ERR', 'reason': 'BAD_REPLY'}


def ctl(method, path):
    """Query the controller REST API directly (script runs in the root namespace)."""
    req = urllib.request.Request(CONTROLLER + path, method=method)
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read())


def _start_bg(host, cmd):
    """Start a background process on a host, return its pid."""
    out = host.cmd('%s > /dev/null 2>&1 & echo $!' % cmd).strip().splitlines()
    return out[-1].strip()


def start_udp_flow(src, dst, mbps, secs):
    """Start a background UDP flow (best-effort load generator). Returns pids to kill later."""
    srv = _start_bg(dst, 'iperf -s -u -o /dev/null')
    time.sleep(0.5)
    cli = _start_bg(src, 'iperf -u -c %s -b %sM -t %d' % (dst.IP(), mbps, secs))
    return [(dst, srv), (src, cli)]


def stop(procs):
    for host, pid in procs:
        host.cmd('kill %s 2>/dev/null' % pid)


RATE_RE = re.compile(r'([\d.]+)\s+([KM])bits/sec\s+[\d.]+\s+ms\s+(\d+)/\s*(\d+)\s+\(([\d.]+)%\)')


def iperf_udp(src, dst, mbps, secs=5, tag='m'):
    """Measure a UDP flow src->dst. Returns (received_mbps, loss_pct) from the receiver's report."""
    f = '/tmp/iperf_%s.txt' % tag
    dst.cmd('rm -f %s' % f)
    pid = _start_bg(dst, 'iperf -s -u -o %s' % f)
    time.sleep(0.5)
    src.cmd('iperf -u -c %s -b %sM -t %d' % (dst.IP(), mbps, secs))
    time.sleep(1.5)
    dst.cmd('kill %s 2>/dev/null' % pid)
    matches = RATE_RE.findall(dst.cmd('cat %s' % f))
    if not matches:
        return 0.0, 100.0
    rate, unit, _, _, loss = matches[-1]
    rate = float(rate) / (1000 if unit == 'K' else 1)
    return rate, float(loss)
