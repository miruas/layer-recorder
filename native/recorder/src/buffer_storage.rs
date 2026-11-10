// SPDX-License-Identifier: GPL-3.0-only
//! Private tmpfs-only replay storage. Never follows an existing user-directory symlink.
use anyhow::{Context, Result, bail};
use std::{
    ffi::CString,
    fs,
    os::unix::{
        ffi::OsStrExt,
        fs::{MetadataExt, PermissionsExt},
    },
    path::{Path, PathBuf},
};
pub const MIN_FREE: u64 = 512 * 1024 * 1024;
pub fn free_bytes(path: &Path) -> Result<u64> {
    let path = CString::new(path.as_os_str().as_bytes())?;
    let mut info: libc::statvfs = unsafe { std::mem::zeroed() };
    if unsafe { libc::statvfs(path.as_ptr(), &mut info) } != 0 {
        return Err(std::io::Error::last_os_error().into());
    }
    Ok(info.f_bavail.saturating_mul(info.f_frsize))
}
pub fn check_private(path: &Path) -> Result<()> {
    let m = fs::symlink_metadata(path)?;
    if !m.is_dir() || m.uid() != unsafe { libc::getuid() } {
        bail!("RAM replay storage must be a private directory owned by this user.");
    }
    fs::set_permissions(path, fs::Permissions::from_mode(0o700))?;
    Ok(())
}
pub fn create() -> Result<PathBuf> {
    let base = Path::new("/dev/shm");
    let c = CString::new(base.as_os_str().as_bytes())?;
    let mut info: libc::statfs = unsafe { std::mem::zeroed() };
    if unsafe { libc::statfs(c.as_ptr(), &mut info) } != 0 {
        return Err(std::io::Error::last_os_error().into());
    }
    if info.f_type != libc::TMPFS_MAGIC && info.f_type != 0x858458f6 {
        bail!("RAM replay storage is unavailable: /dev/shm is not a memory filesystem.");
    }
    let private = base.join(format!("layer-{}", unsafe { libc::getuid() }));
    match fs::create_dir(&private) {
        Ok(()) => (),
        Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => (),
        Err(e) => return Err(e.into()),
    };
    check_private(&private)?;
    if free_bytes(&private)? < MIN_FREE {
        bail!("RAM replay storage has less than 512 MB available.");
    }
    let dir = tempfile::Builder::new()
        .prefix("buffer-")
        .tempdir_in(&private)
        .context("Create RAM buffer")?;
    fs::set_permissions(dir.path(), fs::Permissions::from_mode(0o700))?;
    Ok(dir.keep())
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn ram_directory_is_private() {
        let p = create().unwrap();
        let m = fs::metadata(&p).unwrap();
        assert_eq!(m.mode() & 0o777, 0o700);
        assert!(free_bytes(&p).unwrap() > MIN_FREE);
        fs::remove_dir_all(p).unwrap();
    }
    #[test]
    fn symlink_is_rejected() {
        let t = tempfile::tempdir().unwrap();
        let p = t.path().join("link");
        std::os::unix::fs::symlink(t.path(), &p).unwrap();
        assert!(check_private(&p).is_err());
    }
}
