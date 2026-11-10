#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Fail closed if a source distribution contains local files or credential patterns."""
import re,subprocess,sys,json,hashlib
from pathlib import Path
ROOT=Path(__file__).resolve().parent.parent
ALLOWED_TOP={'native','katman','tests','docs','examples','tools','patches','licenses','packaging','.github','upstream','UPSTREAM.md','MANIFEST.in','README.md','LICENSE','NOTICE.md','CHANGELOG.md','CONTRIBUTING.md','SECURITY.md','CODE_OF_CONDUCT.md','pyproject.toml','install.py','run.sh','.gitignore','.gitattributes','.dockerignore'}
PATTERNS=[r'/ho'+r'me/[^\s/]+/',r'/run'+r'/media/[^\s/]+/',r'(?i)(?:gh[pousr]_[a-zA-Z0-9]{20,}|github_pat_[a-zA-Z0-9_]{20,})',r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----']


def main():
    assets=json.loads((ROOT/'packaging/upstream-assets.json').read_text())
    tracked=subprocess.check_output(['git','ls-files','-z'],cwd=ROOT).decode().split('\0');errors=[]
    for name in filter(None,tracked):
        file=ROOT/name;parts=Path(name).parts
        if parts[0] not in ALLOWED_TOP:errors.append(name+': unexpected top-level entry')
        if any(p in ('__pycache__','installation-backups','recordings','.env','.config') for p in parts):errors.append(name+': local/private directory')
        if file.is_symlink():errors.append(name+': symlink');continue
        data=file.read_bytes()
        if b'\0' in data:
            if name not in assets or hashlib.sha256(data).hexdigest()!=assets[name]['sha256']:errors.append(name+': unapproved binary file')
            continue
        if len(data)>1024*1024:errors.append(name+': file exceeds source-only limit')
        text=data.decode('utf8')
        for pattern in PATTERNS:
            if re.search(pattern,text):errors.append(name+': machine path or credential pattern')
    if errors:raise SystemExit('\n'.join(errors))
    print('Public source audit passed: '+str(len(list(filter(None,tracked))))+' source/documentation files; no machine paths, unapproved binaries or credential patterns.')

if __name__=='__main__':main()
