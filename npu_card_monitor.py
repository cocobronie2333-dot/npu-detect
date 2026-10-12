#!/usr/bin/env python3
"""Local-only web UI and SSH scanner for NPU card availability."""

from __future__ import annotations

import json
import logging
import re
import socket
import argparse
import sys
import os
import tempfile
import threading
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


BASE_DIR = Path(__file__).resolve().parent
HTML_FILE = BASE_DIR / "npu_host_config.html"
CONFIG_FILE = Path(os.environ.get("NPU_CONFIG_FILE", str(BASE_DIR / "npu-hosts.json")))

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
CHIP_ROW_PATTERN = re.compile(r"^(\d+)\s+(.+)$")
MEMORY_PAIR_PATTERN = re.compile(r"(\d+)\s*/\s*(\d+)")

try:
    import paramiko
except ImportError as exc:  # pragma: no cover - environment setup diagnostic
    raise SystemExit(
        "Paramiko is required. Run: python -m pip install -r requirements.txt"
    ) from exc

from npu_key_setup import KeySetupError, enroll_key, fingerprint_of


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
LOGGER = logging.getLogger("npu-card-monitor")
logging.getLogger("paramiko").setLevel(logging.WARNING)
LATEST_SCAN: dict[str, Any] | None = None
LATEST_SCAN_LOCK = threading.Lock()


class HostConfigError(ValueError):
    pass


def validate_hosts(payload: Any, allow_empty: bool = False) -> list[dict[str, str]]:
    if not isinstance(payload, dict) or not isinstance(payload.get("hosts"), list):
        raise HostConfigError("请求必须包含 hosts 数组。")
    hosts = payload["hosts"]
    if (not hosts and not allow_empty) or len(hosts) > MAX_HOSTS:
        raise HostConfigError(f"机器数量必须在 1 到 {MAX_HOSTS} 台之间。")

    clean_hosts = []
    for host in hosts:
        if not isinstance(host, dict):
            raise HostConfigError("机器配置格式错误。")
        ip = host.get("ip")
        username = host.get("username")
        password = host.get("password", "")
        key_path = host.get("key_path", "")
        if not all(isinstance(value, str) for value in (ip, username, password, key_path)):
            raise HostConfigError("每台机器都需要填写 IP、用户名和密码或私钥路径。")
        ip = ip.strip()
        username = username.strip()
        key_path = key_path.strip()
        if not IPV4_PATTERN.fullmatch(ip) or not username or not (password or key_path):
            raise HostConfigError("存在无效 IP、空用户名或缺少认证方式。")
        if key_path and (not Path(key_path).is_absolute() or not Path(key_path).is_file()):
            raise HostConfigError("SSH 私钥路径必须是本机已存在文件的绝对路径。")
        category = host.get("category", "未分类")
        if not isinstance(category, str) or len(category.strip()) > 40:
            raise HostConfigError("机器类别必须是长度不超过 40 的文本。")
        clean_host = {"ip": ip, "username": username, "password": password, "category": category.strip() or "未分类"}
        if key_path:
            clean_host["key_path"] = key_path
        clean_hosts.append(clean_host)
    return clean_hosts


def load_saved_config() -> dict[str, list[dict[str, str]]]:
    try:
        payload = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        return {"hosts": validate_hosts(payload, allow_empty=True)}
    except FileNotFoundError:
        return {"hosts": []}
    except (json.JSONDecodeError, HostConfigError):
        LOGGER.warning("Saved host configuration is invalid; use import or enter hosts in the page.")
        return {"hosts": []}


def save_host_config(hosts: list[dict[str, str]]) -> None:
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=CONFIG_FILE.parent, prefix=".npu-config-", suffix=".tmp", delete=False) as temporary:
            temporary_path = Path(temporary.name)
            json.dump({"hosts": hosts}, temporary, ensure_ascii=False, indent=2)
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        if os.name == "nt":
            identity = f"{os.environ['USERDOMAIN']}\\{os.environ['USERNAME']}"
            permissions = subprocess.run(
                ["icacls", str(temporary_path), "/inheritance:r", "/grant:r", f"{identity}:F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            if permissions.returncode != 0:
                raise OSError("Could not restrict configuration file permissions")
        else:
            temporary_path.chmod(0o600)
        os.replace(temporary_path, CONFIG_FILE)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def parse_npu_smi(output: str) -> list[dict[str, Any]]:
    lines = output.splitlines()
    cards: list[dict[str, Any]] = []
    in_card_table = False
    for line_index, line in enumerate(lines):
        if ("NPU ID" in line or re.search(r"\bNPU\s+Name\b", line)) and "Health" in line:
            in_card_table = True
            continue
        if "Process id" in line or "Process name" in line:
            in_card_table = False
        if not in_card_table:
            continue
        match = DEVICE_ROW_PATTERN.match(line)
        if match:
            card_id, name, health, _ = match.groups()
        else:
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            chip_match = CHIP_ROW_PATTERN.fullmatch(cells[0]) if len(cells) == 3 else None
            if not chip_match or not re.search(r"[A-Za-z]", chip_match.group(2)):
                continue
            card_id, name, health = chip_match.group(1), chip_match.group(2), cells[1]
        util_percent = None
        hbm_used_mb = None
        hbm_total_mb = None

        for detail_line in lines[line_index + 1 : line_index + 4]:
            cells = [cell.strip() for cell in detail_line.strip().strip("|").split("|")]
            if len(cells) < 3 or cells[0].isdigit():
                continue
            payload = cells[-1]
            util_match = re.match(r"\s*(\d+)", payload)
            memory_pairs = MEMORY_PAIR_PATTERN.findall(payload)
            if util_match and memory_pairs:
                util_percent = int(util_match.group(1))
                hbm_used_mb, hbm_total_mb = map(int, memory_pairs[-1])
                if len(cells) == 3:
                    chip_id = CHIP_ROW_PATTERN.fullmatch(cells[0])
                    if chip_id:
                        card_id = chip_id.group(2)
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


def parse_npu_processes(output: str) -> dict[int, list[dict[str, Any]]]:
    processes: dict[int, list[dict[str, Any]]] = {}
    in_process_table = False
    for line in output.splitlines():
        if "Process id" in line and "Process name" in line:
            in_process_table = True
            continue
        if not in_process_table:
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) < 4 or not cells[0].isdigit() or not cells[1].isdigit():
            continue
        processes.setdefault(int(cells[0]), []).append({
            "pid": int(cells[1]), "name": cells[2],
            "memory_mb": int(cells[3]) if cells[3].isdigit() else None,
            "container_pid": cells[4] if len(cells) > 4 and cells[4] != "NA" else None,
        })
    return processes


def collect_process_details(client: Any, processes: dict[int, list[dict[str, Any]]]) -> None:
    pids = sorted({process["pid"] for group in processes.values() for process in group})
    if not pids:
        return
    if len(pids) > 256:
        raise RuntimeError("占用进程数量超过查询限制。")
    # Only integer PIDs from npu-smi are interpolated into the shell command.
    command = "for p in " + " ".join(map(str, pids)) + "; do printf '\\nNPU_PID %s\\n' \"$p\"; ps -ww -p \"$p\" -o user=,etime=,args=; printf 'NPU_CGROUP\\n'; cat /proc/\"$p\"/cgroup 2>/dev/null; done"
    _, stdout, _ = client.exec_command(command, timeout=SSH_COMMAND_TIMEOUT_SECONDS)
    raw = stdout.read(MAX_COMMAND_OUTPUT_BYTES + 1)
    if len(raw) > MAX_COMMAND_OUTPUT_BYTES:
        raise RuntimeError("进程信息输出超过限制。")
    details = {}
    for block in raw.decode("utf-8", errors="replace").split("\nNPU_PID ")[1:]:
        pid_line, _, rest = block.partition("\n")
        info, _, cgroup = rest.partition("NPU_CGROUP\n")
        fields = info.strip().split(None, 2)
        container_match = re.search(r"(?<![0-9a-f])[0-9a-f]{64}(?![0-9a-f])", cgroup)
        details[int(pid_line)] = {
            "user": fields[0] if len(fields) == 3 else None,
            "elapsed": fields[1] if len(fields) == 3 else None,
            "command": fields[2] if len(fields) == 3 else None,
            "container_id": container_match.group(0) if container_match else None,
            "cgroup": cgroup.strip() or None,
        }
    containers = {}
    if any(detail["container_id"] for detail in details.values()):
        try:
            _, stdout, _ = client.exec_command("docker ps --no-trunc --format '{{.ID}}|{{.Names}}|{{.Image}}' 2>/dev/null", timeout=SSH_COMMAND_TIMEOUT_SECONDS)
            raw_containers = stdout.read(MAX_COMMAND_OUTPUT_BYTES + 1)
            if len(raw_containers) <= MAX_COMMAND_OUTPUT_BYTES:
                for line in raw_containers.decode("utf-8", errors="replace").splitlines():
                    parts = line.split("|", 2)
                    if len(parts) == 3:
                        containers[parts[0]] = {"container_name": parts[1], "container_image": parts[2]}
        except Exception:
            pass  # Container metadata is optional; preserve the process data.
    for group in processes.values():
        for process in group:
            detail = details.get(process["pid"], {})
            process.update(detail)
            process.update(containers.get(detail.get("container_id"), {}))


def scan_host(host: dict[str, str]) -> dict[str, Any]:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            hostname=host["ip"],
            username=host["username"],
            password=host["password"] or None,
            key_filename=host.get("key_path") or None,
            allow_agent=False,
            look_for_keys=False,
            timeout=SSH_CONNECT_TIMEOUT_SECONDS,
            banner_timeout=SSH_CONNECT_TIMEOUT_SECONDS,
            auth_timeout=SSH_CONNECT_TIMEOUT_SECONDS,
        )
        command = "sudo -S -p '' npu-smi info" if host["password"] else "sudo -n npu-smi info"
        stdin, stdout, _ = client.exec_command(command, timeout=SSH_COMMAND_TIMEOUT_SECONDS)
        if host["password"]:
            stdin.write(host["password"] + "\n")
            stdin.flush()
        stdin.channel.shutdown_write()
        output = stdout.read(MAX_COMMAND_OUTPUT_BYTES + 1)
        if len(output) > MAX_COMMAND_OUTPUT_BYTES:
            raise RuntimeError("npu-smi 输出超过限制。")
        exit_code = stdout.channel.recv_exit_status()
        if exit_code != 0:
            raise RuntimeError("远端 sudo npu-smi 命令执行失败，请检查 sudo 权限。")
        decoded_output = output.decode("utf-8", errors="replace")
        cards = parse_npu_smi(decoded_output)
        processes = parse_npu_processes(decoded_output)
        process_error = None
        try:
            collect_process_details(client, processes)
        except Exception:
            process_error = "部分进程信息查询失败，进程可能已退出或权限不足。"
        for card in cards:
            card["processes"] = processes.get(card["id"], [])
            card["process_error"] = process_error
        host_fingerprint = fingerprint_of(client.get_transport().get_remote_server_key())
        return {
            "ip": host["ip"],
            "hostname": host["ip"],
            "cards": cards,
            "error": None,
            "host_fingerprint": host_fingerprint,
        }
    except paramiko.AuthenticationException:
        return {"ip": host["ip"], "cards": [], "error": "SSH 认证失败，请检查密码或私钥。"}
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
    for host, result in zip(hosts, results):
        result["category"] = host.get("category", "未分类")
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
        path = urlparse(self.path).path
        if path not in {"/api/scan", "/api/config", "/api/enroll-key"}:
            self._send_json({"error": "未找到接口。"}, 404)
            return
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
            if content_length < 1 or content_length > MAX_REQUEST_BYTES:
                raise HostConfigError("请求内容大小无效。")
            payload = json.loads(self.rfile.read(content_length))
            if path == "/api/enroll-key":
                hosts = validate_hosts({"hosts": [payload.get("host")]})
                expected_fingerprint = payload.get("host_fingerprint")
                if not isinstance(expected_fingerprint, str):
                    raise HostConfigError("SSH 主机指纹无效。")
            else:
                hosts = validate_hosts(payload, allow_empty=path == "/api/config")
        except (ValueError, json.JSONDecodeError, HostConfigError):
            self._send_json({"error": "机器配置无效，请检查 IP、用户名和认证方式。"}, 400)
            return

        if path == "/api/enroll-key":
            host = hosts[0]
            scan = scan_host(host)
            if scan["error"]:
                self._send_json({"error": "机器检测未成功，未生成密钥。"}, 409)
                return
            if scan["host_fingerprint"] != expected_fingerprint:
                self._send_json({"error": "SSH 主机指纹与检测时不同，未安装公钥。"}, 409)
                return
            try:
                if host.get("key_path"):
                    self._send_json({"key_path": host["key_path"], "created": False})
                    return
                if not host["password"]:
                    raise KeySetupError("首次安装公钥需要当前 SSH 密码。")
                private_key, created = enroll_key(
                    host["ip"], host["username"], host["password"], expected_fingerprint
                )
            except (KeySetupError, paramiko.SSHException, OSError) as exc:
                LOGGER.warning("SSH key setup failed for %s: %s", host["ip"], type(exc).__name__)
                self._send_json({"error": "密钥安装或验证失败；原有登录配置未改变。"}, 409)
                return
            self._send_json({"key_path": str(private_key), "created": created})
            return

        if path == "/api/config":
            try:
                save_host_config(hosts)
            except OSError:
                self._send_json({"error": "配置保存失败，请检查配置文件目录及写入权限。"}, 500)
                return
            saved_hosts = {(host["ip"], host["category"]) for host in hosts}
            with LATEST_SCAN_LOCK:
                if LATEST_SCAN is not None:
                    LATEST_SCAN["results"] = [
                        result for result in LATEST_SCAN.get("results", [])
                        if (result["ip"], result.get("category", "未分类")) in saved_hosts
                    ]
            self._send_json({"saved": True, "count": len(hosts)})
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
