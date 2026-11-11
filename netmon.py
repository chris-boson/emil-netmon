#!/usr/bin/env python3
"""EMIL NetMon (netmon) - internet connection logger and live dashboard for macOS.

Every tick (default 10s) it probes each rung of the network path in parallel:

  Wi-Fi radio    link up?, signal (RSSI), noise, tx rate, channel   (CoreWLAN helper)
  Router         first hop answers? (TTL-1 probe; works even when ping is blocked)
  Next hop       first hop beyond your router that answers ping (modem / ISP edge)
  Internet       1.1.1.1 and 8.8.8.8
  DNS            name lookup via the system resolver
  Web            HTTPS fetch - the end-to-end truth

An outage is blamed on the lowest rung that failed while everything above it also
failed (a single hop ignoring pings while traffic flows through it is not an outage).
Local outages are split by the Wi-Fi state at that moment: link dropped / weak
signal / signal fine (so congestion, the access point or the router).

Usage:
  python3 netmon.py run [--interval 10]      # log + live dashboard on http://127.0.0.1:8787
  python3 netmon.py report [--open]          # write a static report.html
  python3 netmon.py install                  # run in the background at login (launchd)
  python3 netmon.py uninstall
Data lives in ~/.netmon (override with --data-dir or NETMON_DIR).
"""
import argparse
import bisect
import csv
import hashlib
import json
import os
import random
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

VERSION = "0.9.2"
APP_NAME = "EMIL NetMon"
# The building's hub (hub/Code.gs on a Google Sheet). Everyone who clicks "Share anonymous stats"
# sends to it; build_app.sh --hub can point a build somewhere else.
DEFAULT_HUB = "https://script.google.com/macros/s/AKfycbw9om_qfZW06Ncd7DZRw2D_WpF8JmlimX7S_N0BXi3ZOUinERiqL7oDgc-1XEksTtPXoA/exec"
RELEASES = "https://github.com/chris-boson/emil-netmon/releases/latest/download"  # serves EMIL-NetMon.zip + latest.json
DOWNLOAD_URL = f"{RELEASES}/EMIL-NetMon.zip"
DEFAULT_DIR = Path(os.environ.get("NETMON_DIR", Path.home() / ".netmon"))
LABEL = "com.netmon.agent"
PLIST = Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"
PORT = 8787

PROBE_FIELDS = ["ts", "iface", "gateway", "wifi_if", "wifi_up", "rssi", "noise", "tx_rate", "channel",
                "router_ms", "hop_ip", "hop_ms", "cf_ms", "goog_ms", "dns_ms", "dns_pub_ms", "dns_tcp_ms", "http_ms", "egress", "lan_domain", "gw_mac", "note"]
# Only checks made on the building's own networks (EMIL BEWOHNER / EMIL GAST) are shared. macOS hides
# Wi-Fi names unless an app has Location access, so the networks are recognised by the DHCP domain they
# hand out, or by the building router's MAC address. Neither is ever sent.
BUILDING_DOMAINS = ("emil.local",)
BUILDING_GW_MACS = ("02:1c:7f:0a:59:c8",)
HTTP_URL = "https://www.gstatic.com/generate_204"
DNS_NAME = "www.apple.com"
WEAK_RSSI = -75
SLOW = {"router": 200, "net": 300, "http": 2000}  # ms; above this a check counts as a slowdown
TUNNEL_RE = re.compile(r"netskope|zscaler|prisma|paloalto|umbrella|opendns|iboss|forcepoint|cato|cloudflare.?warp", re.I)

WIFI_SWIFT = r'''
// Prints the current Wi-Fi link state as one JSON line (fast, no sudo needed).
import CoreWLAN
import Foundation
var o: [String: Any] = ["up": false]
if let i = CWWiFiClient.shared().interface() {
    o["ifname"] = i.interfaceName ?? ""
    o["up"] = i.powerOn() && i.rssiValue() != 0
    o["rssi"] = i.rssiValue()
    o["noise"] = i.noiseMeasurement()
    o["tx_rate"] = i.transmitRate()
    if let c = i.wlanChannel() {
        let band = c.channelBand == .band5GHz ? "5GHz" : c.channelBand == .band6GHz ? "6GHz" : "2.4GHz"
        o["channel"] = "\(c.channelNumber) (\(band))"
    }
}
print(String(data: try! JSONSerialization.data(withJSONObject: o), encoding: .utf8)!)
'''


# =========================================================================== probes
def sh(cmd, timeout):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError):
        return None


def net_identity(iface, gw):
    """(DHCP domain, gateway MAC) of the current network; both readable without any permission."""
    dom = mac = ""
    if iface:
        r = sh(["/usr/sbin/ipconfig", "getoption", iface, "domain_name"], 3)
        dom = r.stdout.strip().lower() if r and r.returncode == 0 else ""
    if gw:
        r = sh(["/usr/sbin/arp", "-n", gw], 3)
        m = re.search(r" at ([0-9a-f:]+) ", r.stdout if r else "")
        if m and m.group(1).count(":") == 5:
            mac = ":".join(x.zfill(2) for x in m.group(1).split(":"))
    return dom, mac


def is_building_net(domain, gw_mac):
    return bool(domain and any(domain == x or domain.endswith("." + x) for x in BUILDING_DOMAINS)) or gw_mac in BUILDING_GW_MACS


CURRENT_NET = {"domain": "", "gw_mac": "", "building": None}


def default_route():
    r = sh(["/sbin/route", "-n", "get", "default"], 3)
    if not r or r.returncode != 0:
        return "", ""
    gw = re.search(r"gateway:\s*(\S+)", r.stdout)
    ifc = re.search(r"interface:\s*(\S+)", r.stdout)
    return (ifc.group(1) if ifc else ""), (gw.group(1) if gw else "")


def ping(host, wait_ms=1500):
    if not host:
        return ""
    r = sh(["/sbin/ping", "-c", "1", "-W", str(wait_ms), "-q", host], wait_ms / 1000 + 2)
    if r and r.returncode == 0:
        m = re.search(r"= [\d.]+/([\d.]+)/", r.stdout)
        if m:
            return f"{float(m.group(1)):.1f}"
    return ""


def router_probe(gw):
    """Router reachability even when it ignores ping: TTL-1 probe gets 'time exceeded' from hop 1."""
    if not gw:
        return ""
    r = sh(["/usr/sbin/traceroute", "-n", "-q", "1", "-w", "1", "-m", "1", "1.1.1.1"], 4)
    if r:
        m = re.search(r"^\s*1\s+(\S+)\s+([\d.]+) ms", r.stdout, re.M)
        if m:
            return f"{float(m.group(2)):.1f}"
    return ping(gw, 1000)


def dns(server=None, tcp=False):
    """Lookup via the system resolver, or a given server (UDP unless tcp=True)."""
    cmd = ["/usr/bin/dig", "+time=2", "+tries=1"] + (["+tcp"] if tcp else []) + ([f"@{server}"] if server else [])
    r = sh(cmd + [DNS_NAME, "A"], 5)
    if r and r.returncode == 0:
        st = re.search(r"status: (\w+)", r.stdout)
        qt = re.search(r"Query time: (\d+)", r.stdout)
        if st and st.group(1) in ("NOERROR", "NXDOMAIN"):
            return qt.group(1) if qt else "0"
    return ""


def http():
    r = sh(["/usr/bin/curl", "-s", "-o", "/dev/null", "-m", "4",
            "-w", "%{http_code} %{time_total}", HTTP_URL], 6)
    if r and r.stdout:
        code, _, t = r.stdout.strip().partition(" ")
        if code in ("204", "200"):
            return f"{float(t) * 1000:.0f}"
    return ""


def discover_next_hop():
    """First hop beyond the router that reliably answers ping (modem, ISP edge, or CGNAT)."""
    r = sh(["/usr/sbin/traceroute", "-n", "-q", "1", "-w", "1", "-m", "6", "1.1.1.1"], 15)
    if not r:
        return ""
    hops = re.findall(r"^\s*(\d+)\s+(\d+\.\d+\.\d+\.\d+)", r.stdout, re.M)
    for n, ip in hops:
        if int(n) < 2 or ip in ("1.1.1.1",):
            continue
        rr = sh(["/sbin/ping", "-c", "3", "-i", "0.2", "-W", "800", "-q", ip], 6)
        if rr and re.search(r"([23]) packets received", rr.stdout):
            return ip
    return ""


_whois = {}


def whois_name(ip):
    if ip not in _whois:
        r = sh(["/usr/bin/whois", ip], 15)
        names = re.findall(r"^(?:netname|NetName|org-name|OrgName):\s*(.+)$", r.stdout if r else "", re.M)
        _whois[ip] = (names[-1].strip() if names else "")[:40]
    return _whois[ip]


def discover_egress():
    """Where web traffic and DNS actually leave for the internet. A security tunnel
    (e.g. Netskope) shows up here as a third-party network instead of your ISP."""
    r = sh(["/usr/bin/curl", "-s", "-m", "5", "https://api.ipify.org"], 7)
    ip = r.stdout.strip() if r and re.fullmatch(r"[\d.]+", r.stdout.strip() or "x") else ""
    return f"{ip} {whois_name(ip)}".strip() if ip else ""


ENV_FIELDS = ["ts", "gateway", "prefix", "lan_devices", "my_channel", "nets_total", "nets_same_block", "channels"]
BLOCKS_5G = [(36, 48), (52, 64), (100, 112), (116, 128), (132, 144), (149, 161), (165, 177)]


def chan_block(ch, band):
    """Channels that can interfere with each other: same 80 MHz block on 5 GHz, within 4 channels on 2.4 GHz."""
    if band.startswith("2"):
        return ("2.4", ch)
    return ("5", next((i for i, (a, b) in enumerate(BLOCKS_5G) if a <= ch <= b), ch))


def same_block(a, b):
    if a[0] != b[0]:
        return False
    return abs(a[1] - b[1]) < 5 if a[0] == "2.4" else a[1] == b[1]


def env_snapshot(iface, gw):
    """Building-network evidence: how big the shared LAN is and how crowded our Wi-Fi channel is."""
    row = {"ts": now_iso(), "gateway": gw}
    r = sh(["/usr/sbin/ipconfig", "getoption", iface or "en0", "subnet_mask"], 3)
    if r and r.stdout.strip():
        row["prefix"] = sum(bin(int(x)).count("1") for x in r.stdout.strip().split("."))
    r = sh(["/usr/sbin/arp", "-an", "-i", iface or "en0"], 5)
    if r:
        row["lan_devices"] = sum(1 for l in r.stdout.splitlines() if "incomplete" not in l and " at " in l)
    r = sh(["/usr/sbin/system_profiler", "SPAirPortDataType"], 25)
    if r and "Current Network Information:" in r.stdout:
        cur = r.stdout.split("Current Network Information:", 1)[1]
        m = re.search(r"Channel: (\d+) \((\w+)", cur)
        others = cur.split("Other Local Wi-Fi Networks:", 1)[1] if "Other Local Wi-Fi Networks:" in cur else ""
        others = others.split("awdl0:", 1)[0]
        chans = [(int(c), b) for c, b in re.findall(r"Channel: (\d+) \((\w+)", others)]
        row["nets_total"] = len(chans)
        row["channels"] = ";".join(f"{c}/{b}" for c, b in chans)
        if m:
            mine = (int(m.group(1)), m.group(2))
            row["my_channel"] = f"{mine[0]}/{mine[1]}"
            row["nets_same_block"] = sum(1 for c in chans if same_block(chan_block(*mine), chan_block(*c)))
    return row


class WifiReader:
    """Fast per-tick Wi-Fi stats via the compiled CoreWLAN helper; falls back to system_profiler."""

    def __init__(self, data_dir):
        self.bin = data_dir / "wifistat"
        self.cached, self.lock = {}, threading.Lock()
        if not self.bin.exists():
            threading.Thread(target=self._profiler_loop, daemon=True).start()

    def read(self):
        if self.bin.exists():
            r = sh([str(self.bin)], 4)
            if r and r.returncode == 0:
                try:
                    return json.loads(r.stdout)
                except ValueError:
                    pass
        with self.lock:
            return dict(self.cached)

    def _profiler_loop(self):
        while True:
            r = sh(["/usr/sbin/system_profiler", "SPAirPortDataType"], 20)
            info = {}
            if r and "Current Network Information:" in r.stdout:
                b = r.stdout.split("Current Network Information:", 1)[1][:1500]
                sn = re.search(r"Signal / Noise: (-?\d+) dBm / (-?\d+) dBm", b)
                ch = re.search(r"Channel: (\d+) \((\w+)", b)
                tx = re.search(r"Transmit Rate: (\d+)", b)
                ifn = re.search(r"^\s+(en\d+):", r.stdout, re.M)
                info = {"up": bool(sn), "ifname": ifn.group(1) if ifn else "en0"}
                if sn:
                    info.update(rssi=int(sn.group(1)), noise=int(sn.group(2)))
                if ch:
                    info["channel"] = f"{ch.group(1)} ({ch.group(2)})"
                if tx:
                    info["tx_rate"] = int(tx.group(1))
            elif r:
                info = {"up": False}
            with self.lock:
                self.cached = info
            time.sleep(60)


def build_helper(data_dir, quiet=False):
    """Compile the CoreWLAN helper into the data dir (needs Xcode command line tools)."""
    out = data_dir / "wifistat"
    if out.exists():
        return True
    swiftc = shutil.which("swiftc")
    if not swiftc:
        if not quiet:
            print("swiftc not found - Wi-Fi stats fall back to system_profiler every 60s")
        return False
    src = data_dir / "wifistat.swift"
    src.write_text(WIFI_SWIFT)
    r = sh([swiftc, "-O", str(src), "-o", str(out)], 300)
    if not r or r.returncode != 0:
        if not quiet:
            print("Could not compile Wi-Fi helper; falling back to system_profiler.",
                  (r.stderr[-300:] if r else ""))
        return False
    return True


# =========================================================================== logging
def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def open_log(d):
    """Append to probes.csv; if it has an older column layout, rotate it aside first."""
    path = d / "probes.csv"
    if path.exists() and path.stat().st_size:
        with open(path) as f:
            header = f.readline().strip().split(",")
        if header != PROBE_FIELDS:
            path.rename(d / f"probes-{datetime.now():%Y%m%d-%H%M%S}.csv")
    new = not path.exists() or path.stat().st_size == 0
    f = open(path, "a", newline="")
    w = csv.DictWriter(f, fieldnames=PROBE_FIELDS)
    if new:
        w.writeheader()
        f.flush()
    return f, w


def run(args):
    d = args.data_dir
    d.mkdir(parents=True, exist_ok=True)
    if sys.stdin.isatty():
        build_helper(d)
    f, w = open_log(d)
    wifi = WifiReader(d)
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    if args.port:
        start_server(d, args.port)
    threading.Thread(target=share_loop, args=(d, stop), daemon=True).start()
    threading.Thread(target=update_loop, args=(d, stop), daemon=True).start()

    state = {"gw": None, "hop": "", "hop_at": 0, "egress": ""}
    hop_lock = threading.Lock()

    env_path = d / "env.csv"

    def refresh_hop():
        ip, eg = discover_next_hop(), discover_egress()
        try:
            iface_, gw_ = default_route()
            row = env_snapshot(iface_, gw_)
            new = not env_path.exists()
            with open(env_path, "a", newline="") as ef:
                ew = csv.DictWriter(ef, fieldnames=ENV_FIELDS)
                if new:
                    ew.writeheader()
                ew.writerow({k: row.get(k, "") for k in ENV_FIELDS})
        except Exception as e:  # evidence is best-effort; never stop probing
            print("netmon: env snapshot failed:", e, flush=True)
        with hop_lock:
            state["hop"], state["hop_at"], state["egress"] = ip, time.time(), eg

    print(f"netmon: logging every {args.interval}s to {d}", flush=True)
    last = None
    try:
        while not stop.is_set():
            start, wall = time.monotonic(), time.time()
            if last and wall - last > max(3 * args.interval, 45):
                w.writerow({"ts": now_iso(), "note": f"sleep_gap {wall - last:.0f}s"})
            last = wall

            iface, gw = default_route()
            with hop_lock:
                changed = gw != state["gw"]
                if changed or time.time() - state["hop_at"] > 600:
                    if changed:
                        state["hop"] = ""
                    state["gw"], state["hop_at"] = gw, time.time()
                    threading.Thread(target=refresh_hop, daemon=True).start()
                hop, egress = state["hop"], state["egress"]

            res = {}
            jobs = {"router_ms": lambda: router_probe(gw), "hop_ms": lambda: ping(hop),
                    "cf_ms": lambda: ping("1.1.1.1"), "goog_ms": lambda: ping("8.8.8.8"),
                    "dns_ms": dns, "dns_pub_ms": lambda: dns("1.1.1.1"), "dns_tcp_ms": lambda: dns("1.1.1.1", True),
                    "http_ms": http, "_wifi": wifi.read, "_net": lambda: net_identity(iface, gw)}
            ths = [threading.Thread(target=lambda k=k, fn=fn: res.__setitem__(k, fn())) for k, fn in jobs.items()]
            for t in ths:
                t.start()
            for t in ths:
                t.join(8)
            wi = res.pop("_wifi", None) or {}
            dom, mac = res.pop("_net", None) or ("", "")
            CURRENT_NET.update(domain=dom, gw_mac=mac, building=is_building_net(dom, mac) if gw else None)
            row = {"ts": now_iso(), "iface": iface, "gateway": gw, "hop_ip": hop, "egress": egress, **res,
                   "lan_domain": dom, "gw_mac": mac,
                   "wifi_if": wi.get("ifname", ""),
                   "wifi_up": "" if "up" not in wi else int(bool(wi["up"])),
                   "rssi": wi.get("rssi", "") if wi.get("up") else "",
                   "noise": wi.get("noise", "") if wi.get("up") else "",
                   "tx_rate": wi.get("tx_rate", "") if wi.get("up") else "",
                   "channel": wi.get("channel", "") if wi.get("up") else "",
                   "note": "" if gw else "no_route"}
            if time.time() - wall > max(3 * args.interval, 45):
                continue  # the Mac slept during this check; the next round logs the sleep gap
            w.writerow(row)
            f.flush()
            if args.verbose:
                print(" ".join(f"{k}={row.get(k, '')}" for k in PROBE_FIELDS if k != "note"), flush=True)
            stop.wait(max(0.5, args.interval - (time.monotonic() - start)))
    except KeyboardInterrupt:
        pass
    print("netmon: stopped")


# =========================================================================== analysis
def fnum(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def load_ticks(d):
    files = sorted(d.glob("probes-*.csv")) + [d / "probes.csv"]
    legacy_wifi = []
    if (d / "wifi.csv").exists():
        with open(d / "wifi.csv", newline="") as f:
            for w in csv.DictReader(f):
                try:
                    legacy_wifi.append((datetime.fromisoformat(w["ts"]), fnum(w["rssi"]), fnum(w["noise"]),
                                        fnum(w.get("tx_rate")), w.get("channel", "")))
                except (TypeError, ValueError, KeyError):
                    pass
    lw_ts = [w[0] for w in legacy_wifi]
    ticks = []
    for p in files:
        if not p.exists():
            continue
        with open(p, newline="") as f:
            for r in csv.DictReader(f):
                if r.get("note") is None or not r.get("ts"):
                    continue  # half-written line
                try:
                    ts = datetime.fromisoformat(r["ts"])
                except ValueError:
                    continue
                if r["note"].startswith("sleep_gap"):
                    ticks.append({"ts": ts, "gap": True})
                    continue
                t = {"ts": ts, "gap": False, "iface": r.get("iface") or "", "gateway": r.get("gateway") or "",
                     "wifi_if": r.get("wifi_if") or "", "channel": r.get("channel") or "",
                     "wifi_up": {"1": True, "0": False}.get(r.get("wifi_up") or ""),
                     "router": fnum(r.get("router_ms") if "router_ms" in r else r.get("gw_ms")),
                     "hop_ip": r.get("hop_ip") or "", "egress": r.get("egress") or "",
                     "building": is_building_net((r.get("lan_domain") or "").lower(), r.get("gw_mac") or "")}
                t["tunnel"] = bool(TUNNEL_RE.search(t["egress"]))
                t["v3"] = r.get("dns_pub_ms") is not None  # row has the public-DNS probes
                t["v2"] = r.get("router_ms") is not None   # router probed with the TTL-1 method (not just ping)
                for k, src in (("rssi", "rssi"), ("noise", "noise"), ("tx", "tx_rate"), ("hop", "hop_ms"),
                               ("cf", "cf_ms"), ("goog", "goog_ms"), ("dns", "dns_ms"), ("dns_pub", "dns_pub_ms"),
                               ("dns_tcp", "dns_tcp_ms"), ("http", "http_ms")):
                    t[k] = fnum(r.get(src))
                if t["rssi"] is None and legacy_wifi:
                    i = bisect.bisect_left(lw_ts, ts)
                    near = [legacy_wifi[j] for j in (i - 1, i) if 0 <= j < len(legacy_wifi)]
                    near = [w for w in near if abs((w[0] - ts).total_seconds()) <= 90]
                    if near:
                        w = min(near, key=lambda w: abs((w[0] - ts).total_seconds()))
                        t.update(rssi=w[1], noise=w[2], tx=w[3], channel=w[4], wifi_up=True)
                t["net"] = min([x for x in (t["cf"], t["goog"]) if x is not None], default=None)
                ticks.append(t)
    ticks.sort(key=lambda t: t["ts"])
    return drop_sleep_artifacts(ticks)


WAKE_SETTLE = 45  # seconds after a wake while Wi-Fi, DNS and tunnels reconnect


def drop_sleep_artifacts(ticks):
    """With the lid closed, macOS wakes briefly every 15-30 min (Power Nap / dark wake). The check
    that was running when the Mac fell asleep finishes after the wake, and the first checks after a
    wake fail while Wi-Fi reconnects. Neither says anything about the network, so both are dropped."""
    out, wake = [], None
    for i, t in enumerate(ticks):
        if t["gap"]:
            wake = t["ts"]
            out.append(t)
            continue
        if i + 1 < len(ticks) and ticks[i + 1]["gap"]:
            continue  # started before sleep, finished after the wake
        if wake and (t["ts"] - wake).total_seconds() < WAKE_SETTLE:
            continue
        if any((t[k] or 0) > 60000 for k in ("router", "hop", "cf", "goog", "dns", "http")):
            continue  # a probe "took" minutes: the Mac slept during it
        out.append(t)
    return out


LAYERS = {
    "wifi_link": ("Wi-Fi link dropped", "Mac lost its Wi-Fi association or roamed to another access point"),
    "wifi_weak": ("Wi-Fi: weak signal", f"Nothing past the Mac answered while signal was below {WEAK_RSSI} dBm"),
    "local": ("Local: signal OK, router unreachable", "Signal was fine but the router didn't answer: congestion, access point or router"),
    "local_or_isp": ("Local or ISP (router never answers)", "This network's router can't be probed, so local vs ISP can't be split"),
    "next_hop": ("Router uplink / ISP access", "Router answered, but the next hop (modem / ISP edge) and beyond did not"),
    "upstream": ("ISP / internet beyond the access line", "Router and next hop answered, the wider internet did not"),
    "dns": ("DNS (your resolver)", "Internet reachable, but the DNS server your Mac uses (normally the router) didn't answer"),
    "http_tunnel": ("Web via security tunnel", "Everything else fine; HTTPS through the security tunnel (e.g. Netskope) failed"),
    "http": ("Web (HTTPS) only", "Pings and DNS fine, HTTPS fetch failed"),
}
# what the building hub gets: which rung broke, in six plain groups
FAIL_GROUPS = {"wifi": "Wi-Fi", "router": "Router / building Wi-Fi", "uplink": "Building uplink",
               "internet": "Internet", "dns": "DNS", "web": "Web only"}
FAIL_GROUP = {"wifi_link": "wifi", "wifi_weak": "wifi", "local": "router", "local_or_isp": "router",
              "next_hop": "uplink", "upstream": "internet", "dns": "dns", "http": "web", "http_tunnel": "web"}
LOCAL_LAYERS = {"wifi_link", "wifi_weak", "local"}
ISP_LAYERS = {"next_hop", "upstream"}


def classify(t, prev, router_known):
    if t["http"] is not None:
        return "ok"
    on_wifi = not t["iface"] or (t["wifi_if"] and t["iface"] == t["wifi_if"])
    if on_wifi and t["wifi_up"] is False:
        return "wifi_link"
    if not t["gateway"]:
        return "wifi_link" if on_wifi else "local"
    net_ok, hop_ok = t["net"] is not None, t["hop"] is not None
    if t["router"] is None and not net_ok and not hop_ok and t["dns"] is None:
        if not router_known:
            return "local_or_isp"
        if on_wifi and prev and prev.get("channel") and t["channel"] and prev["channel"] != t["channel"]:
            return "wifi_link"
        if on_wifi and t["rssi"] is not None and t["rssi"] < WEAK_RSSI:
            return "wifi_weak"
        return "local"
    if t["hop_ip"] and not hop_ok and not net_ok:
        return "next_hop"
    if not net_ok:
        return "upstream"
    if t["dns"] is None:
        return "dns"
    return "http_tunnel" if t["tunnel"] else "http"


def is_slow(t):
    return any(t[k] is not None and t[k] > lim for k, lim in SLOW.items())


def pct(vals, p):
    vals = sorted(v for v in vals if v is not None)
    if not vals:
        return None
    return vals[min(len(vals) - 1, int(round(p / 100 * (len(vals) - 1))))]


def fmt_dur(s):
    s = int(round(s))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60:02d}s"
    return f"{s // 3600}h {(s % 3600) // 60:02d}m"


def r1(v):
    return None if v is None else round(v, 1)


_cache = {}


def analyze(d, hours=None, gateway=None, min_ticks=2):
    files = sorted(d.glob("probes*.csv"))
    key = (tuple((p.name, p.stat().st_mtime) for p in files), hours, gateway, min_ticks)
    if key in _cache:
        return _cache[key]
    all_ticks = load_ticks(d)
    real_all = [t for t in all_ticks if not t["gap"]]
    nets = Counter(t["gateway"] for t in real_all if t["gateway"])
    last_seen = {}
    for t in real_all:
        if t["gateway"]:
            last_seen[t["gateway"]] = t["ts"]
    ticks = all_ticks
    if hours:
        cutoff = datetime.now().astimezone() - timedelta(hours=hours)
        ticks = [t for t in ticks if t["ts"] >= cutoff]
    if gateway:
        # keep sleep gaps and no-route ticks that sit between this network's ticks
        ticks = [t for t in ticks if t["gap"] or t["gateway"] in (gateway, "")]
    real = [t for t in ticks if not t["gap"]]
    if gateway:
        while real and real[0]["gateway"] == "":
            real.pop(0)
    networks = [{"gw": g, "ticks": n, "last": last_seen[g].strftime("%d %b %H:%M")}
                for g, n in sorted(nets.items(), key=lambda kv: last_seen[kv[0]], reverse=True)]
    if not real:
        out = {"empty": True, "networks": networks}
        _cache.clear(); _cache[key] = out
        return out

    sp = sorted((b["ts"] - a["ts"]).total_seconds() for a, b in zip(real, real[1:]))
    interval = max(1, sp[len(sp) // 2]) if sp else 10
    with_route = [t for t in real if t["gateway"]]
    seen, answered = Counter(), Counter()
    for t in with_route:
        seen[t["gateway"]] += 1
        answered[t["gateway"]] += t["router"] is not None
    known = {g: answered[g] / seen[g] > 0.2 for g in seen}
    router_known = all(known.values()) if known else True
    prev = None
    for i, t in enumerate(real):
        t["i"] = i
        t["state"] = classify(t, prev, known.get(t["gateway"] or (prev or {}).get("gateway", ""), True))
        prev = t

    # ---- events
    events, cur = [], []

    def flush():
        if not cur:
            return
        states = Counter(t["state"] for t in cur)
        layer = "wifi_link" if "wifi_link" in states else states.most_common(1)[0][0]
        rs = [t["rssi"] for t in cur if t["rssi"] is not None]
        start, end = cur[0]["ts"], cur[-1]["ts"] + timedelta(seconds=interval)
        before = next((t for t in reversed(real[max(0, cur[0]["i"] - 6):cur[0]["i"]]) if t["rssi"] is not None or t["gateway"]), None)
        events.append({"start": start, "end": end, "dur": (end - start).total_seconds(), "ticks": len(cur),
                       "layer": layer, "rssi": min(rs) if rs else (before["rssi"] if before else None),
                       "tx": cur[0]["tx"], "channel": cur[0]["channel"] or (before["channel"] if before else ""),
                       "gateway": cur[0]["gateway"] or (before["gateway"] if before and before["gateway"] else "none")})
        cur.clear()

    for t in ticks:
        if t["gap"] or t.get("state", "ok") == "ok":
            flush()
            continue
        if cur and (t["ts"] - cur[-1]["ts"]).total_seconds() > 3 * interval:
            flush()
        cur.append(t)
    flush()
    outages = [e for e in events if e["ticks"] >= min_ticks]
    blips = [e for e in events if e["ticks"] < min_ticks]

    first, last = real[0]["ts"], real[-1]["ts"]
    span = max(1, (last - first).total_seconds())
    monitored = len(real) * interval
    down = sum(e["dur"] for e in outages)
    bad = sum(t["state"] != "ok" for t in real)

    # ---- time series
    bucket = next((b for b in (10, 30, 60, 120, 300, 600, 1800, 3600) if b >= max(interval, span / 300)), 3600)
    groups = defaultdict(list)
    for t in real:
        k = int(t["ts"].timestamp() // bucket * bucket)
        groups[k].append(t)
    series = []
    for k in sorted(groups):
        g = groups[k]
        series.append({"t": datetime.fromtimestamp(k).astimezone().isoformat(),
                       "router": r1(pct([t["router"] for t in g], 95)), "hop": r1(pct([t["hop"] for t in g], 95)),
                       "net": r1(pct([t["net"] for t in g], 95)),
                       "rssi": pct([t["rssi"] for t in g], 50), "rssi_lo": pct([t["rssi"] for t in g], 0),
                       "tx": pct([t["tx"] for t in g], 50),
                       "bad": round(100 * sum(t["state"] != "ok" for t in g) / len(g), 1)})

    # ---- heatmap: share of checks per hour that failed or were slow (catches short hiccups too)
    slow = is_slow
    heat, cover = defaultdict(float), defaultdict(float)
    cells = defaultdict(lambda: [0, 0, 0, 0, 0])  # checks, failed, slow, slow at router, slow beyond router
    for t in real:
        k = (t["ts"].strftime("%Y-%m-%d"), t["ts"].hour)
        cover[k] += interval / 60
        c = cells[k]
        c[0] += 1
        if t["state"] != "ok":
            c[1] += 1
        elif slow(t):
            c[2] += 1
        if t["router"] is not None and t["router"] > SLOW["router"]:
            c[3] += 1
        elif t["hop"] is not None and t["router"] is not None and t["hop"] - t["router"] > SLOW["router"]:
            c[4] += 1
    for e in outages:
        c = e["start"]
        while c < e["end"]:
            nxt = min(e["end"], c.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1))
            heat[(c.strftime("%Y-%m-%d"), c.hour)] += (nxt - c).total_seconds() / 60
            c = nxt
    days = sorted({k[0] for k in cover})
    heatmap = []
    for dd in days:
        for h in range(24):
            c = cells.get((dd, h))
            if c:
                heatmap.append({"day": dd, "hour": h, "down": round(heat.get((dd, h), 0), 2), "cover": round(cover[(dd, h)], 1),
                                "checks": c[0], "failed": c[1], "slow": c[2], "rate": round(100 * (c[1] + c[2]) / c[0], 1)})
    by_hour = []
    for h in range(24):
        cs = [cells[(dd, h)] for dd in days if (dd, h) in cells]
        n = sum(c[0] for c in cs)
        by_hour.append({"rate": round(100 * sum(c[1] + c[2] for c in cs) / n, 2) if n else 0,
                        "failed": sum(c[1] for c in cs), "slow": sum(c[2] for c in cs), "checks": n,
                        "slow_local": sum(c[3] for c in cs), "slow_uplink": sum(c[4] for c in cs),
                        "down": round(sum(heat.get((dd, h), 0) for dd in days), 1)})

    # ---- layer breakdown + latency budget
    lc, ld = Counter(e["layer"] for e in outages), defaultdict(float)
    for e in outages:
        ld[e["layer"]] += e["dur"]
    layers = [{"key": k, "label": LAYERS[k][0], "desc": LAYERS[k][1], "count": lc[k], "dur": fmt_dur(ld[k]),
               "share": round(100 * ld[k] / down, 1) if down else 0} for k in sorted(lc, key=lambda k: -ld[k])]
    ok = [t for t in real if t["state"] == "ok"]
    budget = [{"name": n, "p50": r1(pct([t[k] for t in ok], 50)), "p95": r1(pct([t[k] for t in ok], 95)),
               "loss": round(100 * sum(t[k] is None for t in with_route if (k != "hop" or t["hop_ip"]) and (k != "router" or t["v2"]))
                             / max(1, sum(1 for t in with_route if (k != "hop" or t["hop_ip"]) and (k != "router" or t["v2"]))), 1)}
              for n, k in (("Router (Wi-Fi + LAN)", "router"), ("Next hop (modem / ISP edge)", "hop"),
                           ("Internet (1.1.1.1 / 8.8.8.8)", "net"),
                           ("Web page (HTTPS" + (", via security tunnel)" if any(t["tunnel"] for t in real) else ")"), "http"))
              if any(t[k] is not None for t in real)]
    rssis = [t["rssi"] for t in real if t["rssi"] is not None]
    tun = [t for t in real if t["tunnel"]]
    tunnel = None
    if tun:
        probed = [t for t in tun if t["v3"] and t["net"] is not None]
        tunnel = {"egress": Counter(t["egress"] for t in tun).most_common(1)[0][0], "share": round(100 * len(tun) / max(1, sum(1 for t in real if t["v3"]))),
                  "udp_fail": round(100 * sum(t["dns_pub"] is None for t in probed) / max(1, len(probed)), 1),
                  "tcp_fail": round(100 * sum(t["dns_tcp"] is None for t in probed) / max(1, len(probed)), 1),
                  "web_p50": r1(pct([t["http"] for t in tun], 50)), "web_p95": r1(pct([t["http"] for t in tun], 95)),
                  "web_slow": sum(1 for t in tun if t["http"] is not None and t["http"] > SLOW["http"]),
                  "web_fail": sum(1 for t in tun if t["http"] is None and t["net"] is not None), "checks": len(tun)}
    lag = sum(1 for t in real if t["router"] is not None and t["router"] > 200)

    # ---- verdict
    verdict = []
    local_s = sum(ld[k] for k in LOCAL_LAYERS)
    isp_s = sum(ld[k] for k in ISP_LAYERS)
    rmed = pct(rssis, 50)
    if outages:
        if local_s >= 0.6 * down:
            verdict.append(f"{100 * local_s / down:.0f}% of downtime is on your side of the router (Wi-Fi or LAN).")
        elif isp_s >= 0.6 * down:
            verdict.append(f"{100 * isp_s / down:.0f}% of downtime is beyond your router: the router answered while the line out of it did not. "
                           "This is the pattern to show your ISP.")
        elif down - local_s - isp_s >= 0.6 * down:
            verdict.append(f"{100 * (down - local_s - isp_s) / down:.0f}% of downtime was DNS or web only: the network itself was up, "
                           "but name lookups or web pages failed.")
        else:
            verdict.append(f"Downtime is split: {100 * local_s / down:.0f}% local (Wi-Fi/LAN), {100 * isp_s / down:.0f}% beyond the router, "
                           f"{100 * (down - local_s - isp_s) / down:.0f}% DNS or web only.")
        if ld["wifi_weak"] + ld["wifi_link"] > 0.3 * down:
            verdict.append("Many outages coincide with a dropped Wi-Fi link or weak signal, so the radio link itself is a prime suspect.")
        if ld["local"] > 0.3 * down:
            verdict.append("Some local outages happened with decent signal: that points at congestion, the access point or the router rather than distance.")
    else:
        verdict.append("No outages in this window.")
    if rmed is not None and rmed < -70:
        word = "weak" if rmed < WEAK_RSSI else "marginal"
        verdict.append(f"Wi-Fi signal is {word} (median {rmed:.0f} dBm; good is above −67). Expect slowdowns and drops at this level.")
    rb = next((b for b in budget if b["name"].startswith("Router")), None)
    nb = next((b for b in budget if b["name"].startswith("Internet")), None)
    if rb and nb and rb["p95"] and nb["p95"] and nb["p95"] > 80 and rb["p95"] >= 0.5 * nb["p95"]:
        verdict.append(f"Latency spikes start at the first hop (router p95 {rb['p95']:.0f} ms of {nb['p95']:.0f} ms total), so the delay is added on Wi-Fi/LAN, not by the ISP.")
    busy = sorted(range(24), key=lambda h: -by_hour[h]["rate"])[:3]
    tot = sum(b["checks"] for b in by_hour)
    avg = 100 * sum(b["failed"] + b["slow"] for b in by_hour) / max(1, tot)
    hot = [h for h in busy if by_hour[h]["checks"] >= 30 and by_hour[h]["rate"] >= max(2 * avg, 1)]
    if hot:
        verdict.append("Problems cluster at " + ", ".join(f"{h:02d}:00–{h:02d}:59 ({by_hour[h]['rate']:.1f}% of checks failed or slow)" for h in sorted(hot))
                       + f" vs {avg:.1f}% overall. A daily peak like this usually means shared capacity running out at busy times.")
    sl, su = sum(b["slow_local"] for b in by_hour), sum(b["slow_uplink"] for b in by_hour)
    if sl + su >= 10:
        if sl >= 2 * su:
            verdict.append(f"Slowdowns start at the first hop ({sl} slow router replies vs {su} slow uplink): congestion is on the Wi-Fi/LAN side.")
        elif su >= 2 * sl:
            verdict.append(f"Slowdowns are added between the router and the next hop ({su} vs {sl} at the router): the building's internet uplink is saturating.")
        else:
            verdict.append(f"Slowdowns appear both at the router ({sl}) and on the uplink ({su}).")
    if tunnel and (tunnel["udp_fail"] > 3 or tunnel["web_slow"]):
        verdict.append(f"Your traffic also passes a security tunnel ({tunnel['egress']}): public DNS over UDP failed {tunnel['udp_fail']}% "
                       f"(TCP {tunnel['tcp_fail']}%), {tunnel['web_slow']} web checks took > 2 s. That part is separate from the building network.")
    if not router_known:
        verdict.append("This network's router doesn't answer probes, so local vs ISP can't be separated here.")

    # ---- building evidence: evening vs rest of day, LAN size, channel crowding
    env = []
    if (d / "env.csv").exists():
        with open(d / "env.csv", newline="") as ef:
            for e in csv.DictReader(ef):
                try:
                    ts = datetime.fromisoformat(e["ts"])
                except (TypeError, ValueError):
                    continue
                if (not hours or ts >= datetime.now().astimezone() - timedelta(hours=hours)) and (not gateway or e["gateway"] == gateway):
                    env.append({"ts": ts, "gw": e["gateway"], "prefix": fnum(e["prefix"]), "lan": fnum(e["lan_devices"]),
                                "mine": e["my_channel"], "same": fnum(e["nets_same_block"]), "total": fnum(e["nets_total"]),
                                "channels": e["channels"]})
    EVE = range(18, 23)

    def part(ts_list, evening):
        return [t for t in ts_list if (t["ts"].hour in EVE) == evening]

    def summary(ts_list, en):
        wr = [t for t in ts_list if t["gateway"] and t["v2"]]
        return {"checks": len(ts_list),
                "problem": round(100 * sum(t["state"] != "ok" or slow(t) for t in ts_list) / len(ts_list), 1) if ts_list else None,
                "router_p50": r1(pct([t["router"] for t in ts_list], 50)), "router_p95": r1(pct([t["router"] for t in ts_list], 95)),
                "router_loss": round(100 * sum(t["router"] is None for t in wr) / len(wr), 1) if wr else None,
                "hop_p95": r1(pct([t["hop"] for t in ts_list], 95)), "net_p95": r1(pct([t["net"] for t in ts_list], 95)),
                "rssi": pct([t["rssi"] for t in ts_list], 50),
                "same": r1(sum(e["same"] for e in en if e["same"] is not None) / max(1, sum(e["same"] is not None for e in en))) if en else None}
    ev_t, ot_t = part(real, True), part(real, False)
    latest = env[-1] if env else None
    evidence = {"evening": summary(ev_t, part(env, True)), "other": summary(ot_t, part(env, False)),
                "lan_max": max((e["lan"] for e in env if e["lan"] is not None), default=None),
                "latest": {k: latest[k] for k in ("prefix", "lan", "mine", "same", "total", "channels")} if latest else None,
                "snapshots": len(env), "dns_intercepted": any(t["tunnel"] for t in real)}
    if latest and latest["lan"] and latest["lan"] >= 20:
        verdict.append(f"Your Mac can see {int(evidence['lan_max'])} devices on its local network ({'/' + str(int(latest['prefix'])) if latest['prefix'] else ''} subnet). "
                       "That's far more than one apartment, so apartments share one network: everyone's broadcast traffic reaches every Wi-Fi, and one central router serves the whole building.")
    if latest and latest["same"]:
        verdict.append(f"{int(latest['same'])} other Wi-Fi networks use the same 5 GHz channel block as yours ({latest['mine']}); they have to take turns on air.")

    # ---- live strip (last 90 ticks)
    strip = []
    for t in real[-90:]:
        on_wifi = not t["iface"] or (t["wifi_if"] and t["iface"] == t["wifi_if"])
        wifi_s = "na" if not on_wifi or t["wifi_up"] is None else ("fail" if not t["wifi_up"] else
                                                                     ("weak" if (t["rssi"] or 0) < WEAK_RSSI else "ok"))
        strip.append({"t": t["ts"].strftime("%H:%M:%S"), "state": t["state"], "wifi": wifi_s, "rssi": t["rssi"],
                      "tx": t["tx"], "router": t["router"], "hop": t["hop"], "hop_ip": t["hop_ip"], "net": t["net"],
                      "dns": t["dns"], "dns_pub": t.get("dns_pub"), "has_pub": t["v3"],
                      "http": t["http"], "gw": t["gateway"], "tunnel": t["tunnel"]})

    out = {
        "empty": False, "networks": networks, "layers": layers, "budget": budget, "verdict": verdict, "tunnel": tunnel, "evidence": evidence,
        "series": series, "bucket": bucket, "heatmap": heatmap, "days": days, "by_hour": by_hour, "strip": strip,
        "events": [{"start": e["start"].strftime("%a %Y-%m-%d %H:%M:%S"), "iso": e["start"].isoformat(),
                    "dur": fmt_dur(e["dur"]), "secs": e["dur"], "layer": LAYERS[e["layer"]][0], "key": e["layer"],
                    "rssi": e["rssi"], "tx": e["tx"], "channel": e["channel"], "gateway": e["gateway"]}
                   for e in sorted(outages, key=lambda e: e["start"], reverse=True)[:500]],
        "stats": {"range": f"{first:%a %d %b %H:%M} → {last:%a %d %b %H:%M}", "monitored": fmt_dur(monitored),
                  "uptime": f"{100 * (1 - bad / len(real)):.2f}%", "outages": len(outages), "downtime": fmt_dur(down),
                  "longest": fmt_dur(max((e["dur"] for e in outages), default=0)), "blips": len(blips),
                  "rssi_med": rmed, "rssi_out": pct([e["rssi"] for e in outages], 50), "lag": lag,
                  "net_p50": r1(pct([t["net"] for t in ok], 50)), "net_p95": r1(pct([t["net"] for t in ok], 95)),
                  "interval": interval, "min_ticks": min_ticks, "router_known": router_known,
                  "sleep_gaps": sum(t["gap"] for t in ticks)},
        "generated": now_iso(),
    }
    _cache.clear()
    _cache[key] = out
    return out


# =========================================================================== building sharing
def load_config(d):
    cfg = {"share": False, "hub_url": "", "label": "", "device_id": "", "asked": False}
    for p in (d / "defaults.json", d / "config.json"):
        try:
            cfg.update(json.loads(p.read_text()))
        except (OSError, ValueError):
            pass
    if not cfg["device_id"]:
        cfg["device_id"] = os.urandom(6).hex()
        save_config(d, cfg)
    return cfg


def hub_of(cfg):
    return cfg.get("hub_url") or DEFAULT_HUB


def save_config(d, cfg):
    keep = {k: cfg.get(k) for k in ("share", "hub_url", "label", "device_id", "asked")}
    (d / "config.json").write_text(json.dumps(keep, indent=2))


def hourly_summary(d, hours=48):
    """Anonymous per-hour aggregates - the only thing ever sent to the building hub.
    No raw checks, no Wi-Fi names, no MAC addresses, no public IPs."""
    cutoff = datetime.now().astimezone() - timedelta(hours=hours)
    ticks = [t for t in load_ticks(d) if not t["gap"] and t["ts"] >= cutoff and t["gateway"] and t["building"]]
    seen, answered = Counter(), Counter()
    for t in ticks:
        seen[t["gateway"]] += 1
        answered[t["gateway"]] += t["router"] is not None
    prev = None
    for t in ticks:
        t["state"] = classify(t, prev, answered[t["gateway"]] / seen[t["gateway"]] > 0.2)
        prev = t
    env = defaultdict(list)
    if (d / "env.csv").exists():
        with open(d / "env.csv", newline="") as f:
            for e in csv.DictReader(f):
                try:
                    ts = datetime.fromisoformat(e["ts"])
                except (TypeError, ValueError):
                    continue
                env[(ts.replace(minute=0, second=0, microsecond=0), e["gateway"])].append(e)
    groups = defaultdict(list)
    for t in ticks:
        groups[(t["ts"].replace(minute=0, second=0, microsecond=0), t["gateway"])].append(t)
    now_hour = datetime.now().astimezone().replace(minute=0, second=0, microsecond=0)
    out = []
    for (h, gw), g in sorted(groups.items()):
        if h >= now_hour or len(g) < 10:  # only complete hours with enough checks
            continue
        en = env.get((h, gw), [])
        wr = [t for t in g if t["v2"]]
        fails = Counter(FAIL_GROUP[t["state"]] for t in g if t["state"] != "ok")
        slots = [0] * 12  # failed checks per 5 minutes, to spot apartments failing at the same moment
        for t in g:
            if t["state"] != "ok":
                slots[t["ts"].minute // 5] += 1
        ok = [t for t in g if t["state"] == "ok"]
        out.append({"hour": h.isoformat(), "epoch": int(h.timestamp()), "local_hour": h.hour, "gw": gw, "checks": len(g),
                    "failed": sum(t["state"] != "ok" for t in g), "slow": sum(t["state"] == "ok" and is_slow(t) for t in g),
                    **{"f_" + k: fails[k] for k in FAIL_GROUPS},
                    "s_router": sum(t["router"] is not None and t["router"] > SLOW["router"] for t in ok),
                    "s_net": sum(t["net"] is not None and t["net"] > SLOW["net"] and not (t["router"] or 0) > SLOW["router"] for t in ok),
                    "s_web": sum(t["http"] > SLOW["http"] and not (t["net"] or 0) > SLOW["net"] and not (t["router"] or 0) > SLOW["router"] for t in ok),
                    "slots": "".join(str(min(9, s)) if s else "_" for s in slots),  # "_" not "0": never parsed as a number
                    "router_p50": r1(pct([t["router"] for t in g], 50)), "router_p95": r1(pct([t["router"] for t in g], 95)),
                    "router_loss": round(100 * sum(t["router"] is None for t in wr) / len(wr), 1) if wr else None,
                    "hop_p95": r1(pct([t["hop"] for t in g], 95)), "net_p95": r1(pct([t["net"] for t in g], 95)),
                    "rssi": pct([t["rssi"] for t in g], 50),
                    "same_block": pct([fnum(e["nets_same_block"]) for e in en], 50),
                    "lan": pct([fnum(e["lan_devices"]) for e in en], 50)})
    return out


SHARE_STATUS = {"last": None, "ok": None, "msg": "not shared yet"}


def curl_json(url, payload=None, timeout=20):
    """GET or POST JSON with curl (system trust store; -L follows Google Apps Script's redirect)."""
    cmd = ["/usr/bin/curl", "-s", "-L", "-m", str(timeout)]
    if payload is not None:
        cmd += ["-H", "Content-Type: application/json", "--data-binary", "@-"]
    try:
        r = subprocess.run(cmd + [url], input=json.dumps(payload) if payload is not None else None,
                           capture_output=True, text=True, timeout=timeout + 5)
    except (subprocess.TimeoutExpired, OSError):
        r = None
    if not r or r.returncode != 0:
        raise RuntimeError("hub unreachable")
    try:
        return json.loads(r.stdout)
    except ValueError:
        raise RuntimeError("hub returned something that isn't JSON - check the hub URL")


def building_rows(cfg, days):
    hub = hub_of(cfg)
    sep = "&" if "?" in hub else "?"
    res = curl_json(f"{hub}{sep}action=building&days={days}", timeout=40)
    if not res.get("ok"):
        raise RuntimeError(res.get("error", "hub error"))
    cols = res.get("cols", [])
    rows = [dict(zip(cols, r)) for r in res.get("rows", [])]
    for r in rows:  # Sheets may have stored "504" as the number 504
        for k in ("device", "label", "gw", "slots", "version"):
            if r.get(k) is not None:
                r[k] = str(r[k]).removesuffix(".0") if isinstance(r[k], float) else str(r[k])
    return rows


def building_csv(d, days=30):
    cfg = load_config(d)
    if not hub_of(cfg):
        return ""
    rows = sorted((r for r in building_rows(cfg, days) if r.get("checks") and building_era(r)), key=lambda r: (r.get("epoch") or 0, r.get("label") or "", r.get("device") or ""))
    for r in rows:
        r["hour"] = datetime.fromtimestamp(r["epoch"]).astimezone().isoformat() if r.get("epoch") else ""
    cols = ["hour", "local_hour", "label", "device", "gw", "checks", "failed", "slow", "router_p50", "router_p95",
            "router_loss", "hop_p95", "net_p95", "rssi", "same_block", "lan",
            "f_wifi", "f_router", "f_uplink", "f_internet", "f_dns", "f_web", "s_router", "s_net", "s_web", "slots", "version"]
    import io
    buf = io.StringIO()
    w = csv.DictWriter(buf, cols, extrasaction="ignore")
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue()


BUILDING_CACHE = {"t": 0, "view": None}


def building_view(d, days=7, max_age=600):
    """Cached for 10 minutes: with ~100 apartments the hub returns a few thousand rows per call."""
    if BUILDING_CACHE["view"] is not None and time.time() - BUILDING_CACHE["t"] < max_age:
        return BUILDING_CACHE["view"]
    view = _building_view(d, days)
    if not view.get("error"):
        BUILDING_CACHE.update(t=time.time(), view=view)
    return view


def building_era(r):
    return vtuple(r.get("version") or "0") >= (0, 8)


def _building_view(d, days):
    cfg = load_config(d)
    if not hub_of(cfg):
        return {"configured": False}
    try:
        rows = building_rows(cfg, days)
    except RuntimeError as e:
        return {"configured": True, "error": str(e)}
    if not rows:
        return {"configured": True, "devices": [], "by_hour": [], "verdict": ["No apartment has shared data yet."]}
    rows = [r for r in rows if r.get("checks")]  # 0 checks = an hour the app took back
    if not rows:
        return {"configured": True, "devices": [], "by_hour": [], "verdict": ["No apartment has shared data yet."]}
    # From 0.8 on the app only shares checks made on the EMIL networks. Apartments can sit on different
    # subnets (different gateway IPs), so keep them all; drop rows from older apps, which shared any network.
    rows = [r for r in rows if building_era(r)]
    if not rows:
        return {"configured": True, "devices": [], "by_hour": [], "verdict": ["No apartment has shared data yet."]}
    gws = {r["gw"] for r in rows}
    EVE = range(18, 23)

    def rate(rs):
        n = sum(r["checks"] for r in rs)
        return round(100 * sum(r["failed"] + r["slow"] for r in rs) / n, 2) if n else None

    def med(rs, k):
        return pct([r[k] for r in rs if r.get(k) not in ("", None)], 50)

    def detail(rs):
        """Totals for hover text: what failed and what was slow."""
        out = {"checks": sum(r["checks"] for r in rs), "failed": sum(r["failed"] for r in rs),
               "slow": sum(r["slow"] for r in rs), "router_p95": med(rs, "router_p95"), "rssi": med(rs, "rssi")}
        if any(r.get("f_wifi") not in ("", None) for r in rs):  # apps from 0.9 on send the breakdown
            out["fail"] = {k: sum(int(r.get("f_" + k) or 0) for r in rs) for k in FAIL_GROUPS}
            out["slow_at"] = {k: sum(int(r.get("s_" + k) or 0) for r in rs) for k in ("router", "net", "web")}
        return out

    devs = defaultdict(list)
    for r in rows:
        devs[r["device"]].append(r)
    names = {dev: next((r["label"] for r in sorted(rs, key=lambda r: -int(r["epoch"])) if r.get("label")), "") or f"#{dev[:4]}"
             for dev, rs in devs.items()}
    dup = Counter(names.values())
    seen = Counter()
    for dev in sorted(devs, key=lambda x: min(int(r["epoch"]) for r in devs[x])):
        if dup[names[dev]] > 1:  # two Macs in the same apartment
            seen[names[dev]] += 1
            names[dev] = f"{names[dev]} ({seen[names[dev]]})"
    devices, peaky, router_peaky = [], 0, 0
    for dev, rs in devs.items():
        ev = [r for r in rs if int(r["local_hour"]) in EVE]
        ot = [r for r in rs if int(r["local_hour"]) not in EVE]
        rev, rot = rate(ev), rate(ot)
        pev, pot = med(ev, "router_p95"), med(ot, "router_p95")
        if rev is not None and rot is not None and rev >= max(1, 2 * rot):
            peaky += 1
        if pev and pot and pev >= 2 * pot:
            router_peaky += 1
        det = detail(rs)
        top = max(det.get("fail", {}).items(), key=lambda kv: kv[1], default=(None, 0))
        devices.append({"device": dev, "label": names[dev], "hours": len(rs),
                        "last": max(int(r["epoch"]) for r in rs), "rate_eve": rev, "rate_other": rot,
                        "router_eve": pev, "router_other": pot, "rssi": med(rs, "rssi"),
                        "same_block": med(rs, "same_block"), "lan": med(rs, "lan"), "detail": det,
                        "breaks_at": FAIL_GROUPS[top[0]] if top[1] else ""})
    devices.sort(key=lambda x: -(x["rate_eve"] or 0))
    by_hour = []
    for h in range(24):
        rs = [r for r in rows if int(r["local_hour"]) == h]
        by_hour.append({"hour": h, "rate": rate(rs) or 0, "apts": len({r["device"] for r in rs}),
                        "apts_failed": len({r["device"] for r in rs if r["failed"]}), **detail(rs)})
    grid = []
    for dv in devices:
        for h in range(24):
            rs = [r for r in devs[dv["device"]] if int(r["local_hour"]) == h]
            if rs:
                grid.append({"device": dv["device"], "label": dv["label"], "hour": h, "rate": rate(rs), "hours": len(rs), **detail(rs)})

    # moments when several apartments had failed checks in the same 5 minutes
    at, online = defaultdict(dict), defaultdict(set)
    for r in rows:
        online[int(r["epoch"])].add(r["device"])
    for r in rows:
        sl = str(r.get("slots") or "")
        if len(sl) == 12:
            for i, c in enumerate(sl):
                if c.isdigit() and c != "0":
                    at[int(r["epoch"]) + 300 * i][names[r["device"]]] = int(c)
    # count a moment only if at least a third of the apartments online that hour failed together
    together = [{"epoch": e, "apts": sorted(v.items()), "online": len(online[e - e % 3600])} for e, v in at.items()
                if len(v) >= max(3, len(online[e - e % 3600]) / 3)]
    together.sort(key=lambda x: (-len(x["apts"]), -x["epoch"]))

    n = len(devices)
    verdict = [f"{n} apartment{'s' if n != 1 else ''} sharing data from the EMIL networks, last {days} days."]
    with_both = sum(1 for x in devices if x["rate_eve"] is not None and x["rate_other"] is not None)
    if with_both:
        verdict.append(f"{peaky} of {with_both} apartments have at least twice as many failed/slow checks in the evening (18–23h) as the rest of the day.")
        verdict.append(f"In {router_peaky} of {with_both} apartments the router itself answers at least twice as slowly in the evening, so the slowdown is inside the building network, before the internet.")
    allf = detail(rows).get("fail")
    if allf and sum(allf.values()):
        tot = sum(allf.values())
        inside = allf["wifi"] + allf["router"] + allf["uplink"]
        verdict.append(f"{round(100 * inside / tot)}% of failed checks broke inside the building (Wi-Fi, router or building uplink), "
                       f"{round(100 * allf['internet'] / tot)}% at the internet provider, {round(100 * (allf['dns'] + allf['web']) / tot)}% were DNS or web only.")
    if together:
        verdict.append(f"{len(together)} times, at least three apartments (and at least a third of those online) had failures in the same 5 minutes: a sign of a shared, building-wide problem. The worst moments are listed below.")
    lans = [x["lan"] for x in devices if x["lan"]]
    if lans:
        verdict.append(f"Apartments see a median of {pct(lans, 50):.0f} devices on their local network"
                       + (", so everyone is on one shared network." if len(gws) == 1 else "."))
    return {"configured": True, "devices": devices, "by_hour": by_hour, "grid": grid,
            "together": together[:10], "groups": FAIL_GROUPS, "days": days, "verdict": verdict}


def share_once(d):
    cfg = load_config(d)
    if not (cfg["share"] and hub_of(cfg)):
        return
    if not cfg["label"]:
        SHARE_STATUS["ok"], SHARE_STATUS["msg"] = False, "paused until you enter your apartment number"
        return
    hours = hourly_summary(d)
    # Hours sent earlier that no longer qualify (e.g. sleep artefacts now filtered out) are sent
    # again with 0 checks, so the building view stops counting them.
    sent_path = d / "shared.json"
    try:
        sent = json.loads(sent_path.read_text())
    except (OSError, ValueError):  # first run after updating from 0.8: any recent hour may have been sent
        top = int(datetime.now().astimezone().replace(minute=0, second=0, microsecond=0).timestamp())
        sent = {f"{top - 3600 * i}|{gw}": top - 3600 * i for i in range(1, 49) for gw in {h["gw"] for h in hours}}
    keep_after = time.time() - 49 * 3600  # hourly_summary covers the last 48 hours
    sent = {k: e for k, e in sent.items() if e >= keep_after}
    have = {f"{h['epoch']}|{h['gw']}" for h in hours}
    for k, e in sent.items():
        if k not in have:
            gw = k.split("|", 1)[1]
            hours.append({"epoch": e, "hour": datetime.fromtimestamp(e).astimezone().isoformat(),
                          "local_hour": datetime.fromtimestamp(e).hour, "gw": gw, "checks": 0, "failed": 0, "slow": 0})
    payload = {"action": "ingest", "device": cfg["device_id"],
               "label": cfg["label"][:20], "version": VERSION, "hours": hours}
    SHARE_STATUS["last"] = now_iso()
    try:
        res = curl_json(hub_of(cfg), payload)
        SHARE_STATUS["ok"] = bool(res.get("ok"))
        if res.get("ok"):
            sent.update({f"{h['epoch']}|{h['gw']}": h["epoch"] for h in hours if h["checks"]})
            sent = {k: e for k, e in sent.items() if k in have}
            sent_path.write_text(json.dumps(sent))
        SHARE_STATUS["msg"] = f"sent {len(have)} hourly summaries" if res.get("ok") else res.get("error", "rejected")
        if "too frequent" in SHARE_STATUS["msg"]:  # hub's per-device limit; the next round retries anyway
            SHARE_STATUS["ok"], SHARE_STATUS["msg"] = None, "sent recently, next update within 30 minutes"
    except RuntimeError as e:
        SHARE_STATUS["ok"], SHARE_STATUS["msg"] = False, str(e)


def share_loop(d, stop, every=1800):
    stop.wait(60)
    while not stop.is_set():
        try:
            share_once(d)
        except Exception as e:  # sharing must never stop the monitor
            SHARE_STATUS["ok"], SHARE_STATUS["msg"] = False, f"error: {e}"
        stop.wait(every)


# =========================================================================== updates
UPDATE = {"latest": None, "notes": "", "sha256": "", "state": "", "msg": ""}


def vtuple(v):
    try:
        return tuple(int(x) for x in str(v).strip().lstrip("v").split("."))
    except ValueError:
        return (0,)


def releases_url(d):
    return load_config(d).get("release_url") or RELEASES  # override only for testing


def check_update(d):
    """Reads latest.json from the newest GitHub release (a plain download, so no API rate limits)."""
    try:
        info = curl_json(f"{releases_url(d)}/latest.json")
    except RuntimeError:
        return False
    if vtuple(info.get("version")) > vtuple(VERSION):
        UPDATE.update(latest=str(info["version"]), notes=str(info.get("notes", ""))[:500], sha256=str(info.get("sha256", "")))
    else:
        UPDATE["latest"] = None
    return True


def update_loop(d, stop):
    stop.wait(random.uniform(60, 900))  # spread the building's Macs out
    while not stop.is_set():
        try:
            check_update(d)
        except Exception:
            pass
        stop.wait(6 * 3600 + random.uniform(0, 1800))


def app_location(d):
    """The EMIL NetMon.app the launcher last ran from (it writes ~/.netmon/app_path)."""
    try:
        p = Path((d / "app_path").read_text().strip())
    except OSError:
        return None
    return p if p.suffix == ".app" and (p / "Contents" / "Resources").is_dir() and os.access(p, os.W_OK) else None


def apply_update(d):
    """Downloads the newest app, swaps it in for the installed one and restarts the background agent.
    Runs only when the user clicks "Update now"."""
    if UPDATE["state"] == "working":
        return
    UPDATE.update(state="working", msg="Downloading the new version…")
    try:
        tmp = Path(tempfile.mkdtemp(prefix="emil-netmon-update-"))
        z = tmp / "EMIL-NetMon.zip"
        r = subprocess.run(["/usr/bin/curl", "-sfL", "-m", "300", "-o", str(z), f"{releases_url(d)}/EMIL-NetMon.zip"], capture_output=True)
        if r.returncode != 0:
            raise RuntimeError("the download failed. Check your connection and try again.")
        if UPDATE["sha256"] and hashlib.sha256(z.read_bytes()).hexdigest() != UPDATE["sha256"]:
            raise RuntimeError("the download was damaged. Try again later.")
        subprocess.run(["/usr/bin/ditto", "-x", "-k", str(z), str(tmp / "x")], check=True, capture_output=True)
        new = next(iter(sorted((tmp / "x").glob("**/*.app"))), None)
        if not new or not (new / "Contents/Resources/netmon.py").exists():
            raise RuntimeError("the download doesn't contain the app.")
        ver = subprocess.run([sys.executable, str(new / "Contents/Resources/netmon.py"), "version"],
                             capture_output=True, text=True, timeout=30).stdout.strip()
        if vtuple(ver) <= vtuple(VERSION):
            raise RuntimeError("the download isn't newer than this version.")
        app = app_location(d)
        if app:  # update the app in place (works without admin rights: the user owns the bundle)
            UPDATE["msg"] = "Installing…"
            r = subprocess.run(["/usr/bin/rsync", "-a", "--delete", f"{new}/", f"{app}/"], capture_output=True, text=True)
            if r.returncode != 0:
                app = None  # couldn't replace the app; the background monitor still gets updated below
        src = (app or new) / "Contents/Resources/netmon.py"
        UPDATE["msg"] = "Restarting…"
        # new session, so the restart (which stops this very process) can finish
        with open(d / "update.log", "w") as log:
            subprocess.Popen([sys.executable, str(src), "--data-dir", str(d), "install"],
                             start_new_session=True, stdout=log, stderr=subprocess.STDOUT)
    except Exception as e:
        UPDATE.update(state="error", msg=f"Update failed: {e}")


# =========================================================================== report / server
def page(data=None, live=False):
    return (TEMPLATE.replace("/*DATA*/null", json.dumps(data, default=str) if data else "null")
            .replace("/*LIVE*/false", "true" if live else "false").replace("/*NAME*/", APP_NAME))


def build_report(args):
    data = analyze(args.data_dir, args.hours, args.gateway, args.min_ticks)
    if data.get("empty"):
        sys.exit(f"No data yet in {args.data_dir}. Run `netmon.py run` first.")
    out = args.out or (args.data_dir / "report.html")
    out.write_text(page(data))
    s = data["stats"]
    print(f"Report written to {out}\n  {s['range']} | uptime {s['uptime']} | {s['outages']} outages "
          f"({s['downtime']}) | {s['blips']} blips")
    for l in data["layers"]:
        print(f"  {l['label']}: {l['count']} outages, {l['dur']} ({l['share']}%)")
    for v in data["verdict"]:
        print("  -", v)
    if args.open:
        subprocess.run(["open", str(out)])


def start_server(d, port):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            u = urlparse(self.path)
            if u.path == "/api/data":
                q = parse_qs(u.query)
                hours = fnum((q.get("hours") or [""])[0]) or None
                gw = (q.get("gw") or [""])[0] or None
                try:
                    body = json.dumps(analyze(d, hours, gw), default=str).encode()
                    ctype = "application/json"
                except Exception as e:  # keep the server alive on bad data
                    self.send_response(500); self.end_headers(); self.wfile.write(str(e).encode()); return
            elif u.path == "/api/config":
                cfg = load_config(d)
                body = json.dumps({**cfg, "version": VERSION, "status": SHARE_STATUS, "hub": bool(hub_of(cfg)), "update": UPDATE, "on_building_net": CURRENT_NET["building"],
                                   "download_url": DOWNLOAD_URL}).encode()
                ctype = "application/json"
            elif u.path == "/api/building":
                try:
                    body = json.dumps(building_view(d), default=str).encode()
                except RuntimeError as e:
                    body = json.dumps({"configured": True, "error": str(e)}).encode()
                ctype = "application/json"
            elif u.path == "/api/building.csv":
                try:
                    body = building_csv(d).encode()
                except RuntimeError as e:
                    self.send_response(502); self.end_headers(); self.wfile.write(str(e).encode()); return
                self.send_response(200)
                self.send_header("Content-Type", "text/csv; charset=utf-8")
                self.send_header("Content-Disposition", 'attachment; filename="building-network-data.csv"')
                self.end_headers()
                self.wfile.write(body)
                return
            elif u.path == "/":
                body, ctype = page(live=True).encode(), "text/html; charset=utf-8"
            else:
                self.send_response(404); self.end_headers(); return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            # Custom header => browsers must preflight, so other websites can't trigger these.
            if self.headers.get("X-Netmon") != "1":
                self.send_response(403); self.end_headers(); return
            n = int(self.headers.get("Content-Length") or 0)
            try:
                data = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                data = {}
            u = urlparse(self.path).path
            out = {"ok": True}
            if u == "/api/config":
                cfg = load_config(d)
                was = cfg["share"] and bool(cfg["label"])  # sharing without a label is paused
                if "share" in data:
                    cfg["share"] = bool(data["share"])
                if "label" in data:
                    cfg["label"] = " ".join(str(data["label"]).split())[:20]
                if cfg["share"] and not cfg["label"]:
                    self.send_response(400); self.send_header("Content-Type", "application/json"); self.end_headers()
                    self.wfile.write(json.dumps({"ok": False, "error": "Enter your apartment number first."}).encode())
                    return
                cfg["asked"] = True
                save_config(d, cfg)
                if cfg["share"] and not was:
                    def first_share():
                        share_once(d)
                        BUILDING_CACHE["t"] = 0  # show our own data in the building view right away
                    threading.Thread(target=first_share, daemon=True).start()
            elif u == "/api/update":
                threading.Thread(target=apply_update, args=(d,), daemon=True).start()
            elif u == "/api/check-update":
                out = {"ok": bool(check_update(d)), "update": UPDATE}
            elif u == "/api/share-now":
                share_once(d)
                out = {"ok": bool(SHARE_STATUS["ok"]), "status": SHARE_STATUS}
            elif u == "/api/uninstall":
                def later():
                    time.sleep(1)
                    uninstall(argparse.Namespace(data_dir=d))
                threading.Thread(target=later, daemon=True).start()
            else:
                self.send_response(404); self.end_headers(); return
            body = json.dumps(out).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

    try:
        srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    except OSError as e:
        print(f"netmon: dashboard not started on port {port} ({e.strerror}); logging continues", flush=True)
        return
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print(f"netmon: live dashboard at http://127.0.0.1:{port}", flush=True)


# =========================================================================== launchd
def install(args):
    d = args.data_dir
    d.mkdir(parents=True, exist_ok=True)
    target = d / "netmon.py"
    if Path(__file__).resolve() != target.resolve():
        m = target.exists() and re.search(r'^VERSION = "([0-9.]+)"', target.read_text(errors="ignore"), re.M)
        if m and vtuple(m.group(1)) > vtuple(VERSION) and not args.force:
            print(f"A newer version ({m.group(1)}) is already installed; keeping it (use --force to replace).")
            return install_existing(args, target)
        shutil.copy2(__file__, target)
    here = Path(__file__).resolve().parent
    for extra in ("wifistat", "defaults.json"):  # shipped inside Netmon.app
        src = here / extra
        if src.exists() and src.resolve() != (d / extra).resolve():
            shutil.copy2(src, d / extra)
    if build_helper(d):
        print("Wi-Fi helper ready (per-tick signal readings)")
    py = next((c for c in ("/opt/homebrew/bin/python3", "/usr/local/bin/python3") if os.path.exists(c)), sys.executable)
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    PLIST.write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>{LABEL}</string>
  <key>ProgramArguments</key><array>
    <string>{py}</string><string>{target}</string>
    <string>--data-dir</string><string>{d}</string>
    <string>run</string><string>--interval</string><string>{args.interval}</string>
    <string>--port</string><string>{args.port}</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ProcessType</key><string>Background</string>
  <key>StandardOutPath</key><string>{d}/agent.log</string>
  <key>StandardErrorPath</key><string>{d}/agent.log</string>
</dict></plist>
""")
    uid = os.getuid()
    subprocess.run(["launchctl", "bootout", f"gui/{uid}/{LABEL}"], capture_output=True)
    for _ in range(10):  # bootout finishes asynchronously; retry until the old job is gone
        time.sleep(1)
        r = subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(PLIST)], capture_output=True, text=True)
        if r.returncode == 0:
            break
    if r.returncode != 0:
        sys.exit(f"launchctl bootstrap failed: {r.stderr.strip()}")
    print(f"Installed: runs at login, logs to {d}\nLive dashboard: http://127.0.0.1:{args.port}")


def install_existing(args, target):
    r = subprocess.run([sys.executable, str(target), "--data-dir", str(args.data_dir), "install",
                        "--interval", str(args.interval), "--port", str(args.port)])
    sys.exit(r.returncode)


def uninstall(args):
    if PLIST.exists():  # remove first: bootout may stop this very process
        PLIST.unlink()
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"], capture_output=True)
    print(f"Uninstalled. Logs kept in {args.data_dir}")


def main():
    p = argparse.ArgumentParser(description="Internet connection logger for macOS")
    p.add_argument("--data-dir", type=Path, default=DEFAULT_DIR)
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="log continuously and serve the live dashboard")
    r.add_argument("--interval", type=int, default=10)
    r.add_argument("--port", type=int, default=PORT, help="dashboard port (0 = no dashboard)")
    r.add_argument("-v", "--verbose", action="store_true")
    rp = sub.add_parser("report", help="write a static HTML report")
    rp.add_argument("--out", type=Path)
    rp.add_argument("--open", action="store_true")
    rp.add_argument("--hours", type=float, help="only the last N hours")
    rp.add_argument("--gateway", help="only this network (gateway IP)")
    rp.add_argument("--min-ticks", type=int, default=2, help="consecutive failed ticks that count as an outage")
    i = sub.add_parser("install", help="run in the background via launchd")
    i.add_argument("--interval", type=int, default=10)
    i.add_argument("--port", type=int, default=PORT)
    i.add_argument("--force", action="store_true", help="replace a newer installed version")
    sub.add_parser("uninstall")
    sub.add_parser("version")
    args = p.parse_args()
    {"run": run, "report": build_report, "install": install, "uninstall": uninstall,
     "version": lambda a: print(VERSION)}[args.cmd](args)


TEMPLATE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>/*NAME*/</title>
<style>
:root{color-scheme:light;--surface:#fcfcfb;--card:#ffffff;--text:#0b0b0b;--text2:#52514e;--muted:#8a8984;--grid:#e6e5e0;--line:#d8d7d1;
--s1:#2a78d6;--s2:#eb6834;--s3:#1baf7a;--crit:#d03b3b;--warn:#fab219;--good:#0ca30c;--na:#e9e8e3;
--q0:#f1f0ec;--q1:#cde2fb;--q2:#86b6ef;--q3:#3987e5;--q4:#1c5cab;--q5:#0d366b}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){color-scheme:dark;--surface:#1a1a19;--card:#222221;--text:#f0efec;--text2:#c3c2b7;--muted:#8f8e86;--grid:#33332f;--line:#44443f;
--s1:#3987e5;--s2:#d95926;--s3:#199e70;--na:#2e2e2b;--q0:#2a2a28;--q1:#104281;--q2:#1c5cab;--q3:#3987e5;--q4:#86b6ef;--q5:#cde2fb}}
:root[data-theme="dark"]{color-scheme:dark;--surface:#1a1a19;--card:#222221;--text:#f0efec;--text2:#c3c2b7;--muted:#8f8e86;--grid:#33332f;--line:#44443f;
--s1:#3987e5;--s2:#d95926;--s3:#199e70;--na:#2e2e2b;--q0:#2a2a28;--q1:#104281;--q2:#1c5cab;--q3:#3987e5;--q4:#86b6ef;--q5:#cde2fb}
*{box-sizing:border-box}body{margin:0;background:var(--surface);color:var(--text);font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
main{max-width:1100px;margin:0 auto;padding:24px 16px 64px}
h1{font-size:22px;margin:0 0 4px}h2{font-size:15px;margin:0 0 2px}.sub{color:var(--text2);margin:0 0 12px;font-size:13px}
.bar{display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin:12px 0}
select{font:inherit;padding:5px 8px;border-radius:6px;border:1px solid var(--line);background:var(--card);color:var(--text)}
.pill{font-size:12px;color:var(--text2);border:1px solid var(--line);border-radius:999px;padding:3px 10px}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--good);margin-right:6px;vertical-align:0}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin:16px 0}
.tile,.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.tile .k{color:var(--text2);font-size:12px}.tile .v{font-size:24px;font-weight:600;margin-top:2px}.tile .n{color:var(--muted);font-size:12px}
.card{margin:16px 0}svg{display:block;width:100%;height:auto;overflow:visible}
.axis text{fill:var(--muted);font-size:11px}.grid line{stroke:var(--grid)}.lbl{fill:var(--text2);font-size:12px}
table{width:100%;border-collapse:collapse;font-size:13px}th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--grid);vertical-align:top}
th{color:var(--text2);font-weight:500}td.num{font-variant-numeric:tabular-nums;white-space:nowrap}.desc{color:var(--muted);font-size:12px}
.legend{display:flex;gap:16px;flex-wrap:wrap;font-size:12px;color:var(--text2);margin:6px 0}.sw{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:6px;vertical-align:-1px}
.tip{position:fixed;pointer-events:none;background:var(--card);border:1px solid var(--line);border-radius:6px;padding:6px 8px;font-size:12px;box-shadow:0 2px 8px rgba(0,0,0,.15);display:none;z-index:9;white-space:nowrap}
.scroll{overflow-x:auto}.note{color:var(--text2);font-size:12px}
.verdict{margin:0;padding-left:18px}.verdict li{margin:4px 0}
.empty{padding:40px;text-align:center;color:var(--text2)}
h3{font-size:13px;color:var(--text2);font-weight:500;margin:16px 0 6px}
button{font:inherit;padding:6px 12px;border-radius:6px;border:1px solid var(--s1);background:var(--s1);color:#fff;cursor:pointer}
button.ghost{background:transparent;color:var(--text);border-color:var(--line)}
.sharerow{display:flex;gap:8px;flex-wrap:wrap;align-items:center}.lblin{font:inherit;padding:6px 8px;border-radius:6px;border:1px solid var(--line);background:var(--surface);color:var(--text);width:180px}
.form{display:grid;gap:10px;max-width:560px}.form label{display:grid;gap:4px;font-size:13px;color:var(--text2)}
.form label:first-child{display:flex;gap:8px;align-items:center;color:var(--text)}
.form input:not([type]){font:inherit;padding:6px 8px;border-radius:6px;border:1px solid var(--line);background:var(--surface);color:var(--text);width:100%}
a{color:var(--s1)}
</style></head><body><main>
<h1>/*NAME*/</h1>
<p class="sub" id="range"></p>
<div class="bar" id="controls" hidden>
  <label>Range <select id="hours"><option value="1">Last hour</option><option value="6">Last 6 hours</option><option value="24" selected>Last 24 hours</option><option value="168">Last 7 days</option><option value="">All</option></select></label>
  <label>Network <select id="gw"><option value="">All networks</option></select></label>
  <span class="pill" id="live"><span class="dot"></span>Live</span>
</div>
<div class="card" id="upd" hidden style="border-color:var(--s1)"><h2 id="upd_title"></h2><p class="sub" id="upd_notes"></p>
<button id="upd_go">Update now</button> <span class="note" id="upd_msg"></span></div>
<div class="card" id="invite" hidden style="border-color:var(--s1)"><h2 id="inv_title">Help get the building's internet fixed</h2>
<p class="sub" id="inv_sub">Share stats with your neighbours, so the Building view can show whether everyone has the same problem at the same times. While you're on EMIL BEWOHNER or EMIL GAST, EMIL NetMon sends once per hour, with your apartment number: how many checks failed or were slow and where they broke (Wi-Fi, router, building uplink, internet, DNS or web), router delay, Wi-Fi signal strength and how crowded your channel is. No websites, names, network names or addresses. Everyone in the building can see these numbers.</p>
<div class="sharerow"><input class="lblin" maxlength="20" placeholder="Apartment, e.g. 3B" aria-label="Apartment number"> <button class="sharebtn">Share anonymous stats</button> <button id="inv_no" class="ghost">Not now</button></div><p class="note shareerr" hidden></p></div>
<div id="body"></div>
<div class="card" id="building" hidden><h2>Building view</h2><p class="sub" id="b_sub"></p><ul class="verdict" id="b_verdict"></ul>
<h3>Problem rate by hour, all apartments</h3><svg id="b_hours"></svg>
<h3>Problem rate by apartment and hour</h3><div class="scroll"><svg id="b_grid"></svg></div>
<h3>Apartments</h3><div class="scroll"><table id="b_table"></table></div>
<div id="b_tog_box" hidden><h3>When several apartments failed at once</h3><p class="sub">5-minute windows in which at least three apartments, and at least a third of those online, had failed checks, most apartments first.</p><div class="scroll"><table id="b_tog"></table></div></div>
<p class="note" style="margin-top:10px"><a href="/api/building.csv">Download the building data (CSV)</a> to attach for building IT: one row per apartment and hour, last 30 days.</p></div>
<div class="card" id="settings" hidden><h2>Sharing</h2>
<div id="s_off"><p class="sub">Share stats with your neighbours for the Building view. While you're on EMIL BEWOHNER or EMIL GAST, EMIL NetMon sends once per hour, with your apartment number: how many checks failed or were slow and where they broke (Wi-Fi, router, building uplink, internet, DNS or web), router delay, Wi-Fi signal strength and how crowded your channel is. No websites, names, network names or addresses. Everyone in the building can see these numbers.</p>
<div class="sharerow"><input class="lblin" maxlength="20" placeholder="Apartment, e.g. 3B" aria-label="Apartment number"> <button class="sharebtn">Share anonymous stats</button></div><p class="note shareerr" hidden></p></div>
<div id="s_on" hidden><p class="sub" id="s_onmsg"><span id="s_lead">You're sharing stats with the building.</span> <span id="c_status"></span></p>
<p class="note" id="s_net"></p><p class="note" id="s_need" hidden style="color:var(--crit)">Sharing is paused: enter your apartment number and click Save.</p>
<div class="form"><label>Apartment number (shown to neighbours) <input id="c_label" maxlength="20" placeholder="e.g. 3B"></label>
<div><button id="c_save" class="ghost">Save</button> <button id="s_stop" class="ghost">Stop sharing</button></div></div></div>
<p class="note" id="s_nohub" hidden>Sharing isn't set up in this copy of EMIL NetMon.</p>
<div id="s_tell"><h3 style="margin-top:16px">Tell your neighbours</h3><p class="sub">The more apartments share, the stronger the evidence. Paste this into your building chat:</p>
<textarea id="c_msg" readonly rows="6" style="font:inherit;font-size:13px;width:100%;max-width:640px;padding:8px;border-radius:6px;border:1px solid var(--line);background:var(--surface);color:var(--text)"></textarea>
<div><button id="c_copy" class="ghost" style="margin-top:6px">Copy message</button></div></div>
<p class="note" style="margin-top:14px">EMIL NetMon <span id="c_ver"></span> · <a href="#" id="c_chk">Check for updates</a> <span id="c_chkmsg"></span> · data in ~/.netmon · <a href="#" id="c_uninstall">Stop monitoring and uninstall</a> (keeps your logs)</p></div>
</main><div class="tip" id="tip"></div>
<script>
const LIVE=/*LIVE*/false;let D=/*DATA*/null;
const $=id=>document.getElementById(id);const NS="http://www.w3.org/2000/svg";
const el=(t,a={},p)=>{const e=document.createElementNS(NS,t);for(const k in a)e.setAttribute(k,a[k]);if(p)p.appendChild(e);return e};
const tip=$("tip");function showTip(ev,html){tip.innerHTML=html;tip.style.display="block";const x=Math.min(ev.clientX+12,innerWidth-tip.offsetWidth-8);const y=ev.clientY+12+tip.offsetHeight>innerHeight?ev.clientY-tip.offsetHeight-12:ev.clientY+12;tip.style.left=x+"px";tip.style.top=y+"px"}
function hideTip(){tip.style.display="none"}
function hover(node,html){node.addEventListener("mousemove",e=>showTip(e,typeof html==="function"?html(e):html));node.addEventListener("mouseleave",hideTip)}
const esc=s=>String(s??"").replace(/[&<>]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));
const ms=v=>v==null?"—":(v>=100?Math.round(v):v.toFixed(1))+" ms";
const pad=h=>String(h).padStart(2,"0");
function niceTicks(mx){mx=Math.max(mx,1e-6);const raw=mx/4,p=Math.pow(10,Math.floor(Math.log10(raw))),s=[1,2,5,10].find(m=>m*p>=raw)*p;const a=[];for(let v=0;v<mx+s*0.999;v+=s)a.push(+v.toFixed(6));return a.length<2?[0,s]:a}
function roundTop(x,y,w,h,r){r=Math.min(r,h,w/2);return `M${x},${y+h}V${y+r}Q${x},${y} ${x+r},${y}H${x+w-r}Q${x+w},${y} ${x+w},${y+r}V${y+h}Z`}

const SECTIONS=`
<div class="tiles" id="tiles"></div>
<div class="card"><h2>What's going on</h2><ul class="verdict" id="verdict"></ul></div>
<div class="card"><h2>Right now — last 90 checks</h2><p class="sub">Each column is one check (every ~10s), oldest left. Read bottom-up: the lowest red row is where the chain broke.</p>
<div class="legend"><span><i class="sw" style="background:var(--good)"></i>OK</span><span><i class="sw" style="background:var(--warn)"></i>Weak signal</span><span><i class="sw" style="background:var(--crit)"></i>Failed</span><span><i class="sw" style="background:var(--na)"></i>Not applicable</span></div>
<div class="scroll"><svg id="strip"></svg></div></div>
<div class="card"><h2>Where it breaks</h2><p class="sub">Each outage is blamed on the lowest rung that failed while everything above it failed too.</p><table id="layers"></table></div>
<div class="card"><h2>Evidence: evenings (18:00–23:00) vs the rest of the day</h2><p class="sub">Pings to the router and next hop bypass the security tunnel, so these numbers describe the building network itself.</p><table id="evid"></table><p class="sub" id="envnote" style="margin-top:10px"></p></div>
<div class="card" id="tunnelcard" hidden><h2>Security tunnel</h2><p class="sub" id="tunnelsub"></p><table id="tunnel"></table></div>
<div class="card"><h2>Where latency is added</h2><p class="sub">Round-trip time to each rung during normal operation. If the router is already slow, the delay is on Wi-Fi/LAN.</p><table id="budget"></table></div>
<div class="card"><h2>Latency over time (p95)</h2><div class="legend"><span><i class="sw" style="background:var(--s2)"></i>Router</span><span><i class="sw" style="background:var(--s3)"></i>Next hop (modem / ISP edge)</span><span><i class="sw" style="background:var(--s1)"></i>Internet</span><span><i class="sw" style="background:var(--crit);opacity:.3"></i>Failed checks</span></div><svg id="lat"></svg></div>
<div class="card"><h2>Wi-Fi signal</h2><p class="sub">Median RSSI per interval. Above −67 dBm is good, below −75 dBm is weak. Red bands = outages.</p><svg id="rssi"></svg></div>
<div class="card"><h2>When it happens — problem checks by day and hour</h2><p class="sub">Share of checks that failed or were slow (router > 200 ms, internet > 300 ms, web > 2 s). Stronger blue = more problems. Gray = monitored, no problems. Blank = not monitored.</p><div class="scroll"><svg id="heat"></svg></div></div>
<div class="card"><h2>Problem rate by hour of day (all days)</h2><p class="sub">% of checks in that hour that failed or were slow.</p><svg id="hours_c"></svg></div>
<div class="card"><h2>Outage log</h2><p class="sub" id="evnote"></p><div class="scroll"><table id="events"></table></div></div>
<p class="note" id="foot"></p>`;

function render(){
  const body=$("body");
  if(LIVE){const sel=$("gw"),cur=sel.value;sel.innerHTML='<option value="">All networks</option>'+(D.networks||[]).map(n=>`<option value="${esc(n.gw)}">${esc(n.gw)} · last seen ${esc(n.last)}</option>`).join("");sel.value=cur}
  if(D.empty){body.innerHTML='<div class="card empty">No checks recorded in this window yet.</div>';$("range").textContent="";return}
  if(!$("tiles"))body.innerHTML=SECTIONS;
  const S=D.stats;
  $("range").textContent=S.range+(LIVE?"":" · generated "+D.generated);
  const tiles=[["Uptime",S.uptime,"of "+S.monitored+" monitored"],["Outages",S.outages,"≥"+S.min_ticks*S.interval+"s each · "+S.downtime+" total"],["Longest outage",S.longest,S.blips+" short blips"],
   ["Wi-Fi signal",S.rssi_med!=null?Math.round(S.rssi_med)+" dBm":"—",S.rssi_out!=null?"during outages: "+Math.round(S.rssi_out)+" dBm":"median"],
   ["Internet latency",S.net_p50!=null?Math.round(S.net_p50)+" ms":"—","p95 "+ms(S.net_p95)],["Local lag spikes",S.lag,"router replies > 200 ms"]];
  $("tiles").innerHTML=tiles.map(t=>`<div class="tile"><div class="k">${t[0]}</div><div class="v">${t[1]}</div><div class="n">${t[2]}</div></div>`).join("");
  $("verdict").innerHTML=D.verdict.map(v=>`<li>${esc(v)}</li>`).join("");
  $("layers").innerHTML=D.layers.length?"<tr><th>Failed rung</th><th>Outages</th><th>Downtime</th><th>Share</th></tr>"+D.layers.map(l=>`<tr><td>${esc(l.label)}<div class="desc">${esc(l.desc)}</div></td><td class="num">${l.count}</td><td class="num">${l.dur}</td><td class="num">${l.share}%</td></tr>`).join(""):"<tr><td>No outages in this window.</td></tr>";
  $("budget").innerHTML="<tr><th>Rung</th><th>Typical (p50)</th><th>Bad moments (p95)</th><th>Unanswered</th></tr>"+D.budget.map(b=>`<tr><td>${esc(b.name)}</td><td class="num">${ms(b.p50)}</td><td class="num">${ms(b.p95)}</td><td class="num">${b.loss}%</td></tr>`).join("");
  const E=D.evidence,ev=E.evening,ot=E.other,f=(v,u="")=>v==null?"—":v+u;
  const er=[["Checks",ev.checks,ot.checks],["Checks failed or slow",f(ev.problem,"%"),f(ot.problem,"%")],["Router reply, typical",ms(ev.router_p50),ms(ot.router_p50)],["Router reply, bad moments (p95)",ms(ev.router_p95),ms(ot.router_p95)],
   ["Router unanswered",f(ev.router_loss,"%"),f(ot.router_loss,"%")],["Next hop, bad moments (p95)",ms(ev.hop_p95),ms(ot.hop_p95)],["Internet, bad moments (p95)",ms(ev.net_p95),ms(ot.net_p95)],
   ["Wi-Fi signal (median)",f(ev.rssi," dBm"),f(ot.rssi," dBm")],["Other networks on your channel block (avg)",f(ev.same),f(ot.same)]];
  $("evid").innerHTML="<tr><th>Measure</th><th>Evenings</th><th>Rest of day</th></tr>"+er.map(r=>`<tr><td>${r[0]}</td><td class="num">${r[1]}</td><td class="num">${r[2]}</td></tr>`).join("");
  const Lt=E.latest;$("envnote").textContent=(Lt?`Latest snapshot: ${Lt.lan??"?"} devices visible on the local network${Lt.prefix?" (/"+Lt.prefix+" subnet)":""}; your Wi-Fi on channel ${Lt.mine||"?"}, ${Lt.same??"?"} of ${Lt.total??"?"} nearby networks share its channel block. `:"Network snapshots are taken every 10 minutes. ")+(ev.checks<300||ot.checks<300?"Needs a few days of data before the comparison is solid.":"")+(E.dns_intercepted?" DNS from this Mac is handled by the security tunnel, so DNS results don't reflect the building network.":"");
  const T=D.tunnel;$("tunnelcard").hidden=!T;
  if(T){$("tunnelsub").textContent=`${T.share}% of checks (since tunnel detection started) left the internet via ${T.egress}, not your ISP. Pings bypass it; DNS to public servers and web traffic go through it.`;
   $("tunnel").innerHTML=`<tr><th>Check</th><th>Result</th></tr><tr><td>Public DNS over UDP (1.1.1.1)</td><td class="num">${T.udp_fail}% failed</td></tr><tr><td>Public DNS over TCP (1.1.1.1)</td><td class="num">${T.tcp_fail}% failed</td></tr><tr><td>Web page load</td><td class="num">typical ${ms(T.web_p50)}, bad moments ${ms(T.web_p95)}</td></tr><tr><td>Web checks slower than 2 s</td><td class="num">${T.web_slow} of ${T.checks}</td></tr><tr><td>Web failed while pings worked</td><td class="num">${T.web_fail}</td></tr>`}
  strip();heat();hours();
  const bucketLbl=D.bucket>=60?(D.bucket/60)+" min":D.bucket+"s";
  timeChart($("lat"),D.series,[{k:"router",c:"var(--s2)"},{k:"hop",c:"var(--s3)"},{k:"net",c:"var(--s1)"}],{bands:D.series.filter(p=>p.bad>0).map(p=>({t:p.t,w:D.bucket*1e3,a:p.bad/100})),gap:Math.max(D.bucket*3e3,120e3),
   fmt:p=>`<span style="color:var(--text2)">per ${bucketLbl}</span><br>Router p95: ${ms(p.router)}<br>Next hop p95: ${ms(p.hop)}<br>Internet p95: ${ms(p.net)}<br>Failed checks: ${p.bad}%`});
  const outs=D.events.map(e=>({t:e.iso,w:Math.max(e.secs*1e3,D.bucket*1e3),a:1}));
  timeChart($("rssi"),D.series.filter(p=>p.rssi!=null),[{k:"rssi",c:"var(--s1)"}],{neg:true,h:180,gap:Math.max(D.bucket*3e3,120e3),bands:outs,
   fmt:p=>`Signal (median): ${p.rssi} dBm<br>Weakest: ${p.rssi_lo} dBm<br>Tx rate: ${p.tx??"—"} Mbps`});
  $("evnote").textContent=D.events.length?`${D.events.length} outages, newest first. Signal = weakest Wi-Fi reading during the outage.`:"No outages recorded in this window.";
  $("events").innerHTML=D.events.length?"<tr><th>Start</th><th>Duration</th><th>Failed rung</th><th>Wi-Fi signal</th><th>Tx rate</th><th>Channel</th><th>Network</th></tr>"+D.events.map(e=>`<tr><td class="num">${esc(e.start)}</td><td class="num">${e.dur}</td><td>${esc(e.layer)}</td><td class="num">${e.rssi!=null?Math.round(e.rssi)+" dBm":"—"}</td><td class="num">${e.tx!=null?Math.round(e.tx)+" Mbps":"—"}</td><td>${esc(e.channel)}</td><td>${esc(e.gateway)}</td></tr>`).join(""):"";
  $("foot").textContent=`Check interval ${S.interval}s · ${S.sleep_gaps} sleep/wake gaps excluded · `+(S.router_known?"":"router doesn't answer probes on this network · ")+"An access point and the router behind it can't be told apart from a wireless client: to confirm a Wi-Fi problem, compare against a wired (Ethernet) connection."+(LIVE?" · updated "+new Date().toLocaleTimeString():"");
}

function strip(){const svg=$("strip");svg.innerHTML="";const rows=[["Web","http"],["Public DNS","dns_pub"],["DNS","dns"],["Internet","net"],["Next hop","hop"],["Router","router"],["Wi-Fi","wifi"]];
 const n=D.strip.length,L=72,cw=9,ch=16,W=L+90*cw,H=rows.length*ch+16;svg.setAttribute("viewBox",`0 0 ${W} ${H}`);svg.style.minWidth=Math.min(W,700)+"px";
 rows.forEach(([lbl],i)=>el("text",{x:L-8,y:i*ch+ch/2+4,"text-anchor":"end",class:"lbl"},svg).textContent=lbl);
 D.strip.forEach((c,j)=>{const x=L+(90-n+j)*cw;rows.forEach(([lbl,k],i)=>{let s;if(k==="wifi")s=c.wifi;else if(k==="hop"&&!c.hop_ip)s="na";else if(k==="dns_pub"&&!c.has_pub)s="na";else s=c[k]!=null?"ok":"fail";
  const col={ok:"var(--good)",fail:"var(--crit)",weak:"var(--warn)",na:"var(--na)"}[s];el("rect",{x:x+1,y:i*ch+1,width:cw-2,height:ch-2,rx:2,fill:col},svg)});
  const hit=el("rect",{x,y:0,width:cw,height:rows.length*ch,fill:"transparent"},svg);
  hover(hit,`<b>${c.t}</b> · ${esc(c.gw||"no network")}<br>Wi-Fi: ${c.wifi==="na"?"n/a":c.rssi!=null?Math.round(c.rssi)+" dBm, "+(c.tx??"—")+" Mbps":"down"}<br>Router: ${ms(c.router)}<br>Next hop: ${c.hop_ip?ms(c.hop)+" ("+esc(c.hop_ip)+")":"n/a"}<br>Internet: ${ms(c.net)}<br>DNS (your resolver): ${ms(c.dns)}<br>Public DNS 1.1.1.1: ${c.has_pub?ms(c.dns_pub):"n/a"}<br>Web: ${ms(c.http)}${c.tunnel?" (via security tunnel)":""}`)});
 if(n){el("text",{x:L,y:H-2,class:"axis"},svg).textContent=D.strip[0].t;el("text",{x:W,y:H-2,"text-anchor":"end",class:"axis"},svg).textContent=D.strip[n-1].t}
 svg.querySelectorAll("text").forEach(t=>t.classList.contains("lbl")||t.setAttribute("fill","var(--muted)"))}

function heat(){const svg=$("heat");svg.innerHTML="";const cw=30,ch=20,L=88,T=18,days=D.days;const W=L+24*cw,H=T+days.length*ch+4;svg.setAttribute("viewBox",`0 0 ${W} ${H}`);svg.style.minWidth=W+"px";
 const mx=Math.max(1,...D.heatmap.map(c=>c.rate));const g=el("g",{class:"axis"},svg);
 for(let h=0;h<24;h+=2)el("text",{x:L+h*cw+cw/2,y:12,"text-anchor":"middle"},g).textContent=pad(h);
 days.forEach((d,i)=>{el("text",{x:L-8,y:T+i*ch+ch/2+4,"text-anchor":"end"},g).textContent=new Date(d+"T12:00").toLocaleDateString(undefined,{weekday:"short",month:"short",day:"numeric"})});
 D.heatmap.forEach(c=>{const i=days.indexOf(c.day);const q=c.rate<=0?0:Math.min(5,1+Math.floor(4*c.rate/mx));
  hover(el("rect",{x:L+c.hour*cw+1,y:T+i*ch+1,width:cw-2,height:ch-2,rx:3,fill:`var(--q${q})`},svg),`<b>${c.day} ${pad(c.hour)}:00</b><br>${c.rate}% of ${c.checks} checks had problems<br>Failed: ${c.failed} · Slow: ${c.slow}<br>Outage downtime: ${c.down.toFixed(1)} min`)})}

function hours(){const svg=$("hours_c");svg.innerHTML="";const W=1000,H=170,L=40,B=22,T=10;svg.setAttribute("viewBox",`0 0 ${W} ${H}`);const vals=D.by_hour.map(b=>b.rate);const tk=niceTicks(Math.max(...vals,0.5));const top=tk[tk.length-1];const bw=(W-L)/24;
 const g=el("g",{class:"grid"},svg),ax=el("g",{class:"axis"},svg);
 tk.forEach(v=>{const y=T+(H-T-B)*(1-v/top);el("line",{x1:L,x2:W,y1:y,y2:y},g);el("text",{x:L-6,y:y+4,"text-anchor":"end"},ax).textContent=v+"%"});
 D.by_hour.forEach((b,h)=>{const v=b.rate;const hgt=(H-T-B)*v/top,x=L+h*bw+2,w=bw-4;if(v>0)el("path",{d:roundTop(x,H-B-hgt,w,hgt,4),fill:"var(--s1)"},svg);
  el("text",{x:x+w/2,y:H-6,"text-anchor":"middle"},ax).textContent=pad(h);
  hover(el("rect",{x:L+h*bw,y:T,width:bw,height:H-T-B,fill:"transparent"},svg),`<b>${pad(h)}:00–${pad(h)}:59</b><br>${v}% of ${b.checks} checks failed or slow<br>Failed: ${b.failed} · Slow: ${b.slow}<br>Slow at router: ${b.slow_local} · slow on uplink: ${b.slow_uplink}<br>Outage downtime: ${b.down} min`)})}

function timeChart(svg,pts,lines,opts){svg.innerHTML="";const W=1000,H=opts.h||220,L=50,R=10,T=10,B=24;svg.setAttribute("viewBox",`0 0 ${W} ${H}`);
 if(!pts.length){el("text",{x:W/2,y:H/2,"text-anchor":"middle",fill:"var(--muted)"},svg).textContent="No data";return}
 const ts=pts.map(p=>+new Date(p.t));const t0=Math.min(...ts),t1=Math.max(...ts,t0+1),span=t1-t0;const X=t=>L+(W-L-R)*(t-t0)/span;
 const vals=pts.flatMap(p=>lines.map(l=>p[l.k])).filter(v=>v!=null);let lo=0,hi,tk;
 if(opts.neg){lo=Math.floor((Math.min(...vals)-2)/10)*10;hi=Math.ceil((Math.max(...vals)+2)/10)*10;tk=[];for(let v=lo;v<=hi;v+=10)tk.push(v)}else{tk=niceTicks(Math.max(1,...vals));hi=tk[tk.length-1]}
 const Y=v=>T+(H-T-B)*(1-(v-lo)/((hi-lo)||1));const g=el("g",{class:"grid"},svg),ax=el("g",{class:"axis"},svg);
 tk.forEach(v=>{el("line",{x1:L,x2:W-R,y1:Y(v),y2:Y(v)},g);el("text",{x:L-6,y:Y(v)+4,"text-anchor":"end"},ax).textContent=v});
 for(let i=0;i<=6;i++){const t=t0+span*i/6,d=new Date(t);el("text",{x:X(t),y:H-6,"text-anchor":i==0?"start":i==6?"end":"middle"},ax).textContent=(span>36e5*30?d.toLocaleDateString(undefined,{month:"short",day:"numeric"})+" ":"")+d.toLocaleTimeString([],{hour:"2-digit",minute:"2-digit"})}
 (opts.bands||[]).forEach(b=>{const bt=+new Date(b.t);if(bt<t0-b.w||bt>t1)return;const x1=Math.max(L,X(bt));el("rect",{x:x1,y:T,width:Math.max(2,Math.min(W-R,X(bt+b.w))-x1),height:H-T-B,fill:"var(--crit)",opacity:Math.min(.35,.08+b.a*.3)},svg)});
 lines.forEach(l=>{let d="",pen=false,prev=null;pts.forEach((p,i)=>{const v=p[l.k];if(v==null||(prev!=null&&ts[i]-prev>opts.gap))pen=false;if(v!=null){d+=(pen?"L":"M")+X(ts[i]).toFixed(1)+","+Y(v).toFixed(1);pen=true}prev=ts[i]});el("path",{d,fill:"none",stroke:l.c,"stroke-width":2,"stroke-linejoin":"round"},svg)});
 const cross=el("line",{y1:T,y2:H-B,stroke:"var(--muted)","stroke-width":1,opacity:0},svg);const hit=el("rect",{x:L,y:T,width:W-L-R,height:H-T-B,fill:"transparent"},svg);
 hit.addEventListener("mousemove",e=>{const r=svg.getBoundingClientRect();const t=t0+((e.clientX-r.left)/r.width*W-L)/(W-L-R)*span;let bi=0,bd=Infinity;ts.forEach((v,i)=>{const dd=Math.abs(v-t);if(dd<bd){bd=dd;bi=i}});
  cross.setAttribute("x1",X(ts[bi]));cross.setAttribute("x2",X(ts[bi]));cross.setAttribute("opacity",.6);showTip(e,`<b>${new Date(ts[bi]).toLocaleString()}</b><br>`+opts.fmt(pts[bi]))});
 hit.addEventListener("mouseleave",()=>{cross.setAttribute("opacity",0);hideTip()})}

async function refresh(){try{const q=new URLSearchParams({hours:$("hours").value,gw:$("gw").value});const r=await fetch("/api/data?"+q,{cache:"no-store"});if(!r.ok)throw new Error(r.status);D=await r.json();render();$("live").innerHTML='<span class="dot"></span>Live';}
 catch(e){$("live").innerHTML='<span class="dot" style="background:var(--crit)"></span>Reconnecting…'}}
const post=(u,b)=>fetch(u,{method:"POST",headers:{"X-Netmon":"1","Content-Type":"application/json"},body:JSON.stringify(b||{})}).then(r=>r.json());
let C=null,notNow=false;
let updating=null;
async function loadConfig(){try{const c=C=await (await fetch("/api/config")).json();$("settings").hidden=false;$("c_ver").textContent=c.version;
 if(updating&&c.version!==updating){location.reload();return}
 const u=c.update||{};$("upd").hidden=!(u.latest||updating);if(u.latest){$("upd_title").textContent=`Update available: EMIL NetMon ${u.latest}`;$("upd_notes").textContent=(u.notes?u.notes+" ":"")+`You have ${c.version}. Your data and settings are kept.`}
 if(u.state==="error"){updating=null;$("upd_go").disabled=false;$("upd_msg").textContent=u.msg}else if(updating)$("upd_msg").textContent=u.msg||"Updating…";
 const paused=c.share&&!c.label;$("invite").hidden=!(c.hub&&((!c.share&&!c.asked&&!notNow)||paused));$("s_need").hidden=!paused;
 $("inv_title").textContent=paused?"Add your apartment number":"Help get the building's internet fixed";$("inv_no").hidden=paused;
 document.querySelectorAll(".sharebtn").forEach(b=>b.textContent=paused?"Resume sharing":"Share anonymous stats");$("s_off").hidden=!c.hub||c.share;$("s_on").hidden=!c.share;$("s_nohub").hidden=c.hub;$("s_tell").hidden=!c.hub;
 if(document.activeElement.tagName!=="INPUT"){$("c_label").value=c.label||"";document.querySelectorAll(".lblin").forEach(i=>i.value=c.label||"")}
 const st=c.status;$("s_net").textContent=c.on_building_net===false?"You're not on an EMIL network right now, so nothing from this network is shared. Only checks made on EMIL BEWOHNER or EMIL GAST count.":c.on_building_net?"You're on an EMIL network: these checks count for the building.":"";
 $("s_lead").textContent=paused?"Sharing is paused.":"You're sharing stats with the building.";$("c_status").hidden=paused;$("c_status").textContent=st.last?`Last sent ${new Date(st.last).toLocaleTimeString()}: ${st.msg}.`:"The first summary goes out within a minute.";
 $("c_msg").value=`Is the internet in our building dropping out for you too, especially in the evenings? I'm collecting evidence for building IT with EMIL NetMon, a free Mac app (macOS 12+) that measures where the connection breaks: Wi-Fi, router, building network or internet provider.\n\nDownload, unzip and open it, then type your apartment number and click "Share anonymous stats": ${c.download_url}\n\nOnly hourly numbers are shared, with your apartment number (failed checks, delays, Wi-Fi signal). No websites, names or addresses.`;
 if(c.hub)loadBuilding();else $("building").hidden=true}catch(e){}}
async function saveConfig(b){await post("/api/config",b);await loadConfig();setTimeout(loadConfig,5000)}
document.querySelectorAll(".sharebtn").forEach(x=>x.onclick=async()=>{const box=x.closest(".card"),inp=box.querySelector(".lblin"),err=box.querySelector(".shareerr"),label=inp.value.trim();
 if(!label){err.textContent="Enter your apartment number first, so neighbours can tell the apartments apart.";err.hidden=false;inp.focus();return}
 err.hidden=true;const r=await post("/api/config",{share:true,label});if(!r.ok){err.textContent=r.error||"Couldn't start sharing.";err.hidden=false;return}await loadConfig();setTimeout(loadConfig,5000)});
$("inv_no").onclick=()=>{notNow=true;$("invite").hidden=true;post("/api/config",{})};
$("s_stop").onclick=()=>{if(confirm("Stop sharing? Summaries already sent stay in the Building view for up to 60 days."))saveConfig({share:false})};
$("c_save").onclick=async()=>{if(!$("c_label").value.trim()){$("s_need").hidden=false;$("c_label").focus();return}await saveConfig({label:$("c_label").value})};
$("upd_go").onclick=async()=>{updating=C.version;$("upd_go").disabled=true;$("upd_msg").textContent="Updating…";await post("/api/update");
 const t=setInterval(async()=>{if(!updating){clearInterval(t);return}await loadConfig()},2000);setTimeout(()=>{if(updating){clearInterval(t);$("upd_msg").textContent="This is taking long. Open EMIL NetMon again from Applications."}},120000)};
$("c_copy").onclick=()=>{$("c_msg").select();try{navigator.clipboard.writeText($("c_msg").value)}catch(e){document.execCommand("copy")}$("c_copy").textContent="Copied"};
$("c_chk").onclick=async e=>{e.preventDefault();const m=$("c_chkmsg");m.textContent="checking…";
 try{const r=await post("/api/check-update");if(!r.ok)m.textContent="(couldn't reach GitHub, try again later)";
  else if(r.update&&r.update.latest){m.textContent="";await loadConfig();$("upd").scrollIntoView({behavior:"smooth"})}
  else m.textContent="(you have the latest version)"}catch(_){m.textContent="(the app isn't running)"}};
$("c_uninstall").onclick=async e=>{e.preventDefault();if(!confirm("Stop netmon and remove it from login items? Your logs in ~/.netmon are kept."))return;await post("/api/uninstall");document.body.innerHTML='<main><h1>Monitoring stopped</h1><p class="sub">It no longer runs at login. Your logs are still in ~/.netmon. To start again, open the EMIL NetMon app.</p></main>'};
const RUNGS=["wifi","router","uplink","internet","dns","web"];
function bdetail(c){const ind=(k,v)=>`<br><span style="padding-left:12px">${esc(k)}: ${v}</span>`;
 let s=`${c.checks} checks · <b>${c.failed} failed</b> · <b>${c.slow} slow</b>`;
 if(c.fail){const f=RUNGS.filter(k=>c.fail[k]).sort((a,b)=>c.fail[b]-c.fail[a]);if(f.length)s+="<br>Failed at:"+f.map(k=>ind(B.groups[k],c.fail[k])).join("");
  const sl=[["router","Router"],["net","Internet"],["web","Web"]].filter(([k])=>c.slow_at[k]).sort((a,b)=>c.slow_at[b[0]]-c.slow_at[a[0]]);if(sl.length)s+="<br>Slow at:"+sl.map(([k,n])=>ind(n,c.slow_at[k])).join("")}
 else if(c.failed)s+=`<br><span style="color:var(--muted)">No breakdown (app older than 0.9 or hub not updated)</span>`;
 return s+`<br>Router p95 (median): ${ms(c.router_p95)}`+(c.rssi!=null?` · Wi-Fi ${Math.round(c.rssi)} dBm`:"")}
let B=null;async function loadBuilding(){try{B=await (await fetch("/api/building")).json();renderBuilding()}catch(e){}}
function renderBuilding(){const box=$("building");if(!B||!B.configured){box.hidden=true;return}box.hidden=false;
 if(B.error){$("b_sub").textContent="Couldn't load the building view: "+B.error;["b_verdict","b_table"].forEach(i=>$(i).innerHTML="");$("b_hours").innerHTML="";$("b_grid").innerHTML="";return}
 $("b_sub").textContent=B.devices.length?`Hourly summaries from ${B.devices.length} apartment(s). Hover over the charts and table to see what failed. "Problem rate" = share of checks that failed or were slow.`:"";
 $("b_verdict").innerHTML=B.verdict.map(v=>`<li>${esc(v)}</li>`).join("");if(!B.devices.length)return;
 {const svg=$("b_hours");svg.innerHTML="";const W=1000,H=160,L=40,Bt=22,T=10;svg.setAttribute("viewBox",`0 0 ${W} ${H}`);const tk=niceTicks(Math.max(...B.by_hour.map(b=>b.rate),0.5)),top=tk[tk.length-1],bw=(W-L)/24;
  const g=el("g",{class:"grid"},svg),ax=el("g",{class:"axis"},svg);tk.forEach(v=>{const y=T+(H-T-Bt)*(1-v/top);el("line",{x1:L,x2:W,y1:y,y2:y},g);el("text",{x:L-6,y:y+4,"text-anchor":"end"},ax).textContent=v+"%"});
  B.by_hour.forEach(b=>{const hg=(H-T-Bt)*b.rate/top,x=L+b.hour*bw+2,w=bw-4;if(b.rate>0)el("path",{d:roundTop(x,H-Bt-hg,w,hg,4),fill:"var(--s1)"},svg);el("text",{x:x+w/2,y:H-6,"text-anchor":"middle"},ax).textContent=pad(b.hour);
   hover(el("rect",{x:L+b.hour*bw,y:T,width:bw,height:H-T-Bt,fill:"transparent"},svg),`<b>${pad(b.hour)}:00–${pad(b.hour)}:59, all days</b><br>${b.rate}% problem rate · ${b.apts_failed} of ${b.apts} apartment(s) had failures<br>${bdetail(b)}`)})}
 {const svg=$("b_grid");svg.innerHTML="";const labels=B.devices.map(d=>d.label),ids=B.devices.map(d=>d.device),cw=30,ch=20,L=90,T=18,W=L+24*cw,H=T+labels.length*ch+4;svg.setAttribute("viewBox",`0 0 ${W} ${H}`);svg.style.minWidth=W+"px";
  const mx=Math.max(1,...B.grid.map(c=>c.rate||0)),g=el("g",{class:"axis"},svg);for(let h=0;h<24;h+=2)el("text",{x:L+h*cw+cw/2,y:12,"text-anchor":"middle"},g).textContent=pad(h);
  labels.forEach((l,i)=>el("text",{x:L-8,y:T+i*ch+ch/2+4,"text-anchor":"end"},g).textContent=l);
  B.grid.forEach(c=>{const i=ids.indexOf(c.device),q=!c.rate?0:Math.min(5,1+Math.floor(4*c.rate/mx));hover(el("rect",{x:L+c.hour*cw+1,y:T+i*ch+1,width:cw-2,height:ch-2,rx:3,fill:`var(--q${q})`},svg),`<b>${esc(c.label)} · ${pad(c.hour)}:00–${pad(c.hour)}:59</b><br>${c.rate}% problem rate over ${c.hours} hour(s)<br>${bdetail(c)}`)})}
 const f=(v,u)=>v==null?"—":Math.round(v*10)/10+u;
 $("b_table").innerHTML="<tr><th>Apartment</th><th>Problem rate evening / rest</th><th>Router p95 evening / rest</th><th>Usually breaks at</th><th>Wi-Fi signal</th><th>Networks on same channel</th><th>Devices on LAN</th><th>Last report</th></tr>"+B.devices.map(d=>`<tr><td>${esc(d.label)}</td><td class="num">${f(d.rate_eve,"%")} / ${f(d.rate_other,"%")}</td><td class="num">${ms(d.router_eve)} / ${ms(d.router_other)}</td><td>${esc(d.breaks_at||"—")}</td><td class="num">${f(d.rssi," dBm")}</td><td class="num">${f(d.same_block,"")}</td><td class="num">${f(d.lan,"")}</td><td class="num">${new Date(d.last*1000).toLocaleString([], {month:"short",day:"numeric",hour:"2-digit",minute:"2-digit"})}</td></tr>`).join("");
 $("b_table").querySelectorAll("tr").forEach((tr,i)=>{if(i)hover(tr,`<b>${esc(B.devices[i-1].label)}, last ${B.days||7} days</b><br>${bdetail(B.devices[i-1].detail)}`)});
 const tg=B.together||[];$("b_tog_box").hidden=!tg.length;
 $("b_tog").innerHTML=tg.length?"<tr><th>Time</th><th>Failed / online</th><th>Apartments with failures (failed checks)</th></tr>"+tg.map(t=>`<tr><td class="num">${new Date(t.epoch*1000).toLocaleString([],{weekday:"short",month:"short",day:"numeric",hour:"2-digit",minute:"2-digit"})}</td><td class="num">${t.apts.length} of ${t.online}</td><td>${t.apts.slice(0,12).map(([l,n])=>esc(l)+" ("+n+")").join(", ")}${t.apts.length>12?` and ${t.apts.length-12} more`:""}</td></tr>`).join(""):""}
if(LIVE){loadConfig();setInterval(loadConfig,300000);$("controls").hidden=false;try{const s=JSON.parse(localStorage.getItem("netmon")||"{}");if(s.hours!=null)$("hours").value=s.hours;window._gw=s.gw||""}catch(e){}
 const save=()=>{try{localStorage.setItem("netmon",JSON.stringify({hours:$("hours").value,gw:$("gw").value}))}catch(e){}};
 $("hours").onchange=()=>{save();refresh()};$("gw").onchange=()=>{save();refresh()};
 (async()=>{await refresh();if(window._gw){$("gw").value=window._gw;if($("gw").value)refresh()}})();setInterval(refresh,10000)}
else render();
</script></body></html>"""

if __name__ == "__main__":
    main()
