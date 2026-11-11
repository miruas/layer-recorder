# QEMU/KVM distro VM tests

`tools/distro-vms.py` prepares and runs clean Ubuntu 24.04 and Debian 13 cloud
images for Layer installation, SSH, desktop and packaging tests. It is a
scripted harness, not a CI job: `prepare` downloads and verifies an image, and
`boot` starts a VM only when you run it.

The harness deliberately does **not**:

- install host packages or change firewall, group, kernel or GPU settings;
- pass through a host GPU, `/dev/dri`, NVIDIA or vfio devices;
- publish ports beyond `127.0.0.1`;
- start, stop or delete unrelated containers or VMs;
- start automatically, download in the background, or ask interactive questions.

Everything it owns is under `.state/vm-tests`, `/tmp/layer-vm-*`, and containers
named `layer-vm-<distro>` carrying the `io.layer-recorder.owner=distro-vms`
label.

## Requirements

- Linux x86_64 with `/dev/kvm` readable and writable by your user. Grant that
  access outside this tool; the script never changes host permissions.
- Docker with permission to run containers.
- An OpenSSH client on the host for `status` and the SSH fallback in `stop`.
- Enough disk for the base image, a 20 GiB growable COW disk, and the guest.
- Network access for `prepare`.

If `/dev/kvm` is missing, `boot` fails instead of silently falling back to slow
emulation.

## Build the QEMU host image

```sh
docker build -f packaging/vm-tests/qemu-host.Dockerfile -t qemu-host packaging/vm-tests
```

The Ubuntu 24.04 image installs `qemu-system-x86`, `qemu-utils`,
`cloud-image-utils`, `openssh-client`, `curl` and CA certificates. It runs as a
non-root user by default. The harness overrides the UID/GID to the invoking host
user and adds the group that owns `/dev/kvm`.

The image contains no GPU passthrough configuration. `/dev/kvm` is supplied at
run time with `--device /dev/kvm`.

## Prepare a distro

```sh
python3 tools/distro-vms.py prepare ubuntu24.04
python3 tools/distro-vms.py prepare debian13
```

This creates:

```text
.state/vm-tests/<distro>/          mode 0700
  base/<official-image>            read-only, checksum verified
  disk.qcow2                       qcow2 COW overlay, 20 GiB virtual size
  seed.iso                         cloud-init NoCloud seed
  user-data, meta-data             user layerqa and its public key only
  id_ed25519, id_ed25519.pub       generated private/public key, key mode 0600
  known_hosts                      per-VM SSH host keys
  state.json                       non-secret bookkeeping
```

`prepare` is idempotent. It keeps an existing COW disk and an existing key, so
rerunning it does not reset a VM. It never overwrites a base image that fails
verification.

### Image sources and checksums

| Distro | Official cloud image | Official checksum file |
| --- | --- | --- |
| `ubuntu24.04` | `https://cloud-images.ubuntu.com/releases/24.04/release/ubuntu-24.04-server-cloudimg-amd64.img` | `SHA256SUMS` |
| `debian13` | `https://cloud.debian.org/images/cloud/trixie/latest/debian-13-genericcloud-amd64.qcow2` | `SHA512SUMS` |

Ubuntu publishes `SHA256SUMS`. Debian's Trixie cloud tree publishes
`SHA512SUMS`; the harness supports both algorithms. The image is downloaded to
a `.part` file, resumed with an HTTP range request after an interruption,
retried, hashed locally, and only then renamed into place. Redirects and TLS
certificate checks are handled by Python's standard library.

The harness never falls back to an unverified download. If the official
checksum file is unavailable or does not list the image, pass the digest
explicitly:

```sh
python3 tools/distro-vms.py prepare ubuntu24.04 --checksum <official-digest>
python3 tools/distro-vms.py prepare ubuntu24.04 --sha256 <official-sha256>
```

`--checksum` accepts a 64-character SHA-256 or 128-character SHA-512 digest.
An explicit digest is cross-checked against the official file when both are
available and use the same algorithm. If an existing base image no longer
matches, the tool stops and does not delete or replace it.

Use `--state-dir` or `LAYER_VM_STATE` to choose a different private state
directory:

```sh
python3 tools/distro-vms.py --state-dir /var/tmp/layer-vm-state prepare debian13
```

Do not place state inside a shared or world-readable directory. `.state/` is
already ignored by the project's build and Git rules.

## Boot, status and stop

```sh
python3 tools/distro-vms.py boot ubuntu24.04
python3 tools/distro-vms.py boot debian13 --wait 240
python3 tools/distro-vms.py status ubuntu24.04 | python3 -m json.tool
python3 tools/distro-vms.py stop ubuntu24.04
```

Defaults are 2048 MiB RAM and 2 vCPUs. They can be changed per boot:

```sh
python3 tools/distro-vms.py boot ubuntu24.04 --memory 4096 --cpus 4
```

`boot` returns after the container starts unless `--wait` is used. `--wait`
waits for authenticated SSH readiness and reports an error while leaving the VM
running if the deadline expires.

`status` always prints JSON. It reports the container state, boot count, SSH
banner and authentication readiness, guest `os-release` details, cloud-init
status when available, and the virtual GPU facts. A representative shape is:

```json
{
  "distro": "ubuntu24.04",
  "prepared": true,
  "boots": 1,
  "ready": true,
  "version": "Ubuntu 24.04 LTS",
  "container": {"name": "layer-vm-ubuntu24.04", "running": true, "status": "running"},
  "ssh": {"host": "127.0.0.1", "port": 22281, "user": "layerqa", "ready": true, "banner": "SSH-2.0-OpenSSH_..."},
  "guest": {"pretty_name": "Ubuntu 24.04 LTS", "version_id": "24.04", "kernel": "...", "cloud_init": "status: done"},
  "gpu": {
    "device": "virtio-vga",
    "host_gpu_passthrough": false,
    "hardware_encoding_tested": false,
    "note": "Virtual framebuffer only; no host GPU or hardware encoder is exposed."
  },
  "vnc": {"enabled": false, "endpoint": null},
  "disk": {"path": "ubuntu24.04/disk.qcow2", "format": "qcow2", "size": "20G", "cow": true}
}
```

`stop` asks the guest to power down with QMP `system_powerdown`, falls back to
`sudo -n systemctl poweroff` over SSH, waits for the container to exit, and only
after the timeout runs `docker stop` on the **owned** container. It never stops a
container whose name or ownership labels do not match. The default graceful
timeout is 90 seconds; use `--timeout` to change it.

## SSH access

The cloud-init seed creates `layerqa` with passwordless sudo, disables password
authentication and root login, and installs only the generated public key. The
private key stays inside the 0700 state directory and is never printed by the
tool.

Ubuntu 24.04 uses host port 22281; Debian 13 uses 22282. Both are bound to
`127.0.0.1` only.

```sh
ssh -p 22281 \
  -i .state/vm-tests/ubuntu24.04/id_ed25519 \
  -o UserKnownHostsFile=.state/vm-tests/ubuntu24.04/known_hosts \
  -o IdentitiesOnly=yes \
  -o StrictHostKeyChecking=accept-new \
  layerqa@127.0.0.1
```

For Debian, use port 22282 and the Debian state directory.

```sh
ssh -p 22282 \
  -i .state/vm-tests/debian13/id_ed25519 \
  -o UserKnownHostsFile=.state/vm-tests/debian13/known_hosts \
  -o IdentitiesOnly=yes \
  -o StrictHostKeyChecking=accept-new \
  layerqa@127.0.0.1
```

After login:

```sh
cloud-init status --wait
sudo apt-get update
```

Do not copy the private key out of the state directory, print it in logs, or
commit it. If the key is lost, the VM cannot be reached with the old seed; the
safe recovery is an explicit, user-approved reset of that distro's own state,
not a host-wide cleanup.

## Desktop and UI options

### VM UI portal over VNC

```sh
python3 tools/distro-vms.py boot ubuntu24.04 --vnc
```

The default loopback endpoints are `127.0.0.1:5901` for Ubuntu and
`127.0.0.1:5902` for Debian. They are unauthenticated and are not exposed on a
LAN interface. Connect with a local VNC client, or tunnel from a remote machine:

```sh
ssh -L 5901:127.0.0.1:5901 user@test-host
```

Then connect the VNC client to `127.0.0.1:5901`. Do not publish the VNC port on
`0.0.0.0`, and do not leave an unauthenticated VNC session reachable by others.

VNC shows the guest framebuffer provided by the virtual `virtio-vga` device. It
is a way to see and drive the VM desktop. It is **not** a GPU encoding path and
does not prove NVIDIA NVENC, AMD/Intel VA-API, or any hardware encoder works.

### Install a desktop inside the guest

Ubuntu 24.04, over SSH:

```sh
sudo apt-get update
sudo apt-get install -y ubuntu-desktop-minimal
sudo systemctl set-default graphical.target
sudo systemctl isolate graphical.target
```

Debian 13, over SSH:

```sh
sudo apt-get update
sudo apt-get install -y task-gnome-desktop
sudo systemctl set-default graphical.target
sudo systemctl isolate graphical.target
```

For a smaller test, install only what the test needs, for example
`openbox` and `xterm`, and start the session from the console. Package names and
desktop behaviour differ by release; verify them in the guest rather than
assuming a display manager starts automatically.

### X11 forwarding or a private RDP tunnel

X11 forwarding keeps the application window on the host:

```sh
ssh -X -p 22281 -i .state/vm-tests/ubuntu24.04/id_ed25519 \
  -o UserKnownHostsFile=.state/vm-tests/ubuntu24.04/known_hosts \
  layerqa@127.0.0.1
```

For an RDP desktop, install and enable `xrdp` in the guest, then tunnel its
loopback port instead of exposing 3389:

```sh
ssh -p 22281 -L 3389:127.0.0.1:3389 \
  -i .state/vm-tests/ubuntu24.04/id_ed25519 \
  -o UserKnownHostsFile=.state/vm-tests/ubuntu24.04/known_hosts \
  layerqa@127.0.0.1
```

Connect the RDP client to `127.0.0.1:3389`.

### VM UI portal versus hardware encoding

These are separate test questions:

- **VM UI portal:** can the guest start the desktop, display the application and
  accept input? Use VNC, X11 forwarding or a loopback RDP tunnel.
- **Hardware encoding:** can the host GPU and driver encode with NVENC or
  VA-API? This VM has no host GPU, no `/dev/dri` and no vfio device. Its
  `virtio-vga` adapter is a virtual framebuffer only.

`status` always reports `gpu.host_gpu_passthrough` and
`gpu.hardware_encoding_tested` as `false`. Never cite a result from this harness
as a hardware-encoder test. Run those tests on a physical GPU host with the
relevant driver, or use a separate, explicitly configured passthrough setup that
is outside this tool's scope.

## Ports, names and state

| Distro | Container | SSH (loopback) | VNC (loopback) | State |
| --- | --- | --- | --- | --- |
| `ubuntu24.04` | `layer-vm-ubuntu24.04` | `127.0.0.1:22281` | `127.0.0.1:5901` | `.state/vm-tests/ubuntu24.04` |
| `debian13` | `layer-vm-debian13` | `127.0.0.1:22282` | `127.0.0.1:5902` | `.state/vm-tests/debian13` |

QEMU uses user-mode networking with a single `hostfwd` rule binding
`127.0.0.1:<port>`. The optional VNC server also binds `127.0.0.1` only. The QMP
socket is kept short at `/tmp/layer-vm-<hash>/qmp.sock` and mounted into the
container as `/run/layer-vm/qmp.sock`; this avoids Unix-socket path limits even
when the checkout path is long.

The disk is a qcow2 copy-on-write overlay with a 20 GiB virtual size over the
read-only base image. The base image is never modified.

Aliases accepted by the CLI include `ubuntu`, `ubuntu-24.04`, `noble`,
`debian`, `debian-13` and `trixie`.

## Adding Fedora or openSUSE

Distro-specific behaviour lives in the `DISTROS` mapping in
`tools/distro-vms.py`; there is no engine code keyed on a distro name. Both
Fedora Cloud Base and openSUSE cloud images are qcow2 images with cloud-init
NoCloud support, so adding one requires only metadata:

- a pinned official `image_url` and matching `image_name`;
- the official `checksum_url` and `checksum_algorithm` (`sha256` or `sha512`);
- a unique `ssh_port` and `vnc_display`;
- a unique `container` name and `hostname`;
- the existing `disk_size` of `20G`.

Use the next free host ports (for example 22283 and 22284) and keep every bind on
`127.0.0.1`. Do not claim Fedora or openSUSE coverage until `prepare`, `boot`,
`status` and `stop` have actually been run for those images.

## Troubleshooting

- **`/dev/kvm` permission denied:** fix host device access yourself, then retry.
  The script never edits groups, udev rules or permissions.
- **QEMU host image missing:** run the `docker build` command above. `prepare`
  also needs the image for `qemu-img`, `cloud-localds` and `ssh-keygen`.
- **Port already in use:** stop the conflicting process or VM. The harness
  refuses to kill arbitrary listeners.
- **Container name exists but is not owned:** the harness refuses to touch it.
  Inspect it with `docker ps -a --filter name=layer-vm-` and resolve it
  deliberately.
- **Checksum mismatch:** do not bypass verification. Confirm the official
  digest, then remove only that distro's own base image if you intend to
  download again. The tool never deletes it automatically.
- **Boot but no SSH:** check `docker logs --tail 100 layer-vm-<distro>` and, if
  SSH is available, `cloud-init status --long`. The seed user is `layerqa`, not
  the cloud image's default user.
- **Guest desktop does not appear:** install a display server/desktop first;
  `virtio-vga` provides the virtual display, not a desktop environment.

## Scope and verification status

This directory implements the `prepare`, `boot`, `status` and `stop` scripts and
the Docker host recipe. Ubuntu 24.04 and Debian 13 are the configured distros.
Fedora and openSUSE are metadata extension points, not enabled claims. No
hardware GPU or encoder result can be produced by this VM design.
