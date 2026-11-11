#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Assemble auditable corresponding sources for the Flatpak, without binaries or user data.

Run after staging all public changes and building the matching Flatpak. This tool
exports the Git index (not an older HEAD), exact pinned external checkouts, locked
Rust sources, original dependency archives, and Python sdists. It fails closed
when an OpenCV wheel introduces an additional untracked FFmpeg build.
"""
from __future__ import annotations

import argparse
import configparser
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import urllib.request

ROOT = Path(__file__).resolve().parent.parent
FORBIDDEN_PARTS = {'.git', '__pycache__', '.venv', 'target', '.config', 'recordings', 'installation-backups'}
PRIVATE_PATTERNS = [re.compile(rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
                    re.compile(rb'(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})')]
# Source trees may include upstream test images and certificates. Only explicit
# model/media outputs are omitted; original upstream archives remain unchanged.
OMIT_SUFFIXES = {'.mp4', '.mkv', '.webm', '.mka', '.wav', '.mp3', '.flac', '.onnx', '.gguf', '.ggml'}


def run(*command: object, cwd: Path = ROOT, **kwargs):
    return subprocess.run([str(part) for part in command], cwd=cwd, check=True, **kwargs)


def git(*command: str, cwd: Path = ROOT) -> str:
    return subprocess.check_output(['git', *command], cwd=cwd, text=True).strip()


def load_tool(filename: str):
    spec = importlib.util.spec_from_file_location(filename.replace('-', '_'), ROOT/'tools'/filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def digest(path: Path, algorithm: str = 'sha256') -> str:
    value = hashlib.new(algorithm)
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def safe_relative(value: str) -> Path:
    path = PurePosixPath(value)
    if path.is_absolute() or '..' in path.parts or not path.parts:
        raise ValueError('Unsafe source path: ' + value)
    return Path(*path.parts)


def privacy(data: bytes, label: str):
    # Generic upstream examples of Linux paths are not personal information.
    # Actual build-machine paths and credential material must never be shipped.
    if any(token in data for token in (str(ROOT).encode(), str(Path.home()).encode())):
        raise RuntimeError('Personal build path in source: ' + label)
    if any(pattern.search(data) for pattern in PRIVATE_PATTERNS):
        raise RuntimeError('Credential material in source: ' + label)
    if compiled_format(data, label):
        raise RuntimeError('Compiled executable/library in source tree: ' + label)


def compiled_format(data: bytes, filename: str) -> str | None:
    signatures = ((b'\x7fELF', 'ELF'), (b'MZ', 'PE'), (b'!<arch>\n', 'archive'),
                  (b'\x00asm', 'WebAssembly'), (b'BC\xc0\xde', 'LLVM bitcode'),
                  (b'\xcf\xfa\xed\xfe', 'Mach-O'), (b'\xfe\xed\xfa\xcf', 'Mach-O'),
                  (b'\xce\xfa\xed\xfe', 'Mach-O'), (b'\xfe\xed\xfa\xce', 'Mach-O'),
                  (b'\xca\xfe\xba\xbe', 'Mach-O universal'))
    for signature, kind in signatures:
        if data.startswith(signature):
            return kind
    # Some COFF objects have no common leading magic. A source license named
    # COPYING.LIB must remain source text, so extensions alone are insufficient.
    if Path(filename).suffix.lower() in {'.a', '.lib', '.o', '.obj', '.dll', '.exe', '.so', '.dylib', '.rlib'} and b'\0' in data:
        return 'compiled object/library'
    return None


# Exact upstream files reviewed as unused for Linux builds. No broad directory
# or extension exclusion can silently hide a newly introduced native library.
# Version and original digest changes require another review before release.
OMIT_VENDOR_COMPILED = {
    ('libloading', '0.8.9', 'tests/nagisa32.dll'): '5c69b2bd9c8a6ad04165c221075fc9fade1dd66ca697399ace528a5a62328e36',
    ('libloading', '0.8.9', 'tests/nagisa64.dll'): 'e20b95e3036f3289421abd100760874d4f455afd33c3b5b64fec56b191f7d477',
    ('windows_aarch64_gnullvm', '0.52.6', 'lib/libwindows.0.52.0.a'): 'cd18bd02383f76d06385b08252519dc0244b56db48f40ac3f5a30dbc376156bc',
    ('windows_aarch64_msvc', '0.52.6', 'lib/windows.0.52.0.lib'): 'ca85dd7757f338ba00d0bc19229f176ee426a707425ee4fcf205ec491a95bd5a',
    ('windows_i686_gnu', '0.52.6', 'lib/libwindows.0.52.0.a'): '5a94ca1fca8c0f897380a2c857f246c0a34a0796218f2700d70cd724b5fe127c',
    ('windows_i686_gnullvm', '0.52.6', 'lib/libwindows.0.52.0.a'): '3c4f19addd23d359f1ec7664e902ce3cc587e526cd24c6e6f9ed183678295e1f',
    ('windows_i686_msvc', '0.52.6', 'lib/windows.0.52.0.lib'): '820506ebd44db30b5f429891fe629f31be412e583001b13df7ed8847886ad178',
    ('windows_x86_64_gnu', '0.52.6', 'lib/libwindows.0.52.0.a'): '33f0f658b3d2108a4b7ba7809e2dcb5ad0431d9c474be5adc5efa2944f24f665',
    ('windows_x86_64_gnullvm', '0.52.6', 'lib/libwindows.0.52.0.a'): '7d7617eae9e20e8026c39861f3c59906772bc8370996b9de45d12f53e4c78465',
    ('windows_x86_64_msvc', '0.52.6', 'lib/windows.0.52.0.lib'): '24d8cbc445955b0d48041948a3c71ce2cddb948c089b25ec7106fadd9f3efde0',
    ('wit-bindgen', '0.57.1', 'wasi-cli@0.2.0.wasm'): '60b274319c9dce183269a0e3ad922b8ee4ead915f34d171de9da23a390863119',
    ('wit-bindgen', '0.57.1', 'src/rt/libwit_bindgen_cabi.a'): 'a18a4de12821dd777e6003625cc33896984a91a55bbf03c70abc5675842c60c1',
    ('wit-bindgen', '0.57.1', 'src/rt/wit_bindgen_cabi_realloc.o'): 'f5bef0033f3baaada79949e9c8e506d1de1722259c24dc752d2084784317f8de',
    ('wit-bindgen', '0.57.1', 'src/rt/wit_bindgen_cabi_wasip3.o'): '76a7b6bf1529c2657f0e7620a443db23294647b009916df1b3b55646879ef61a',
}


def sanitize_vendor(vendor: Path):
    modifications = []
    for crate in sorted(vendor.iterdir()):
        if not crate.is_dir():
            continue
        package = tomllib.loads((crate/'Cargo.toml').read_text())['package']
        name, version = package['name'], package['version']
        checksum_path = crate/'.cargo-checksum.json'
        original_checksum = digest(checksum_path)
        checksum = json.loads(checksum_path.read_text())
        removed = []
        for path in sorted(crate.rglob('*')):
            if not path.is_file():
                continue
            relative = path.relative_to(crate).as_posix()
            data = path.read_bytes()
            kind = compiled_format(data, relative)
            if not kind:
                continue
            original_sha = hashlib.sha256(data).hexdigest()
            if OMIT_VENDOR_COMPILED.get((name, version, relative)) != original_sha:
                raise RuntimeError('Unreviewed compiled vendor artifact; no source archive produced: '+crate.name+'/'+relative)
            if checksum['files'].get(relative) != original_sha:
                raise RuntimeError('Vendor fixture checksum differs from Cargo manifest: '+crate.name+'/'+relative)
            if name == 'libloading':
                reason = 'Windows-only ordinal-loading test DLL; tests/windows.rs is cfg(windows) and remains included.'
            elif name.startswith('windows_'):
                reason = 'Windows import library; this target-specific crate is unused by the Linux build.'
            else:
                reason = 'WebAssembly/WASI component or runtime object; build.rs returns before linking these on non-wasm targets. C/Rust sources remain included.'
            path.unlink()
            del checksum['files'][relative]
            removed.append({'path': 'vendor/'+crate.name+'/'+relative, 'original_sha256': original_sha,
                            'format': kind, 'reason': reason})
        if removed:
            # Cargo's package checksum continues to identify the original registry
            # archive; only its per-file map records the disclosed source changes.
            checksum_path.write_text(json.dumps(checksum, sort_keys=True, separators=(',', ':'))+'\n')
            modifications.append({'crate': name, 'version': version, 'removed_files': removed,
                                  'checksum_manifest': 'vendor/'+crate.name+'/.cargo-checksum.json',
                                  'original_checksum_manifest_sha256': original_checksum,
                                  'modified_checksum_manifest_sha256': digest(checksum_path),
                                  'original_package_checksum_preserved': checksum['package']})
    return modifications


def copy_file(source: Path, destination: Path, label: str):
    if source.is_symlink():
        # Git source symlinks are materialized only if their target stays inside
        # the source checkout; no host paths or external link targets survive.
        resolved = source.resolve(strict=True)
        if not resolved.is_relative_to(ROOT):
            raise RuntimeError('External source symlink: ' + label)
        source = resolved
    data = source.read_bytes()
    privacy(data, label)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(data)
    destination.chmod(0o755 if source.stat().st_mode & 0o111 else 0o644)


def copy_git_checkout(source: Path, destination: Path, *, expected: str | None = None):
    revision = git('rev-parse', 'HEAD', cwd=source)
    if expected and revision != expected:
        raise RuntimeError('External checkout does not match native-lock.json: ' + source.name)
    submodules = []
    omitted = []
    listing = subprocess.check_output(['git', 'ls-files', '-s', '-z'], cwd=source).decode().split('\0')
    for item in filter(None, listing):
        metadata, name = item.split('\t', 1)
        mode, object_id, stage = metadata.split()
        relative = safe_relative(name)
        if stage != '0':
            raise RuntimeError('Unmerged external source: ' + name)
        if mode == '160000':
            child = source/relative
            if not (child/'.git').exists():
                raise RuntimeError('Missing external submodule: ' + name)
            submodules.append({'path': name, **copy_git_checkout(child, destination/relative, expected=object_id)})
        elif not any(part in FORBIDDEN_PARTS for part in relative.parts):
            if relative.suffix.lower() in OMIT_SUFFIXES or (relative.name.startswith('ggml-') and relative.suffix == '.bin'):
                omitted.append(name)
                continue
            file = source/relative
            if not file.exists():
                raise RuntimeError('Missing tracked external source: ' + name)
            copy_file(file, destination/relative, name)
    return {'revision': revision, 'submodules': submodules, 'omitted_upstream_test_media': omitted}


def public_index(destination: Path):
    if subprocess.run(['git', 'diff', '--quiet'], cwd=ROOT).returncode:
        raise RuntimeError('Stage all public changes before archiving; the working tree differs from the index.')
    # The existing source audit also verifies approved upstream binary assets.
    run(sys.executable, ROOT/'tools/check-public-tree.py')
    listing = subprocess.check_output(['git', 'ls-files', '-s', '-z'], cwd=ROOT).decode().split('\0')
    for item in filter(None, listing):
        metadata, name = item.split('\t', 1)
        mode, _, stage = metadata.split()
        relative = safe_relative(name)
        if stage != '0' or mode == '160000':
            raise RuntimeError('Unmerged or unsupported index entry: ' + name)
        if any(part in FORBIDDEN_PARTS for part in relative.parts):
            raise RuntimeError('Private/generated path in public index: ' + name)
        data = subprocess.check_output(['git', 'show', ':'+name], cwd=ROOT)
        privacy(data, name)
        target = destination/relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if mode == '120000':
            raise RuntimeError('Public index symlink: ' + name)
        target.write_bytes(data)
        target.chmod(0o755 if mode == '100755' else 0o644)
    return git('write-tree')


def put_archive(source: Path, target: Path, algorithm: str, expected: str):
    if not source.is_file():
        raise RuntimeError('Missing original dependency archive: ' + source.name)
    if digest(source, algorithm) != expected:
        raise RuntimeError('Dependency archive checksum mismatch: ' + source.name)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    return {'path': target.name, algorithm: expected}


def pinned_archives(stage: Path, build: Path):
    output = stage/'dependency-archives'
    records = []
    wraps = ROOT/'upstream/gpu-screen-recorder/subprojects'
    for wrap in sorted(wraps.glob('*.wrap')):
        configuration = configparser.ConfigParser(interpolation=None)
        configuration.read(wrap)
        spec = configuration['wrap-file']
        filename = spec['source_filename']
        entry = put_archive(wraps/'packagecache'/filename, output/'gsr'/filename, 'sha256', spec['source_hash'])
        records.append({'component': wrap.stem, 'url': spec['source_url'], **entry})
    broker = load_tool('build-flatpak-broker.py')
    for filename, url, algorithm, expected in (
        ('libdrm.tar.xz', broker.DRM_URL, 'sha256', broker.DRM_SHA256),
        ('kms-server-proxy.tar.gz', broker.PROXY_URL, 'sha512', broker.PROXY_SHA512),
    ):
        entry = put_archive(build/'broker'/filename, output/'broker'/filename, algorithm, expected)
        records.append({'component': filename, 'url': url, **entry})
    driver = load_tool('build-flatpak-drivers.py')
    entry = put_archive(build/'intel-vaapi-driver/source.tar.gz', output/'intel-vaapi-driver.tar.gz', 'sha256', driver.SHA256)
    records.append({'component': 'intel-vaapi-driver', 'url': driver.URL, **entry})
    opencv = load_tool('build-flatpak-opencv.py')
    entry = put_archive(build/'opencv/source.tar.gz', output/'opencv-4.14.0.tar.gz', 'sha256', opencv.SHA256)
    records.append({'component': 'opencv', 'url': opencv.URL, **entry})
    renderer = load_tool('build-flatpak-renderer.py')
    for name, (url, expected, _) in renderer.INPUTS.items():
        # New recipes use checksum-qualified cache names; support earlier caches
        # only when they actually match the pinned current source.
        candidates = [build/'renderer'/f'{name}-{expected[:12]}.tar.gz', build/'renderer'/f'{name}.tar.gz']
        source = next((path for path in candidates if path.is_file() and digest(path) == expected), candidates[0])
        entry = put_archive(source, output/'renderer'/f'{name}.tar.gz', 'sha256', expected)
        records.append({'component': name, 'url': url, **entry})
    return records


def python_sdists(stage: Path, cache: Path, *, fetch: bool):
    cache.mkdir(parents=True, exist_ok=True)
    records = []
    for line in (ROOT/'packaging/flatpak/requirements.txt').read_text().splitlines():
        line = line.split('#', 1)[0].strip()
        if not line:
            continue
        if not re.fullmatch(r'[A-Za-z0-9_-]+==[A-Za-z0-9_.]+', line):
            raise RuntimeError('Python requirements must use exact package==version pins.')
        name, version = line.split('==')
        metadata = cache/f'{name}-{version}.json'
        if not metadata.exists():
            if not fetch:
                raise RuntimeError('Python sdist metadata is missing; use --fetch-sdists: ' + line)
            with urllib.request.urlopen(f'https://pypi.org/pypi/{name}/{version}/json', timeout=90) as response:
                metadata.write_bytes(response.read())
        package = json.loads(metadata.read_text())
        source = next((entry for entry in package['urls'] if entry['packagetype'] == 'sdist'), None)
        if not source:
            raise RuntimeError('No source distribution found for ' + line)
        filename = safe_relative(source['filename'])
        if len(filename.parts) != 1:
            raise RuntimeError('Unsafe Python source filename.')
        archive = cache/filename
        if not archive.exists():
            if not fetch:
                raise RuntimeError('Python sdist missing; use --fetch-sdists: ' + line)
            with urllib.request.urlopen(source['url'], timeout=90) as response, archive.open('wb') as output:
                shutil.copyfileobj(response, output)
        entry = put_archive(archive, stage/'dependency-archives/python'/filename, 'sha256', source['digests']['sha256'])
        records.append({'component': name, 'version': version, 'url': source['url'], **entry})
    return records


def wheel_components(stage: Path, source_manifest: Path | None):
    """Require explicit sources for non-system native libraries bundled in wheels.

    Manifest format: {"components":[{"name":"opencv-wheel-ffmpeg", "url":...,
    "source":"relative/archive.tar.xz", "sha256":..., "build_recipe":"relative/build.sh"}, ...]}.
    Include every redistributed native wheel component, not just FFmpeg. The
    attestation is reviewed by the release maintainer; filenames alone do not
    establish corresponding-source completeness.
    """
    if source_manifest is None:
        requirements = (ROOT/'packaging/flatpak/requirements.txt').read_text().lower()
        if re.search(r'^opencv[-_]', requirements, re.MULTILINE):
            raise RuntimeError('An OpenCV wheel introduces an additional FFmpeg build. '
                               'Use the source-built OpenCV recipe or supply --wheel-sources with exact native sources and recipes.')
        return []
    manifest = json.loads(source_manifest.read_text())
    components = manifest.get('components', [])
    if not components or not manifest.get('reviewed_complete'):
        raise RuntimeError('The wheel source manifest must attest reviewed_complete and list all native components.')
    results = []
    for entry in components:
        name = entry['name']
        if not re.fullmatch(r'[A-Za-z0-9_.-]+', name):
            raise RuntimeError('Unsafe wheel component name.')
        source = source_manifest.parent/safe_relative(entry['source'])
        recipe = source_manifest.parent/safe_relative(entry['build_recipe'])
        target = stage/'dependency-archives/wheel-components'/name
        archive = put_archive(source, target/source.name, 'sha256', entry['sha256'])
        copy_file(recipe, target/'BUILD.sh', name+' build recipe')
        results.append({'component': name, 'url': entry['url'], **archive})
    return results


def vendor_rust(stage: Path, cargo: str, cargo_home: Path):
    environment = os.environ.copy()
    environment['CARGO_HOME'] = str(cargo_home)
    project = stage/'layer-recorder'
    # The Flatpak builder invokes Cargo at the project root with --manifest-path.
    # Cargo discovers configuration from that working directory, so a per-crate
    # .cargo directory would silently be ignored during the packaged build.
    run(cargo, 'vendor', '--locked', '--manifest-path', ROOT/'native/editor/Cargo.toml',
        '--sync', ROOT/'native/recorder/Cargo.toml', project/'vendor',
        env=environment, stdout=subprocess.DEVNULL)
    modifications = sanitize_vendor(project/'vendor')
    (project/'VENDOR_MODIFICATIONS.json').write_text(json.dumps(modifications, indent=2, sort_keys=True)+'\n')
    (project/'.cargo').mkdir(exist_ok=True)
    (project/'.cargo/config.toml').write_text('[source.crates-io]\nreplace-with = "vendored-sources"\n[source.vendored-sources]\ndirectory = "vendor"\n')
    records = []
    for component in ('editor', 'recorder'):
        packages = tomllib.loads((ROOT/'native'/component/'Cargo.lock').read_text())['package']
        records.append({'component': component, 'packages': [
            {key: package[key] for key in ('name', 'version', 'source', 'checksum') if key in package}
            for package in packages]})
    return records, modifications


def helper_snapshot(destination: Path, spec: dict):
    patch = ROOT/spec['patch'] if spec.get('patch') else None
    metadata = {'revision': spec['revision'], 'patch_sha256': digest(patch) if patch else None}
    (destination/'.layer-source.json').write_text(json.dumps(metadata, indent=2, sort_keys=True)+'\n')
    return metadata


REBUILD = '''# Layer Flatpak corresponding sources

This archive contains the indexed Layer tree, pinned patched GSRUI and its
submodules, pinned whisper.cpp sources, locked vendored Rust dependencies,
original checksum-verified capture/renderer/helper/driver source archives,
Python source distributions, and optional reviewed sources/build recipes for
additional native libraries. OpenCV is built without its own multimedia libraries;
video decoding uses the matching known FFmpeg CLI. NumPy and Pillow are built from the included source distributions against the SDK;
NumPy uses a generic C baseline without external BLAS/Fortran runtimes, and Pillow
uses SDK image/font libraries rather than wheel-vendored codec libraries. No recordings, user settings, model weights,
compiled applications, build logs, Git metadata, or developer caches are included.

See SOURCE_MANIFEST.json and FILE_SHA256SUMS for the exact contents. The source
archive describes the matching release binary; a clean build may use the
Freedesktop SDK/runtime and compiler packages as documented system components.
Those platform packages are obtained separately from Flathub's source repositories.

To rebuild, enter layer-recorder and follow docs/FLATPAK.md. Restore external/gsr-ui
to build/native/gsr-ui/source and external/whisper to build/native/whisper/source.
The external trees already include Layer patches; do not apply them a second time.
The bundled build helper uses existing source directories with --reuse and verifies
their .layer-source.json snapshot metadata. Preserve these directories and metadata
when building; exact original revisions and submodules are in SOURCE_MANIFEST.json.
SDK builds use LAYER_NATIVE_BUILD_SUFFIX=sdk26.08 for separate compilation caches;
the verified source directories remain shared.
Copy dependency-archives/gsr/* into upstream/gpu-screen-recorder/subprojects/packagecache.
The wrap files and packagefiles there contain Meson's matching source patches.
The helper, renderer and legacy Intel recipes are tools/build-flatpak-*.py; their
source URLs, checksums, local patches and compiler flags are in those scripts.
Use their original archives from dependency-archives/ instead of downloading them.
The project root has .cargo/config.toml pointing to its unified vendor directory,
covering both locked Rust crates even when Cargo runs with --manifest-path.
VENDOR_MODIFICATIONS.json discloses the exact original hashes of removed Windows
test DLLs, Windows import libraries and WebAssembly objects. Their upstream test
and C/Rust source files remain included. Only reviewed artifacts unused by Linux
are omitted; the source archive targets Linux rather than Windows or WebAssembly.
Per-file Cargo checksum maps disclose those removals; registry package checksums
remain unchanged. Unexpected compiled vendor files cause an error.
Python source distributions are under dependency-archives/python/. Restore them
to build/flatpak08/source-sdists; tools/build-flatpak-python.py verifies their
checksums and rebuilds them with the recorded SDK/compiler flags. Build-tool pins
are in that script; they are not copied into the application runtime.
Model weights are optional user downloads, deliberately excluded from this archive.

The overall Layer fork is GPL-3.0-only. Third-party components keep their own
licenses. Every original archive and vendored dependency retains its license texts.
'''


def make_archive(stage: Path, output: Path, epoch: int):
    def normalized(info):
        info.uid = info.gid = 0
        info.uname = info.gname = 'root'
        info.mtime = epoch
        info.pax_headers = {}
        return info
    with tarfile.open(output, 'w:xz', format=tarfile.PAX_FORMAT, preset=6) as archive:
        for path in sorted(stage.rglob('*')):
            if path.is_symlink():
                raise RuntimeError('Unexpected symlink in completed source tree.')
            archive.add(path, arcname=str(Path(stage.name)/path.relative_to(stage)), recursive=False, filter=normalized)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--build-dir', type=Path, default=ROOT/'build/flatpak08')
    parser.add_argument('--output', type=Path, default=ROOT/'dist/flatpak')
    parser.add_argument('--cargo', default=shutil.which('cargo') or 'cargo')
    parser.add_argument('--cargo-home', type=Path)
    parser.add_argument('--fetch-sdists', action='store_true', help='download pinned PyPI sdists and their release metadata')
    parser.add_argument('--wheel-sources', type=Path, help='reviewed exact sources and recipes for native wheel dependencies')
    parser.add_argument('--development', action='store_true', help='allow dirty development binary provenance; never use for a public release')
    parser.add_argument('--check', action='store_true', help='report prerequisites; do not stage, vendor, fetch or archive')
    args = parser.parse_args()
    build = args.build_dir.resolve()
    lock = json.loads((ROOT/'packaging/native-lock.json').read_text())
    version = tomllib.loads((ROOT/'pyproject.toml').read_text())['project']['version']
    if args.check:
        print(json.dumps({'version': version, 'index_ready': subprocess.run(['git', 'diff', '--quiet'], cwd=ROOT).returncode == 0,
                          'external_checkouts': {name: (ROOT/'build/native'/name/'source').is_dir() for name in ('gsr-ui', 'whisper')},
                          'cargo_available': bool(shutil.which(args.cargo)),
                          'wheel_source_manifest': bool(args.wheel_sources and args.wheel_sources.is_file()),
                          'release_requires': ['staged audited public tree', 'matching completed Flatpak build',
                                               'all pinned original dependency archives', 'exact Rust vendoring',
                                               'Python sdists', 'source-built OpenCV without bundled FFmpeg' ]}, indent=2))
        return
    binary_manifest_path = build/'app/files/share/doc/layer-recorder/SOURCES.json'
    if not binary_manifest_path.is_file():
        parser.error('Build the matching Flatpak first; its SOURCES.json manifest is required.')
    binary_manifest = json.loads(binary_manifest_path.read_text())
    if binary_manifest.get('version') != version:
        parser.error('Flatpak build version does not match the source version.')
    if not args.development and (binary_manifest.get('dirty') or binary_manifest.get('revision') != git('rev-parse', 'HEAD') or subprocess.run(['git', 'diff', '--cached', '--quiet'], cwd=ROOT).returncode):
        parser.error('Public source archives require the clean committed sources used by the matching binary. Rebuild after committing, or use --development for private QA.')
    args.output.mkdir(parents=True, exist_ok=True)
    work = ROOT/'build/flatpak-sources'
    work.mkdir(parents=True, exist_ok=True)
    epoch = int(os.environ.get('SOURCE_DATE_EPOCH', git('log', '-1', '--format=%ct')))
    with tempfile.TemporaryDirectory(prefix='source-stage-', dir=work) as temporary:
        stage = Path(temporary)/f'layer-recorder-{version}-sources'
        stage.mkdir()
        index = public_index(stage/'layer-recorder')
        external = {}
        for name in ('gsr-ui', 'whisper'):
            external[name] = copy_git_checkout(ROOT/'build/native'/name/'source', stage/'external'/name, expected=lock[name]['revision'])
            external[name]['snapshot'] = helper_snapshot(stage/'external'/name, lock[name])
        records = pinned_archives(stage, build)
        records += python_sdists(stage, build/'source-sdists', fetch=args.fetch_sdists)
        records += wheel_components(stage, args.wheel_sources.resolve() if args.wheel_sources else None)
        rust, vendor_modifications = vendor_rust(stage, args.cargo, (args.cargo_home or build/'cargo').resolve())
        manifest = {'version': version, 'source_date_epoch': epoch, 'layer_index_tree': index,
                    'layer_head': git('rev-parse', 'HEAD'), 'development_candidate': args.development,
                    'flatpak_binary_provenance': binary_manifest, 'external': external, 'archives': records, 'rust': rust, 'rust_vendor_modifications': vendor_modifications,
                    'system_components': {'platform': 'org.freedesktop.Platform/26.08', 'sdk': 'org.freedesktop.Sdk/26.08',
                                          'rust_sdk_extension': 'org.freedesktop.Sdk.Extension.rust-stable/26.08'}}
        (stage/'SOURCE_MANIFEST.json').write_text(json.dumps(manifest, indent=2, sort_keys=True)+'\n')
        (stage/'REBUILD.md').write_text(REBUILD)
        sums = []
        for path in sorted(stage.rglob('*')):
            if path.is_file():
                privacy(path.read_bytes(), str(path.relative_to(stage)))
                sums.append(digest(path)+'  '+path.relative_to(stage).as_posix()+'\n')
        (stage/'FILE_SHA256SUMS').write_text(''.join(sums))
        output = args.output/f'layer-recorder-{version}-flatpak-corresponding-sources.tar.xz'
        pending = output.with_suffix(output.suffix+'.partial')
        make_archive(stage, pending, epoch)
        pending.replace(output)
        (output.parent/(output.name+'.sha256')).write_text(digest(output)+'  '+output.name+'\n')
        print(json.dumps({'archive': str(output), 'sha256': digest(output), 'index_tree': index}, indent=2))


if __name__ == '__main__':
    main()
