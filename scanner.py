"""LAN scanner for IP cameras and DVR/NVR devices."""
import ipaddress
import re
import socket
import subprocess
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests
from requests.auth import HTTPBasicAuth, HTTPDigestAuth

CAMERA_PORTS = [80, 81, 82, 443, 554, 8000, 8080, 8081, 8899, 9000, 37777, 34567]

VENDOR_SIGNATURES = {
    "hikvision": ["hikvision", "app-webs", "dnvrs-webs", "web components"],
    "dahua": ["dahua", "webs", "device webservice"],
    "axis": ["axis"],
    "foscam": ["foscam", "netwave"],
    "tplink": ["tp-link", "tplink"],
    "reolink": ["reolink"],
    "uniview": ["uniview", "webserver"],
    "amcrest": ["amcrest"],
    "tiandy": ["tiandy"],
    "vivotek": ["vivotek"],
}

VENDOR_PROBES = {
    "hikvision": [
        ("/ISAPI/System/deviceInfo", "digest"),
        ("/ISAPI/Security/userCheck", "digest"),
    ],
    "dahua": [
        ("/cgi-bin/magicBox.cgi?action=getSystemInfo", "digest"),
        ("/cgi-bin/magicBox.cgi?action=getDeviceType", "digest"),
    ],
    "generic": [
        ("/", "basic"),
        ("/", "digest"),
    ],
}


def detect_local_subnet() -> str:
    """Return the primary LAN subnet in CIDR, e.g. '192.168.1.0/24'."""
    try:
        out = subprocess.run(
            ["ip", "-4", "-o", "addr", "show"],
            capture_output=True, text=True, check=True, timeout=5,
        ).stdout
        for line in out.splitlines():
            if "tailscale" in line or " lo " in line:
                continue
            m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)/(\d+)", line)
            if m:
                net = ipaddress.ip_network(f"{m.group(1)}/{m.group(2)}", strict=False)
                return str(net)
    except Exception:
        pass
    return "192.168.1.0/24"


def get_tailscale_ip() -> str:
    try:
        r = subprocess.run(
            ["tailscale", "ip", "-4"],
            capture_output=True, text=True, check=True, timeout=5,
        )
        return r.stdout.strip().splitlines()[0]
    except Exception:
        return ""


def nmap_scan(subnet: str) -> list[dict]:
    """Return list of {ip, ports:[int]} for live hosts with any camera port open."""
    ports_arg = ",".join(str(p) for p in CAMERA_PORTS)
    try:
        out = subprocess.run(
            ["nmap", "-Pn", "-T4", "--open", "-p", ports_arg,
             "-oX", "-", subnet],
            capture_output=True, text=True, check=True, timeout=600,
        ).stdout
    except subprocess.TimeoutExpired:
        return []
    except Exception as e:
        return [{"error": str(e)}]

    hosts = []
    try:
        root = ET.fromstring(out)
    except ET.ParseError:
        return []
    for host in root.findall("host"):
        addr = host.find("address[@addrtype='ipv4']")
        if addr is None:
            continue
        ip = addr.get("addr")
        open_ports = []
        for port in host.findall(".//port"):
            state = port.find("state")
            if state is not None and state.get("state") == "open":
                open_ports.append(int(port.get("portid")))
        if open_ports:
            hosts.append({"ip": ip, "ports": sorted(open_ports)})
    return hosts


def _http_fingerprint(ip: str, port: int, timeout: float = 3.0) -> dict:
    """Return a brief HTTP fingerprint {server, title, vendor, status}."""
    scheme = "https" if port in (443,) else "http"
    url = f"{scheme}://{ip}:{port}/"
    info = {"server": "", "title": "", "vendor": "", "status": 0, "realm": ""}
    try:
        r = requests.get(url, timeout=timeout, verify=False, allow_redirects=False)
        info["status"] = r.status_code
        info["server"] = r.headers.get("Server", "")
        www_auth = r.headers.get("WWW-Authenticate", "")
        m = re.search(r'realm="([^"]+)"', www_auth)
        if m:
            info["realm"] = m.group(1)
        text = r.text[:4096].lower() if r.text else ""
        haystack = " ".join([info["server"].lower(), info["realm"].lower(), text])
        for vendor, needles in VENDOR_SIGNATURES.items():
            if any(n in haystack for n in needles):
                info["vendor"] = vendor
                break
        m = re.search(r"<title>([^<]+)</title>", r.text or "", re.I)
        if m:
            info["title"] = m.group(1).strip()[:120]
    except Exception as e:
        info["error"] = type(e).__name__
    return info


def fingerprint_hosts(hosts: list[dict]) -> list[dict]:
    """Add HTTP fingerprints to each host (per http-ish port)."""
    http_ports = {80, 81, 82, 443, 8000, 8080, 8081, 8899, 34567}

    def _one(host):
        host["fingerprints"] = {}
        for port in host["ports"]:
            if port in http_ports:
                host["fingerprints"][port] = _http_fingerprint(host["ip"], port)
        hints = {fp.get("vendor") for fp in host["fingerprints"].values()}
        hints.discard("")
        host["vendor_guess"] = next(iter(hints), "")
        host["is_camera_candidate"] = (
            bool(hints)
            or 554 in host["ports"]
            or 37777 in host["ports"]
            or 34567 in host["ports"]
        )
        return host

    with ThreadPoolExecutor(max_workers=16) as ex:
        return list(ex.map(_one, hosts))


def _try_auth(url: str, user: str, password: str, scheme: str,
              timeout: float = 5.0) -> tuple[bool, int, str]:
    auth = HTTPDigestAuth(user, password) if scheme == "digest" else HTTPBasicAuth(user, password)
    try:
        r = requests.get(url, auth=auth, timeout=timeout, verify=False)
        snippet = (r.text or "")[:400]
        return (r.status_code == 200, r.status_code, snippet)
    except Exception as e:
        return (False, 0, f"err: {type(e).__name__}")


def probe_credentials(ip: str, port: int, user: str, password: str,
                      vendor_guess: str = "") -> dict:
    """Try HTTP auth against known vendor endpoints; return {ok, endpoint, status, info}."""
    scheme = "https" if port == 443 else "http"
    base = f"{scheme}://{ip}:{port}"

    probes = []
    if vendor_guess and vendor_guess in VENDOR_PROBES:
        probes += VENDOR_PROBES[vendor_guess]
    for v, p_list in VENDOR_PROBES.items():
        if v == vendor_guess:
            continue
        probes += p_list

    seen = set()
    for path, auth_scheme in probes:
        key = (path, auth_scheme)
        if key in seen:
            continue
        seen.add(key)
        ok, status, snippet = _try_auth(f"{base}{path}", user, password, auth_scheme)
        if ok:
            return {
                "ok": True,
                "endpoint": path,
                "status": status,
                "auth": auth_scheme,
                "info": snippet,
            }
    return {"ok": False, "endpoint": "", "status": 0, "auth": "", "info": "no endpoint accepted credentials"}


def _socat_targets_from_proc() -> dict:
    """Scan /proc for socat processes; return {listen_port: 'target_ip:port'}."""
    out = {}
    try:
        for pdir in Path("/proc").glob("[0-9]*"):
            try:
                cmdline = (pdir / "cmdline").read_bytes().replace(b"\x00", b" ").decode(errors="replace")
            except Exception:
                continue
            if "socat" not in cmdline:
                continue
            m1 = re.search(r'(TCP4?|UDP4?)[-_]?LISTEN:(\d+)', cmdline, re.I)
            m2 = re.search(r'(TCP4?|UDP4?):([\d.]+):(\d+)', cmdline)
            if m1 and m2:
                out[int(m1.group(2))] = {
                    "target_ip": m2.group(2),
                    "target_port": int(m2.group(3)),
                    "proto": m1.group(1).lower().rstrip("4") or "tcp",
                    "pid": int(pdir.name),
                }
    except Exception:
        pass
    return out


def detect_existing_forwards(tailscale_ip: str) -> list[dict]:
    """List every TCP/UDP port listening on tailscale/wildcard addrs.
    Includes socat/nginx/haproxy/whatever is already running on the host."""
    out = ""
    for prefix in [["sudo", "-n"], []]:
        try:
            r = subprocess.run(
                prefix + ["ss", "-H", "-tulnp"],
                capture_output=True, text=True, check=True, timeout=5,
            )
            out = r.stdout
            break
        except Exception:
            continue

    socat_targets = _socat_targets_from_proc()

    results = []
    seen = set()
    for line in out.splitlines():
        parts = line.split(None, 6)
        if len(parts) < 5:
            continue
        proto = parts[0].lower()
        if proto not in ("tcp", "udp"):
            continue
        local = parts[4]
        proc_field = parts[6] if len(parts) > 6 else ""

        if ":" not in local or local.startswith("["):
            continue
        ip, _, port_s = local.rpartition(":")
        try:
            port = int(port_s)
        except ValueError:
            continue
        if ip not in (tailscale_ip, "0.0.0.0", "*"):
            continue
        key = (proto, port)
        if key in seen:
            continue
        seen.add(key)

        procs = re.findall(r'"([^"]+)",pid=(\d+)', proc_field)
        proc_names, proc_pids = [], []
        for name, pid in procs:
            if name not in proc_names:
                proc_names.append(name)
                proc_pids.append(int(pid))

        target_info = socat_targets.get(port, {})
        target = ""
        if target_info and target_info.get("proto", "tcp") == proto:
            target = f"{target_info['target_ip']}:{target_info['target_port']}"

        results.append({
            "port": port,
            "proto": proto,
            "bind": ip,
            "process": ", ".join(proc_names) or "unknown",
            "pids": proc_pids,
            "target": target,
        })

    results.sort(key=lambda x: (x["port"], x["proto"]))
    return results


def scan(subnet: str = "") -> dict:
    subnet = subnet or detect_local_subnet()
    hosts = nmap_scan(subnet)
    hosts = fingerprint_hosts(hosts)
    cameras = [h for h in hosts if h.get("is_camera_candidate")]
    return {
        "subnet": subnet,
        "tailscale_ip": get_tailscale_ip(),
        "total_hosts": len(hosts),
        "cameras": cameras,
        "others": [h for h in hosts if not h.get("is_camera_candidate")],
    }


if __name__ == "__main__":
    import json
    print(json.dumps(scan(), indent=2))
