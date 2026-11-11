#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Prepare and run isolated QEMU/KVM distro VMs for Layer tests.

Only .state/vm-tests, /tmp/layer-vm-*, and containers named
layer-vm-<distro> with the ownership label are managed. No host packages,
firewall rules, GPU devices or unrelated containers are changed.
"""

import argparse
import fcntl
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_STATE_DIR = ROOT / ".state" / "vm-tests"
DEFAULT_QEMU_IMAGE = os.environ.get("LAYER_VM_QEMU_IMAGE", "qemu-host")
OWNER_LABEL = "io.layer-recorder.owner"
OWNER_VALUE = "distro-vms"
DISTRO_LABEL = "io.layer-recorder.vm-test"
SSH_USER = "layerqa"
USER_AGENT = "layer-distro-vms/1"

# Adding Fedora or openSUSE is metadata-only. Both publish qcow2 cloud images
# with cloud-init/NoCloud support; provide a pinned official image URL, its
# official checksum file, a unique SSH/VNC port and a container name.
DISTROS = {
    "opensuse16": {
        "family": "suse",
        "image_url": "https://download.opensuse.org/distribution/leap/16.0/appliances/Leap-16.0-Minimal-VM.x86_64-Cloud-Build18.92.qcow2",
        "image_name": "Leap-16.0-Minimal-VM.x86_64-Cloud-Build18.92.qcow2",
        "image_format": "qcow2",
        "checksum_url": "https://download.opensuse.org/distribution/leap/16.0/appliances/Leap-16.0-Minimal-VM.x86_64-Cloud-Build18.92.qcow2.sha256",
        "checksum_algorithm": "sha256",
        "ssh_port": 22283,
        "vnc_display": 3,
        "container": "layer-vm-opensuse16",
        "hostname": "layer-opensuse16",
        "disk_size": "24G",
    },
    "ubuntu24.04": {
        "family": "debian",
        "image_url": "https://cloud-images.ubuntu.com/releases/24.04/release/ubuntu-24.04-server-cloudimg-amd64.img",
        "image_name": "ubuntu-24.04-server-cloudimg-amd64.img",
        "image_format": "qcow2",
        "checksum_url": "https://cloud-images.ubuntu.com/releases/24.04/release/SHA256SUMS",
        "checksum_algorithm": "sha256",
        "ssh_port": 22281,
        "vnc_display": 1,
        "container": "layer-vm-ubuntu24.04",
        "hostname": "layer-ubuntu24-04",
        "disk_size": "20G",
    },
    "debian13": {
        "family": "debian",
        "image_url": "https://cloud.debian.org/images/cloud/trixie/latest/debian-13-genericcloud-amd64.qcow2",
        "image_name": "debian-13-genericcloud-amd64.qcow2",
        "image_format": "qcow2",
        # Debian publishes SHA512SUMS for this tree, not SHA256SUMS.
        "checksum_url": "https://cloud.debian.org/images/cloud/trixie/latest/SHA512SUMS",
        "checksum_algorithm": "sha512",
        "ssh_port": 22282,
        "vnc_display": 2,
        "container": "layer-vm-debian13",
        "hostname": "layer-debian13",
        "disk_size": "20G",
    },
}

ALIASES = {
    "ubuntu": "ubuntu24.04",
    "ubuntu-24.04": "ubuntu24.04",
    "ubuntu24-04": "ubuntu24.04",
    "noble": "ubuntu24.04",
    "debian": "debian13",
    "debian-13": "debian13",
    "trixie": "debian13",
}


class VmError(RuntimeError):
    """A safe, user-facing failure."""


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def canonical_distro(value):
    key = value.strip().lower()
    key = ALIASES.get(key, key)
    if key not in DISTROS:
        raise VmError("unknown distro %r; choose one of: %s" % (value, ", ".join(sorted(DISTROS))))
    return key


def run(command, *, check=True, timeout=None):
    """Run a command without a shell and without echoing secrets."""
    try:
        result = subprocess.run(
            command, text=True, capture_output=True, stdin=subprocess.DEVNULL, timeout=timeout
        )
    except FileNotFoundError as exc:
        raise VmError("required command not found: %s" % command[0]) from exc
    except subprocess.TimeoutExpired as exc:
        raise VmError("command timed out: %s" % command[0]) from exc
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        if len(detail) > 800:
            detail = detail[-800:]
        raise VmError("%s failed (exit %s)%s" % (command[0], result.returncode, (": " + detail) if detail else ""))
    return result


def docker(arguments, *, check=True, timeout=None):
    return run(["docker", *arguments], check=check, timeout=timeout)


def ensure_qemu_image(image):
    result = docker(["image", "inspect", image], check=False)
    if result.returncode != 0:
        raise VmError(
            "QEMU host image %r is missing. Build it first:\n"
            "  docker build -f packaging/vm-tests/qemu-host.Dockerfile -t %s packaging/vm-tests"
            % (image, image)
        )


def docker_tool(image, state_dir, arguments, *, timeout=None):
    """Run qemu-img/cloud-localds/ssh-keygen in the pinned host image."""
    command = [
        "docker", "run", "--rm", "--network", "none", "--read-only",
        "--tmpfs", "/tmp:rw,nosuid,nodev,size=128m",
        "--user", "%d:%d" % (os.getuid(), os.getgid()),
        "-e", "HOME=/tmp", "-w", "/state",
        "-v", "%s:/state:rw" % state_dir,
        image, *arguments,
    ]
    return run(command, timeout=timeout)


def kvm_group_arguments():
    try:
        gid = Path("/dev/kvm").stat().st_gid
    except OSError:
        return []
    if gid == os.getgid():
        return []
    return ["--group-add", str(gid)]


def require_kvm():
    device = Path("/dev/kvm")
    if not device.exists():
        raise VmError("/dev/kvm is missing; this tool requires QEMU KVM acceleration")
    if not os.access(device, os.R_OK | os.W_OK):
        raise VmError(
            "/dev/kvm is not readable/writable by this user. Grant device access "
            "outside this tool; it never changes host permissions."
        )


def require_port_free(port, label):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError as exc:
            raise VmError("%s port 127.0.0.1:%d is already in use" % (label, port)) from exc


def atomic_write(path, text, mode=0o600):
    path = Path(path)
    temporary = path.with_name(path.name + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, mode)
    finally:
        temporary.unlink(missing_ok=True)


def hash_file(path, algorithm):
    digest = hashlib.new(algorithm)
    with Path(path).open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def fetch_text(url, *, retries=4, timeout=60):
    last = None
    for attempt in range(1, retries + 1):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept-Encoding": "identity"})
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = response.read(2 * 1024 * 1024)
            return payload.decode("utf-8")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last = exc
            if attempt < retries:
                time.sleep(min(2 ** attempt, 15))
    raise VmError("could not fetch %s: %s" % (url, last))


def parse_checksum(text, filename):
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = re.match(r"^(?:SHA256|SHA512) \((.+)\) = ([0-9a-fA-F]+)$", line, re.IGNORECASE)
        if match:
            name, digest = match.group(1), match.group(2)
        else:
            parts = line.split()
            if len(parts) < 2:
                continue
            digest, name = parts[0], parts[-1]
        if not re.fullmatch(r"[0-9a-fA-F]{64}|[0-9a-fA-F]{128}", digest):
            continue
        name = name.lstrip("*").lstrip("./")
        if name == filename or name.endswith("/" + filename):
            return digest.lower()
    return None


def download_verified(url, destination, expected, algorithm, *, retries=4, timeout=60):
    destination = Path(destination)
    partial = destination.with_name(destination.name + ".part")
    for attempt in range(1, retries + 1):
        try:
            existing = partial.stat().st_size if partial.exists() else 0
            headers = {"User-Agent": USER_AGENT, "Accept-Encoding": "identity"}
            if existing:
                headers["Range"] = "bytes=%d-" % existing
            request = urllib.request.Request(url, headers=headers)
            try:
                response = urllib.request.urlopen(request, timeout=timeout)
            except urllib.error.HTTPError as exc:
                if exc.code == 416 and partial.exists():
                    partial.unlink()
                raise
            with response:
                mode = "ab" if getattr(response, "status", 200) == 206 and existing else "wb"
                if mode == "wb":
                    existing = 0
                print(
                    "downloading %s%s" % (destination.name, " (resuming at %.0f MiB)" % (existing / 1048576) if existing else ""),
                    file=sys.stderr,
                )
                with partial.open(mode) as handle:
                    while block := response.read(1024 * 1024):
                        handle.write(block)
                    handle.flush()
                    os.fsync(handle.fileno())
            actual = hash_file(partial, algorithm)
            if actual != expected:
                partial.unlink(missing_ok=True)
                raise VmError(
                    "checksum mismatch for %s (expected %s..., got %s...); refusing unverified image"
                    % (destination.name, expected[:16], actual[:16])
                )
            os.replace(partial, destination)
            os.chmod(destination, 0o444)
            return actual
        except VmError:
            raise
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            if attempt == retries:
                raise VmError("download failed for %s: %s" % (url, exc)) from exc
            print("download attempt %d failed: %s; retrying" % (attempt, exc), file=sys.stderr)
            time.sleep(min(2 ** attempt, 15))
    raise VmError("download failed for %s" % url)


@contextmanager
def prepare_lock(path):
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise VmError("another prepare operation is already running for this distro") from exc
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


class Vm:
    def __init__(self, key, state_root, image):
        self.key = key
        self.meta = dict(DISTROS[key])
        self.state_root = Path(state_root).expanduser().resolve()
        self.image = image
        self.state_id=hashlib.sha256(str(self.state_root).encode()).hexdigest()[:8]
        self.meta["container"] += "-" + self.state_id
        self.directory = self.state_root / key
        self.base_directory = self.directory / "base"
        self.checksum_directory = self.directory / "checksums"
        self.base_image = self.base_directory / self.meta["image_name"]
        self.disk = self.directory / "disk.qcow2"
        self.seed = self.directory / "seed.iso"
        self.user_data = self.directory / "user-data"
        self.meta_data = self.directory / "meta-data"
        self.private_key = self.directory / "id_ed25519"
        self.public_key = self.directory / "id_ed25519.pub"
        self.known_hosts = self.directory / "known_hosts"
        self.state_file = self.directory / "state.json"
        self.lock_file = self.directory / ".prepare.lock"
        runtime_hash = hashlib.sha256(("%s\0%s" % (self.directory, key)).encode()).hexdigest()[:12]
        self.runtime_directory = Path("/tmp") / ("layer-vm-" + runtime_hash)
        self.qmp_socket = self.runtime_directory / "qmp.sock"

    def ensure_directories(self):
        resolved = self.state_root.resolve()
        for forbidden in {Path("/"), Path("/tmp"), Path.home().resolve()}:
            if resolved == forbidden:
                raise VmError("refusing to use %s as a private VM state directory" % resolved)
        try:
            self.state_root.mkdir(parents=True, exist_ok=True)
            os.chmod(self.state_root, 0o700)
            for directory in (self.directory, self.base_directory, self.checksum_directory):
                directory.mkdir(parents=True, exist_ok=True)
                os.chmod(directory, 0o700)
        except OSError as exc:
            raise VmError("cannot create private state directory %s: %s" % (self.state_root, exc)) from exc

    def read_state(self):
        if not self.state_file.exists():
            return {}
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise VmError("cannot read state file %s: %s" % (self.state_file, exc)) from exc
        if not isinstance(data, dict):
            raise VmError("state file %s is not a JSON object" % self.state_file)
        return data

    def write_state(self, state):
        atomic_write(self.state_file, json.dumps(state, indent=2, sort_keys=True) + "\n", 0o600)

    def is_prepared(self):
        state = self.read_state()
        required = (self.base_image, self.disk, self.seed, self.private_key, self.public_key)
        return state.get("prepared") is True and all(path.is_file() for path in required)

    def require_prepared(self):
        if not self.is_prepared():
            raise VmError(
                "%s is not prepared; run: python3 tools/distro-vms.py prepare %s"
                % (self.key, self.key)
            )

    def container_info(self):
        name = self.meta["container"]
        result = docker(["container", "inspect", name], check=False)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").lower()
            if "no such" in detail:
                return None
            raise VmError("docker inspect failed for %s: %s" % (name, detail.strip()))
        try:
            info = json.loads(result.stdout)[0]
        except (ValueError, IndexError) as exc:
            raise VmError("unexpected docker inspect output for %s" % name) from exc
        labels = (info.get("Config") or {}).get("Labels") or {}
        if labels.get(OWNER_LABEL) != OWNER_VALUE or labels.get(DISTRO_LABEL) != self.key or labels.get("io.layer-recorder.state") != self.state_id:
            raise VmError(
                "refusing to touch container %r: it is not owned by distro-vms for %s"
                % (name, self.key)
            )
        return info

    def container_running(self):
        info = self.container_info()
        return bool(info and (info.get("State") or {}).get("Running"))

    def ensure_private_key(self):
        if self.public_key.exists() and not self.private_key.exists():
            raise VmError("public key exists without its private key in %s" % self.directory)
        key_path = "/state/%s/id_ed25519" % self.key
        if not self.private_key.exists():
            if self.public_key.exists():
                raise VmError("refusing to replace an existing public key without its private key")
            docker_tool(
                self.image,
                self.state_root,
                ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "layerqa@layer-vm", "-f", key_path],
            )
        os.chmod(self.private_key, 0o600)
        public_text = ""
        if self.public_key.exists():
            public_text = self.public_key.read_text(encoding="utf-8").strip()
        if not public_text.startswith(("ssh-", "ecdsa-", "sk-")):
            result = docker_tool(self.image, self.state_root, ["ssh-keygen", "-y", "-f", key_path])
            public_text = result.stdout.strip()
            if not public_text:
                raise VmError("could not derive the public key; the private key may be unusable")
            atomic_write(self.public_key, public_text + "\n", 0o644)
        os.chmod(self.public_key, 0o644)

    def cloud_init_texts(self):
        public_key = self.public_key.read_text(encoding="utf-8").strip()
        hostname = self.meta["hostname"]
        user_data = """#cloud-config
hostname: {hostname}
manage_etc_hosts: true
preserve_hostname: false
users:
  - name: layerqa
    gecos: Layer VM test user
    groups: [adm]
    shell: /bin/bash
    lock_passwd: true
    sudo: "ALL=(ALL) NOPASSWD:ALL"
    ssh_authorized_keys:
      - {public_key}
ssh_pwauth: false
disable_root: true
package_update: false
package_upgrade: false
runcmd:
  - sh -c 'systemctl enable --now ssh 2>/dev/null || systemctl enable --now sshd 2>/dev/null || true'
""".format(hostname=hostname, public_key=json.dumps(public_key))
        meta_data = "instance-id: %s\nlocal-hostname: %s\n" % (hostname, hostname)
        return user_data, meta_data

    def write_cloud_init(self, state):
        user_data, meta_data = self.cloud_init_texts()
        atomic_write(self.user_data, user_data, 0o600)
        atomic_write(self.meta_data, meta_data, 0o600)
        source_hash = hashlib.sha256((user_data + "\0" + meta_data).encode()).hexdigest()
        if self.seed.exists() and state.get("seed_source_sha256") == source_hash:
            return state
        temporary = self.seed.with_name(self.seed.name + ".tmp")
        temporary.unlink(missing_ok=True)
        docker_tool(
            self.image,
            self.state_root,
            [
                "cloud-localds",
                "/state/%s/%s" % (self.key, temporary.name),
                "/state/%s/user-data" % self.key,
                "/state/%s/meta-data" % self.key,
            ],
        )
        os.replace(temporary, self.seed)
        os.chmod(self.seed, 0o600)
        state["seed_source_sha256"] = source_hash
        self.write_state(state)
        return state

    def decide_checksum(self, state, explicit, retries, timeout):
        algorithm = self.meta["checksum_algorithm"]
        explicit_algorithm = None
        if explicit:
            explicit = explicit.strip().lower()
            if re.fullmatch(r"[0-9a-f]{64}", explicit):
                explicit_algorithm = "sha256"
            elif re.fullmatch(r"[0-9a-f]{128}", explicit):
                explicit_algorithm = "sha512"
            else:
                raise VmError("--checksum must be a 64- or 128-character hexadecimal digest")
        recorded = state.get("base_checksum") or {}
        official = None
        if explicit is not None or not self.base_image.exists() or not recorded:
            try:
                text = fetch_text(self.meta["checksum_url"], retries=retries, timeout=timeout)
            except VmError:
                if explicit is None and not recorded:
                    raise
                text = ""
            if text:
                checksum_name = self.meta["checksum_url"].rsplit("/", 1)[-1]
                atomic_write(self.checksum_directory / checksum_name, text, 0o600)
                official = parse_checksum(text, self.meta["image_name"])
                if official is None and explicit is None:
                    raise VmError(
                        "official %s does not list %s; pass --checksum <official digest> explicitly"
                        % (checksum_name, self.meta["image_name"])
                    )
        if explicit is not None:
            if official and explicit_algorithm == algorithm and official != explicit:
                raise VmError("--checksum does not match the official checksum file")
            if recorded and recorded.get("algorithm") == explicit_algorithm and recorded.get("value") != explicit:
                raise VmError("--checksum does not match the recorded checksum for the prepared image")
            return {"algorithm": explicit_algorithm, "value": explicit, "source": "explicit"}
        if official is not None:
            return {"algorithm": algorithm, "value": official, "source": self.meta["checksum_url"]}
        if recorded.get("algorithm") and recorded.get("value"):
            return dict(recorded)
        raise VmError(
            "no expected checksum is available for %s; pass --checksum <official digest>"
            % self.meta["image_name"]
        )

    def verify_base_image(self):
        result = docker_tool(
            self.image,
            self.state_root,
            ["qemu-img", "info", "--output=json", "/state/%s/base/%s" % (self.key, self.meta["image_name"])],
        )
        try:
            info = json.loads(result.stdout)
        except ValueError as exc:
            raise VmError("qemu-img returned invalid information for the base image") from exc
        if info.get("format") != self.meta["image_format"]:
            raise VmError(
                "base image format is %r, expected %r"
                % (info.get("format"), self.meta["image_format"])
            )
        if int(info.get("virtual-size", 0)) <= 0:
            raise VmError("base image reports an invalid virtual size")

    def create_cow_disk(self):
        disk_path = "/state/%s/disk.qcow2" % self.key
        if self.disk.exists():
            result = docker_tool(self.image, self.state_root, ["qemu-img", "info", "--output=json", disk_path])
            try:
                info = json.loads(result.stdout)
            except ValueError as exc:
                raise VmError("qemu-img returned invalid information for disk.qcow2") from exc
            if info.get("format") != "qcow2":
                raise VmError("existing disk.qcow2 is not qcow2; refusing to replace it")
            backing = info.get("backing-filename")
            if backing and not backing.endswith(self.meta["image_name"]):
                raise VmError("existing disk.qcow2 has an unexpected backing image")
            return
        docker_tool(
            self.image,
            self.state_root,
            [
                "qemu-img", "create", "-f", "qcow2", "-F", self.meta["image_format"],
                "-b", "/state/%s/base/%s" % (self.key, self.meta["image_name"]),
                disk_path, self.meta["disk_size"],
            ],
        )
        os.chmod(self.disk, 0o600)

    def prepare(self, *, checksum, retries, timeout):
        ensure_qemu_image(self.image)
        self.ensure_directories()
        with prepare_lock(self.lock_file):
            state = self.read_state()
            self.ensure_private_key()
            state = self.write_cloud_init(state)
            decision = self.decide_checksum(state, checksum, retries, timeout)
            if self.base_image.exists():
                actual = hash_file(self.base_image, decision["algorithm"])
                if actual != decision["value"]:
                    raise VmError(
                        "existing base image %s does not match the expected checksum; "
                        "it was not modified. Remove it only if you intend to download again."
                        % self.base_image
                    )
            else:
                download_verified(
                    self.meta["image_url"], self.base_image, decision["value"], decision["algorithm"],
                    retries=retries, timeout=timeout,
                )
            self.verify_base_image()
            self.create_cow_disk()
            state.update(
                {
                    "schema": 1,
                    "distro": self.key,
                    "container": self.meta["container"],
                    "base_image": str(self.base_image.relative_to(self.state_root)),
                    "base_checksum": decision,
                    "disk": "disk.qcow2",
                    "disk_size": self.meta["disk_size"],
                    "ssh_port": self.meta["ssh_port"],
                    "prepared": True,
                }
            )
            self.write_state(state)
        print("prepared %s" % self.key)
        print("  base: %s (%s verified)" % (self.base_image.relative_to(self.state_root), decision["algorithm"]))
        print("  disk: %s (COW, %s)" % (self.disk.relative_to(self.state_root), self.meta["disk_size"]))
        print("  ssh:  127.0.0.1:%d user %s" % (self.meta["ssh_port"], SSH_USER))
        print("  key:  %s (private, mode 0600, never printed)" % self.private_key.relative_to(self.state_root))
        return 0


class QmpClient:
    def __init__(self, path, timeout=5):
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.socket.settimeout(timeout)
        self.stream = None
        try:
            self.socket.connect(str(path))
            self.stream = self.socket.makefile("rwb")
            greeting = self._read()
            if "QMP" not in greeting:
                raise VmError("unexpected QMP greeting")
            self.command("qmp_capabilities")
        except Exception:
            self.close()
            raise

    def _read(self):
        line = self.stream.readline()
        if not line:
            raise VmError("QMP connection closed")
        try:
            return json.loads(line.decode("utf-8"))
        except ValueError as exc:
            raise VmError("invalid QMP response") from exc

    def _wait_for_return(self):
        while True:
            message = self._read()
            if "error" in message:
                description = (message.get("error") or {}).get("desc", "QMP command failed")
                raise VmError(description)
            if "return" in message:
                return message["return"]

    def command(self, name, **arguments):
        payload = {"execute": name}
        if arguments:
            payload["arguments"] = arguments
        self.stream.write(json.dumps(payload).encode("utf-8") + b"\n")
        self.stream.flush()
        return self._wait_for_return()

    def close(self):
        if self.stream is not None:
            try:
                self.stream.close()
            except OSError:
                pass
        try:
            self.socket.close()
        except OSError:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def qmp_powerdown(path):
    try:
        with QmpClient(path) as client:
            client.command("system_powerdown")
        return True
    except (OSError, VmError, ValueError):
        return False


def ssh_banner(port, timeout=2):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout) as connection:
            connection.settimeout(timeout)
            data = b""
            while len(data) < 512:
                block = connection.recv(256)
                if not block:
                    break
                data += block
                if b"\n" in data:
                    break
            line = data.split(b"\n", 1)[0].decode("ascii", "replace").strip()
            return line if line.startswith("SSH-") else None
    except OSError:
        return None


class VmOperations(Vm):
    def ssh_arguments(self, connect_timeout=3):
        self.known_hosts.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self.known_hosts.parent, 0o700)
        if not self.known_hosts.exists():
            self.known_hosts.touch(mode=0o600, exist_ok=True)
        os.chmod(self.known_hosts, 0o600)
        return [
            "ssh", "-T", "-p", str(self.meta["ssh_port"]),
            "-i", str(self.private_key),
            "-o", "BatchMode=yes",
            "-o", "IdentitiesOnly=yes",
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", "UserKnownHostsFile=%s" % self.known_hosts,
            "-o", "GlobalKnownHostsFile=/dev/null",
            "-o", "PasswordAuthentication=no",
            "-o", "KbdInteractiveAuthentication=no",
            "-o", "ConnectionAttempts=1",
            "-o", "ConnectTimeout=%d" % connect_timeout,
            "-o", "LogLevel=ERROR",
            "%s@127.0.0.1" % SSH_USER,
        ]

    def ssh_run(self, command, *, timeout=10, connect_timeout=3):
        return run(self.ssh_arguments(connect_timeout) + [command], check=False, timeout=timeout)

    def ssh_ready(self, connect_timeout=3):
        result = self.ssh_run("true", timeout=connect_timeout + 3, connect_timeout=connect_timeout)
        return result.returncode == 0

    def ssh_poweroff(self):
        for command in ("sudo -n systemctl poweroff", "sudo -n /sbin/poweroff"):
            result = self.ssh_run(command, timeout=10, connect_timeout=3)
            if result.returncode in (0, 255):
                return True
        return False

    def guest_info(self):
        remote = (
            "sh -c '. /etc/os-release; echo \"$PRETTY_NAME\"; echo \"$VERSION_ID\"; "
            "uname -r; cloud-init status --short 2>/dev/null || true'"
        )
        result = self.ssh_run(remote, timeout=10, connect_timeout=5)
        if result.returncode != 0:
            return {"error": (result.stderr or "guest command failed").strip()[:300]}
        lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        guest = {}
        for index, field in enumerate(("pretty_name", "version_id", "kernel")):
            if index < len(lines):
                guest[field] = lines[index]
        if len(lines) > 3:
            guest["cloud_init"] = " ".join(lines[3:])
        return guest

    def wait_for_ssh(self, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.container_running():
                raise VmError("the VM stopped before SSH became ready")
            if self.ssh_ready(connect_timeout=3):
                return True
            time.sleep(2)
        return False

    def boot(self, *, memory, cpus, vnc, vnc_display, wait):
        self.require_prepared()
        require_kvm()
        ensure_qemu_image(self.image)
        existing = self.container_info()
        if existing is not None:
            if (existing.get("State") or {}).get("Running"):
                raise VmError("%s is already running; use status or stop" % self.meta["container"])
            docker(["container", "rm", existing["Id"]])
        require_port_free(self.meta["ssh_port"], "SSH")
        display = vnc_display if vnc_display is not None else self.meta["vnc_display"]
        if vnc:
            require_port_free(5900 + display, "VNC")
        self.ensure_directories()
        self.runtime_directory.mkdir(parents=True, exist_ok=True)
        os.chmod(self.runtime_directory, 0o700)
        self.qmp_socket.unlink(missing_ok=True)

        qemu = [
            "qemu-system-x86_64",
            "-name", "layer-" + self.meta["hostname"].removeprefix("layer-"),
            "-machine", "q35,accel=kvm",
            "-cpu", "host",
            "-smp", str(cpus),
            "-m", str(memory),
            "-display", "none",
            "-vga", "none",
            "-device", "virtio-vga",
            "-device", "virtio-rng-pci",
            "-device", "virtio-net-pci,netdev=net0",
            "-netdev", "user,id=net0,hostfwd=tcp:127.0.0.1:%d-:22" % self.meta["ssh_port"],
            "-drive", "file=/state/%s/disk.qcow2,if=virtio,format=qcow2,cache=writeback,discard=unmap" % self.key,
            "-drive", "file=/state/%s/seed.iso,if=virtio,format=raw,readonly=on" % self.key,
            "-qmp", "unix:/run/layer-vm/qmp.sock,server=on,wait=off",
            "-rtc", "base=utc",
            "-boot", "order=c",
            "-no-reboot",
        ]
        if vnc:
            qemu.extend(["-vnc", "127.0.0.1:%d" % display])

        command = [
            "run", "-d", "--rm",
            "--name", self.meta["container"],
            "--label", "%s=%s" % (OWNER_LABEL, OWNER_VALUE),
            "--label", "%s=%s" % (DISTRO_LABEL, self.key),
            "--label", "io.layer-recorder.managed=true",
            "--label", "io.layer-recorder.state=" + self.state_id,
            "--device", "/dev/kvm",
            "--network", "host",
            "--user", "%d:%d" % (os.getuid(), os.getgid()),
            *kvm_group_arguments(),
            "--read-only",
            "--tmpfs", "/tmp:rw,nosuid,nodev,size=256m",
            "--security-opt", "no-new-privileges",
            "-e", "HOME=/tmp",
            "-v", "%s:/state:rw" % self.state_root,
            "-v", "%s:/run/layer-vm:rw" % self.runtime_directory,
            self.image,
            *qemu,
        ]
        result = docker(command)
        container_id = result.stdout.strip()
        time.sleep(1.5)
        try:
            current = self.container_info()
        except VmError:
            current = None
        if not current or not (current.get("State") or {}).get("Running"):
            logs = docker(["logs", "--tail", "60", self.meta["container"]], check=False)
            detail = ((logs.stdout or "") + (logs.stderr or "")).strip()
            raise VmError("QEMU container exited immediately%s" % (("\n" + detail[-1500:]) if detail else ""))

        state = self.read_state()
        state.update(
            {
                "schema": 1,
                "distro": self.key,
                "container": self.meta["container"],
                "container_id": container_id[:12],
                "boots": int(state.get("boots", 0)) + 1,
                "last_boot": utc_now(),
                "memory_mb": memory,
                "cpus": cpus,
                "vnc_enabled": bool(vnc),
                "vnc_display": display if vnc else None,
                "prepared": True,
            }
        )
        self.write_state(state)
        print("started %s" % self.meta["container"])
        print("  ssh: 127.0.0.1:%d (user %s, loopback only)" % (self.meta["ssh_port"], SSH_USER))
        print("  gpu: virtio-vga (virtual only; no host GPU passthrough)")
        if vnc:
            print("  vnc: 127.0.0.1:%d (loopback only, unauthenticated)" % (5900 + display))
        if wait:
            if not self.wait_for_ssh(wait):
                raise VmError(
                    "%s started but SSH was not ready within %d seconds; the VM remains running"
                    % (self.meta["container"], wait)
                )
            print("  ssh readiness: ready")
        return 0

    def status(self, ssh_timeout):
        state = self.read_state()
        info = self.container_info()
        running = bool(info and (info.get("State") or {}).get("Running"))
        prepared = self.is_prepared()
        ssh = {
            "host": "127.0.0.1",
            "port": self.meta["ssh_port"],
            "user": SSH_USER,
            "ready": False,
            "banner": None,
        }
        guest = None
        if running:
            ssh["banner"] = ssh_banner(self.meta["ssh_port"], timeout=min(2, ssh_timeout))
            try:
                ssh["ready"] = self.ssh_ready(connect_timeout=ssh_timeout)
                if ssh["ready"]:
                    guest = self.guest_info()
            except VmError as exc:
                ssh["error"] = str(exc)
        container = {"name": self.meta["container"], "present": info is not None, "running": running, "status": "absent"}
        if info is not None:
            container["status"] = (info.get("State") or {}).get("Status", "unknown")
            container["started_at"] = (info.get("State") or {}).get("StartedAt")
            container["id"] = (info.get("Id") or "")[:12]
        vnc_enabled = bool(state.get("vnc_enabled")) if running else False
        vnc_display = state.get("vnc_display") or self.meta["vnc_display"]
        result = {
            "distro": self.key,
            "prepared": prepared,
            "boots": int(state.get("boots", 0)),
            "last_boot": state.get("last_boot"),
            "last_stop": state.get("last_stop"),
            "ready": bool(ssh["ready"]),
            "version": (guest or {}).get("pretty_name"),
            "container": container,
            "ssh": ssh,
            "guest": guest,
            "gpu": {
                "device": "virtio-vga",
                "host_gpu_passthrough": False,
                "hardware_encoding_tested": False,
                "note": "Virtual framebuffer only; no host GPU or hardware encoder is exposed.",
            },
            "vnc": {
                "enabled": vnc_enabled,
                "endpoint": ("127.0.0.1:%d" % (5900 + vnc_display)) if vnc_enabled else None,
            },
            "disk": {
                "path": str(self.disk.relative_to(self.state_root)),
                "format": "qcow2",
                "size": self.meta["disk_size"],
                "cow": True,
            },
            "memory_mb": state.get("memory_mb"),
            "cpus": state.get("cpus"),
        }
        if not prepared:
            result["hint"] = "run: python3 tools/distro-vms.py prepare %s" % self.key
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0

    def stop(self, timeout):
        info = self.container_info()
        if info is None:
            print("%s is not running" % self.meta["container"])
            return 0
        if not (info.get("State") or {}).get("Running"):
            print("%s is already stopped" % self.meta["container"])
            return 0

        def ssh_available():
            try:
                return self.ssh_ready(connect_timeout=3)
            except VmError:
                return False

        methods = []
        if qmp_powerdown(self.qmp_socket):
            methods.append("qmp")
        qmp_sent = "qmp" in methods
        if not qmp_sent and ssh_available():
            if self.ssh_poweroff():
                methods.append("ssh")

        start = time.monotonic()
        ssh_attempted = "ssh" in methods
        next_qmp_attempt = 5
        next_ssh_attempt = 8
        while time.monotonic() - start < timeout:
            if not self.container_running():
                break
            elapsed = time.monotonic() - start
            if not qmp_sent and elapsed >= next_qmp_attempt:
                next_qmp_attempt = elapsed + 5
                if qmp_powerdown(self.qmp_socket):
                    methods.append("qmp")
                    qmp_sent = True
            if not ssh_attempted and elapsed >= next_ssh_attempt:
                next_ssh_attempt = elapsed + 10
                if ssh_available():
                    self.ssh_poweroff()
                    methods.append("ssh")
                    ssh_attempted = True
            time.sleep(2)
        if self.container_running():
            current = self.container_info()
            if current is not None and (current.get("State") or {}).get("Running"):
                print("graceful powerdown timed out; stopping only the owned container", file=sys.stderr)
                stopped = docker(["stop", "--time", "10", current["Id"]], check=False, timeout=30)
                methods.append("docker-stop" if stopped.returncode == 0 else "docker-stop-failed")
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline and self.container_running():
                    time.sleep(1)
        if self.container_running():
            raise VmError("%s did not stop; inspect it before acting" % self.meta["container"])
        try:
            state = self.read_state()
            state.update({"last_stop": utc_now(), "last_stop_method": ",".join(methods) or "stopped"})
            self.write_state(state)
        except (OSError, VmError):
            pass
        self.qmp_socket.unlink(missing_ok=True)
        print("stopped %s (%s)" % (self.meta["container"], ", ".join(methods) or "already exited"))
        return 0


def build_parser():
    parser = argparse.ArgumentParser(
        description="Prepare and run loopback-only QEMU/KVM distro VMs for Layer tests.",
        epilog="Only owned state, runtime sockets and layer-vm-<distro> containers are touched.",
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=Path(os.environ.get("LAYER_VM_STATE", DEFAULT_STATE_DIR)),
        help="private state directory (default: .state/vm-tests; mode 0700)",
    )
    parser.add_argument(
        "--image",
        default=DEFAULT_QEMU_IMAGE,
        help="local Docker image built from packaging/vm-tests/qemu-host.Dockerfile",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    prepare = subcommands.add_parser("prepare", help="download, verify and prepare a distro image")
    prepare.add_argument("distro", help="ubuntu24.04 or debian13")
    checksum = prepare.add_mutually_exclusive_group()
    checksum.add_argument("--checksum", help="expected official digest (64 or 128 hex characters)")
    checksum.add_argument("--sha256", help="expected SHA-256 digest")
    prepare.add_argument("--retries", type=int, default=4, help="download retry count (default: 4)")
    prepare.add_argument("--timeout", type=int, default=60, help="per-request timeout in seconds (default: 60)")

    boot = subcommands.add_parser("boot", help="start the prepared VM")
    boot.add_argument("distro", help="ubuntu24.04 or debian13")
    boot.add_argument("--memory", type=int, default=2048, help="guest RAM in MiB (default: 2048)")
    boot.add_argument("--cpus", type=int, default=2, help="guest vCPUs (default: 2)")
    boot.add_argument("--vnc", action="store_true", help="serve the guest display on 127.0.0.1 only")
    boot.add_argument("--vnc-display", type=int, help="VNC display number; default is distro metadata")
    boot.add_argument(
        "--wait",
        nargs="?",
        const=180,
        type=int,
        default=None,
        help="wait up to N seconds for SSH readiness (default when flag is bare: 180)",
    )

    status = subcommands.add_parser("status", help="print JSON status and SSH readiness")
    status.add_argument("distro", help="ubuntu24.04 or debian13")
    status.add_argument("--ssh-timeout", type=int, default=5, help="SSH connect timeout in seconds (default: 5)")

    stop = subcommands.add_parser("stop", help="gracefully power down the owned VM")
    stop.add_argument("distro", help="ubuntu24.04 or debian13")
    stop.add_argument("--timeout", type=int, default=90, help="graceful shutdown timeout in seconds (default: 90)")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        key = canonical_distro(args.distro)
    except VmError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    try:
        vm = VmOperations(key, args.state_dir, args.image)
        if args.command == "prepare":
            if args.retries < 1 or args.retries > 20:
                raise VmError("--retries must be between 1 and 20")
            if args.timeout < 5 or args.timeout > 600:
                raise VmError("--timeout must be between 5 and 600 seconds")
            checksum = args.sha256 or args.checksum
            return vm.prepare(checksum=checksum, retries=args.retries, timeout=args.timeout)
        if args.command == "boot":
            if args.memory < 512 or args.memory > 65536:
                raise VmError("--memory must be between 512 and 65536 MiB")
            if args.cpus < 1 or args.cpus > 32:
                raise VmError("--cpus must be between 1 and 32")
            if args.vnc_display is not None and not args.vnc:
                raise VmError("--vnc-display requires --vnc")
            if args.vnc_display is not None and not (1 <= args.vnc_display <= 99):
                raise VmError("--vnc-display must be between 1 and 99")
            if args.wait is not None and not (0 <= args.wait <= 3600):
                raise VmError("--wait must be between 0 and 3600 seconds")
            return vm.boot(
                memory=args.memory,
                cpus=args.cpus,
                vnc=args.vnc,
                vnc_display=args.vnc_display,
                wait=args.wait,
            )
        if args.command == "status":
            if args.ssh_timeout < 1 or args.ssh_timeout > 60:
                raise VmError("--ssh-timeout must be between 1 and 60 seconds")
            return vm.status(args.ssh_timeout)
        if args.command == "stop":
            if args.timeout < 1 or args.timeout > 600:
                raise VmError("--timeout must be between 1 and 600 seconds")
            return vm.stop(args.timeout)
        raise VmError("unknown command")
    except VmError as exc:
        if args.command == "status":
            print(json.dumps({"distro": key, "error": str(exc)}, indent=2, sort_keys=True))
            return 2
        print("error: %s" % exc, file=sys.stderr)
        return 1
    except OSError as exc:
        if args.command == "status":
            print(json.dumps({"distro": key, "error": str(exc)}, indent=2, sort_keys=True))
            return 2
        print("error: %s" % exc, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
