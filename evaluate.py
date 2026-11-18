"""
Performance evaluation.

Experiment A - congestion protection (multiple background loads)
    Proposed : h1->h3 reserves 4 Mbps (controller meters best-effort to the remainder)
    Baseline : same traffic, no reservation (static forwarding)
    Metrics  : received throughput and packet loss of the h1->h3 UDP flow (4 Mbps offered)

Experiment B - admission control (multiple request loads)
    Proposed : requests go through the controller (capacity + conflict checks)
    Baseline : accept-all (no admission control), computed from the same request list
    Metrics  : accepted / rejected counts, utilization, oversubscription

Prerequisite: controller running (ryu-manager controller.py).
Usage:  sudo python3 evaluate.py        (results are written to results/*.csv and results/*.png)
"""
import csv
import os
import random
import time

from mininet.log import setLogLevel

from helpers import ctl, iperf_udp, request, start_udp_flow, stop
from topology import DIR, build, start_server

OUT = os.path.join(DIR, 'results')
CAPACITY = 10.0
BG_LOADS = [0, 5, 10, 15]          # Mbps of best-effort background traffic (h2->h4)
REPS = 3                           # repetitions per condition (averaged)
REQUEST_COUNTS = [5, 10, 20, 40]   # number of reservation requests in Experiment B


def write_csv(name, header, rows):
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, name), 'w', newline='') as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


def avg(xs):
    return sum(xs) / len(xs)


def experiment_a(net):
    h1, h2, h3, h4 = [net.get(n) for n in ('h1', 'h2', 'h3', 'h4')]
    rows = []
    print('\n=== Experiment A: reserved flow (4 Mbps) under background load ===')
    print('%-8s | %-24s | %-24s' % ('bg Mbps', 'baseline (rate, loss%)', 'reservation (rate, loss%)'))
    for bg_mbps in BG_LOADS:
        res = {}
        for mode in ('baseline', 'reserved'):
            rates, losses = [], []
            for i in range(REPS):
                rid = None
                if mode == 'reserved':
                    rid = request(h1, 'RESERVE', '10.0.0.3', 4, 120, quiet=True)['id']
                    time.sleep(0.5)
                bg = start_udp_flow(h2, h4, bg_mbps, 10) if bg_mbps else []
                time.sleep(1)
                rate, loss = iperf_udp(h1, h3, 4, 5, tag='ea')
                stop(bg)
                if rid:
                    request(h1, 'RELEASE', rid, quiet=True)
                time.sleep(1)
                rates.append(rate)
                losses.append(loss)
            res[mode] = (avg(rates), avg(losses))
        rows.append([bg_mbps, *['%.2f' % v for v in res['baseline']], *['%.2f' % v for v in res['reserved']]])
        print('%-8s | %6.2f, %6.1f%%          | %6.2f, %6.1f%%' %
              (bg_mbps, *res['baseline'], *res['reserved']))
    write_csv('congestion.csv', ['bg_mbps', 'baseline_mbps', 'baseline_loss_pct',
                                 'reserved_mbps', 'reserved_loss_pct'], rows)
    return rows


def experiment_b(net):
    hosts = [net.get(n) for n in ('h1', 'h2', 'h3', 'h4')]
    rows = []
    print('\n=== Experiment B: admission control vs accept-all (capacity %g Mbps) ===' % CAPACITY)
    print('%-8s | %-34s | %-30s' % ('requests', 'admission control',
                                     'accept-all baseline'))
    for n in REQUEST_COUNTS:
        rng = random.Random(n)                       # same request list for both approaches
        reqs = []
        for _ in range(n):
            src, dst = rng.sample(hosts, 2)
            reqs.append((src, dst, rng.choice([1, 2, 3, 4])))
        ctl('POST', '/stats/reset')
        granted = []
        for src, dst, mbps in reqs:
            rep = request(src, 'RESERVE', dst.IP(), mbps, 120, quiet=True)
            if rep.get('status') == 'OK':
                granted.append((src, rep['id']))
        st = ctl('GET', '/stats')
        rejected = st['rejected_conflict'] + st['rejected_capacity']
        for src, rid in granted:
            request(src, 'RELEASE', rid, quiet=True)
        demand = sum(m for _, _, m in reqs)
        base_util = 100 * demand / CAPACITY            # accept-all: everything is "granted"
        rows.append([n, st['accepted'], rejected,
                     round(100 * st['peak_used'] / CAPACITY, 1), n, round(base_util, 1)])
        print('%-8d | acc %2d rej %2d peak util %5.1f%%   | acc %2d rej  0 util %6.1f%%%s' %
              (n, st['accepted'], rejected, 100 * st['peak_used'] / CAPACITY, n, base_util,
               '  (oversubscribed)' if base_util > 100 else ''))
    write_csv('admission.csv', ['requests', 'accepted', 'rejected', 'peak_util_pct',
                                'baseline_accepted', 'baseline_util_pct'], rows)
    return rows


def plot(a_rows, b_rows):
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
    except ImportError:
        print('(matplotlib not installed - skipping plots, CSVs are saved)')
        return
    fig, ax = plt.subplots(1, 2, figsize=(10, 4))
    x = [r[0] for r in a_rows]
    ax[0].plot(x, [float(r[1]) for r in a_rows], 'o--', label='baseline (no reservation)')
    ax[0].plot(x, [float(r[3]) for r in a_rows], 's-', label='with reservation')
    ax[0].axhline(4, color='gray', linestyle=':', label='reserved 4 Mbps')
    ax[0].set(xlabel='background load (Mbps)', ylabel='reserved flow throughput (Mbps)',
              title='A: throughput under congestion')
    ax[0].legend()
    x = [r[0] for r in b_rows]
    ax[1].plot(x, [r[3] for r in b_rows], 's-', label='admission control (peak)')
    ax[1].plot(x, [r[5] for r in b_rows], 'o--', label='accept-all baseline')
    ax[1].axhline(100, color='gray', linestyle=':', label='capacity')
    ax[1].set(xlabel='number of requests', ylabel='reserved / capacity (%)',
              title='B: utilization vs oversubscription')
    ax[1].legend()
    fig.tight_layout()
    fig.savefig(os.path.join(OUT, 'results.png'), dpi=120)
    print('Saved', os.path.join(OUT, 'results.png'))


def main():
    net = build()
    server = start_server()
    try:
        a = experiment_a(net)
        b = experiment_b(net)
        plot(a, b)
    finally:
        server.terminate()
        net.stop()


if __name__ == '__main__':
    setLogLevel('warning')
    main()
