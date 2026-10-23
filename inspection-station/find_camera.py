#!/usr/bin/env python3
"""
Find the Genie Nano (or any GigE Vision camera) - no Teledyne software needed.

    python find_camera.py                         list cameras and this computer's network adapters
    python find_camera.py --force-ip 169.254.10.100
                                                  give the camera a temporary IP (until power-off) so
                                                  it is on this computer's network (when it says
                                                  "different network")
    python find_camera.py --persistent 192.168.10.10 --mask 255.255.255.0
                                                  store a fixed IP in the camera (stays after power-off)

Windows: if Windows Firewall asks, allow Python on Private AND Public networks (a cable straight
to the camera counts as an "unidentified / public" network) - otherwise the answers are blocked.
"""
import argparse
import sys

from inspection_station.gige import discover, force_ip, set_persistent


def main():
    ap = argparse.ArgumentParser(description="Find GigE Vision cameras (Genie Nano) on the network")
    ap.add_argument("--force-ip", metavar="IP", help="temporary IP for the camera")
    ap.add_argument("--persistent", metavar="IP", help="store this fixed IP in the camera")
    ap.add_argument("--mask", default=None, help="subnet mask for --force-ip / --persistent")
    ap.add_argument("--mac", help="which camera (if more than one)")
    a = ap.parse_args()

    print("Looking for GigE cameras on every network adapter...\n")
    r = discover()
    print("This computer:")
    for i in r["interfaces"]:
        print(f"  {i['ip']:<16} mask {i['mask']:<16} {i['name']}")
    if not r["interfaces"]:
        print("  (no network adapters with an IPv4 address)")
    print()

    cams = r["cameras"]
    if not cams:
        print("No camera answered. Check:")
        print("  1. Camera power: the status LED must be on. A laptop's Ethernet port gives NO power -")
        print("     use a PoE injector (802.3af) or a 10-36 V supply on the camera's 10-pin connector.")
        print("  2. LED slow-flashing blue = no network link: check the cable / adapter.")
        print("  3. Just plugged in? Wait ~1 minute: with no router, Windows takes a while to give the")
        print("     Ethernet port a 169.254.x.x address (the camera does the same).")
        print("  4. Windows Firewall: allow Python on Public networks, or try again with the firewall off.")
        print("  5. Turn off VPN software on that Ethernet adapter.")
        return 1

    for c in cams:
        print(f"Camera: {c['manufacturer']} {c['model']}   serial {c['serial']}   MAC {c['mac']}")
        print(f"  IP        {c['ip']}  (mask {c['mask']}, gateway {c['gateway']}) - {c['ip_mode']}")
        if c["user_name"]:
            print(f"  Name      {c['user_name']}")
        if c["reachable_via"]:
            print(f"  OK - same network as this computer's {c['reachable_via']['ip']} "
                  f"({c['reachable_via']['name'] or 'adapter'}).")
            print(f"  Put {c['ip']} in the dashboard: Settings > Camera > Camera IP.")
        else:
            s = c["suggested_ip"]
            print("  ! The camera is on a DIFFERENT network than this computer, so programs can't talk to it.")
            if s:
                print(f"    Fix: python find_camera.py --force-ip {s['ip']} --mask {s['mask']}"
                      f"   (or change this computer's Ethernet IP to the camera's subnet)")
        print()

    target = None
    if a.force_ip or a.persistent:
        if a.mac:
            target = next((c for c in cams if c["mac"].lower() == a.mac.lower()), None)
        elif len(cams) == 1:
            target = cams[0]
        if not target:
            print("More than one camera - choose one with --mac")
            return 1

    if a.force_ip:
        mask = a.mask or (target["suggested_ip"] or {}).get("mask") or "255.255.0.0"
        print(f"Setting temporary IP {a.force_ip} / {mask} on {target['model']} ({target['mac']})...")
        ok = force_ip(target["mac"], a.force_ip, mask)
        print("Done - the camera answers at the new IP." if ok else "The camera did not confirm - run again to check.")
        target = dict(target, ip=a.force_ip)

    if a.persistent:
        mask = a.mask or "255.255.255.0"
        print(f"Storing fixed IP {a.persistent} / {mask} in {target['model']} (talking to it at {target['ip']})...")
        try:
            set_persistent(target["ip"], a.persistent, mask)
        except RuntimeError as e:
            print(f"Failed: {e}")
            return 1
        print("Stored. It is used from the next power-up (power-cycle the camera).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
