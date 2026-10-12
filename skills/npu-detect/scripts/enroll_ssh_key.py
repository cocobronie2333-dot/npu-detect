"""Install an existing SSH public key for one authorized host and verify key login."""

import argparse
import base64
import getpass
import hashlib
import shlex
from pathlib import Path

import paramiko


class VerifiedHostKey(paramiko.MissingHostKeyPolicy):
    def __init__(self, expected_fingerprint: str):
        self.expected = expected_fingerprint.removeprefix("SHA256:").rstrip("=")

    def missing_host_key(self, client, hostname, key):
        actual = base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode().rstrip("=")
        if actual != self.expected:
            raise paramiko.SSHException(f"SSH host fingerprint mismatch for {hostname}")


def connect(ip: str, username: str, fingerprint: str, **auth):
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(VerifiedHostKey(fingerprint))
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


def enroll(ip: str, username: str, private_key: Path, fingerprint: str, password: str) -> None:
    key_path = private_key.expanduser().resolve(strict=True)
    public_path = Path(str(key_path) + ".pub")
    public_key = public_path.read_text(encoding="utf-8").strip()
    parts = public_key.split()
    if len(public_key.splitlines()) != 1 or len(parts) < 2 or not parts[0].startswith(("ssh-", "ecdsa-")):
        raise ValueError("public key file is invalid")
    client = connect(ip, username, fingerprint, password=password)
    try:
        command = "umask 077; mkdir -p ~/.ssh && chmod 700 ~/.ssh && touch ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys && (grep -qxF -- {key} ~/.ssh/authorized_keys || printf '%s\\n' {key} >> ~/.ssh/authorized_keys)".format(key=shlex.quote(public_key))
        _, stdout, stderr = client.exec_command(command, timeout=10)
        if stdout.channel.recv_exit_status() != 0:
            raise RuntimeError("Could not update authorized_keys: " + stderr.read(200).decode("utf-8", errors="replace"))
    finally:
        client.close()
    test_client = connect(ip, username, fingerprint, key_filename=str(key_path))
    test_client.close()
    print(f"Verified SSH key login for {username}@{ip}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ip", help="authorized target IPv4 address")
    parser.add_argument("username", help="target SSH account")
    parser.add_argument("private_key", type=Path, help="existing local private key path")
    parser.add_argument("--host-key-sha256", required=True, help="trusted SSH host fingerprint from an independent source")
    args = parser.parse_args()
    password = getpass.getpass(f"SSH password for {args.username}@{args.ip}: ")
    enroll(args.ip, args.username, args.private_key, args.host_key_sha256, password)


if __name__ == "__main__":
    main()
