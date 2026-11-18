# Network-Resource-Reservation-Management
A bandwidth reservation system for SDN. Clients request bandwidth over TCP sockets; a Ryu controller checks for conflicts and capacity, then installs OpenFlow meters to enforce each reservation, with expiry and utilization tracking. A dynamic best-effort meter protects reserved flows. Evaluated in Mininet against no-reservation baselines
