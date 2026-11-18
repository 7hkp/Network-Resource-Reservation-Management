"""
Mininet topology for the Network Resource Reservation System.

      h1 (10.0.0.1)                          h3 (10.0.0.3)
              \                                /
               s1 ======= 10 Mbps ======= s2
              /  \                             \
      h2 (10.0.0.2) root (10.0.0.100)           h4 (10.0.0.4)
                    (reservation server)

The s1--s2 link is the bottleneck whose capacity (10 Mbps) the controller manages.
The 'root' node is the machine's root namespace attached to s1, so that hosts can
reach the reservation server (10.0.0.100:5000) while the server reaches the Ryu
controller's REST API on 127.0.0.1:8080.

Usage:  sudo python3 topology.py
"""
import os
import subprocess
import time
from functools import partial

from mininet.cli import CLI
from mininet.link import TCLink
from mininet.log import setLogLevel
from mininet.net import Mininet
from mininet.node import Node, OVSSwitch, RemoteController

DIR = os.path.dirname(os.path.abspath(__file__))
SERVER_IP = '10.0.0.100'


def build():
    net = Mininet(controller=None, link=TCLink,
                  switch=partial(OVSSwitch, protocols='OpenFlow13'))
    net.addController('c0', controller=RemoteController, ip='127.0.0.1', port=6653)

    s1, s2 = net.addSwitch('s1'), net.addSwitch('s2')
    for name, ip, sw in [('h1', '10.0.0.1/24', s1), ('h2', '10.0.0.2/24', s1),
                         ('h3', '10.0.0.3/24', s2), ('h4', '10.0.0.4/24', s2)]:
        net.addLink(net.addHost(name, ip=ip), sw)
    net.addLink(s1, s2, bw=10)                       # bottleneck link

    root = Node('root', inNamespace=False)           # root namespace attached to s1
    root_link = net.addLink(root, s1)
    net.start()
    root.setIP(SERVER_IP + '/24', intf=root_link.intf1)
    return net


def start_server():
    proc = subprocess.Popen(['python3', os.path.join(DIR, 'reservation_server.py')])
    time.sleep(1)
    return proc


if __name__ == '__main__':
    setLogLevel('info')
    net = build()
    server = start_server()
    try:
        CLI(net)
    finally:
        server.terminate()
        net.stop()
