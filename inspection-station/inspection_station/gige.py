"""
Find GigE Vision cameras (Genie Nano) on the network - no camera software needed.

Uses the GigE Vision control protocol (GVCP, UDP port 3956) directly:
  * discover()        broadcast "who is there?" on every network adapter; each camera
                      answers with its model, serial number, MAC and IP settings.
                      Works even when the camera is on a different subnet than this computer.
  * force_ip()        give a camera a temporary IP (until it is power-cycled) so it is on
                      this computer's subnet - the fix for "found, but different network".
  * set_persistent()  store a fixed IP in the camera (survives power-off).

Pure Python standard library - works on Windows, macOS, Linux and the Pi.
Command line: python find_camera.py  (in the project folder).
"""
from __future__ import annotations

import ipaddress
import random
import re
import select
import socket
import struct
import subprocess
import sys
import time

GVCP_PORT = 3956
KEY = 0x42
DISCOVERY_CMD, DISCOVERY_ACK = 0x0002, 0x0003
FORCEIP_CMD, FORCEIP_ACK = 0x0004, 0x0005
READREG_CMD, READREG_ACK = 0x0080, 0x0081
WRITEREG_CMD, WRITEREG_ACK = 0x0082, 0x0083
FLAG_ACK = 0x01                  # please answer
FLAG_BCAST_ACK = 0x10            # answer by broadcast (reaches us even across subnets)

REG_IP_CONFIG = 0x0014           # bit0 persistent, bit1 DHCP, bit2 link-local
REG_PERSIST_IP, REG_PERSIST_MASK, REG_PERSIST_GW = 0x064C, 0x065C, 0x066C
REG_CCP = 0x0A00                 # control channel privilege


def _ip(n: int) -> str:
    return str(ipaddress.IPv4Address(n))


def _str(b: bytes) -> str:
    return b.split(b"\0", 1)[0].decode("ascii", "replace").strip()


# =============================================================================
# This computer's network adapters
# =============================================================================
def local_interfaces() -> list[dict]:
    """[{ip, mask, name}] for every IPv4 adapter (not loopback). Best effort, no extra packages."""
    found: dict[str, dict] = {}

    def add(ip, mask=None, name=""):
        try:
            a = ipaddress.IPv4Address(ip)
        except ValueError:
            return
        if a.is_loopback or a.is_unspecified:
            return
        if mask is None:                             # unknown: link-local is /16, guess /24 otherwise
            mask = "255.255.0.0" if a.is_link_local else "255.255.255.0"
        found.setdefault(ip, {"ip": ip, "mask": mask, "name": name})

    try:
        if sys.platform.startswith("win"):
            out = subprocess.run(["ipconfig"], capture_output=True, text=True, timeout=5).stdout
            name, ip = "", None
            for line in out.splitlines():
                if line and not line.startswith(" "):
                    name, ip = line.strip().rstrip(":"), None
                m = re.search(r"IPv4[^:]*:\s*([\d.]+)", line)
                if m:
                    ip = m.group(1)
                m = re.search(r"(?:Subnet Mask|Subnetzmaske|Masque)[^:]*:\s*([\d.]+)", line)
                if m and ip:
                    add(ip, m.group(1), name)
        elif sys.platform == "darwin":
            out = subprocess.run(["ifconfig"], capture_output=True, text=True, timeout=5).stdout
            name = ""
            for line in out.splitlines():
                if line and not line[0].isspace():
                    name = line.split(":")[0]
                m = re.search(r"inet ([\d.]+) netmask 0x([0-9a-f]{8})", line)
                if m:
                    add(m.group(1), _ip(int(m.group(2), 16)), name)
        else:
            _linux_interfaces(add)
    except (OSError, subprocess.SubprocessError):
        pass
    try:                                             # fallback / extra: what the OS resolves for us
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            add(info[4][0])
    except OSError:
        pass
    return list(found.values())


def _linux_interfaces(add):
    """Address + netmask of each adapter via ioctl (no 'ip' command needed)."""
    import fcntl
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        for _, name in socket.if_nameindex():
            req = struct.pack("256s", name.encode()[:15])
            try:
                ip = socket.inet_ntoa(fcntl.ioctl(s.fileno(), 0x8915, req)[20:24])      # SIOCGIFADDR
                mask = socket.inet_ntoa(fcntl.ioctl(s.fileno(), 0x891B, req)[20:24])    # SIOCGIFNETMASK
            except OSError:
                continue                                                              # no IPv4 address
            add(ip, mask, name)
    finally:
        s.close()


def same_subnet(ip_a: str, ip_b: str, mask: str) -> bool:
    try:
        net = ipaddress.IPv4Network(f"{ip_a}/{mask}", strict=False)
        return ipaddress.IPv4Address(ip_b) in net
    except ValueError:
        return False


# =============================================================================
# Discovery
# =============================================================================
def _sockets(ifaces):
    """One socket on all adapters + one per adapter (Windows sends a broadcast out of only one adapter
    per socket, so binding one socket to each adapter reaches cameras on every port)."""
    socks = []
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.bind(("", 0))
    socks.append((s, None))
    for i in ifaces:
        try:
            t = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            t.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            t.bind((i["ip"], 0))
            socks.append((t, i))
        except OSError:
            pass
    return socks


def _targets(ifaces):
    t = {"255.255.255.255", "169.254.255.255"}
    for i in ifaces:
        try:
            t.add(str(ipaddress.IPv4Network(f"{i['ip']}/{i['mask']}", strict=False).broadcast_address))
        except ValueError:
            pass
    return t


def parse_discovery_ack(data: bytes) -> dict | None:
    if len(data) < 8 + 0xF8:
        return None
    status, answer, length, _ = struct.unpack(">HHHH", data[:8])
    if answer != DISCOVERY_ACK or status != 0:
        return None
    p = data[8:8 + 0xF8]
    mac_hi, = struct.unpack(">H", p[0x0A:0x0C])
    mac_lo, = struct.unpack(">I", p[0x0C:0x10])
    options, current = struct.unpack(">II", p[0x10:0x18])
    ip, = struct.unpack(">I", p[0x24:0x28])
    mask, = struct.unpack(">I", p[0x34:0x38])
    gw, = struct.unpack(">I", p[0x44:0x48])
    mac = ":".join(f"{b:02X}" for b in (mac_hi.to_bytes(2, "big") + mac_lo.to_bytes(4, "big")))
    if ipaddress.IPv4Address(ip).is_link_local:
        mode = "automatic link-local address (no DHCP server found)"
    elif current & 1 and options & 1:
        mode = "fixed (persistent) IP"
    else:
        mode = "DHCP or temporary (Force IP)"
    return {"ip": _ip(ip), "mask": _ip(mask), "gateway": _ip(gw), "mac": mac,
            "manufacturer": _str(p[0x48:0x68]), "model": _str(p[0x68:0x88]),
            "version": _str(p[0x88:0xA8]), "serial": _str(p[0xD8:0xE8]),
            "user_name": _str(p[0xE8:0xF8]), "ip_mode": mode,
            "persistent_enabled": bool(options & current & 1)}


def discover(timeout: float = 1.5) -> dict:
    """Broadcast a discovery and collect answers.
    Returns {"cameras": [...], "interfaces": [...]}; each camera has "reachable_via" (the adapter
    on the same subnet) or None when this computer and the camera are on different networks."""
    ifaces = local_interfaces()
    req = random.randint(1, 0xFFFF)
    pkt = struct.pack(">BBHHH", KEY, FLAG_ACK | FLAG_BCAST_ACK, DISCOVERY_CMD, 0, req)
    socks = _sockets(ifaces)
    targets = _targets(ifaces)
    cams: dict[str, dict] = {}
    try:
        for s, iface in socks:
            for t in (targets if iface is None else {"255.255.255.255"}):
                try:
                    s.sendto(pkt, (t, GVCP_PORT))
                except OSError:
                    pass
        end = time.monotonic() + timeout
        while (left := end - time.monotonic()) > 0:
            ready, _, _ = select.select([s for s, _ in socks], [], [], left)
            for s in ready:
                try:
                    data, addr = s.recvfrom(2048)
                except OSError:
                    continue
                cam = parse_discovery_ack(data)
                if cam:
                    cam["answered_from"] = addr[0]
                    cams.setdefault(cam["mac"], cam)
    finally:
        for s, _ in socks:
            s.close()
    for cam in cams.values():
        via = [i for i in ifaces if same_subnet(cam["ip"], i["ip"], cam["mask"])]
        cam["reachable_via"] = via[0] if via else None
        cam["suggested_ip"] = None if via else suggest_ip(ifaces, cam)
    return {"cameras": sorted(cams.values(), key=lambda c: c["ip"]), "interfaces": ifaces}


def suggest_ip(ifaces, cam=None) -> dict | None:
    """An unused-looking IP for the camera on this computer's camera network (for Force IP).
    Prefers the link-local adapter (a cable straight to the camera, no router) or a wired one."""
    if not ifaces:
        return None
    def rank(i):
        a = ipaddress.IPv4Address(i["ip"])
        name = i.get("name", "").lower()
        wired = any(k in name for k in ("ethernet", "eth", "enp", "eno", "en0", "en1", "lan"))
        wireless = any(k in name for k in ("wi-fi", "wifi", "wlan", "wireless"))
        return (not a.is_link_local, not wired, wireless)
    pick = sorted(ifaces, key=rank)[0]
    net = ipaddress.IPv4Network(f"{pick['ip']}/{pick['mask']}", strict=False)
    own = int(ipaddress.IPv4Address(pick["ip"]))
    base, usable = int(net.network_address), max(1, net.num_addresses - 2)
    cand = base + ((own - base + 100) % usable) + 1
    if cand == own:
        cand = base + ((own - base + 101) % usable) + 1
    return {"ip": _ip(cand), "mask": pick["mask"], "via": pick["ip"], "adapter": pick.get("name", "")}


# =============================================================================
# Changing the camera's IP
# =============================================================================
def force_ip(mac: str, ip: str, mask: str, gateway: str = "0.0.0.0", timeout: float = 2.0) -> bool:
    """Temporary IP (lost when the camera is power-cycled). Sent by broadcast, so it works across subnets."""
    mb = bytes.fromhex(mac.replace(":", "").replace("-", ""))
    payload = (b"\0\0" + mb[:2] + mb[2:] + b"\0" * 12 + socket.inet_aton(ip) + b"\0" * 12
               + socket.inet_aton(mask) + b"\0" * 12 + socket.inet_aton(gateway))
    req = random.randint(1, 0xFFFF)
    pkt = struct.pack(">BBHHH", KEY, FLAG_ACK | FLAG_BCAST_ACK, FORCEIP_CMD, len(payload), req) + payload
    ifaces = local_interfaces()
    socks = _sockets(ifaces)
    try:
        for s, iface in socks:
            for t in (_targets(ifaces) if iface is None else {"255.255.255.255"}):
                try:
                    s.sendto(pkt, (t, GVCP_PORT))
                except OSError:
                    pass
        end = time.monotonic() + timeout
        while (left := end - time.monotonic()) > 0:
            ready, _, _ = select.select([s for s, _ in socks], [], [], left)
            for s in ready:
                try:
                    data, _ = s.recvfrom(2048)
                except OSError:
                    continue
                if len(data) >= 8:
                    status, answer = struct.unpack(">HH", data[:4])
                    if answer == FORCEIP_ACK:
                        return status == 0
    finally:
        for s, _ in socks:
            s.close()
    # some cameras don't acknowledge - check by discovery
    time.sleep(0.5)
    return any(c["ip"] == ip for c in discover(1.0)["cameras"])


def _cmd(sock, cam_ip, cmd, payload, ack, req):
    pkt = struct.pack(">BBHHH", KEY, FLAG_ACK, cmd, len(payload), req) + payload
    sock.sendto(pkt, (cam_ip, GVCP_PORT))
    end = time.monotonic() + 1.0
    while (left := end - time.monotonic()) > 0:
        ready, _, _ = select.select([sock], [], [], left)
        if not ready:
            break
        data, _ = sock.recvfrom(2048)
        status, answer, length, ack_id = struct.unpack(">HHHH", data[:8])
        if answer == ack and ack_id == req:
            if status != 0:
                raise RuntimeError(f"camera refused (GVCP status 0x{status:04X})")
            return data[8:8 + length]
    raise RuntimeError("no answer from the camera - it must be on this computer's subnet first (use Force IP)")


def set_persistent(cam_ip: str, ip: str, mask: str, gateway: str = "0.0.0.0"):
    """Store a fixed IP in the camera (used from the next power-up). The camera must be reachable now."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("", 0))
    req = random.randint(1, 0x7FFF)
    try:
        _cmd(s, cam_ip, WRITEREG_CMD, struct.pack(">II", REG_CCP, 2), WRITEREG_ACK, req)          # take control
        data = _cmd(s, cam_ip, READREG_CMD, struct.pack(">I", REG_IP_CONFIG), READREG_ACK, req + 1)
        config, = struct.unpack(">I", data[:4])
        regs = [(REG_PERSIST_IP, int(ipaddress.IPv4Address(ip))),
                (REG_PERSIST_MASK, int(ipaddress.IPv4Address(mask))),
                (REG_PERSIST_GW, int(ipaddress.IPv4Address(gateway))),
                (REG_IP_CONFIG, config | 0x1 | 0x4)]                                              # persistent + LLA
        _cmd(s, cam_ip, WRITEREG_CMD, b"".join(struct.pack(">II", a, v) for a, v in regs), WRITEREG_ACK, req + 2)
        _cmd(s, cam_ip, WRITEREG_CMD, struct.pack(">II", REG_CCP, 0), WRITEREG_ACK, req + 3)      # release
    finally:
        s.close()
