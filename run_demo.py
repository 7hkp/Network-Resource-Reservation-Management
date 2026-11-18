"""
Automated end-to-end test for Deliverable 1.
Starts the topology + reservation server, runs the test cases, prints PASS/FAIL.

Prerequisite: the controller is already running (ryu-manager controller.py).
Usage:  sudo python3 run_demo.py
"""
import json
import time

from mininet.log import setLogLevel

from topology import DIR, build, start_server

results = []


def check(name, cond):
    results.append(cond)
    print('  [%s] %s' % ('PASS' if cond else 'FAIL', name))


def request(host, *args):
    """Run client.py on a Mininet host and parse the JSON reply."""
    cmd = ' '.join(str(a) for a in args)
    out = host.cmd('python3 %s/client.py %s' % (DIR, cmd)).strip().splitlines()
    out = out[-1] if out else '{}'
    print('  %s> %s\n      -> %s' % (host.name, cmd, out))
    try:
        return json.loads(out)
    except ValueError:
        return {'status': 'ERR', 'reason': 'BAD_REPLY'}


def main():
    net = build()
    server = start_server()
    h1, h2, h3, h4 = [net.get(n) for n in ('h1', 'h2', 'h3', 'h4')]
    s1 = net.get('s1')
    try:
        print('\n== Test 0: connectivity through the SDN controller')
        check('pingAll has 0% loss', net.pingAll() == 0)

        print('\n== Test 1: valid reservation h1->h3, 4 Mbps')
        a = request(h1, 'RESERVE', '10.0.0.3', 4, 60)
        check('accepted', a.get('status') == 'OK')

        print('\n== Test 2: conflicting reservation (same src/dst)')
        r = request(h1, 'RESERVE', '10.0.0.3', 1, 60)
        check('rejected with CONFLICT', 'CONFLICT' in r.get('reason', ''))

        print('\n== Test 3: second reservation h2->h4, 5 Mbps (total 9 of 10)')
        b = request(h2, 'RESERVE', '10.0.0.4', 5, 60)
        check('accepted', b.get('status') == 'OK')

        print('\n== Test 4: request exceeding remaining capacity (3 Mbps, only 1 left)')
        r = request(h2, 'RESERVE', '10.0.0.3', 3, 60)
        check('rejected with INSUFFICIENT_BANDWIDTH', 'INSUFFICIENT' in r.get('reason', ''))

        print('\n== Test 5: STATUS / LIST')
        st = request(h1, 'STATUS')
        check('used == 9 Mbps', st.get('used') == 9)
        request(h1, 'LIST')

        print('\n== Test 6: SDN policy is installed and enforced')
        print(s1.cmd('ovs-ofctl -O OpenFlow13 dump-meters s1'))
        flows = s1.cmd('ovs-ofctl -O OpenFlow13 dump-flows s1 | grep nw_src')
        print(flows)
        check('reservation flow present on s1', 'meter' in flows)
        print('  UDP iperf h1->h3 offering 8 Mbps (reserved 4 Mbps), look for ~4 Mbps:')
        h3.cmd('iperf -s -u -i 1 > /tmp/iperf_h3.txt &')
        time.sleep(1)
        print(h1.cmd('iperf -u -c 10.0.0.3 -b 8M -t 5'))
        h3.cmd('pkill -f "iperf -s"')

        print('\n== Test 7: release reservations removes policies')
        check('release h1 ok', request(h1, 'RELEASE', a['id']).get('status') == 'OK')
        check('release h2 ok', request(h2, 'RELEASE', b['id']).get('status') == 'OK')
        time.sleep(1)
        flows = s1.cmd('ovs-ofctl -O OpenFlow13 dump-flows s1 | grep nw_src')
        check('no reservation flows left', 'nw_src' not in flows)
        check('used == 0 Mbps', request(h1, 'STATUS').get('used') == 0)

        print('\n== Test 8: concurrent requests (3 clients x 4 Mbps, capacity 10)')
        for h in (h1, h2, h3):
            h.cmd('python3 %s/client.py RESERVE 10.0.0.4 4 60 > /tmp/res_%s.txt &' % (DIR, h.name))
        time.sleep(3)
        replies = []
        for h in (h1, h2, h3):
            out = h.cmd('cat /tmp/res_%s.txt' % h.name).strip()
            print('  %s -> %s' % (h.name, out))
            replies.append(json.loads(out))
        ok = [x for x in replies if x['status'] == 'OK']
        check('exactly 2 accepted, 1 rejected', len(ok) == 2)
        for h, x in zip((h1, h2, h3), replies):      # clean up
            if x['status'] == 'OK':
                request(h, 'RELEASE', x['id'])
    finally:
        server.terminate()
        net.stop()
        print('\nSummary: %d/%d checks passed' % (sum(results), len(results)))


if __name__ == '__main__':
    setLogLevel('warning')
    main()
