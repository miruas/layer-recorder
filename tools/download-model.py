#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Download the optional public Whisper base model with SHA-256 verification."""
import argparse,hashlib,os,tempfile,urllib.request
from pathlib import Path
URL='https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.bin'
SHA256='60ed5bc3dd14eea856493d334349b405782ddcaf0028d4b5df4088345fba2efe'


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,default=Path(__file__).resolve().parent.parent/'speech/ggml-base.bin');args=parser.parse_args();dest=args.output.expanduser();dest.parent.mkdir(parents=True,exist_ok=True)
    if dest.exists():
        if hashlib.sha256(dest.read_bytes()).hexdigest()==SHA256:print('Verified existing model.');return
        raise SystemExit('The destination exists with different content. Choose a new output path.')
    descriptor,name=tempfile.mkstemp(prefix='.whisper-download-',dir=dest.parent);temp=Path(name);digest=hashlib.sha256()
    try:
        with os.fdopen(descriptor,'wb') as file,urllib.request.urlopen(URL,timeout=60) as response:
            while block:=response.read(1024*1024):file.write(block);digest.update(block)
        if digest.hexdigest()!=SHA256:raise RuntimeError('Model checksum mismatch; download was not installed.')
        os.link(temp,dest);print('Verified multilingual base model: '+str(dest))
    finally:temp.unlink(missing_ok=True)

if __name__=='__main__':main()
