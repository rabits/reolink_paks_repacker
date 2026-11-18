#!/usr/bin/env python3
"""Pack an unpacked Reolink firmware directory back into a flashable .paks.

Single command:
    ./encode_paks.py firmware_new [-o OUT.paks] [--bump]

Reads firmware_new/manifest.json (written by decode_paks.py), then for each
section in the original order:
  * rootfs/app (UBI sections): if fs_<name>/ exists, rebuild its SquashFS with
    the stock parameters (xz, 256 KiB blocks, original archive uid/gid), and
    splice it into the pristine UBI template (<name>.bin) — LEB data regions
    only, all UBI headers kept byte-intact. Fails if the tree outgrows the
    volume capacity.
  * every other section (or UBI sections without an fs_ dir): take
    <name>.bin verbatim (edit those files directly to swap kernel/uboot...).
Rebuilds the SKAP container from the recorded headers and recomputes ALL
three checksum levels (section CRCs, file +0x18, file +0x04), then
re-validates its own output.

--bump: increment the build number (the 5764 in v3.1.0.5764_2512171966) in
the file header, the app section header and fs_app/version.json — required
for the camera to accept the file as an update (the upgrade binary rejects
"the same version"). Also reflected in the default output file name.
"""
import os, re, sys, json, struct, zlib, shutil, tempfile, subprocess


def tree_is_newer(dirpath, stamp_ns):
    """True if anything under dirpath is newer than stamp_ns (the .bin mtime)."""
    st = os.lstat(dirpath)
    if int(st.st_mtime) > stamp_ns:
        return True
    for root, dirs, files in os.walk(dirpath):
        for n in dirs + files:
            if int(os.lstat(os.path.join(root, n)).st_mtime) > stamp_ns:
                return True
    return False

PEB, VID_OFF, DATA_OFF = 131072, 2048, 4096
LEB = PEB - DATA_OFF
_VID = '>4sBBBBII4sIIII4sQ12sI'
VID_FIELDS = ['magic','version','vol_type','copy_flag','compat','vol_id','lnum',
              'pad','data_pad','data_size','used_ebs','pad2','pad3','sqnum','pad4','crc']

def mapped_lebs(img):
    """{lnum: peb} for volume 0 of a UBI image."""
    m = {}
    for p in range(len(img) // PEB):
        v = img[p*PEB + VID_OFF : p*PEB + VID_OFF + 64]
        if v[:4] != b'UBI!':
            continue
        f = dict(zip(VID_FIELDS, struct.unpack(_VID, bytes(v))))
        if f['vol_id'] == 0:
            m[f['lnum']] = p
    return m

def splice(template, filesystem):
    """Return new image bytes: template UBI with volume 0's payload replaced
    by `filesystem` (0xFF-padded to the original volume capacity)."""
    d = bytearray(template)
    sq = bytes(filesystem)
    lebs = [p for _, p in sorted(mapped_lebs(d).items())]
    cap = len(lebs) * LEB
    if len(sq) > cap:
        raise ValueError(f'new filesystem {len(sq)} > volume capacity {cap} '
                         f'({len(lebs)} LEBs) — shrink it (remove unused resources)')
    blob = sq + b'\xff' * (cap - len(sq))
    for i, p in enumerate(lebs):
        d[p*PEB + DATA_OFF : (p+1)*PEB] = blob[i*LEB:(i+1)*LEB]
    return bytes(d)

def crc0(b):
    return zlib.crc32(b, 0xffffffff) ^ 0xffffffff

def main(outdir, out=None, bump=False, rebuild=False):
    man = json.load(open(os.path.join(outdir, 'manifest.json')))
    file_hdr = bytearray(bytes.fromhex(man['file_header']))
    secs = [(dict(s), bytearray(bytes.fromhex(s['header']))) for s in man['sections']]
    owner = man.get('fs_owner', {})

    # optional build-number bump: "v3.1.0.5764_2512171966" -> ...5765_...
    old_fwver = man['fwver']
    if bump:
        nbump = int(re.search(r'\.(\d{4})_', man['fwver']).group(1)) + 1
        newver = re.sub(r'\.\d{4}_', f'.{nbump}_', man['fwver'], count=1)
        assert len(newver) == len(old_fwver)
        def patch_ver(buf):  # fwver appears at +0x3c (file) and +0x28 (section hdr)
            i = buf.find(old_fwver.encode())
            if i >= 0: buf[i:i+len(old_fwver)] = newver.encode().ljust(len(old_fwver), b'\0')
        patch_ver(file_hdr)
        for _meta, h in secs: patch_ver(h)
        vj = os.path.join(outdir, 'fs_app', 'version.json')
        if os.path.isfile(vj):
            txt = open(vj).read()
            frag_old, frag_new = old_fwver.split('.')[3], newver.split('.')[3]
            open(vj, 'w').write(txt.replace(frag_old, frag_new))
        print(f'build bumped {old_fwver} -> {newver}')
    else:
        newver = old_fwver

    if out is None:
        orig = man['original']
        base = os.path.basename(orig).replace(old_fwver.split('.')[3], newver.split('.')[3])
        if base == os.path.basename(orig):
            base = os.path.splitext(base)[0] + '_patched.paks'
        out = os.path.join(os.getcwd(), base)

    # gather payloads, rebuilding squashfs where an fs_ tree exists
    body = bytearray(bytes(file_hdr))
    for (meta, hdr) in secs:
        name = meta['name']
        payload_path = os.path.join(outdir, f'{name}.bin')
        payload = open(payload_path, 'rb').read()
        fsdir = os.path.join(outdir, f'fs_{name}')
        if os.path.isdir(fsdir) and payload[:4] == b'UBI#':
            # rebuild only when the tree was actually touched (or --rebuild):
            # keeps an untouched round-trip byte-identical
            stamp = man.get('unpacked_at', int(os.path.getmtime(payload_path))) + 1
            if not rebuild and not tree_is_newer(fsdir, stamp):
                print(f'  {name}: fs_{name}/ unchanged — keeping original image bytes')
                body += hdr + payload
                continue
            uid, gid = owner.get(name, [1014, 1014])
            tmp = tempfile.mktemp(suffix='.sqsh')
            cmd = ['mksquashfs', fsdir, tmp, '-comp', 'xz', '-b', '262144',
                   '-noappend', '-force-uid', str(uid), '-force-gid', str(gid),
                   '-no-progress']
            env = dict(os.environ, SOURCE_DATE_EPOCH=str(man.get('orig_mtime', 1765000000)))
            try:
                subprocess.run(cmd, check=True, capture_output=True, env=env)
            except subprocess.CalledProcessError as e:
                sys.exit('mksquashfs failed: ' + e.stderr.decode(errors='replace')[:300])
            payload = splice(payload, open(tmp, 'rb').read())
            os.remove(tmp)
            print(f'  {name}: squashfs rebuilt from {fsdir}/ '
                  f'({struct.unpack_from("<I", hdr, 0x48)[0]} B volume)')
        elif os.path.isdir(fsdir):
            print(f'  {name}: WARNING fs_{name}/ exists but section is not UBI — ignored')
        size = struct.unpack_from('<I', hdr, 0x48)[0]
        if name in ('rootfs', 'app'):
            assert len(payload) == size, f'{name} UBI must stay exactly {size} B'
        struct.pack_into('<I', hdr, 0x48, len(payload))
        struct.pack_into('<I', hdr, 0x04, crc0(payload))
        body += hdr + payload

    struct.pack_into('<I', body, 0x08, len(body))
    struct.pack_into('<I', body, 0x5c, len(body) - 104)
    struct.pack_into('<I', body, 0x18, crc0(bytes(body[0x1c:])))
    struct.pack_into('<I', body, 0x04, crc0(bytes(body[8:])))

    # self-check, then write
    off = 0x68
    assert crc0(bytes(body[8:])) == struct.unpack_from('<I', body, 4)[0]
    assert crc0(bytes(body[0x1c:])) == struct.unpack_from('<I', body, 0x18)[0]
    while off < len(body):
        size = struct.unpack_from('<I', body, off + 0x48)[0]
        assert body[off:off+4] == b'SGMI'
        assert crc0(bytes(body[off+88:off+88+size])) == struct.unpack_from('<I', body, off+4)[0]
        off += 88 + size
    assert off == len(body)
    open(out, 'wb').write(body)
    print(f'{out} written ({len(body)} bytes) — all CRCs verified')

if __name__ == '__main__':
    args = sys.argv[1:]
    bump = '--bump' in args; args = [a for a in args if a != '--bump']
    rebuild = '--rebuild' in args; args = [a for a in args if a != '--rebuild']
    o = None
    if '-o' in args:
        i = args.index('-o'); o = args[i+1]; del args[i:i+2]
    if not args:
        sys.exit(__doc__)
    main(*args, out=o, bump=bump, rebuild=rebuild)
