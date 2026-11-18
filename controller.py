"""
SDN controller for the Network Resource Reservation System (Ryu, OpenFlow 1.3).

Responsibilities
  1. Basic L2 learning switch so hosts can talk.
  2. Admission control for reservation requests (capacity + conflict check).
  3. Install / remove a per-reservation policy: a meter (rate limit) plus a
     high-priority flow matching src IP -> dst IP on every switch.
  4. Expose a small REST API used by the TCP reservation server.

Run:  ryu-manager controller.py
"""
import json
import threading
import time

from ryu.app.wsgi import ControllerBase, WSGIApplication, route
from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, set_ev_cls
from ryu.lib.packet import ethernet, packet
from ryu.ofproto import ofproto_v1_3
from webob import Response

APP_NAME = 'reservation_app'
CAPACITY_MBPS = 10.0          # capacity of the s1--s2 bottleneck link (see topology.py)
RESERVATION_PRIORITY = 100    # above the learning-switch flows (priority 1)
ETH_IP = 0x0800
BE_METER_ID = 1000            # meter for best-effort (unreserved) traffic
BE_PRIORITY = 10              # above learning switch (1), below reservations (100)
MIN_BE_KBPS = 100             # floor so control traffic still works when fully reserved


class ReservationController(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]
    _CONTEXTS = {'wsgi': WSGIApplication}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.mac_to_port = {}
        self.datapaths = {}
        self.reservations = {}     # id -> {src, dst, mbps}
        self.next_id = 1
        self.lock = threading.Lock()
        self.counters = {'accepted': 0, 'rejected_conflict': 0,
                         'rejected_capacity': 0, 'rejected_invalid': 0}
        self.peak_used = 0.0
        self.util_integral = 0.0      # integral of used Mbps over time
        self.start_t = self.last_t = time.time()
        kwargs['wsgi'].register(ReservationApi, {APP_NAME: self})

    # ------------------------------------------------------------------
    # Learning switch
    # ------------------------------------------------------------------
    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def on_switch_features(self, ev):
        dp = ev.msg.datapath
        ofp, parser = dp.ofproto, dp.ofproto_parser
        self.datapaths[dp.id] = dp
        actions = [parser.OFPActionOutput(ofp.OFPP_CONTROLLER, ofp.OFPCML_NO_BUFFER)]
        self._add_flow(dp, 0, parser.OFPMatch(), actions)   # table-miss
        self._install_best_effort(dp)
        self.logger.info('Switch s%s connected', dp.id)

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def on_packet_in(self, ev):
        msg = ev.msg
        dp = msg.datapath
        ofp, parser = dp.ofproto, dp.ofproto_parser
        in_port = msg.match['in_port']
        eth = packet.Packet(msg.data).get_protocols(ethernet.ethernet)[0]
        if eth.ethertype == 0x88cc:    # ignore LLDP
            return

        table = self.mac_to_port.setdefault(dp.id, {})
        table[eth.src] = in_port
        out_port = table.get(eth.dst, ofp.OFPP_FLOOD)
        actions = [parser.OFPActionOutput(out_port)]
        if out_port != ofp.OFPP_FLOOD:
            match = parser.OFPMatch(in_port=in_port, eth_src=eth.src, eth_dst=eth.dst)
            self._add_flow(dp, 1, match, actions)
        data = msg.data if msg.buffer_id == ofp.OFP_NO_BUFFER else None
        dp.send_msg(parser.OFPPacketOut(datapath=dp, buffer_id=msg.buffer_id,
                                        in_port=in_port, actions=actions, data=data))

    def _add_flow(self, dp, priority, match, actions, meter_id=None):
        ofp, parser = dp.ofproto, dp.ofproto_parser
        inst = []
        if meter_id is not None:
            inst.append(parser.OFPInstructionMeter(meter_id))
        inst.append(parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions))
        dp.send_msg(parser.OFPFlowMod(datapath=dp, priority=priority,
                                      match=match, instructions=inst))

    # ------------------------------------------------------------------
    # Reservation logic (called from the REST API)
    # ------------------------------------------------------------------
    def add_reservation(self, src, dst, mbps):
        """Validate a request; on success install the SDN policy.
        Returns (accepted: bool, reply: dict)."""
        with self.lock:
            if mbps <= 0 or mbps > CAPACITY_MBPS:
                self.counters['rejected_invalid'] += 1
                return False, {'status': 'ERR', 'reason': 'INVALID: bandwidth must be in (0, %g] Mbps' % CAPACITY_MBPS}
            for rid, r in self.reservations.items():
                if r['src'] == src and r['dst'] == dst:
                    self.counters['rejected_conflict'] += 1
                    return False, {'status': 'ERR',
                                   'reason': 'CONFLICT: %s->%s already reserved (id=%d)' % (src, dst, rid)}
            used = sum(r['mbps'] for r in self.reservations.values())
            if used + mbps > CAPACITY_MBPS:
                self.counters['rejected_capacity'] += 1
                return False, {'status': 'ERR',
                               'reason': 'INSUFFICIENT_BANDWIDTH: requested %g, available %g Mbps'
                                         % (mbps, CAPACITY_MBPS - used)}
            rid = self.next_id
            self.next_id += 1
            self._account()
            self.reservations[rid] = {'src': src, 'dst': dst, 'mbps': mbps}
            self._install_policy(rid, src, dst, mbps)
            self._update_best_effort()
            self.counters['accepted'] += 1
            self.peak_used = max(self.peak_used, self._used())
        self.logger.info('ACCEPT id=%d %s->%s %g Mbps', rid, src, dst, mbps)
        return True, {'status': 'OK', 'id': rid, 'src': src, 'dst': dst, 'mbps': mbps}

    def remove_reservation(self, rid):
        with self.lock:
            self._account()
            r = self.reservations.pop(rid, None)
            if r is None:
                return False, {'status': 'ERR', 'reason': 'NOT_FOUND: id=%d' % rid}
            self._remove_policy(rid, r['src'], r['dst'])
            self._update_best_effort()
        self.logger.info('REMOVE id=%d', rid)
        return True, {'status': 'OK', 'id': rid}

    def status(self):
        with self.lock:
            used = sum(r['mbps'] for r in self.reservations.values())
            return {'status': 'OK', 'capacity': CAPACITY_MBPS, 'used': used,
                    'available': CAPACITY_MBPS - used, 'active': len(self.reservations)}

    # ------------------------------------------------------------------
    # Utilization tracking
    # ------------------------------------------------------------------
    def _used(self):
        return sum(r['mbps'] for r in self.reservations.values())

    def _account(self):
        """Integrate reserved bandwidth over time (call before 'used' changes)."""
        now = time.time()
        self.util_integral += self._used() * (now - self.last_t)
        self.last_t = now

    def stats(self):
        with self.lock:
            self._account()
            used = self._used()
            elapsed = max(self.last_t - self.start_t, 1e-9)
            out = {'status': 'OK', 'capacity': CAPACITY_MBPS, 'used': used,
                   'utilization_pct': round(100 * used / CAPACITY_MBPS, 1),
                   'avg_utilization_pct': round(100 * self.util_integral / elapsed / CAPACITY_MBPS, 1),
                   'peak_used': self.peak_used}
            out.update(self.counters)
            return out

    def reset_stats(self):
        with self.lock:
            for k in self.counters:
                self.counters[k] = 0
            self.peak_used = self._used()
            self.util_integral = 0.0
            self.start_t = self.last_t = time.time()
        return {'status': 'OK'}

    # ------------------------------------------------------------------
    # Dynamic best-effort policy: unreserved traffic is metered to whatever
    # bandwidth is NOT reserved, so reserved flows keep their guarantee.
    # Updated on every reserve / release / expiry.
    # ------------------------------------------------------------------
    def _be_band(self, parser):
        kbps = max(int((CAPACITY_MBPS - self._used()) * 1000), MIN_BE_KBPS)
        return parser.OFPMeterBandDrop(rate=kbps, burst_size=max(kbps // 10, 10))

    def _install_best_effort(self, dp):
        ofp, parser = dp.ofproto, dp.ofproto_parser
        dp.send_msg(parser.OFPMeterMod(datapath=dp, command=ofp.OFPMC_ADD, flags=ofp.OFPMF_KBPS,
                                       meter_id=BE_METER_ID, bands=[self._be_band(parser)]))
        match = parser.OFPMatch(eth_type=ETH_IP)
        actions = [parser.OFPActionOutput(ofp.OFPP_NORMAL)]
        self._add_flow(dp, BE_PRIORITY, match, actions, meter_id=BE_METER_ID)

    def _update_best_effort(self):
        for dp in self.datapaths.values():
            ofp, parser = dp.ofproto, dp.ofproto_parser
            dp.send_msg(parser.OFPMeterMod(datapath=dp, command=ofp.OFPMC_MODIFY, flags=ofp.OFPMF_KBPS,
                                           meter_id=BE_METER_ID, bands=[self._be_band(parser)]))

    def _install_policy(self, rid, src, dst, mbps):
        """Meter + flow (src IP -> dst IP) on every connected switch. Meter id == reservation id."""
        for dp in self.datapaths.values():
            ofp, parser = dp.ofproto, dp.ofproto_parser
            kbps = int(mbps * 1000)
            band = parser.OFPMeterBandDrop(rate=kbps, burst_size=max(kbps // 10, 10))
            dp.send_msg(parser.OFPMeterMod(datapath=dp, command=ofp.OFPMC_ADD,
                                           flags=ofp.OFPMF_KBPS, meter_id=rid, bands=[band]))
            match = parser.OFPMatch(eth_type=ETH_IP, ipv4_src=src, ipv4_dst=dst)
            # NORMAL = let OVS forward as an L2 switch; the meter polices the rate.
            actions = [parser.OFPActionOutput(ofp.OFPP_NORMAL)]
            self._add_flow(dp, RESERVATION_PRIORITY, match, actions, meter_id=rid)

    def _remove_policy(self, rid, src, dst):
        for dp in self.datapaths.values():
            ofp, parser = dp.ofproto, dp.ofproto_parser
            match = parser.OFPMatch(eth_type=ETH_IP, ipv4_src=src, ipv4_dst=dst)
            dp.send_msg(parser.OFPFlowMod(datapath=dp, command=ofp.OFPFC_DELETE,
                                          out_port=ofp.OFPP_ANY, out_group=ofp.OFPG_ANY,
                                          match=match))
            dp.send_msg(parser.OFPMeterMod(datapath=dp, command=ofp.OFPMC_DELETE,
                                           flags=ofp.OFPMF_KBPS, meter_id=rid))


class ReservationApi(ControllerBase):
    """REST API on port 8080, used by reservation_server.py."""

    def __init__(self, req, link, data, **config):
        super().__init__(req, link, data, **config)
        self.app = data[APP_NAME]

    @staticmethod
    def _reply(ok, body):
        return Response(content_type='application/json', charset='utf-8',
                        status=200 if ok else 409, body=json.dumps(body).encode())

    @route('reserve', '/reservations', methods=['POST'])
    def create(self, req, **kwargs):
        body = req.json
        try:
            src, dst, mbps = str(body['src']), str(body['dst']), float(body['mbps'])
        except (KeyError, ValueError, TypeError):
            return self._reply(False, {'status': 'ERR', 'reason': 'INVALID: bad request body'})
        return self._reply(*self.app.add_reservation(src, dst, mbps))

    @route('release', '/reservations/{rid}', methods=['DELETE'])
    def delete(self, req, rid, **kwargs):
        return self._reply(*self.app.remove_reservation(int(rid)))

    @route('status', '/status', methods=['GET'])
    def status(self, req, **kwargs):
        return self._reply(True, self.app.status())

    @route('stats', '/stats', methods=['GET'])
    def stats(self, req, **kwargs):
        return self._reply(True, self.app.stats())

    @route('stats_reset', '/stats/reset', methods=['POST'])
    def stats_reset(self, req, **kwargs):
        return self._reply(True, self.app.reset_stats())
