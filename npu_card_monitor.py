#!/usr/bin/env python3
"""Local-only web UI and SSH scanner for NPU card availability."""

from __future__ import annotations

import json
import logging
import re
import socket
import argparse
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


BASE_DIR = Path(__file__).resolve().parent
HTML_FILE = BASE_DIR / "npu_host_config.html"
CONFIG_FILE = BASE_DIR / "npu-hosts.json"
SSH_PYTHON_PACKAGES = Path(r"D:\workspace\ssh\temp\python_packages")

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
MAX_HOSTS = 64
MAX_REQUEST_BYTES = 1024 * 1024
MAX_COMMAND_OUTPUT_BYTES = 128 * 1024
MAX_SSH_WORKERS = 8
SSH_CONNECT_TIMEOUT_SECONDS = 8
SSH_COMMAND_TIMEOUT_SECONDS = 15
IDLE_MAX_UTIL_PERCENT = 5
IDLE_MIN_FREE_HBM_RATIO = 0.20
IPV4_PATTERN = re.compile(r"^(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)$")
DEVICE_ROW_PATTERN = re.compile(r"^\s*\|\s*(\d+)\s*\|\s*([^|]+)\|\s*([^|]+)\|\s*(.*?)\|\s*$")
MEMORY_PAIR_PATTERN = re.compile(r"(\d+)\s*/\s*(\d+)")

if SSH_PYTHON_PACKAGES.is_dir() and str(SSH_PYTHON_PACKAGES) not in sys.path:
    sys.path.insert(0, str(SSH_PYTHON_PACKAGES))

try:
    import paramiko
except ImportError as exc:  # pragma: no cover - environment setup diagnostic
    raise SystemExit(
        "Paramiko is required. Install it in this Python environment or place it in "
        f"{SSH_PYTHON_PACKAGES}."
    ) from exc


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
LOGGER = logging.getLogger("npu-card-monitor")
logging.getLogger("paramiko").setLevel(logging.WARNING)
LATEST_SCAN: dict[str, Any] | None = None
LATEST_SCAN_LOCK = threading.Lock()


class HostConfigError(ValueError):
    pass


def validate_hosts(payload: Any) -> list[dict[str, str]]:
    if not isinstance(payload, dict) or not isinstance(payload.get("hosts"), list):
        raise HostConfigError("请求必须包含 hosts 数组。")
    hosts = payload["hosts"]
    if not hosts or len(hosts) > MAX_HOSTS:
        raise HostConfigError(f"机器数量必须在 1 到 {MAX_HOSTS} 台之间。")

    clean_hosts = []
    for host in hosts:
        if not isinstance(host, dict):
            raise HostConfigError("机器配置格式错误。")
        ip = host.get("ip")
        username = host.get("username")
        password = host.get("password")
        if not all(isinstance(value, str) for value in (ip, username, password)):
            raise HostConfigError("每台机器都需要填写 IP、用户名和密码。")
        ip = ip.strip()
        username = username.strip()
        if not IPV4_PATTERN.fullmatch(ip) or not username or not password:
            raise HostConfigError("存在无效 IP、空用户名或空密码。")
        clean_hosts.append({"ip": ip, "username": username, "password": password})
    return clean_hosts


def load_saved_config() -> dict[str, list[dict[str, str]]]:
    try:
        payload = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        return {"hosts": validate_hosts(payload)}
    except FileNotFoundError:
        return {"hosts": []}
    except (json.JSONDecodeError, HostConfigError):
        LOGGER.warning("Saved host configuration is invalid; use import or enter hosts in the page.")
        return {"hosts": []}


def parse_npu_smi(output: str) -> list[dict[str, Any]]:
    lines = output.splitlines()
    cards: list[dict[str, Any]] = []
    in_card_table = False
    for line_index, line in enumerate(lines):
        if "NPU ID" in line and "Name" in line and "Health" in line:
            in_card_table = True
            continue
        if "Process id" in line or "Process name" in line:
            in_card_table = False
        if not in_card_table:
            continue
        match = DEVICE_ROW_PATTERN.match(line)
        if not match:
            continue
        card_id, name, health, _ = match.groups()
        util_percent = None
        hbm_used_mb = None
        hbm_total_mb = None

        for detail_line in lines[line_index + 1 : line_index + 4]:
            cells = [cell.strip() for cell in detail_line.strip().strip("|").split("|")]
            if len(cells) < 4 or cells[0].isdigit():
                continue
            payload = cells[-1]
            util_match = re.match(r"\s*(\d+)", payload)
            memory_pairs = MEMORY_PAIR_PATTERN.findall(payload)
            if util_match and memory_pairs:
                util_percent = int(util_match.group(1))
                hbm_used_mb, hbm_total_mb = map(int, memory_pairs[-1])
                break

        hbm_free_mb = None
        free_ratio = None
        if hbm_used_mb is not None and hbm_total_mb:
            hbm_free_mb = max(hbm_total_mb - hbm_used_mb, 0)
            free_ratio = hbm_free_mb / hbm_total_mb
        health_ok = health.strip().casefold() in {"ok", "healthy"}
        is_idle = (
            health_ok
            and util_percent is not None
            and util_percent <= IDLE_MAX_UTIL_PERCENT
            and free_ratio is not None
            and free_ratio >= IDLE_MIN_FREE_HBM_RATIO
        )
        cards.append(
            {
                "id": int(card_id),
                "name": name.strip(),
                "health": health.strip(),
                "util_percent": util_percent,
                "hbm_used_mb": hbm_used_mb,
                "hbm_total_mb": hbm_total_mb,
                "hbm_free_mb": hbm_free_mb,
                "hbm_free_percent": round(free_ratio * 100, 1) if free_ratio is not None else None,
                "is_idle": is_idle,
            }
        )

    if not cards:
        raise RuntimeError("没有从 npu-smi 输出中识别到 NPU 卡信息。")
    return cards


def scan_host(host: dict[str, str]) -> dict[str, Any]:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            hostname=host["ip"],
            username=host["username"],
            password=host["password"],
            allow_agent=False,
            look_for_keys=False,
            timeout=SSH_CONNECT_TIMEOUT_SECONDS,
            banner_timeout=SSH_CONNECT_TIMEOUT_SECONDS,
            auth_timeout=SSH_CONNECT_TIMEOUT_SECONDS,
        )
        _, stdout, _ = client.exec_command("npu-smi info", timeout=SSH_COMMAND_TIMEOUT_SECONDS)
        output = stdout.read(MAX_COMMAND_OUTPUT_BYTES + 1)
        if len(output) > MAX_COMMAND_OUTPUT_BYTES:
            raise RuntimeError("npu-smi 输出超过限制。")
        exit_code = stdout.channel.recv_exit_status()
        if exit_code != 0:
            raise RuntimeError("远端 npu-smi 命令执行失败。")
        cards = parse_npu_smi(output.decode("utf-8", errors="replace"))
        return {
            "ip": host["ip"],
            "hostname": host["ip"],
            "cards": cards,
            "error": None,
        }
    except paramiko.AuthenticationException:
        return {"ip": host["ip"], "cards": [], "error": "SSH 认证失败，请检查用户名和密码。"}
    except (paramiko.SSHException, socket.timeout, TimeoutError, OSError):
        return {"ip": host["ip"], "cards": [], "error": "SSH 连接失败或连接超时。"}
    except RuntimeError as exc:
        return {"ip": host["ip"], "cards": [], "error": str(exc)}
    except Exception:
        LOGGER.exception("NPU scan failed for %s", host["ip"])
        return {"ip": host["ip"], "cards": [], "error": "检测失败，请查看本地后端日志。"}
    finally:
        client.close()


def perform_scan(hosts: list[dict[str, str]]) -> dict[str, Any]:
    with ThreadPoolExecutor(max_workers=min(MAX_SSH_WORKERS, len(hosts))) as executor:
        results = list(executor.map(scan_host, hosts))
    return {
        "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "idle_util_threshold_percent": IDLE_MAX_UTIL_PERCENT,
        "idle_free_hbm_threshold_percent": int(IDLE_MIN_FREE_HBM_RATIO * 100),
        "results": results,
    }


class RequestHandler(BaseHTTPRequestHandler):
    server_version = "LocalNPUStatus/1.0"
    sys_version = ""

    def _origin_is_local(self) -> bool:
        host = self.headers.get("Host", "")
        allowed_hosts = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
        if host not in allowed_hosts:
            return False
        origin = self.headers.get("Origin")
        return origin is None or origin in {f"http://{allowed_host}" for allowed_host in allowed_hosts}

    def _send_json(self, data: Any, status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if not self._origin_is_local():
            self._send_json({"error": "只允许通过本机地址访问。"}, 403)
            return
        path = urlparse(self.path).path
        if path in {"/", "/npu_host_config.html"}:
            try:
                body = HTML_FILE.read_bytes()
            except OSError:
                self.send_error(404, "HTML 页面不存在")
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/config":
            self._send_json(load_saved_config())
        elif path == "/api/results":
            with LATEST_SCAN_LOCK:
                self._send_json(LATEST_SCAN or {})
        else:
            self._send_json({"error": "未找到页面。"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        global LATEST_SCAN
        if not self._origin_is_local():
            self._send_json({"error": "只允许通过本机地址访问。"}, 403)
            return
        if urlparse(self.path).path != "/api/scan":
            self._send_json({"error": "未找到接口。"}, 404)
            return
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
            if content_length < 1 or content_length > MAX_REQUEST_BYTES:
                raise HostConfigError("请求内容大小无效。")
            payload = json.loads(self.rfile.read(content_length))
            hosts = validate_hosts(payload)
        except (ValueError, json.JSONDecodeError, HostConfigError):
            self._send_json({"error": "机器配置无效，请检查 IP、用户名和密码。"}, 400)
            return

        result = perform_scan(hosts)
        with LATEST_SCAN_LOCK:
            LATEST_SCAN = result
        self._send_json(result)

    def log_message(self, format_string: str, *args: Any) -> None:
        # Request bodies and credentials are never written to logs.
        LOGGER.info("%s - %s", self.address_string(), format_string % args)


def main() -> None:
    parser = argparse.ArgumentParser(description="Local NPU card status page and SSH scanner")
    parser.add_argument(
        "--scan-once",
        action="store_true",
        help="scan the saved temp/npu-hosts.json once and print credential-free JSON",
    )
    args = parser.parse_args()
    if args.scan_once:
        result = perform_scan(load_saved_config()["hosts"])
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    server = ThreadingHTTPServer((DEFAULT_HOST, DEFAULT_PORT), RequestHandler)
    server.daemon_threads = True
    LOGGER.info("NPU status page listening at http://%s:%s", DEFAULT_HOST, DEFAULT_PORT)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        LOGGER.info("Stopping local NPU status server.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
