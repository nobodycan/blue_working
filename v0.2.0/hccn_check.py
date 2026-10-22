#!/usr/bin/env python3
"""Ascend A2 HCCN evidence collection and explicit cross-node RoCE checks.

Python 3.8+, standard library only. Run on each HOST with the driver-provided
hccn_tool. Inventory reads configuration; it never assigns IPs or changes TLS.
Examples (output directories must be new):
  python3 hccn_check.py inventory --role P --out /tmp/P-hccn-001
  python3 hccn_check.py inventory --role D --out /tmp/D-hccn-001
Copy D's report.json to P using an approved file-transfer method, then:
  python3 hccn_check.py ping --role P --peer-report /tmp/D-report.json \
      --matrix all --out /tmp/P-to-D-001
Reverse roles for D-to-P. No IPv4/link UP -> SKIP, never a hardware verdict.

Once addresses and links are ready, stop workloads on the test NPUs and use two
host terminals. Start receiver FIRST; peer-ip is its NPU IP, not its host IP:
  python3 hccn_check.py bw-server --role D --device 0 --out /tmp/D-bw-001
  python3 hccn_check.py bw-client --role P --device 0 --peer-ip ACTUAL_D_NPU0_IP \
      --out /tmp/P-bw-001
Both ends must use identical --operation, --size, --iterations, --port, --tclass.
Defaults: send, 1048576 bytes, 1000 iterations, port 18515, existing TC policy.
Server/client describe test roles: for --operation read, data flows server to client.
Missing numeric BW report never passes, even if the command exits zero.
On interruption/timeout a device-side test may remain. After verifying the
selected card has ONLY your own roce_test, cleanup explicitly on that HOST:
  /usr/local/Ascend/driver/tools/hccn_tool -i CARD -roce_test reset
Reset terminates all roce_test processes on that card; this script never resets.

Return codes: 0=collection complete / all selected ping paths pass / positive BW;
2=incomplete or failed check; 1=invalid usage or runtime error; 130=interrupted.
Inventory exit 0 is NOT proof of RoCE availability. Inspect summary.txt/report.json.
Official A2 commands:
https://www.hiascend.com/doc_center/source/zh/HDK/2610/A2/A2HCCN/hccntoola2_005.html
https://www.hiascend.com/doc_center/source/zh/HDK/2610/A2/A2HCCN/hccntoola2_054.html
"""
import argparse
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import shlex
import shutil
import socket
import subprocess
import sys
import time

VERSION = "1.0.0"
QUERIES = {
    "ipv4": ["-ip", "-g"], "ipv6": ["-ip", "-inet6", "-g"],
    "link": ["-link", "-g"], "optical": ["-optical", "-g"],
    "lldp": ["-lldp", "-g"], "speed": ["-speed", "-g"],
    "netdetect": ["-netdetect", "-g"], "health": ["-net_health", "-g"],
    "tls": ["-tls", "-g"], "mtu": ["-mtu", "-g"],
}


def utc():
    return datetime.now(timezone.utc).isoformat()


def card(value):
    number = int(value)
    if not 0 <= number <= 255:
        raise ValueError("Device ID must be between 0 and 255")
    return number


def cards(value):
    result = [card(part.strip()) for part in value.split(",")]
    if not result or len(set(result)) != len(result):
        raise ValueError("Device IDs must be nonempty and unique")
    return result


def unicast4(value):
    addr = ipaddress.IPv4Address(value)
    if addr.is_unspecified or addr.is_multicast or addr.is_loopback or int(addr) == 0xFFFFFFFF:
        raise ValueError("Expected a non-loopback unicast NPU IPv4 address")
    return str(addr)


def text(value):
    return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else (value or "")


def execute(root, label, command, timeout):
    started = utc()
    begin = time.monotonic()
    record = dict(argv=command, started_utc=started, returncode=None,
                  timed_out=False, interrupted=False, error=None, output="")
    path = root / "raw" / (label + ".log")
    path.parent.mkdir(parents=True, exist_ok=True)
    running_header = json.dumps(dict(argv=command, started_utc=started, status="RUNNING")) + "\n\n"
    print("RUN", label, "LOG", path, flush=True)
    with path.open("w", encoding="utf-8") as log:
        log.write(running_header)
        log.flush()
        try:
            process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)
            try:
                record["returncode"] = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
                record.update(timed_out=True, error="command timeout; device-side test may remain")
            except KeyboardInterrupt:
                process.kill()
                process.wait()
                record.update(interrupted=True, error="interrupted; device-side test may remain")
        except OSError as exc:
            record["error"] = repr(exc)
    record["output"] = path.read_text(encoding="utf-8", errors="replace")[len(running_header):]
    record["elapsed_s"] = round(time.monotonic() - begin, 6)
    header = {k: v for k, v in record.items() if k != "output"}
    path.write_text(json.dumps(header, indent=2) + "\n\n" + record["output"], encoding="utf-8")
    record["raw_file"] = str(path)
    if record["interrupted"]:
        raise KeyboardInterrupt
    return record


def successful(record):
    return record["returncode"] == 0 and not record["timed_out"] and not record["error"]


def ipv4_state(record):
    value = record["output"]
    if re.search(r"no\s+ip\s+was\s+preset", value, re.I):
        return dict(status="UNCONFIGURED", addresses=[])
    addresses = []
    for match in re.finditer(r"(?im)^\s*ipaddr\s*:\s*(\d+\.\d+\.\d+\.\d+)\b", value):
        try:
            addresses.append(unicast4(match[1]))
        except ValueError:
            pass
    return dict(status="CONFIGURED" if successful(record) and addresses else "UNKNOWN",
                addresses=sorted(set(addresses)))


def link_state(record):
    match = re.search(r"\blink\s+status\s*:\s*(UP|DOWN)\b", record["output"], re.I)
    return match[1].upper() if successful(record) and match else "UNKNOWN"


def optical_state(record):
    match = re.search(r"(?im)^\s*present\s*:\s*(not present|present)\s*$", record["output"], re.I)
    if not successful(record) or not match:
        return "UNKNOWN"
    return "PRESENT" if match[1].lower() == "present" else "NOT_PRESENT"


def probe(args, root, tool, device, names):
    records = {name: execute(root, "device%d-%s" % (device, name),
                            [tool, "-i", str(device)] + QUERIES[name], args.query_timeout)
               for name in names}
    ip = ipv4_state(records["ipv4"])
    addresses = ip["addresses"]
    reserved = [addr for addr in addresses if addr.startswith(("192.168.1.", "192.168.2."))]
    return dict(device=device, ipv4=ip, link=link_state(records["link"]),
                a2_internal_range_warning=reserved,
                optical=optical_state(records["optical"]) if "optical" in records else "NOT_QUERIED",
                queries=records)


def save(root, report, lines):
    report["finished_utc"] = utc()
    (root / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    content = "\n".join(lines) + "\n"
    (root / "summary.txt").write_text(content, encoding="utf-8")
    print(content, end="")
    print("REPORT", root / "report.json")


def inventory(args, root, tool, report):
    smi = shutil.which("npu-smi") or "/usr/local/bin/npu-smi"
    report["npu_smi"] = execute(root, "npu-smi", [smi, "info"], args.query_timeout)
    driver = Path("/usr/local/Ascend/driver/version.info")
    try:
        data = driver.read_bytes()
        (root / "driver-version.info").write_bytes(data)
        report["driver_version_file"] = dict(bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
    except OSError as exc:
        report["driver_version_file"] = dict(error=repr(exc))
    conf = Path("/etc/hccn.conf")
    try:
        data = conf.read_bytes()
        (root / "hccn.conf.snapshot").write_bytes(data)
        report["hccn_conf"] = dict(bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
    except OSError as exc:
        report["hccn_conf"] = dict(error=repr(exc))
    report["devices"] = [probe(args, root, tool, d, QUERIES) for d in args.devices]
    report["hardware_roce_capability"] = "UNVERIFIED_CHECK_PRODUCT_SKU_AND_PORTS"
    report["roce_interconnect"] = "NOT_TESTED"
    lines = ["HCCN inventory: host=%s role=%s" % (report["host"], args.role),
             "CARD  IPv4          LINK     OPTICAL       A2_INTERNAL_RANGE"]
    for row in report["devices"]:
        lines.append("%-5s %-13s %-8s %-13s %s" % (row["device"], row["ipv4"]["status"],
            row["link"], row["optical"], bool(row["a2_internal_range_warning"])))
    lines += ["IPv4 missing is not proof that hardware lacks RoCE.",
              "LINK UP is local physical-link evidence, not proof of peer RoCE transfer.",
              "IPv6/LLDP/speed/health/TLS/MTU and exact failures are in raw/ and report.json.",
              "Optical NOT_PRESENT/UNKNOWN needs cable/port and driver-context review.",
              "net_health Init/timeout may reflect an unset/unreachable netdetect target.",
              "Collection finished; no RoCE pass verdict is issued by inventory."]
    save(root, report, lines)
    return 0


def ping_state(record):
    match = re.search(r"(\d+)\s+packets transmitted,\s*(\d+)\s+(?:packets\s+)?received,"
                      r"\s*([0-9.]+)%\s*packet loss", record["output"], re.I)
    if not successful(record):
        return "COMMAND_FAILED"
    if not match:
        return "UNKNOWN_OUTPUT"
    sent, received, loss = int(match[1]), int(match[2]), float(match[3])
    if sent > 0 and received == sent and loss == 0:
        return "PASS"
    return "LOSS_OR_FAILURE"


def ping(args, root, tool, report):
    peer = json.loads(args.peer_report.read_text(encoding="utf-8"))
    if not isinstance(peer, dict) or peer.get("tool") != "hccn_check" or peer.get("mode") != "inventory":
        raise ValueError("--peer-report must be the other host's inventory report.json")
    remote = {card(row["device"]): row for row in peer["devices"]}
    if not remote:
        raise ValueError("Peer inventory contains no device rows")
    report["peer"] = {key: peer.get(key) for key in ("host", "role", "started_utc")}
    report["peer_report_sha256"] = hashlib.sha256(args.peer_report.read_bytes()).hexdigest()
    report["cross_host_identity_verified"] = False
    report["matrix"] = args.matrix
    local = [probe(args, root, tool, d, ("ipv4", "link")) for d in args.devices]
    report["devices"] = local
    results = []
    for own in local:
        targets = sorted(remote) if args.matrix == "all" else [own["device"]]
        for device in targets:
            other = remote.get(device)
            row = dict(local_device=own["device"], peer_device=device, status="SKIP_CONFIG")
            ready = other and all(r["ipv4"]["status"] == "CONFIGURED" and r["link"] == "UP"
                                 and not r.get("a2_internal_range_warning") for r in (own, other))
            if ready and len(other["ipv4"]["addresses"]) == 1:
                address = unicast4(other["ipv4"]["addresses"][0])
                if address in {ip for item in local for ip in item["ipv4"]["addresses"]}:
                    row.update(status="SKIP_LOCAL_ADDRESS", peer_ip=address)
                else:
                    command = [tool, "-i", str(own["device"]), "-ping", "-g",
                               "address", address, "pkt", "128"]
                    result = execute(root, "ping-%d-to-%d" % (own["device"], device),
                                     command, args.query_timeout)
                    row.update(peer_ip=address, status=ping_state(result), command=result)
            else:
                row["reason"] = "Need both IPv4s, links UP and one peer address; regenerate inventories after configuration changes"
            results.append(row)
            print("PAIR", row["local_device"], "->", row["peer_device"], row["status"], flush=True)
    report["pairs"] = results
    passed = sum(row["status"] == "PASS" for row in results)
    report["all_selected_ping_paths_pass"] = bool(results) and passed == len(results)
    save(root, report, ["PING paths: %d/%d PASS" % (passed, len(results)),
        "SKIP/UNKNOWN/command failure does not prove hardware unsupported.",
        "Confirm two separate hosts and fresh peer inventories; hostnames alone are not identity proof.",
        "Ping is not an RDMA bandwidth test or a KV-transfer success verdict."])
    return 0 if report["all_selected_ping_paths_pass"] else 2


def bandwidth_rows(record, size, iterations):
    rows, unit = [], None
    for line in record["output"].splitlines():
        if "#bytes" in line and "BW average" in line:
            match = re.search(r"BW\s+average\[([^\]]+)\]", line)
            unit = match[1] if match else None
            continue
        parts = line.split()
        if not unit or len(parts) < 5:
            continue
        try:
            values = [float(part) for part in parts[:5]]
        except ValueError:
            continue
        if all(math.isfinite(v) for v in values) and values[0] == size and values[1] == iterations:
            rows.append(dict(bytes=size, iterations=iterations, peak=values[2],
                             average=values[3], message_rate=values[4], unit=unit))
    return rows


def bandwidth(args, root, tool, report):
    own = probe(args, root, tool, args.device, ("ipv4", "link"))
    report["device"] = own
    if own["ipv4"]["status"] != "CONFIGURED" or own["link"] != "UP" or own["a2_internal_range_warning"]:
        report["bandwidth_status"] = "SKIP_CONFIG"
        save(root, report, ["SKIP_CONFIG: need usable NPU IPv4 and link UP before RoCE testing."])
        return 2
    command = [tool, "-i", str(args.device), "-roce_test", "ib_%s_bw" % args.operation,
               "-s", str(args.size), "-n", str(args.iterations), "-p", str(args.port)]
    if args.tclass is not None:
        command += ["-tclass", str(args.tclass)]
    if args.mode == "bw-client":
        address = unicast4(args.peer_ip)
        if address in own["ipv4"]["addresses"]:
            raise ValueError("Peer IP equals selected local NPU IP")
        report["peer_ip"] = address
        command += ["address", address]
    command += ["-tcp"]
    print("Stop workloads on the selected NPUs. Both ends must use matching parameters.", flush=True)
    print("NATIVE COMMAND:", " ".join(shlex.quote(part) for part in command), flush=True)
    print("Receiver must be started first. Timeout/interruption may require explicit cleanup of your device-side test.", flush=True)
    result = execute(root, "roce-bw", command, args.test_timeout)
    report["command"] = result
    report["operation"] = args.operation
    rows = bandwidth_rows(result, args.size, args.iterations)
    report["bandwidth_rows"] = rows
    has_report = bool(re.search(r"roce_report\s*:", result["output"], re.I))
    known_unit = all(row["unit"] in ("MB/sec", "MB/s", "GB/sec", "GB/s", "Gb/sec", "Gb/s") for row in rows)
    positive = successful(result) and has_report and bool(rows) and known_unit and all(row["average"] > 0 for row in rows)
    report["bandwidth_status"] = "BW_POSITIVE" if positive else "BW_NOT_CONFIRMED"
    report["cross_host_identity_verified"] = False
    lines = [report["bandwidth_status"], "Operation: " + args.operation,
             "Numeric rows: " + json.dumps(rows),
             "Correlate BOTH host reports and verify peer IP/device/operation; this is one selected path only.",
             "-tcp selects test connection setup; the requested ib_*_bw operation is the RoCE test.",
             "No automatic reset was performed. On abnormal exit, inspect and clean up only your own test."]
    save(root, report, lines)
    return 0 if positive else 2


def parser():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="mode", required=True)
    for mode in ("inventory", "ping", "bw-server", "bw-client"):
        q = sub.add_parser(mode)
        q.add_argument("--hccn", help="Path to this host driver's hccn_tool")
        q.add_argument("--role", choices=("P", "D", "HOST"), default="HOST")
        q.add_argument("--out", type=Path, required=True, help="New output directory; never overwrites a run")
        q.add_argument("--query-timeout", type=float, default=15)
        if mode in ("inventory", "ping"):
            q.add_argument("--devices", default="0,1,2,3,4,5,6,7")
        if mode == "ping":
            q.add_argument("--peer-report", type=Path, required=True)
            q.add_argument("--matrix", choices=("all", "same"), default="all")
        if mode.startswith("bw-"):
            q.add_argument("--device", type=int, required=True)
            q.add_argument("--operation", choices=("send", "read", "write"), default="send")
            q.add_argument("--size", type=int, default=1048576)
            q.add_argument("--iterations", type=int, default=1000)
            q.add_argument("--port", type=int, default=18515)
            q.add_argument("--tclass", type=int)
            q.add_argument("--test-timeout", type=float, default=180)
        if mode == "bw-client":
            q.add_argument("--peer-ip", required=True)
    return p


def main():
    args = parser().parse_args()
    if args.query_timeout <= 0 or not math.isfinite(args.query_timeout):
        raise ValueError("--query-timeout must be finite and positive")
    if hasattr(args, "devices"):
        args.devices = cards(args.devices)
    if args.mode.startswith("bw-"):
        args.device = card(args.device)
        if not (1 <= args.size <= 874736000 and 5 <= args.iterations <= 20000 and 4097 <= args.port <= 65535):
            raise ValueError("Invalid size/iterations/port; see A2 HCCN reference")
        if args.tclass is not None and not 0 <= args.tclass <= 255:
            raise ValueError("--tclass must be 0..255 and match the actual network policy")
        if args.test_timeout <= 0 or not math.isfinite(args.test_timeout):
            raise ValueError("--test-timeout must be finite and positive")
    candidates = [args.hccn] if args.hccn else [shutil.which("hccn_tool"), "/usr/local/Ascend/driver/tools/hccn_tool"]
    tool = next((str(Path(x).resolve()) for x in candidates if x and Path(x).is_file() and os.access(x, os.X_OK)), None)
    if not tool:
        raise ValueError("hccn_tool unavailable: use the matching host driver installation; this is not a hardware verdict")
    args.out.mkdir(parents=True, exist_ok=False)
    report = dict(tool="hccn_check", version=VERSION, mode=args.mode, role=args.role,
                  host=socket.gethostname(), uid=os.geteuid() if hasattr(os, "geteuid") else None,
                  started_utc=utc(), hccn=tool, platform=sys.platform,
                  hardware_support_not_inferred_from_query_failures=True)
    if report["uid"] not in (0, None):
        print("NOTE: non-root; permission failures are UNKNOWN, not lack of hardware support.", flush=True)
    if args.mode == "inventory":
        return inventory(args, args.out, tool, report)
    if args.mode == "ping":
        return ping(args, args.out, tool, report)
    return bandwidth(args, args.out, tool, report)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("Interrupted. Partial raw logs may exist. For RoCE tests, inspect device-side test state before explicit cleanup.", file=sys.stderr)
        sys.exit(130)
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print("ERROR:", exc, file=sys.stderr)
        sys.exit(1)
