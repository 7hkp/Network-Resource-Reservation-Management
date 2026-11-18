"""
Deliverable 2 tests: dynamic SDN behaviour, expiry, access control, and challenging scenarios.

Prerequisite: controller running (ryu-manager controller.py).
Usage:  sudo python3 test_d2.py
"""
import itertools
import json
import time

from mininet.log import setLogLevel

from helpers import ctl, iperf_udp, request, start_udp_flow, stop
from topology import DIR, build, start_server

results = []


def check(name, cond):
    results.append(cond)
    print('  [%s] %s' % ('PASS' if cond else 'FAIL', name))


def be_meter(s1):
    return s1.cmd('ovs-ofctl -O OpenFlow13 dump-meters s1')


def main():
    net = build()
    server = start_server()
    h1, h2, h3, h4 = [net.get(n) for n in ('h1', 'h2', 'h3', 'h4')]
    s1 = net.get('s1')
    hosts = {h.name: h for h in (h1, h2, h3, h4)}
    try:
        print('\n== Test 1: dynamic policy - best-effort meter follows reservations')
        check('no reservations: best-effort limit = 10000 kbps', 'rate=10000' in be_meter(s1))
        a = request(h1, 'RESERVE', '10.0.0.3', 4, 60)
        time.sleep(0.5)
        check('4 Mbps reserved: best-effort limit drops to 6000 kbps', 'rate=6000' in be_meter(s1))
        request(h1, 'RELEASE', a['id'])
        time.sleep(0.5)
        check('released: best-effort limit restored to 10000 kbps', 'rate=10000' in be_meter(s1))

        print('\n== Test 2: reservation expiry')
        request(h1, 'RESERVE', '10.0.0.3', 2, 3)
        check('used == 2 right after reserve', request(h1, 'STATUS', quiet=True).get('used') == 2)
        time.sleep(5)
        check('used == 0 after 3 s duration', request(h1, 'STATUS', quiet=True).get('used') == 0)
        check('policy removed from switch', 'nw_src' not in s1.cmd(
            'ovs-ofctl -O OpenFlow13 dump-flows s1 | grep nw_src'))

        print('\n== Test 3: only the owner can release')
        a = request(h1, 'RESERVE', '10.0.0.3', 2, 60)
        r = request(h2, 'RELEASE', a['id'])
        check('h2 release denied', 'DENIED' in r.get('reason', ''))
        check('h1 release ok', request(h1, 'RELEASE', a['id']).get('status') == 'OK')

        print('\n== Test 4: bad input is rejected cleanly')
        check('negative bandwidth', 'INVALID' in request(h1, 'RESERVE', '10.0.0.3', -5, 10).get('reason', ''))
        check('over capacity (20 Mbps)', 'INVALID' in request(h1, 'RESERVE', '10.0.0.3', 20, 10).get('reason', ''))
        check('bad IP', 'INVALID' in request(h1, 'RESERVE', 'abc', 1, 10).get('reason', ''))
        check('unknown command', 'UNKNOWN' in request(h1, 'FOO').get('reason', ''))

        print('\n== Test 5 (challenging): congestion - 10 Mbps best-effort flood vs 4 Mbps reservation')
        bg = start_udp_flow(h2, h4, 10, 12)
        time.sleep(1)
        base_rate, base_loss = iperf_udp(h1, h3, 4, 5, tag='base')
        print('  baseline (no reservation): %.2f Mbps, loss %.1f%%' % (base_rate, base_loss))
        a = request(h1, 'RESERVE', '10.0.0.3', 4, 60)
        time.sleep(0.5)
        res_rate, res_loss = iperf_udp(h1, h3, 4, 5, tag='res')
        print('  with reservation:          %.2f Mbps, loss %.1f%%' % (res_rate, res_loss))
        stop(bg)
        request(h1, 'RELEASE', a['id'])
        check('reserved flow gets >= 90%% of 4 Mbps (got %.2f)' % res_rate, res_rate >= 3.6)
        check('reservation beats baseline under congestion', res_rate > base_rate)

        print('\n== Test 6 (challenging): burst of 12 concurrent requests x 2 Mbps (capacity 10)')
        ctl('POST', '/stats/reset')
        pairs = [(s, d) for s, d in itertools.permutations(hosts, 2)]       # 12 distinct pairs
        for s, d in pairs:
            hosts[s].cmd('python3 %s/client.py RESERVE %s 2 60 > /tmp/b_%s_%s.txt &'
                         % (DIR, hosts[d].IP(), s, d))
        time.sleep(4)
        accepted = []
        for s, d in pairs:
            out = hosts[s].cmd('cat /tmp/b_%s_%s.txt' % (s, d)).strip()
            rep = json.loads(out) if out else {'status': 'ERR'}
            if rep['status'] == 'OK':
                accepted.append((s, rep['id']))
        st = ctl('GET', '/stats')
        print('  accepted=%d rejected_capacity=%d peak_used=%s'
              % (len(accepted), st['rejected_capacity'], st['peak_used']))
        check('exactly 5 accepted (5 x 2 = 10 Mbps)', len(accepted) == 5)
        check('capacity never exceeded (peak <= 10)', st['peak_used'] <= 10)
        check('7 rejected for capacity', st['rejected_capacity'] == 7)
        for s, rid in accepted:
            request(hosts[s], 'RELEASE', rid, quiet=True)
        check('all released, used == 0', request(h1, 'STATUS', quiet=True).get('used') == 0)
    finally:
        server.terminate()
        net.stop()
        print('\nSummary: %d/%d checks passed' % (sum(results), len(results)))


if __name__ == '__main__':
    setLogLevel('warning')
    main()
