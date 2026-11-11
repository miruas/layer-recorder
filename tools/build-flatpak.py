#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Build the Layer Flatpak with flatpak's own build commands.

This intentionally does not use flatpak-builder.  It drives the installed
``flatpak build-init``, ``flatpak build``, ``flatpak build-finish``,
``flatpak build-export`` and ``flatpak build-bundle`` commands from one
reproducible recipe.

The source checkout is bind-mounted at /run/layer-src inside the SDK sandbox.
Compiling there, instead of in the checkout path, keeps every path that can end
up in an ELF file public and identical on every developer machine.  No private
model, recording, credential or user configuration is staged.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import time
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGING = ROOT / "packaging" / "flatpak"
DEFAULT_BUILD = ROOT / "build" / "flatpak08"
DEFAULT_OUTPUT = ROOT / "dist" / "flatpak"
SANDBOX_ROOT = "/run/layer-src"
DEFAULT_APP_ID = "io.github.miruas.Layer"
DEFAULT_RUNTIME = "org.freedesktop.Platform"
DEFAULT_SDK = "org.freedesktop.Sdk"
DEFAULT_SDK_EXTENSIONS = ("org.freedesktop.Sdk.Extension.rust-stable",)
DEFAULT_RUNTIME_VERSION = "26.08"
DEFAULT_BRANCH = "stable"
DEFAULT_RUNTIME_REPO = "https://dl.flathub.org/repo/flathub.flatpakrepo"
APP_ID_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)+$")
BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
REQUIRED_PYTHON = ("Pillow", "numpy")


def host_arch() -> str:
    machine = platform.machine().lower()
    return {"amd64": "x86_64", "arm64": "aarch64", "i386": "x86", "i686": "x86"}.get(machine, machine)


def log(message: str) -> None:
    print("build-flatpak: " + message, file=sys.stderr)


def die(message: str) -> "NoReturn":
    raise SystemExit("build-flatpak: error: " + message)


def run(command: list[object], *, cwd: Path | None = None) -> None:
    rendered = " ".join(shlex.quote(str(part)) for part in command)
    log("+ " + rendered)
    subprocess.run([str(part) for part in command], cwd=cwd, check=True)


def output(command: list[object], *, cwd: Path | None = None) -> str:
    return subprocess.check_output([str(part) for part in command], cwd=cwd, text=True, stderr=subprocess.DEVNULL).strip()


def git(*arguments: str) -> str | None:
    try:
        return output(["git", "-C", str(ROOT), *arguments])
    except (OSError, subprocess.CalledProcessError):
        return None


def project_version() -> str:
    try:
        with (ROOT / "pyproject.toml").open("rb") as handle:
            return str(tomllib.load(handle)["project"]["version"])
    except (OSError, KeyError, tomllib.TOMLDecodeError) as error:
        die("cannot read the project version from pyproject.toml: " + str(error))


def read_permissions() -> list[str]:
    path = PACKAGING / "permissions.txt"
    if not path.is_file():
        die("missing permissions file: " + str(path))
    result: list[str] = []
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if not line.startswith("--"):
            die(f"{path}:{number}: permission must start with --")
        result.append(line)
    return result


def read_requirements() -> list[str]:
    path = PACKAGING / "requirements.txt"
    if not path.is_file():
        die("missing Python requirements file: " + str(path))
    result: list[str] = []
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if " " in line:
            die(f"{path}:{number}: use one requirement per line")
        result.append(line)
    return result


def check_static(args: argparse.Namespace) -> None:
    for name in (
        "permissions.txt",
        "requirements.txt",
        "io.github.miruas.Layer.desktop",
        "io.github.miruas.Layer.metainfo.xml",
        "io.github.miruas.Layer.mime.xml",
    ):
        if not (PACKAGING / name).is_file():
            die("missing packaging/flatpak/" + name)

    if args.app_id != DEFAULT_APP_ID:
        die("The matching host helpers require app ID "+DEFAULT_APP_ID)
    if not APP_ID_RE.fullmatch(args.app_id):
        die("invalid Flatpak application id: " + args.app_id)
    if not BRANCH_RE.fullmatch(args.branch):
        die("invalid Flatpak branch: " + args.branch)
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]+){1,3}", args.runtime_version):
        die("invalid runtime version: " + args.runtime_version)

    permissions = read_permissions()
    required_permissions = {
        "--device=all",
        "--filesystem=host",
        "--share=ipc",
        "--share=network",
        "--socket=pulseaudio",
        "--socket=wayland",
        "--socket=x11",
        "--talk-name=org.freedesktop.Flatpak",
    }
    missing = sorted(required_permissions.difference(permissions))
    if missing:
        die("packaging/flatpak/permissions.txt is missing: " + ", ".join(missing))
    if not any("com.dec05eba.gpu_screen_recorder" in item for item in permissions):
        die("permissions must grant access to shared GPU Screen Recorder preferences")

    requirements = read_requirements()
    names = {re.split(r"[=<>!~ ]", item, maxsplit=1)[0].lower() for item in requirements}
    missing = [name for name in REQUIRED_PYTHON if name.lower() not in names]
    if missing:
        die("packaging/flatpak/requirements.txt is missing: " + ", ".join(missing))

    dockerfile = ROOT / "packaging" / "vm-tests" / "qemu-host.Dockerfile"
    if not dockerfile.is_file():
        die("missing packaging/vm-tests/qemu-host.Dockerfile")
    text = dockerfile.read_text(encoding="utf-8")
    if "FROM buildpack-deps:trixie-curl" not in text:
        die("the VM test image must use buildpack-deps:trixie-curl")
    if 'Verify-Peer "false"' in text or "sslverify=false" in text or "Acquire::https::Verify-Peer" in text:
        die("the VM test image must not disable TLS verification")

    source_patch = ROOT / "patches" / "gsr-ui-layer.patch"
    if not source_patch.is_file():
        die("missing GSRUI patch: " + str(source_patch))

    log(f"application: {args.app_id} ({args.branch})")
    log(f"runtime: {args.runtime}//{args.runtime_version}")
    log(f"sdk: {args.sdk}//{args.runtime_version}")
    log("sdk extensions: " + ", ".join(args.sdk_extensions))
    log(f"target architecture: {args.arch}")
    log(f"permissions: {len(permissions)} finish arguments")
    log(f"Python wheels: {', '.join(requirements)}")
    log("static checks passed")


def resolve_paths(args: argparse.Namespace) -> dict[str, Path | str]:
    try:
        build_dir = args.build_dir.expanduser().resolve()
    except OSError as error:
        die("cannot resolve the build directory: " + str(error))
    if not build_dir.is_relative_to(ROOT):
        die("--build-dir must be inside the checkout so it can be mounted at " + SANDBOX_ROOT)
    if build_dir == ROOT:
        die("--build-dir cannot be the checkout root")

    relative_build = build_dir.relative_to(ROOT).as_posix()
    sandbox_build = SANDBOX_ROOT + "/" + relative_build

    def inside(path: Path | None, default: Path, label: str) -> Path:
        value = (path.expanduser() if path else default).resolve()
        if not value.is_relative_to(ROOT):
            die(label + " must be inside the checkout so it can be mounted at " + SANDBOX_ROOT)
        return value

    cargo_home = inside(args.cargo_home, build_dir / "cargo", "--cargo-home")
    gsr_build = inside(args.gsr_build, build_dir / "gsr-build", "--gsr-build")
    return {
        "build": build_dir,
        "sandbox_build": sandbox_build,
        "appdir": build_dir / "app",
        "cargo_home": cargo_home,
        "cargo_target": build_dir / "cargo-target",
        "pip_cache": build_dir / "pip-cache",
        "gsr_build": gsr_build,
        "gsr_ui_stage": build_dir / "gsr-ui-stage",
        "gsr_ui_input": build_dir / "gsr-ui-input",
        "libexec_input": build_dir / "libexec-input",
        "steps": build_dir / "steps.sh",
    }


def ensure_clean(args: argparse.Namespace, paths: dict[str, Path | str]) -> None:
    if not args.clean:
        return
    if args.reuse:
        die("--clean and --reuse cannot be used together")
    build_dir = paths["build"]
    assert isinstance(build_dir, Path)
    if build_dir.name != "flatpak08" and not build_dir.is_relative_to(ROOT / "build"):
        die("refusing to remove an unexpected build directory: " + str(build_dir))
    log("removing build directory " + str(build_dir))
    shutil.rmtree(build_dir, ignore_errors=True)


def check_tools(args: argparse.Namespace) -> None:
    if host_arch() != args.arch:
        die(
            f"flatpak build has no cross-architecture switch; run this recipe on an {args.arch} host "
            f"(current host is {host_arch()})"
        )
    if not shutil.which("flatpak"):
        die("flatpak is not installed")
    try:
        version = output(["flatpak", "--version"])
    except (OSError, subprocess.CalledProcessError) as error:
        die("cannot run flatpak: " + str(error))
    log("using " + version)
    for ref, label in (
        (f"{args.runtime}//{args.runtime_version}", "runtime"),
        (f"{args.sdk}//{args.runtime_version}", "SDK"),
    ):
        try:
            output(["flatpak", "info", "--arch", args.arch, ref])
        except (OSError, subprocess.CalledProcessError):
            die(f"the {label} is not installed: {ref}. Install it with: flatpak install flathub {ref}")
    for extension in args.sdk_extensions:
        try:
            output(["flatpak", "info", "--arch", args.arch, f"{extension}//{args.runtime_version}"])
        except (OSError, subprocess.CalledProcessError):
            die(f"the SDK extension is not installed: {extension}//{args.runtime_version}")
    help_text = output(["flatpak", "build", "--help"])
    if "--bind-mount" not in help_text:
        die("this flatpak is too old: build --bind-mount is required")


def ensure_appdir(args: argparse.Namespace, paths: dict[str, Path | str]) -> None:
    appdir = paths["appdir"]
    assert isinstance(appdir, Path)
    metadata = appdir / "metadata"
    if metadata.is_file():
        text = metadata.read_text(encoding="utf-8")
        fields = {}
        for line in text.splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                fields[key] = value
        if fields.get("name") != args.app_id:
            die(f"{appdir}/metadata belongs to {fields.get('name', 'an unknown app')}; use --clean or another --build-dir")
        expected_suffix = f"/{args.arch}/{args.runtime_version}"
        for key in ("runtime", "sdk"):
            if not fields.get(key, "").endswith(expected_suffix):
                die(f"{appdir}/metadata has an unexpected {key}: {fields.get(key, 'missing')}")
        return

    if appdir.exists() and any(appdir.iterdir()):
        die(str(appdir) + " is not an initialized flatpak build directory; use --clean or another --build-dir")
    appdir.parent.mkdir(parents=True, exist_ok=True)
    command: list[object] = ["flatpak", "build-init", "--arch", args.arch]
    for extension in args.sdk_extensions:
        command += ["--sdk-extension", extension]
    command += [str(appdir), args.app_id, args.sdk, args.runtime, args.runtime_version]
    run(command)


def gsr_build_compatible(path: Path) -> bool:
    coredata = path / "meson-private" / "coredata.dat"
    if not coredata.is_file():
        return True
    data = coredata.read_bytes()
    # A previous host-path build cannot be reused for a public bundle.  Meson
    # stores absolute source paths, so the source must already be the neutral
    # sandbox path.
    return SANDBOX_ROOT.encode() in data and str(ROOT).encode() not in data


def select_gsr_build(args: argparse.Namespace, paths: dict[str, Path | str]) -> None:
    gsr_build = paths["gsr_build"]
    assert isinstance(gsr_build, Path)
    if gsr_build_compatible(gsr_build):
        return
    explicit = args.gsr_build is not None
    replacement = gsr_build.with_name(gsr_build.name + "-public")
    if explicit:
        die(
            f"{gsr_build} was configured from the checkout path and would embed it in the binary. "
            f"Use --clean or point --gsr-build at {replacement}"
        )
    log(f"existing {gsr_build.name} was configured from a host path; using {replacement.name} for a public build")
    paths["gsr_build"] = replacement


def copy_input_dir(source: Path, destination: Path, label: str) -> None:
    source = source.expanduser().resolve()
    if not source.is_dir():
        die(f"{label} is not a directory: {source}")
    for entry in source.rglob("*"):
        if entry.is_symlink():
            die(f"{label} contains a symlink, which is not copied: {entry}")
    shutil.rmtree(destination, ignore_errors=True)
    shutil.copytree(source, destination)
    log(f"staged {label}: {source} -> {destination}")


def prepare_inputs(args: argparse.Namespace, paths: dict[str, Path | str]) -> None:
    if args.libexec_dir:
        copy_input_dir(args.libexec_dir, paths["libexec_input"], "libexec helpers")
    if args.gsr_ui_dir:
        source = args.gsr_ui_dir.expanduser().resolve()
        if not (source / "bin" / "gsr-ui").is_file():
            die("--gsr-ui-dir must contain bin/gsr-ui")
        copy_input_dir(source, paths["gsr_ui_input"], "GSRUI overlay")
        args.gsr_ui_builder = "external"

    # Host helpers require the pinned patched source even when an externally
    # compiled overlay is supplied. Verify the entire current patch on reuse.
    command = [sys.executable, ROOT / 'tools/build-native.py', 'gsr-ui', '--prepare-only']
    if (ROOT / 'build/native/gsr-ui/source').is_dir():
        command.append('--reuse')
    run(command)


def gsr_ui_command(args: argparse.Namespace) -> list[str]:
    if args.gsr_ui_builder == "external":
        return []
    dedicated = ROOT / "tools" / "build-gsr-ui.py"
    if args.gsr_ui_builder in ("auto", "build-gsr-ui") and dedicated.is_file():
        help_text = ""
        try:
            probe = subprocess.run(
                [sys.executable, str(dedicated), "--help"],
                capture_output=True, text=True, timeout=30, check=False,
            )
            help_text = probe.stdout + probe.stderr
        except (OSError, subprocess.SubprocessError):
            help_text = ""
        if "--prefix" in help_text and "--destdir" in help_text:
            return [
                "python3", "tools/build-gsr-ui.py",
                "--prefix", "/app/lib/layer-recorder/overlay",
                "--destdir", "${LAYER_GSR_UI_STAGE}",
            ]
        if args.gsr_ui_builder == "build-gsr-ui":
            die("tools/build-gsr-ui.py does not accept --prefix and --destdir")
        log("tools/build-gsr-ui.py has no recognised interface; falling back to tools/build-native.py gsr-ui")
    elif args.gsr_ui_builder == "build-gsr-ui":
        die("tools/build-gsr-ui.py is not present")

    native_source = ROOT / "build" / "native" / "gsr-ui" / "source"
    reuse = "--reuse" if native_source.is_dir() else ""
    command = ["python3", "tools/build-native.py", "gsr-ui"]
    if reuse:
        command.append(reuse)
    command += ["--prefix", "/app/lib/layer-recorder/overlay", "--destdir", "${LAYER_GSR_UI_STAGE}"]
    return command


def build_script(args: argparse.Namespace, paths: dict[str, Path | str], version: str, epoch: int) -> str:
    def q(value: Path | str) -> str:
        return shlex.quote(str(value))

    sandbox_build = str(paths["sandbox_build"])
    gsr_build = str(paths["gsr_build"]).replace(str(ROOT), SANDBOX_ROOT, 1)
    cargo_home = str(paths["cargo_home"]).replace(str(ROOT), SANDBOX_ROOT, 1)
    cargo_target = str(paths["cargo_target"]).replace(str(ROOT), SANDBOX_ROOT, 1)
    pip_cache = str(paths["pip_cache"]).replace(str(ROOT), SANDBOX_ROOT, 1)
    gsr_ui_stage = str(paths["gsr_ui_stage"]).replace(str(ROOT), SANDBOX_ROOT, 1)
    gsr_ui_input = str(paths["gsr_ui_input"]).replace(str(ROOT), SANDBOX_ROOT, 1) if args.gsr_ui_dir else ""
    libexec_input = str(paths["libexec_input"]).replace(str(ROOT), SANDBOX_ROOT, 1) if args.libexec_dir else ""
    ui_command = gsr_ui_command(args)

    lines = [
        "#!/bin/sh",
        "# Generated by tools/build-flatpak.py; rerun the builder instead of editing.",
        "set -eu",
        "umask 022",
        "export LC_ALL=C.UTF-8",
        "export LANG=C.UTF-8",
        "export PYTHONDONTWRITEBYTECODE=1",
        "export PIP_DISABLE_PIP_VERSION_CHECK=1",
        "export CARGO_TERM_COLOR=never",
        f"export SOURCE_DATE_EPOCH={epoch}",
        f"LAYER_SRC={q(SANDBOX_ROOT)}",
        f"LAYER_APP_ID={q(args.app_id)}",
        f"LAYER_VERSION={q(version)}",
        f"LAYER_STAGE={q('/app/lib/layer-recorder')}",
        f"LAYER_CARGO_HOME={q(cargo_home)}",
        f"LAYER_CARGO_TARGET={q(cargo_target)}",
        f"LAYER_PIP_CACHE={q(pip_cache)}",
        f"LAYER_GSR_SRC={q(SANDBOX_ROOT + '/upstream/gpu-screen-recorder')}",
        f"LAYER_GSR_BUILD={q(gsr_build)}",
        f"LAYER_GSR_UI_STAGE={q(gsr_ui_stage)}",
        f"LAYER_GSR_UI_INPUT={q(gsr_ui_input)}",
        f"LAYER_LIBEXEC_INPUT={q(libexec_input)}",
        f"export LAYER_JOBS={q(str(args.jobs))}",
        "export LAYER_NATIVE_BUILD_SUFFIX=sdk26.08",
        f"LAYER_WRAP_MODE={q('nodownload' if args.offline else 'default')}",
        f"LAYER_BUILD={q(sandbox_build)}",
        "",
        ". /usr/lib/sdk/rust-stable/enable.sh",
        "export PATH=\"/usr/lib/sdk/rust-stable/bin:$PATH\"",
        "",
        "mkdir -p \"$LAYER_CARGO_HOME\" \"$LAYER_CARGO_TARGET\" \"$LAYER_PIP_CACHE\" \"$LAYER_STAGE\"",
        "rm -rf \"$LAYER_STAGE/katman\" \"$LAYER_STAGE/bin\" \"$LAYER_STAGE/overlay\" \"$LAYER_STAGE/python\" /app/libexec /app/bin/layer",
        "mkdir -p \"$LAYER_STAGE/katman\" \"$LAYER_STAGE/bin\" \"$LAYER_STAGE/overlay\" \"$LAYER_STAGE/python\"",
        "",
        "# Locked Rust editor and capture supervisor.  The source root is already",
        "# /run/layer-src, so no checkout or home path is compiled into either ELF.",
        "cd \"$LAYER_SRC\"",
        "export CARGO_HOME=\"$LAYER_CARGO_HOME\"",
        "export CARGO_TARGET_DIR=\"$LAYER_CARGO_TARGET\"",
        "export CARGO_NET_GIT_FETCH_WITH_CLI=true",
        "cargo build -j \"$LAYER_JOBS\" --locked --release --manifest-path native/editor/Cargo.toml",
        "cargo build -j \"$LAYER_JOBS\" --locked --release --manifest-path native/recorder/Cargo.toml",
        "",
        "# The fork is configured exactly like the official GSR Flatpak: static FFmpeg",
        "# 9 for older NVIDIA GPUs, no host capabilities, no systemd unit and no modprobe",
        "# file.  The root-side KMS helper is handled outside the app.",
        "if [ -f \"$LAYER_GSR_BUILD/meson-private/coredata.dat\" ]; then",
        "    meson setup --reconfigure \"$LAYER_GSR_BUILD\" \"$LAYER_GSR_SRC\" \\",
        "        --prefix=/app --buildtype=release --wrap-mode=\"$LAYER_WRAP_MODE\" \\",
        "        -Dffmpeg_static=true -Dcapabilities=false -Dsystemd=false \\",
        "        -Dnvidia_suspend_fix=false -Dstrip=true -Dportal=true -Dapp_audio=true",
        "else",
        "    meson setup \"$LAYER_GSR_BUILD\" \"$LAYER_GSR_SRC\" \\",
        "        --prefix=/app --buildtype=release --wrap-mode=\"$LAYER_WRAP_MODE\" \\",
        "        -Dffmpeg_static=true -Dcapabilities=false -Dsystemd=false \\",
        "        -Dnvidia_suspend_fix=false -Dstrip=true -Dportal=true -Dapp_audio=true",
        "fi",
        "meson compile -C \"$LAYER_GSR_BUILD\" -j \"$LAYER_JOBS\"",
        "install -Dm755 \"$LAYER_GSR_BUILD/gpu-screen-recorder\" \"$LAYER_STAGE/bin/gpu-screen-recorder\"",
        "install -Dm755 \"$LAYER_GSR_BUILD/gsr-cli\" \"$LAYER_STAGE/bin/gsr-cli\"",
        "install -Dm755 \"$LAYER_GSR_BUILD/gsr-kms-server\" \"$LAYER_STAGE/bin/gsr-kms-server\"",
        "",
        "# Patched GSRUI.  Prefer a dedicated builder when one is installed; the",
        "# current tree builds the same patch through tools/build-native.py.",
        "rm -rf \"$LAYER_GSR_UI_STAGE\"",
        "mkdir -p \"$LAYER_GSR_UI_STAGE\"",
    ]
    if ui_command:
        lines += ["cd \"$LAYER_SRC\"", " ".join(ui_command)]
    lines += [
        "",
        "if [ -n \"$LAYER_GSR_UI_INPUT\" ]; then",
        "    cp -a \"$LAYER_GSR_UI_INPUT/.\" \"$LAYER_GSR_UI_STAGE/\"",
        "fi",
        "if [ -d \"$LAYER_GSR_UI_STAGE/app/lib/layer-recorder/overlay\" ]; then",
        "    cp -a \"$LAYER_GSR_UI_STAGE/app/lib/layer-recorder/overlay/.\" \"$LAYER_STAGE/overlay/\"",
        "elif [ -d \"$LAYER_GSR_UI_STAGE/overlay\" ]; then",
        "    cp -a \"$LAYER_GSR_UI_STAGE/overlay/.\" \"$LAYER_STAGE/overlay/\"",
        "else",
        "    cp -a \"$LAYER_GSR_UI_STAGE/.\" \"$LAYER_STAGE/overlay/\"",
        "fi",
        "test -x \"$LAYER_STAGE/overlay/bin/gsr-ui\"",
        "",
        "install -Dm755 \"$LAYER_CARGO_TARGET/release/layer-studio\" \"$LAYER_STAGE/bin/layer-studio\"",
        "install -Dm755 \"$LAYER_CARGO_TARGET/release/layer-recorderd\" \"$LAYER_STAGE/bin/layer-recorderd\"",
        "",
        "python3 \"$LAYER_SRC/tools/build-flatpak-drivers.py\"",
        "export LAYER_GSR_STACK=\"$LAYER_GSR_BUILD/subprojects/ffmpeg-9.0/install\"",
        "python3 \"$LAYER_SRC/tools/build-flatpak-renderer.py\"",
        "",
        "# Headless imaging dependencies live in a private target directory so",
        "# the launcher can put the application root and this directory on PYTHONPATH.",
        "python3 -m pip install --disable-pip-version-check --no-compile --no-warn-script-location \\",
        "    --only-binary=:all: --cache-dir \"$LAYER_PIP_CACHE\" \\",
        "    --target \"$LAYER_STAGE/python\" --requirement \"$LAYER_SRC/packaging/flatpak/requirements.txt\"",
        "",
        "python3 \"$LAYER_SRC/tools/build-flatpak-opencv.py\"",
        "cp -a \"$LAYER_SRC/katman/.\" \"$LAYER_STAGE/katman/\"",
        "find \"$LAYER_STAGE/katman\" -type d -name __pycache__ -prune -exec rm -rf {} +",
        "find \"$LAYER_STAGE/katman\" -type f -name '*.pyc' -delete",
        "",
        "# Optional externally built helpers.  This hook only stages files; root-side",
        "# KMS installation and permission changes stay outside the Flatpak build.",
        "if [ -n \"$LAYER_LIBEXEC_INPUT\" ]; then",
        "    mkdir -p /app/libexec",
        "    cp -a \"$LAYER_LIBEXEC_INPUT/.\" /app/libexec/",
        "fi",
        "",
        "# No model, recording, cache, backup or user setting is ever bundled.",
        "rm -rf \"$LAYER_STAGE/speech\" \"$LAYER_STAGE/models\" \"$LAYER_STAGE/cache\"",
        "python3 \"$LAYER_SRC/tools/build-native.py\" whisper --reuse",
        "install -Dm755 \"$LAYER_SRC/speech/whisper-cli\" \"$LAYER_STAGE/speech/whisper-cli\"",
        "install -Dm644 \"$LAYER_SRC/tools/download-model.py\" \"$LAYER_STAGE/tools/download-model.py\"",
        "",
        "# Rust standard-library license texts from the SDK.",
        "LICENSE_ROOT=\"$LAYER_STAGE/../licenses/io.github.miruas.Layer\"",
        "mkdir -p \"$LICENSE_ROOT\"",
        "if [ -d /usr/lib/sdk/rust-stable/share/doc/rust/licenses ]; then",
        "    cp -a /usr/lib/sdk/rust-stable/share/doc/rust/licenses \"$LICENSE_ROOT/rust-stdlib\"",
        "fi",
        "if [ -f /usr/lib/sdk/rust-stable/share/doc/rust/COPYRIGHT-library.html ]; then",
        "    cp -a /usr/lib/sdk/rust-stable/share/doc/rust/COPYRIGHT-library.html \"$LICENSE_ROOT/rust-stdlib/COPYRIGHT-library.html\"",
        "fi",
        "",
        "echo 'Flatpak build steps completed for '\"$LAYER_APP_ID\"' '\"$LAYER_VERSION\"",
    ]
    return "\n".join(lines) + "\n"


def run_build(args: argparse.Namespace, paths: dict[str, Path | str], version: str, epoch: int) -> None:
    appdir = paths["appdir"]
    steps = paths["steps"]
    assert isinstance(appdir, Path) and isinstance(steps, Path)
    steps.parent.mkdir(parents=True, exist_ok=True)
    steps.write_text(build_script(args, paths, version, epoch), encoding="utf-8")
    steps.chmod(0o755)
    command: list[object] = [
        "flatpak", "build",
        "--share=network",
        "--die-with-parent",
        f"--bind-mount={SANDBOX_ROOT}={ROOT}",
        f"--env=SOURCE_DATE_EPOCH={epoch}",
        str(appdir),
        "/bin/sh", str(paths["sandbox_build"]) + "/steps.sh",
    ]
    run(command)


def launcher_text() -> str:
    return """#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-only
set -eu
APP_ROOT=/app/lib/layer-recorder
export LAYER_FLATPAK=1
export LAYER_PYTHON="${LAYER_PYTHON:-/usr/bin/python3}"
export LANG=C.UTF-8
export LC_ALL=C.UTF-8
export PYTHONPATH="$APP_ROOT:$APP_ROOT/python${PYTHONPATH:+:$PYTHONPATH}"
export PATH="$APP_ROOT/bin:$APP_ROOT/overlay/bin:/app/bin:/app/libexec:/usr/bin${PATH:+:$PATH}"
# Share the actual GSR preferences; never seed a divergent private copy.
if [ -z "${LAYER_GSR_CONFIG:-}" ]; then
    for candidate in "$HOME/.config/gpu-screen-recorder/config_ui" "$HOME/.var/app/com.dec05eba.gpu_screen_recorder/config/gpu-screen-recorder/config_ui"; do
        if [ -f "$candidate" ]; then export LAYER_GSR_CONFIG="$candidate"; break; fi
    done
fi
exec "$LAYER_PYTHON" -m katman "$@"
"""


def stage_static(args: argparse.Namespace, paths: dict[str, Path | str], version: str, epoch: int) -> None:
    appdir = paths["appdir"]
    assert isinstance(appdir, Path)
    app = appdir / "files"
    for directory in (
        "bin",
        "share/applications",
        "share/metainfo",
        "share/mime/packages",
        "share/icons/hicolor/scalable/apps",
        "share/licenses/io.github.miruas.Layer",
        "share/doc/layer-recorder",
    ):
        (app / directory).mkdir(parents=True, exist_ok=True)

    launcher = app / "bin" / "layer"
    launcher.write_text(launcher_text(), encoding="utf-8")
    launcher.chmod(0o755)

    shutil.copy2(PACKAGING / "io.github.miruas.Layer.desktop", app / "share/applications/io.github.miruas.Layer.desktop")
    shutil.copy2(PACKAGING / "io.github.miruas.Layer.metainfo.xml", app / "share/metainfo/io.github.miruas.Layer.metainfo.xml")
    shutil.copy2(PACKAGING / "io.github.miruas.Layer.mime.xml", app / "share/mime/packages/io.github.miruas.Layer.mime.xml")
    shutil.copy2(ROOT / "katman" / "assets" / "katman.svg", app / "share/icons/hicolor/scalable/apps/io.github.miruas.Layer.svg")

    license_root = app / "share/licenses/io.github.miruas.Layer"
    for name in ("LICENSE", "NOTICE.md", "UPSTREAM.md"):
        source = ROOT / name
        if source.is_file():
            shutil.copy2(source, license_root / name)
    components = license_root / "components"
    components.mkdir(exist_ok=True)
    for source in sorted((ROOT / "licenses").glob("*")):
        if source.is_file():
            shutil.copy2(source, components / source.name)
    for name, relative in (
        ("gpu-screen-recorder-LICENSE", "upstream/gpu-screen-recorder/LICENSE"),
        ("gsr-ui-LICENSE", "licenses/gsr-ui-LICENSE"),
    ):
        source = ROOT / relative
        if source.is_file():
            shutil.copy2(source, components / name)

    # The GSR desktop helpers run on the host through the system installation.
    # Keep their documented root share paths available alongside the overlay.
    helper_resources = app / 'share/gsr-ui'
    if helper_resources.exists():
        shutil.rmtree(helper_resources)
    overlay_resources = app / 'lib/layer-recorder/overlay/share/gsr-ui'
    if not overlay_resources.is_dir():
        die('The GSR desktop helper resources are missing from the overlay.')
    shutil.copytree(overlay_resources, helper_resources)

    # License texts for Python wheels and statically linked Rust crates.
    stage_python_licenses(app, paths)
    stage_rust_licenses(app, paths)
    stage_subproject_licenses(app)

    manifest = {
        "application": args.app_id,
        "version": version,
        "branch": args.branch,
        "architecture": args.arch,
        "runtime": f"{args.runtime}//{args.runtime_version}",
        "runtime_commit": output(["flatpak","info","--show-commit","--arch",args.arch,f"{args.runtime}//{args.runtime_version}"]),
        "sdk": f"{args.sdk}//{args.runtime_version}",
        "sdk_commit": output(["flatpak","info","--show-commit","--arch",args.arch,f"{args.sdk}//{args.runtime_version}"]),
        "sdk_extension_commits": {extension:output(["flatpak","info","--show-commit","--arch",args.arch,f"{extension}//{args.runtime_version}"]) for extension in args.sdk_extensions},
        "sdk_extensions": list(args.sdk_extensions),
        "source_root": SANDBOX_ROOT,
        "source_epoch": epoch,
        "revision": git("rev-parse", "HEAD") or "unknown",
        "dirty": bool(git("status", "--porcelain")),
        "gpu_screen_recorder": json.loads((ROOT / "packaging/native-lock.json").read_text())["gpu-screen-recorder"],
        "gsr_ui": json.loads((ROOT / "packaging/native-lock.json").read_text())["gsr-ui"],
        "python_wheels": read_requirements(),
        "models": "not bundled; automatic speech recognition requires a user-provided model",
        "bundled_models": [],
    }
    (app / "share/doc/layer-recorder/SOURCES.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    for name in ("NOTICE.md", "UPSTREAM.md"):
        source = ROOT / name
        if source.is_file():
            shutil.copy2(source, app / "share/doc/layer-recorder" / name)
    shutil.copy2(ROOT / "README.md", app / "share/doc/layer-recorder/README.md")
    if (ROOT / "docs" / "FLATPAK.md").is_file():
        shutil.copy2(ROOT / "docs" / "FLATPAK.md", app / "share/doc/layer-recorder/FLATPAK.md")

    broker = ROOT/'build/flatpak08/broker'
    for name in ('kms-server-proxy','gsr-kms-server','gsr-global-hotkeys','gsr-game-tracker','gsr-wayland-bridge'):
        source=broker/'bin'/name
        if not source.is_file():die('Build static host helpers first with tools/build-flatpak-broker.py: '+name)
        shutil.copy2(source,app/'bin'/name)
    for component in ('libdrm-2.4.134','proxy'):
        source=broker/component
        if source.is_dir():
            target=license_root/component
            for file in source.rglob('*'):
                if file.is_file() and file.name.upper().startswith(('LICENSE','COPYING','COPYRIGHT','NOTICE')):
                    destination=target/file.relative_to(source);destination.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(file,destination)
    # Keep SDK development inputs for incremental compiler caches. Exclude
    # them from the exported runtime instead of deleting cached dependencies.
    validate_stage(args, paths)


def stage_python_licenses(app: Path, paths: dict[str, Path | str]) -> None:
    python_root = app / "lib/layer-recorder/python"
    destination = app / "share/licenses/io.github.miruas.Layer/python"
    if not python_root.is_dir():
        return
    for dist in sorted(python_root.glob("*.dist-info")):
        target = destination / dist.name
        copied = False
        for candidate in sorted(list((dist / "licenses").glob("*")) + list(dist.glob("LICENSE*")) + list(dist.glob("COPYING*"))):
            if candidate.is_file():
                target.mkdir(parents=True, exist_ok=True)
                shutil.copy2(candidate, target / candidate.name)
                copied = True
        if copied:
            continue


def stage_rust_licenses(app: Path, paths: dict[str, Path | str]) -> None:
    destination = app / "share/licenses/io.github.miruas.Layer/rust"
    cargo_home = paths["cargo_home"]
    assert isinstance(cargo_home, Path)
    registry_roots = list((cargo_home / "registry" / "src").glob("*"))
    packages: dict[tuple[str, str], Path] = {}
    for manifest in (ROOT / "native/editor/Cargo.lock", ROOT / "native/recorder/Cargo.lock"):
        try:
            with manifest.open("rb") as handle:
                data = tomllib.load(handle)
        except (OSError, tomllib.TOMLDecodeError):
            continue
        for package in data.get("package", []):
            name, version = package.get("name"), package.get("version")
            if not name or not version or not package.get("source"):
                continue
            for registry in registry_roots:
                candidate = registry / f"{name}-{version}"
                if candidate.is_dir():
                    packages[(name, version)] = candidate
                    break
    for (name, version), source in sorted(packages.items()):
        target = destination / f"{name}-{version}"
        metadata = {}
        cargo_toml = source / "Cargo.toml"
        if cargo_toml.is_file():
            try:
                with cargo_toml.open("rb") as handle:
                    package = tomllib.load(handle).get("package", {})
                metadata = {key: package.get(key) for key in ("name", "version", "license", "repository")}
            except (OSError, tomllib.TOMLDecodeError):
                pass
        target.mkdir(parents=True, exist_ok=True)
        for file in source.iterdir():
            if file.is_file() and file.name.upper().startswith(("LICENSE", "COPYING", "NOTICE", "COPYRIGHT")):
                shutil.copy2(file, target / file.name)
        (target / "package.json").write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def stage_subproject_licenses(app: Path) -> None:
    destination = app / "share/licenses/io.github.miruas.Layer/ffmpeg-stack"
    source_root = ROOT / "upstream" / "gpu-screen-recorder" / "subprojects"
    if not source_root.is_dir():
        return
    for name in ("ffmpeg-9.0", "x264-*", "opus-*", "lame-*", "mbedtls-*", "srt-*", "nv-codec-headers-*"):
        for directory in sorted(source_root.glob(name)):
            if not directory.is_dir():
                continue
            target = destination / directory.name
            for file in directory.iterdir():
                if file.is_file() and file.name.upper().startswith(("LICENSE", "COPYING", "NOTICE", "COPYRIGHT")):
                    target.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(file, target / file.name)


def validate_stage(args: argparse.Namespace, paths: dict[str, Path | str]) -> None:
    appdir = paths["appdir"]
    assert isinstance(appdir, Path)
    app = appdir / "files"
    required = (
        "bin/layer",
        "lib/layer-recorder/bin/layer-studio",
        "lib/layer-recorder/bin/layer-recorderd",
        "lib/layer-recorder/bin/gpu-screen-recorder",
        "lib/layer-recorder/bin/gsr-cli",
        "lib/layer-recorder/bin/gsr-kms-server",
        "lib/layer-recorder/overlay/bin/gsr-ui",
        "lib/layer-recorder/katman/__main__.py",
        "lib/layer-recorder/python/PIL",
        "lib/layer-recorder/python/numpy",
        "lib/layer-recorder/python/cv2",
        "lib/layer-recorder/python/PIL",
    )
    missing = [name for name in required if not (app / name).exists()]
    if missing:
        die("the staged application is incomplete: " + ", ".join(missing))
    for forbidden in ("lib/layer-recorder/models",):
        if (app / forbidden).exists():
            die("refusing to bundle a model directory: " + forbidden)

    speech=app/'lib/layer-recorder/speech'
    if speech.is_dir() and any(path.name!='whisper-cli' for path in speech.iterdir()):die('Only the speech executable may be bundled; no model files.')

    # Compile-time paths in our own components must be public.  Python wheels
    # are excluded because their upstream build systems may retain their own
    # vendor paths and are replaced at runtime by /app/lib/layer-recorder/python.
    markers = [str(ROOT).encode(), str(Path.home()).encode()]
    markers = [marker for marker in markers if len(marker) > 4]
    for relative in ("lib/layer-recorder/katman", "lib/layer-recorder/bin", "lib/layer-recorder/overlay", "libexec", "bin", "lib/layer-recorder/speech", "lib/layer-recorder/python/cv2"):
        base = app / relative
        if not base.exists():
            continue
        files = [base] if base.is_file() else [file for file in base.rglob("*") if file.is_file()]
        for file in files:
            try:
                data = file.read_bytes()[: 128 * 1024 * 1024]
            except OSError:
                continue
            for marker in markers:
                if marker in data:
                    die(f"private build path found in {file.relative_to(app)}; rebuild through this recipe")


def finish_and_export(args: argparse.Namespace, paths: dict[str, Path | str], version: str, epoch: int) -> Path | None:
    appdir = paths["appdir"]
    assert isinstance(appdir, Path)
    permissions = read_permissions()
    # build-finish's export directory is generated, and its presence marks a
    # finalized tree. Recreate it so incremental source builds remain usable.
    exports=appdir/'export'
    if exports.is_symlink():die('Refusing a symlink in generated Flatpak exports.')
    if exports.is_dir():shutil.rmtree(exports)
    run(["flatpak", "build-finish", "--command=layer", str(appdir), *permissions])

    repo = args.repo.expanduser().resolve()
    repo.mkdir(parents=True, exist_ok=True)
    export = [
        "flatpak", "build-export",
        "--exclude=/include/*", "--exclude=/lib/pkgconfig/*", "--exclude=/lib/*.a",
        "--arch", args.arch,
        "--subject", f"Layer {version}",
        "--body", "Layer recorder and independent-track editor",
        "--timestamp", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch)),
        str(repo), str(appdir), args.branch,
    ]
    if args.gpg_sign:
        export += ["--gpg-sign", args.gpg_sign]
    run(export)

    if not args.bundle:
        log("repository written to " + str(repo))
        return None

    output = args.output.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    bundle = output / f"{args.app_id}-{version}-{args.arch}.flatpak"
    command = [
        "flatpak", "build-bundle",
        "--arch", args.arch,
        "--runtime-repo", args.runtime_repo,
        str(repo), str(bundle), args.app_id, args.branch,
    ]
    if args.gpg_sign:
        command += ["--gpg-sign", args.gpg_sign]
    run(command)
    sums = output / "SHA256SUMS"
    import hashlib

    digest = hashlib.sha256(bundle.read_bytes()).hexdigest()
    sums.write_text(f"{digest}  {bundle.name}\n", encoding="utf-8")
    log("bundle: " + str(bundle))
    log("checksum: " + str(sums))
    return bundle


def source_epoch(args: argparse.Namespace) -> int:
    if args.timestamp is not None:
        return args.timestamp
    from_environment = os.environ.get("SOURCE_DATE_EPOCH")
    if from_environment:
        try:
            return int(from_environment)
        except ValueError:
            die("SOURCE_DATE_EPOCH is not an integer")
    commit = git("show", "-s", "--format=%ct", "HEAD")
    if commit and commit.isdigit():
        return int(commit)
    return int(time.time())


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python3 tools/build-flatpak.py --check\n"
            "  python3 tools/build-flatpak.py\n"
            "  python3 tools/build-flatpak.py --clean --gsr-ui-dir build/gsr-ui-overlay\n"
            "  python3 tools/build-flatpak.py --libexec-dir build/libexec\n"
        ),
    )
    parser.add_argument("--app-id", default=DEFAULT_APP_ID, help="Flatpak application id (default: %(default)s)")
    parser.add_argument("--runtime", default=DEFAULT_RUNTIME, help="Flatpak runtime (default: %(default)s)")
    parser.add_argument("--sdk", default=DEFAULT_SDK, help="Flatpak SDK (default: %(default)s)")
    parser.add_argument("--runtime-version", default=DEFAULT_RUNTIME_VERSION, help="runtime/SDK branch (default: %(default)s)")
    parser.add_argument("--sdk-extension", dest="sdk_extensions", action="append", default=None, help="SDK extension to include (repeatable; default: rust-stable)")
    parser.add_argument("--arch", default=host_arch(), help="target architecture; must match the host (default: %(default)s)")
    parser.add_argument("--branch", default=DEFAULT_BRANCH, help="app branch (default: %(default)s)")
    parser.add_argument("--build-dir", type=Path, default=DEFAULT_BUILD, help="flatpak build directory inside the checkout (default: build/flatpak08)")
    parser.add_argument("--cargo-home", type=Path, help="Cargo cache directory inside the checkout (default: BUILD/cargo)")
    parser.add_argument("--gsr-build", type=Path, help="existing GSR meson build directory inside the checkout (default: BUILD/gsr-build)")
    parser.add_argument("--repo", type=Path, default=DEFAULT_OUTPUT / "repo", help="ostree repository (default: dist/flatpak/repo)")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="bundle output directory (default: dist/flatpak)")
    parser.add_argument("--bundle", action=argparse.BooleanOptionalAction, default=True, help="create a .flatpak bundle (default: yes)")
    parser.add_argument("--runtime-repo", default=DEFAULT_RUNTIME_REPO, help="runtime repository advertised by the bundle")
    parser.add_argument("--timestamp", type=int, help="commit timestamp; defaults to the HEAD commit time or SOURCE_DATE_EPOCH")
    parser.add_argument("--gpg-sign", help="GPG key id for build-export and build-bundle")
    parser.add_argument("--jobs", type=int, default=max(1, os.cpu_count() or 1), help="parallel compile jobs (default: CPU count)")
    parser.add_argument("--offline", action="store_true", help="forbid GSR subproject downloads; use the local package cache")
    parser.add_argument("--reuse", action="store_true", help="keep existing build outputs (the default; cannot be combined with --clean)")
    parser.add_argument("--clean", action="store_true", help="remove the whole build directory before starting")
    parser.add_argument("--gsr-ui-builder", choices=("auto", "build-gsr-ui", "build-native", "external"), default="auto", help="patched GSRUI build hook (default: auto)")
    parser.add_argument("--gsr-ui-dir", type=Path, help="use an externally built GSRUI overlay (contains bin/gsr-ui)")
    parser.add_argument("--libexec-dir", type=Path, help="stage externally built helpers into /app/libexec")
    parser.add_argument("--check", action="store_true", help="validate the recipe, permissions and filesystem inputs without building")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.sdk_extensions is None:
        args.sdk_extensions = list(DEFAULT_SDK_EXTENSIONS)
    if not args.sdk_extensions:
        die("at least one SDK extension is required (rust-stable)")
    if args.jobs < 1:
        die("--jobs must be positive")
    if args.gsr_ui_dir and args.gsr_ui_builder not in ("auto", "external"):
        die("--gsr-ui-dir cannot be combined with --gsr-ui-builder " + args.gsr_ui_builder)

    check_static(args)
    if args.check:
        return 0

    version = project_version()
    epoch = source_epoch(args)
    check_tools(args)

    paths = resolve_paths(args)
    ensure_clean(args, paths)
    prepare_inputs(args, paths)
    select_gsr_build(args, paths)
    ensure_appdir(args, paths)

    dirty = git("status", "--porcelain")
    if dirty:
        log("warning: the working tree is dirty; the source manifest records both HEAD and the dirty state")
    log(f"building {args.app_id} {version} for {args.arch} at {paths['build']}")
    run_build(args, paths, version, epoch)
    run([sys.executable,ROOT/"tools/build-flatpak-broker.py","--appdir",paths["appdir"]])
    stage_static(args, paths, version, epoch)
    finish_and_export(args, paths, version, epoch)
    log("done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
