# Network Resource Reservation System (SDN + TCP Sockets)

Jackfruit mini project, Problem 15. Users request bandwidth over a TCP service. The SDN controller validates each request and installs a matching policy in the network.

This README covers **Deliverable 1**: architecture, TCP implementation, Mininet + SDN, socket-SDN integration, and initial testing.

## 1. Problem and objectives

Hosts ask for a bandwidth guarantee towards another host. The system must:

- accept requests over a TCP protocol, handling several clients at once
- keep track of the managed capacity and active reservations
- reject requests that conflict with an existing reservation or exceed capacity
- install a rate-limiting policy in the switches for each accepted reservation
- remove the policy when a reservation is released or expires

Managed resource: the **s1-s2 bottleneck link, 10 Mbps**.

## 2. Architecture

```
 h1 h2 (clients)                                    h3 h4 (clients/receivers)
   \  \                                              /  /
    +--s1 ================ 10 Mbps ================ s2--+
        |                                    OpenFlow 1.3 (meters + flows)
     root ns                                       |
 reservation_server.py  --- REST (127.0.0.1:8080) ---> Ryu controller (controller.py)
   TCP :5000                                          6653 <--> s1, s2
```

| Component | File | Role |
|---|---|---|
| Client | `client.py` | Sends one TCP command from a Mininet host |
| Reservation server | `reservation_server.py` | Multi-threaded TCP server, protocol parsing, reservation table, expiry |
| SDN controller | `controller.py` | Learning switch, admission control (capacity + conflict), meter/flow install and removal, REST API |
| Topology | `topology.py` | Mininet: 4 hosts, 2 switches, 10 Mbps bottleneck, root-namespace link for the server |
| Test script | `run_demo.py` | Automated D1 test cases |

### Communication flow (RESERVE)

1. Client connects to `10.0.0.100:5000` and sends `RESERVE <dst> <mbps> <seconds>`.
2. The server takes the client's IP as the source, then calls `POST /reservations` on the controller.
3. The controller checks for a conflict (same src/dst) and for capacity (sum of reserved + request <= 10 Mbps).
4. If accepted, it installs on every switch a **meter** (drop band at the reserved rate) and a **flow** (priority 100, match IPv4 src -> dst, apply meter, output NORMAL).
5. The reply goes back to the client as JSON. The server records the expiry time; when it passes, the server calls `DELETE /reservations/<id>` and the controller removes the flow and meter.

### Expected network behaviour

Reserved traffic from src to dst is policed to its reserved rate. Other traffic is forwarded by the learning-switch flows.

## 3. TCP protocol

One request per line, one JSON reply per line.

| Request | Reply on success |
|---|---|
| `RESERVE <dst_ip> <mbps> <duration_s>` | `{"status":"OK","id":1,"src":...,"dst":...,"mbps":4,"duration":60}` |
| `RELEASE <id>` (owner only) | `{"status":"OK","id":1}` |
| `LIST` | `{"status":"OK","reservations":[{id, src, dst, mbps, remaining}]}` |
| `STATUS` | `{"status":"OK","capacity":10,"used":9,"available":1,"active":2}` |
| `QUIT` | closes the connection |

Errors: `{"status":"ERR","reason":"<CODE>: ..."}` with codes `CONFLICT`, `INSUFFICIENT_BANDWIDTH`, `INVALID`, `NOT_FOUND`, `DENIED`, `CONTROLLER_UNAVAILABLE`, `UNKNOWN_COMMAND`.

Concurrency: one thread per client connection; shared state is protected by locks in the server and in the controller. The controller's lock makes admission decisions atomic.

## 4. Setup

Requirements: Ubuntu with Mininet, Open vSwitch, Ryu, and iperf (iperf2).

```bash
sudo apt install mininet openvswitch-switch iperf
pip install ryu        # if eventlet errors appear: pip install eventlet==0.30.2
```

## 5. Running

Terminal 1, controller:
```bash
ryu-manager controller.py
```

Terminal 2, automated test:
```bash
sudo python3 run_demo.py
```

Or interactively:
```bash
sudo python3 topology.py          # starts topology + reservation server, opens Mininet CLI
mininet> h1 python3 client.py RESERVE 10.0.0.3 4 60
mininet> h1 python3 client.py STATUS
mininet> sh ovs-ofctl -O OpenFlow13 dump-flows s1
mininet> sh ovs-ofctl -O OpenFlow13 dump-meters s1
mininet> h3 iperf -s -u &
mininet> h1 iperf -u -c 10.0.0.3 -b 8M -t 5      # ~4 Mbps delivered
```

If Mininet is left in a bad state: `sudo mn -c`.

## 6. D1 test cases (`run_demo.py`)

| # | Case | Expected |
|---|---|---|
| 0 | pingAll through controller | 0% loss |
| 1 | h1->h3, 4 Mbps | accepted |
| 2 | h1->h3 again | rejected, `CONFLICT` |
| 3 | h2->h4, 5 Mbps | accepted (9/10 used) |
| 4 | h2->h3, 3 Mbps | rejected, `INSUFFICIENT_BANDWIDTH` |
| 5 | STATUS | used = 9 |
| 6 | Dump flows/meters, UDP iperf h1->h3 at 8 Mbps | meter flow present, ~4 Mbps delivered |
| 7 | RELEASE both | flows/meters removed, used = 0 |
| 8 | 3 clients request 4 Mbps in parallel | exactly 2 accepted, 1 rejected |

## 7. Notes and assumptions

- All reservations are treated as crossing the s1-s2 bottleneck, which is the single managed resource.
- Reservations are unidirectional (src -> dst) and the source IP is the client's own IP.
- Bandwidth is enforced with OpenFlow 1.3 meters (rate limit on reserved traffic). They need an OVS version with meter support.
- Reservation flows use the `NORMAL` output action, so OVS must be running its standard L2 pipeline (the default).
- Duration/expiry is implemented (see Deliverable 2 for utilization stats and evaluation).

---

# Deliverable 2

## 8. What was added

| Area | Addition |
|---|---|
| Complete functionality | Utilization tracking (`STATUS`, `STATS`): used/available, utilization %, time-averaged and peak utilization, counters for accepted / rejected (conflict, capacity, invalid). Expiry and owner-only release from D1. |
| Dynamic SDN | **Best-effort meter** (id 1000, priority 10) on every switch. Its rate is always `capacity - reserved` and is modified by the controller on every reserve, release and expiry. Reserved flows (own meter each) plus best-effort therefore never exceed the link capacity, so a reservation holds even when the link is flooded. |
| Robustness | Congestion scenario and a 12-request concurrent burst (atomic admission), plus invalid input and unauthorized release handling. |
| Evaluation | `evaluate.py`: two experiments against two baselines (below). |

New REST endpoints: `GET /stats`, `POST /stats/reset`. New TCP command: `STATS`.

Priorities on each switch: 100 reservation flows (own meter), 10 best-effort IPv4 (shared meter), 1 learned L2 flows, 0 table-miss.

## 9. Running D2

```bash
ryu-manager controller.py          # terminal 1
sudo python3 test_d2.py            # terminal 2: functional + robustness tests
sudo python3 evaluate.py           # terminal 2: performance evaluation (several minutes)
```
`run_demo.py` (D1 tests) still works.

### test_d2.py cases

| # | Case | Expected |
|---|---|---|
| 1 | Reserve 4 Mbps, then release | best-effort meter 10000 -> 6000 -> 10000 kbps |
| 2 | Reserve with 3 s duration | used drops to 0 after expiry, flow removed |
| 3 | Other host tries `RELEASE` | `DENIED`; owner release succeeds |
| 4 | Negative / oversize bandwidth, bad IP, unknown command | clean `ERR` replies |
| 5 | **Congestion:** 10 Mbps best-effort flood vs 4 Mbps reserved flow | reserved flow >= 90% of 4 Mbps and better than baseline |
| 6 | **Burst:** 12 concurrent 2 Mbps requests, capacity 10 | exactly 5 accepted, 7 rejected, peak <= 10 |

## 10. Performance evaluation

**Experiment A (congestion).** A reserved h1->h3 UDP flow (4 Mbps offered) while h2->h4 sends best-effort UDP at 0, 5, 10 and 15 Mbps. Baseline: same traffic with no reservation. Metrics: received throughput and loss, averaged over 3 runs.

**Experiment B (admission control).** 5, 10, 20 and 40 random requests (1-4 Mbps, random host pairs, fixed seed). Proposed: controller admission control. Baseline: accept-all with no capacity check. Metrics: accepted/rejected, peak utilization, and oversubscription (reserved demand above capacity means guarantees cannot all be honoured).

Outputs: `results/congestion.csv`, `results/admission.csv`, `results/results.png` (needs matplotlib).

### Results

Fill in from your own run (paste the tables printed by `evaluate.py`):

| bg load (Mbps) | baseline (Mbps / loss %) | reservation (Mbps / loss %) |
|---|---|---|
| 0 | | |
| 5 | | |
| 10 | | |
| 15 | | |

| requests | accepted | rejected | peak util % | accept-all util % |
|---|---|---|---|---|
| 5 | | | | |
| 10 | | | | |
| 20 | | | | |
| 40 | | | | |

## 11. Limitations (be ready to explain in the viva)

- The best-effort meter is per switch and counts all unreserved IPv4 traffic, including traffic that does not cross the s1-s2 link. It is a simplification of per-link accounting.
- When capacity is fully reserved, best-effort traffic is floored at 100 kbps so the TCP control channel keeps working.
- One managed link and unidirectional reservations; there is no routing or multi-path logic.
- Policing (drop) is used rather than queue-based scheduling, which is why delivered rate is capped at the reserved rate.
- State lives in memory: restarting the controller loses reservations, and a switch that reconnects later does not get existing reservation flows back.
