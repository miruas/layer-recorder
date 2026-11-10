"""Private Linux RAM replay storage, separate from durable recordings/cache."""
import os
import re
import shutil
import stat
import tempfile
from pathlib import Path

MIN_FREE = 512 * 1024 * 1024


def filesystem_type(path):
    target=Path(path).resolve();matches=[]
    for line in Path('/proc/self/mountinfo').read_text().splitlines():
        before,after=line.split(' - ',1)
        mount=Path(re.sub(r'\\([0-7]{3})',lambda m:chr(int(m[1],8)),before.split()[4]))
        if target.is_relative_to(mount):matches.append((len(mount.parts),after.split()[0]))
    return max(matches,default=(0,''))[1]


def create_ram_buffer():
    base=Path('/dev/shm')
    if filesystem_type(base) not in ('tmpfs','ramfs'):
        raise RuntimeError('RAM replay storage is unavailable: /dev/shm is not a memory filesystem.')
    private=base/('layer-'+str(os.getuid()))
    private.mkdir(mode=0o700,exist_ok=True)
    info=private.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.getuid():
        raise RuntimeError('RAM replay storage must be a private directory owned by this user.')
    private.chmod(0o700)
    if shutil.disk_usage(private).free<MIN_FREE:
        raise RuntimeError('RAM replay storage has less than 512 MB available. Free memory before starting capture.')
    return Path(tempfile.mkdtemp(prefix='buffer-',dir=private))
