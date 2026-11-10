# Security

Do not report credentials, private recordings, home configuration or unredacted logs in a public issue. Use GitHub private vulnerability reporting when available; otherwise open a minimal issue requesting a private contact channel without disclosing exploit-sensitive details.

Keep FFmpeg, Qt, graphics/media drivers and other dependencies updated. Imported media and project files should be treated as untrusted input. Layer validates project-relative media paths and protects original output paths, but this is not a security sandbox.

Capture runs as the current user. The RAM directory is private and must be memory-backed. The installer does not grant system capabilities, replace packaged binaries or silently change device permissions. Optional dependencies/model downloads are explicit user actions.

The public-tree check is a release guard for common accidental files/credential patterns, not a complete secret scanner or independent security audit.
