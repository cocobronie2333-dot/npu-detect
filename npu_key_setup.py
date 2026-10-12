"""Create one local SSH key per host and enroll its public key after verification."""

from __future__ import annotations

import base64
import hashlib
import os
import re
import shlex
import subprocess
from pathlib import Path

import paramiko


FINGERPRINT_PATTERN = re.compile(r"^SHA256:[A-Za-z0-9+/]{43}$")


class KeySetupError(RuntimeError):
    pass


class FingerprintPolicy(paramiko.MissingHostKeyPolicy):
    def __init__(self, expected: str):
        if not FINGERPRINT_PATTERN.fullmatch(expected):
            raise KeySetupError("SSH 主机指纹格式无效。")
        self.expected = expected

    def missing_host_key(self, client, hostname, key):
        if fingerprint_of(key) != self.expected:
            raise KeySetupError(f"{hostname} 的 SSH 主机指纹已变化，未安装公钥。")


def fingerprint_of(key: paramiko.PKey) -> str:
    digest = hashlib.sha256(key.asbytes()).digest()
    return "SHA256:" + base64.b64encode(digest).decode().rstrip("=")


def key_directory() -> Path:
    return Path(__file__).resolve().parent / "keys"


def secure_key_directory(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        identity = f"{os.environ['USERDOMAIN']}\\{os.environ['USERNAME']}"
        result = subprocess.run(
            ["icacls", str(directory), "/inheritance:r", "/grant:r", f"{identity}:(OI)(CI)F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if result.returncode != 0:
            raise KeySetupError("无法限制私钥目录权限。")
    else:
        directory.chmod(0o700)


def ensure_key(ip: str, directory: Path | None = None) -> tuple[Path, bool]:
    if not re.fullmatch(r"[0-9.]+", ip):
        raise KeySetupError("机器 IP 无效。")
    directory = directory or key_directory()
    secure_key_directory(directory)
    private_key = directory / f"npu_{ip}_ed25519"
    public_key = Path(str(private_key) + ".pub")
    if private_key.exists() and public_key.exists():
        return private_key, False
    if private_key.exists() or public_key.exists():
        raise KeySetupError("该机器的密钥对不完整；不会覆盖已有文件。")
    lock = Path(str(private_key) + ".lock")
    try:
        handle = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise KeySetupError("该机器正在生成密钥，请稍后重试。") from exc
    os.close(handle)
    try:
        if private_key.exists() or public_key.exists():
            raise KeySetupError("该机器的密钥已由其他任务创建，请重试。")
        result = subprocess.run(
            ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", f"npu-detect-{ip}", "-f", str(private_key)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if result.returncode != 0 or not private_key.is_file() or not public_key.is_file():
            raise KeySetupError("生成 SSH 密钥失败。")
        return private_key, True
    finally:
        lock.unlink(missing_ok=True)


def connect(ip: str, username: str, fingerprint: str, **auth) -> paramiko.SSHClient:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(FingerprintPolicy(fingerprint))
    client.connect(
        hostname=ip,
        username=username,
        allow_agent=False,
        look_for_keys=False,
        timeout=10,
        auth_timeout=10,
        **auth,
    )
    return client


def enroll_key(ip: str, username: str, password: str, fingerprint: str) -> tuple[Path, bool]:
    private_key, created = ensure_key(ip)
    client = connect(ip, username, fingerprint, password=password or None, key_filename=str(private_key))
    try:
        public_key = Path(str(private_key) + ".pub").read_text(encoding="utf-8").strip()
        if len(public_key.splitlines()) != 1 or len(public_key.split()) < 2:
            raise KeySetupError("本机公钥文件格式无效。")
        quoted = shlex.quote(public_key)
        command = (
            "umask 077; mkdir -p ~/.ssh && chmod 700 ~/.ssh && "
            "touch ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys && "
            f"(grep -qxF -- {quoted} ~/.ssh/authorized_keys || printf '%s\\n' {quoted} >> ~/.ssh/authorized_keys)"
        )
        _, stdout, _ = client.exec_command(command, timeout=10)
        if stdout.channel.recv_exit_status() != 0:
            raise KeySetupError("远端公钥安装失败。")
    finally:
        client.close()
    verifier = connect(ip, username, fingerprint, key_filename=str(private_key))
    verifier.close()
    return private_key, created
