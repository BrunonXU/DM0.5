"""Project-internal UDP endpoints and legacy unused pose constants."""

# The bridge, recorder and teleop process run on the same F1. Keep their UDP
# control plane on loopback so Wi-Fi DHCP changes cannot break startup and
# external hosts cannot inject control packets.
local_ip = "127.0.0.1"
port1 = 8888
port2 = 8899

target_left = [90, -90, -90, -90, 0, 0, 0]
target_right = [-90, -90, 90, -90, 0, 0, 0]

reset_left = [110, -90, -90, -110, 0, 0, 0]
reset_right = [-110, -90, 90, -110, 0, 0, 0]
