#!/usr/bin/env python3
"""Unpack Reolink .paks firmware (SKAP/PAKS container).

Layout:
  file header (104 B):
    +0x00 magic 'SKAP'
    +0x04 u32 crc32-0(file[8:])   -- reflected CRC-32 poly 0xEDB88320, init 0,
    +0x08 u32 total file size        no final xor (Linux crc32; zlib equivalent
    +0x0c u32 format version (1)     is zlib.crc32(b, 0xffffffff)^0xffffffff).
    +0x10 u32 0                      Compute AFTER +0x18: it covers that field.
    +0x14 magic 'DIDB'
    +0x18 u32 crc32-0(file[0x1c:])
    +0x1c char[32] hw model, +0x3c char[32] fw version
    +0x5c u32 payload bytes (= size-104), +0x60 u32 section count
  N sections back-to-back from 0x68, each 88-byte header + raw payload:
    +0x00 'SGMI', +0x04 u32 crc32-0(payload), +0x08 char[32] name,
    +0x28 char[32] version, +0x48 u32 payload size, +0x4c u32 flash offset,
    +0x50 u32 partition size, +0x54 u32 zero

Outputs (into <outdir>/):
  <name>.bin     raw payload per section (app/rootfs = whole-partition UBI img)
  fs_<name>/     extracted SquashFS tree for UBI sections (needs ubireader +
                 squashfs-tools on PATH: pip install ubi-reader; apt squashfs-tools)
  manifest.json  header/section bytes + squashfs uid/gid — input for pack.py

Usage: decode_paks.py FW.paks [outdir]      (default: FW.extracted/)
"""
import struct, sys, os, re, json, glob, subprocess
import time

def main(paks, outdir=None):
    data = open(paks, 'rb').read()
    outdir = outdir or (os.path.splitext(os.path.basename(paks))[0] + '.extracted')
    os.makedirs(outdir, exist_ok=True)
    assert data[:4] == b'SKAP', 'not a SKAP/paks file'
    model  = data[0x1c:0x3c].split(b'\0')[0].decode()
    fver   = data[0x3c:0x5c].split(b'\0')[0].decode()
    nsecs  = struct.unpack_from('<I', data, 0x60)[0]
    print(f'{paks}: model={model} fw={fver} sections={nsecs}')
    manifest = {'original': os.path.abspath(paks), 'model': model, 'fwver': fver,
                'file_header': data[:0x68].hex(), 'sections': [], 'fs_owner': {},
                'orig_mtime': int(os.path.getmtime(paks)),
                'unpacked_at': int(time.time())}   # final value recorded on write below

    off = 0x68
    for i in range(nsecs):
        assert data[off:off+4] == b'SGMI', f'bad SGMI at {off:#x}'
        chk, name, ver, size, flash, part = struct.unpack_from('<I32s24s8xIII', data, off + 4)
        name = name.split(b'\0')[0].decode()
        ver  = ver.split(b'\0')[0].decode(errors='replace')
        payload = data[off + 88: off + 88 + size]
        path = os.path.join(outdir, f'{name}.bin')
        open(path, 'wb').write(payload)
        manifest['sections'].append({'name': name, 'header': data[off:off+88].hex()})
        kind = 'UBI' if payload[:4] == b'UBI#' else ''
        print(f'  [{i}] {name:7s} flash={flash:#010x} size={size:#010x} part={part:#010x} ver={ver!r} {kind}')
        if kind:
            try:
                imgs = os.path.join(outdir, f'imgs_{name}')
                subprocess.run(['ubireader_extract_images', '-o', imgs, path], check=True,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                for vol in glob.glob(os.path.join(imgs, '*', 'img-*_vol-*.ubifs')):
                    fs = os.path.join(outdir, f'fs_{name}')
                    if open(vol, 'rb').read(4) == b'hsqs':
                        subprocess.run(['unsquashfs', '-q', '-f', '-d', fs, vol], check=True)
                        # record the ids stored in the archive (unsquashfs remaps on
                        # extract as non-root, so ask the volume file, not the tree)
                        lst = subprocess.run(['unsquashfs', '-lls', vol], check=True,
                                             capture_output=True, text=True).stdout
                        m = re.search(r'\s(\d+)/(\d+)\s', lst)
                        if m:
                            manifest['fs_owner'][name] = [int(m.group(1)), int(m.group(2))]
                        manifest.setdefault('vols', {})[name] = vol
                        print(f'      -> {fs}/  (uid {manifest["fs_owner"].get(name, "?")})')
                    else:
                        subprocess.run(['ubireader_extract_files', '-o', fs, vol], check=True)
                        print(f'      -> {fs}/ (UBIFS volume)')
            except (FileNotFoundError, subprocess.CalledProcessError) as e:
                print(f'      (UBI auto-extract failed: {e}; pip install ubi-reader?)')
        off += 88 + size
    assert off == len(data), f'trailing {len(data)-off} bytes'
    manifest['unpacked_at'] = int(time.time())
    json.dump(manifest, open(os.path.join(outdir, 'manifest.json'), 'w'), indent=1)
    print(f'manifest -> {os.path.join(outdir, "manifest.json")}')

if __name__ == '__main__':
    main(sys.argv[1], *sys.argv[2:])
