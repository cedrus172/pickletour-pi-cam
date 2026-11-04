"""LAN scanner for IP cameras/DVRs — Windows version (psutil + nmap.exe)."""
import ipaddress
import os
import re
import socket
import subprocess
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import psutil
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

# Dò nmap trong các vị trí phổ biến trên Windows
_NMAP_PATHS = [
    r"C:\Program Files (x86)\Nmap\nmap.exe",
    r"C:\Program Files\Nmap\nmap.exe",
    "nmap",  # trong PATH
]

# Dò tailscale CLI
_TAILSCALE_PATHS = [
    r"C:\Program Files\Tailscale\tailscale.exe",
    r"C:\Program Files (x86)\Tailscale\tailscale.exe",
    "tailscale",
]


def _find_executable(candidates: list) -> str:
    for p in candidates:
        try:
            if "\\" in p or "/" in p:
                if os.path.exists(p):
                    return p
            else:
                # Kiểm tra trong PATH bằng cách gọi thử
                subprocess.run([p, "--version"], capture_output=True, timeout=3)
                return p
        except Exception:
            continue
    return ""


def detect_local_subnet() -> str:
    """Return the primary LAN subnet in CIDR via psutil (OS-agnostic)."""
    try:
        for name, addrs in psutil.net_if_addrs().items():
            low = name.lower()
            if "tailscale" in low or "loopback" in low or "vmware" in low or "virtualbox" in low:
                continue
            # Loại interface không up
            stats = psutil.net_if_stats().get(name)
            if stats and not stats.isup:
                continue
            for a in addrs:
                if a.family != socket.AF_INET:
                    continue
                if not a.address or a.address.startswith("127.") or a.address.startswith("169.254."):
                    continue
                if not a.netmask:
                    continue
                try:
                    net = ipaddress.ip_network(f"{a.address}/{a.netmask}", strict=False)
                    return str(net)
                except Exception:
                    continue
    except Exception:
        pass
    return "192.168.1.0/24"


def get_tailscale_ip() -> str:
    exe = _find_executable(_TAILSCALE_PATHS)
    if not exe:
        return ""
    try:
        r = subprocess.run([exe, "ip", "-4"], capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            return r.stdout.strip().splitlines()[0]
    except Exception:
        pass
    return ""


class ScanError(RuntimeError):
    """Scanner không hoạt động được — caller bắt rồi trả JSON {error:...} cho UI."""


def nmap_scan(subnet: str) -> list:
    """Return list of {ip, ports:[int]} for live hosts với port camera mở.
    Raise ScanError nếu nmap missing/lỗi — không nhét error vào list."""
    exe = _find_executable(_NMAP_PATHS)
    if not exe:
        raise ScanError(
            "nmap chưa được cài. Mở PowerShell admin: "
            "winget install -e --id Insecure.Nmap  "
            "(hoặc tải từ https://nmap.org/download.html)")
    ports_arg = ",".join(str(p) for p in CAMERA_PORTS)
    # -sT = TCP connect scan user-space (không cần Npcap/raw socket).
    # -Pn = bỏ ping (nhiều cam chặn ICMP). -n = skip DNS. -T4 = aggressive.
    args = [exe, "-sT", "-Pn", "-n", "-T4", "--open",
            "-p", ports_arg, "-oX", "-", subnet]
    try:
        out = subprocess.run(
            args, capture_output=True, text=True, check=True, timeout=600,
            creationflags=0x08000000 if os.name == "nt" else 0,
        ).stdout
    except subprocess.TimeoutExpired:
        raise ScanError(f"nmap quá thời gian (10 phút) cho subnet {subnet}")
    except subprocess.CalledProcessError as e:
        err = (e.stderr or "")[:400]
        hint = ""
        if "pcap" in err.lower() or "npcap" in err.lower():
            hint = "  → Nếu cần scan nhanh hơn, cài Npcap: https://npcap.com/dist/npcap-1.79.exe"
        raise ScanError(f"nmap rc={e.returncode}: {err}{hint}")
    except Exception as e:
        raise ScanError(f"nmap lỗi: {type(e).__name__}: {e}")

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


def fingerprint_hosts(hosts: list) -> list:
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


def _try_auth(url: str, user: str, password: str, scheme: str, timeout: float = 5.0):
    auth = HTTPDigestAuth(user, password) if scheme == "digest" else HTTPBasicAuth(user, password)
    try:
        r = requests.get(url, auth=auth, timeout=timeout, verify=False)
        snippet = (r.text or "")[:400]
        return (r.status_code == 200, r.status_code, snippet)
    except Exception as e:
        return (False, 0, f"err: {type(e).__name__}")


def probe_credentials(ip: str, port: int, user: str, password: str,
                      vendor_guess: str = "") -> dict:
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
            return {"ok": True, "endpoint": path, "status": status,
                    "auth": auth_scheme, "info": snippet}
    return {"ok": False, "endpoint": "", "status": 0, "auth": "",
            "info": "no endpoint accepted credentials"}


def detect_existing_forwards(tailscale_ip: str) -> list:
    """List mọi TCP/UDP listener trên tailscale/wildcard IP (dùng psutil).
    Trên Windows không có socat → process nào đang listen cũng show."""
    results = []
    seen = set()
    try:
        conns = psutil.net_connections(kind="inet")
    except (psutil.AccessDenied, PermissionError):
        # Không có quyền list socket mọi process → return empty.
        # Trên Windows, install.ps1 chạy service as SYSTEM nên có quyền;
        # dev trên Mac/Linux non-root sẽ không thấy socket người khác.
        return []

    for c in conns:
        if not c.laddr:
            continue
        # TCP listen state hoặc UDP (UDP không có state)
        if c.type == socket.SOCK_STREAM and c.status != psutil.CONN_LISTEN:
            continue
        ip = c.laddr.ip
        port = c.laddr.port
        if ip not in (tailscale_ip, "0.0.0.0", "::"):
            continue
        proto = "tcp" if c.type == socket.SOCK_STREAM else "udp"
        key = (proto, port)
        if key in seen:
            continue
        seen.add(key)

        pname, pid = "unknown", c.pid or 0
        try:
            if pid:
                pname = psutil.Process(pid).name()
        except Exception:
            pass

        results.append({
            "port": port, "proto": proto, "bind": ip,
            "process": pname, "pids": [pid] if pid else [],
            "target": "",  # không detect được target của process ngoài
        })
    results.sort(key=lambda x: (x["port"], x["proto"]))
    return results


def scan(subnet: str = "") -> dict:
    subnet = subnet or detect_local_subnet()
    try:
        hosts = nmap_scan(subnet)
    except ScanError as e:
        return {
            "subnet": subnet,
            "tailscale_ip": get_tailscale_ip(),
            "total_hosts": 0,
            "cameras": [], "others": [],
            "error": str(e),
        }
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
