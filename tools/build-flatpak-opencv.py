#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Build only OpenCV image/motion analysis; decoding uses Layer's known FFmpeg CLI.

Run inside the Freedesktop SDK after installing the pinned NumPy package. No
OpenCV wheel, embedded FFmpeg, extra codecs, network models or GUI are installed.
"""
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tarfile
import urllib.request

ROOT = Path(__file__).resolve().parent.parent
URL = 'https://github.com/opencv/opencv/archive/refs/tags/4.14.0.tar.gz'
SHA256 = 'ee8fb9b30eb60850431b4656447080e3737b56e45719c92b67f245950609f86e'


def main():
    work = ROOT / 'build/flatpak08/opencv'
    work.mkdir(parents=True, exist_ok=True)
    archive = work / 'source.tar.gz'
    if not archive.exists():
        with urllib.request.urlopen(URL, timeout=90) as response, archive.open('wb') as target:
            shutil.copyfileobj(response, target)
    if hashlib.sha256(archive.read_bytes()).hexdigest() != SHA256:
        raise RuntimeError('OpenCV source checksum mismatch.')
    source = work / 'opencv-4.14.0'
    if not source.is_dir():
        with tarfile.open(archive) as bundle:
            bundle.extractall(work, filter='data')
    python_root = Path('/app/lib/layer-recorder/python')
    environment = os.environ.copy()
    environment['PYTHONPATH'] = str(python_root) + os.pathsep + environment.get('PYTHONPATH', '')
    numpy_include = subprocess.check_output([sys.executable, '-c', 'import numpy; print(numpy.get_include())'], env=environment, text=True).strip()
    build = work / 'build'
    flags = [
        '-DCMAKE_INSTALL_PREFIX=/app', '-DCMAKE_BUILD_TYPE=Release',
        '-DCMAKE_INSTALL_LIBDIR=lib', '-DCMAKE_POSITION_INDEPENDENT_CODE=ON',
        '-DCMAKE_C_FLAGS=-ffile-prefix-map=' + str(ROOT) + '=/usr/src/layer',
        '-DCMAKE_CXX_FLAGS=-ffile-prefix-map=' + str(ROOT) + '=/usr/src/layer',
        '-DBUILD_SHARED_LIBS=OFF', '-DBUILD_LIST=core,imgproc,video,calib3d,features2d,python3',
        '-DBUILD_TESTS=OFF', '-DBUILD_PERF_TESTS=OFF', '-DBUILD_EXAMPLES=OFF',
        '-DBUILD_opencv_apps=OFF', '-DBUILD_DOCS=OFF', '-DBUILD_JAVA=OFF',
        '-DBUILD_opencv_videoio=OFF', '-DBUILD_opencv_highgui=OFF', '-DBUILD_opencv_dnn=OFF',
        '-DBUILD_opencv_imgcodecs=OFF', '-DBUILD_opencv_gapi=OFF',
        '-DWITH_FFMPEG=OFF', '-DWITH_GSTREAMER=OFF', '-DWITH_GTK=OFF', '-DWITH_QT=OFF',
        '-DWITH_V4L=OFF', '-DWITH_OPENCL=OFF', '-DWITH_OPENCLAMDBLAS=OFF',
        '-DWITH_OPENCLAMDFFT=OFF', '-DWITH_IPP=OFF', '-DWITH_ITT=OFF',
        '-DWITH_TBB=OFF', '-DWITH_EIGEN=OFF', '-DWITH_PROTOBUF=OFF', '-DBUILD_PROTOBUF=OFF',
        '-DWITH_LAPACK=OFF', '-DWITH_OPENEXR=OFF', '-DWITH_ADE=OFF',
        '-DENABLE_PRECOMPILED_HEADERS=OFF', '-DOPENCV_ENABLE_NONFREE=OFF',
        '-DPYTHON3_EXECUTABLE=' + sys.executable,
        '-DPYTHON3_NUMPY_INCLUDE_DIRS=' + numpy_include,
        '-DOPENCV_PYTHON3_INSTALL_PATH=' + str(python_root),
        '-DPYTHON3_PACKAGES_PATH=' + str(python_root),
    ]
    if platform.machine() == 'x86_64':
        flags += ['-DCPU_BASELINE=SSE2', '-DCPU_DISPATCH=SSE4_1,SSE4_2,AVX,AVX2']
    fingerprint = {'source_sha256': SHA256, 'python': sys.version, 'flags': flags}
    stamp = build / 'layer-configuration.json'
    if not stamp.exists() or json.loads(stamp.read_text()) != fingerprint:
        subprocess.run(['cmake', '-S', str(source), '-B', str(build), *flags], env=environment, check=True)
        stamp.write_text(json.dumps(fingerprint, sort_keys=True))
    jobs = max(1, int(os.environ.get('LAYER_JOBS', '4')))
    subprocess.run(['cmake', '--build', str(build), '--parallel', str(jobs)], env=environment, check=True)
    # Remove any old wheel payload before installing the exact-source extension.
    # The package root is fixed inside /app; no user site packages are touched.
    for item in python_root.glob('opencv_python*'):
        if item.is_dir(): shutil.rmtree(item)
    if (python_root / 'cv2').is_dir(): shutil.rmtree(python_root / 'cv2')
    subprocess.run(['cmake', '--install', str(build)], env=environment, check=True)
    for extension in (python_root / 'cv2').rglob('*.so'):
        subprocess.run(['strip', '--strip-unneeded', str(extension)], check=True)
    # Development files and unrelated cascade models are not runtime inputs.
    for directory in (Path('/app/include/opencv4'), Path('/app/lib/cmake/opencv4'), Path('/app/share/opencv4')):
        if directory.is_dir(): shutil.rmtree(directory)
    for library in Path('/app/lib').glob('libopencv*.a'): library.unlink()
    for program in Path('/app/bin').glob('opencv_*'):
        if program.is_file(): program.unlink()
    subprocess.run([sys.executable, '-c', 'import cv2; assert not hasattr(cv2,"VideoCapture"); assert hasattr(cv2,"calcOpticalFlowPyrLK"); assert hasattr(cv2,"estimateAffinePartial2D"); print(cv2.__version__)'], env=environment, check=True)
    licenses = Path('/app/share/licenses/io.github.miruas.Layer/opencv')
    licenses.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source / 'LICENSE', licenses / 'LICENSE')
    # Preserve notices from source-included third-party routines as well.
    for path in (source / '3rdparty').rglob('*'):
        if path.is_file() and path.name.upper().startswith(('LICENSE', 'COPYING', 'COPYRIGHT', 'NOTICE')):
            destination = licenses / '3rdparty' / path.relative_to(source / '3rdparty')
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destination)


if __name__ == '__main__':
    main()
