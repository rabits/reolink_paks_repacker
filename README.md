# Reolink RLC-810A paks firmware unpack/patch/pack

I found that reolink paks format is relatively easy to unpack and modify to flash
your changes to the camera - for example to update the watermark image. The updater
does not validate any signature (at least RLC-810A 5764_2512171966 firmware), so
you can do whatever you want with those.

`RLC-810A.5764_2512171966.IPC_56064M8MP.paks` is Reolink's **SKAP/PAKS** container:
a 104-byte file header followed by 6 back-to-back image sections, each with an
88-byte `SGMI` header. The `rootfs` and `app` sections are whole-partition UBI
images (128 KiB PEB, 126976-byte LEB, VID headers at +2048, data at +4096,
sequential LEB i → PEB 2+i), and each UBI volume holds one xz-compressed
SquashFS (block size 262144).

| section | flash addr | size    | what                                          |
|---------|-----------|---------|-----------------------------------------------|
| loader  | 0x0       | 32 KiB  | First-stage loader (ARM Thumb code)           |
| fdt     | 0x40000   | 16 KiB  | Novatek device tree (`nvt-devname` strings)   |
| uboot   | 0x80000   | 388 KiB | U-Boot (`BCL1` image)                         |
| kernel  | 0x180000  | 1.7 MiB | uImage (`27 05 19 56`), lzma/xz self-extracting |
| rootfs  | 0x580000  | 12.5 MiB| UBI → SquashFS: BusyBox base system           |
| app     | 0x1480000 | 9.25 MiB| UBI → SquashFS: Reolink IPC app (**logo files here**) |

**Checksums**, all three levels, are reflected CRC-32 (poly 0xEDB88320, init 0,
no final xor — Linux `crc32()`, in Python: `zlib.crc32(buf, 0xffffffff) ^ 0xffffffff`):

* file header +0x04: CRC of everything from byte 8 to EOF (computed after +0x18)
* file header +0x18: CRC of everything from byte 0x1c to EOF
* each SGMI +0x04: CRC of the section payload only

## Requirements

Any Debian/Ubuntu/WSL x86-64 host; no root needed beyond package installs:

```
$ sudo apt install python3 squashfs-tools   # interpreter + mksquashfs/unsquashfs
$ python3 -m venv .venv
$ . ./.venv/bin/activate
$ pip install --user ubi-reader             # ubireader_extract_images (UBI layer)
```

## Workflow

```sh
# 1. unpack everything: sections -> <name>.bin, UBI volumes, both SquashFS
#    trees -> fs_<name>/, plus manifest.json for the packer
./decode_paks.py RLC-810A.5764_2512171966.IPC_56064M8MP.paks firmware

# 2. modify at will: new BMPs / www / configs under firmware/fs_app/ (or
#    fs_rootfs/); edit firmware/kernel.bin etc. directly to swap raw images

# 3. one command back: rebuilds SquashFS (stock xz/256 KiB params, original
#    uid/gid from the manifest) -> splices into UBI -> reassembles the
#    container from manifest headers, recomputes all 8 CRC fields,
#    self-verifies, and names the output like the input with a new build:
./encode_paks.py firmware --bump              # -> RLC-810A.5765_2512171966.IPC_56064M8MP.paks
./encode_paks.py firmware -o custom.paks      # or choose the output name yourself
```

`encode_paks.py` rebuilds a section's SquashFS only when `fs_<name>/` contains
files modified after unpack (checked against `manifest.json:unpacked_at`);
otherwise the original image bytes are reused and an untouched
unpack→pack cycle is **byte-identical to the input** (md5 match). After you
edit anything, the section is rebuilt — and the resulting .paks will then
legitimately differ from the stock one *even for unchanged files*: SquashFS
content-hash equality is file-tree equality (`diff -rq` after re-unpacking),
not byte equality — mksquashfs repacks inode/id tables and re-runs xz, and
the superblock build date is fixed via `SOURCE_DATE_EPOCH` from the manifest
so repeated builds of the same tree are bit-for-bit identical
(`--rebuild` forces a rebuild, `--bump` increments the build number in all
four places: file header, app section version, `version.json`, filename).

## Logo / watermark (what you actually want to change)

In the `app` filesystem:

* `watermark_s.bmp` (100x30), `watermark_m.bmp` (400x110),
  `watermark_e.bmp` (130x34), `watermark_200_55.bmp` (200x55) —
  16-bit RGB565 bitmaps burned into the video by the `device`/`router`
  processes (strings `SD8560_V3/mnt/app/watermark_m.bmp`).
  Replace with your own BMP of the **same width, height, 16bpp, 1078+header
  byte layout** to cover all OSD-size settings. The camera selects small/medium
  based on stream resolution; `_e` is the "extended" variant. Trick is to save
  as 24 bit bmp and the device will convert to proper color 16bit schema.
* `www/img/` — web-UI logos (not overlaid on video).

## Watermark size limits (from `device` BMP loader @0x256c4 + converters)

Parser constraints:

* file must have 14-byte BMP header + 40-byte DIB; pixel rows read from
  the real `bfOffBits`; only **biBitCount 16 or 24** accepted
  (`do not support bmp format:%d`); 24bpp is auto-converted to RGB565
* row padding (to 4) handled, **bottom-up only** (negative biHeight breaks)
* BI_RGB only (compression field never checked — RLE would misread)
* **no dimension cap**: buffer = `bc_malloc(w*h*bpp/8)`, sized from the
  header; passed to `HD_VIDEOENC_PARAM_IN_STAMP_BUF/_IMG` (Nvt52xAdapter_osd.cpp).
  Driver checks `w,h vs buf size` per stream (phase/pid/vid) and cache
  alignment — both scale with your image. The DTB caps only stamp COUNT
  (`stamp_maximg=16`, enc 32, prc 4/stream) inside a 128 MB HDAL media
  pool (`/hdal-memory/media` @0x8000000, 0x8000000) — memory never the issue.
* black (0x0000) is the transparency colorkey — stock BMPs are 63% pure
  black background; design your logo with black background, not pure-black
  logo pixels.
* `watermark_200_55.bmp` is referenced by NO binary — dead file on this
  model; only `_m`/`_e`/`_s` load.

Real practical limits when going bigger:

1. the logo must fit each stream's frame (variants are picked per stream:
   `_m` main 4K, `_s` small streams) — 400x110 already spans 62% of a
   640x480 sub-stream; size `_s` conservatively
2. squashfs budget: app volume = 9,142,272 bytes, current fs 9,068,544 →
   only ~74 KB slack. Mostly-black BMPs xz-compress tiny (stock 400x110 ≈
   1.5 KB), so a few-hundred-KB logo is fine — but delete something first
   (e.g. the dead `watermark_200_55.bmp`, 22 KB raw)
3. overlay blending costs encode bandwidth: stay ≲ half the frame width

## Updater analysis (fs_app/update + fs_app/upgrade, disassembled with r2)

**No vendor signature exists anywhere in the chain.** What the camera does
enforce (all fatal — parser returns -1 before any flash write; caller prints
`update paks:%s failed.` at 0x10c58):

* `update` (static ARM flasher; entry → `fcn.00010398` → parser `fcn.000156d8`):
  file magic `SKAP`/legacy `REOL`; file CRC = crc32-init0(file[8:]) vs +0x04;
  per-section `SGMI` magic + board-type field + payload CRC; section size must
  be a multiple of the 128 KiB erase size. Zero RSA/EC/ASN.1 strings — the
  apparent `aes|sha1|sha2|crc32` blob is glibc HWCAP names.
* `upgrade` (orchestrator; `cp /mnt/app/update /mnt/tmp/` + exec): links
  libmbedtls only for MD5 + TLS (cloud client); gates on build version
  (`the same version, don't need to update`), model and board type
  (`/mnt/tmp/board_id`) — no image crypto.
* Boot chain: no `/signature` node in the FDT, no FIT-signature code in
  U-Boot, no dm-verity/root-hash in bootargs. The kernel uImage carries only
  the standard legacy-header CRC — recompute (mkimage) only if you patch the
  kernel section itself.

## Flashing

Upload via Web UI → Firmware update (bump the build number first), or
`curl -F file=@NEW.paks http://cam/cgi-bin/cgi.cgi?request_command=UPGRADE`.
Keep serial access available the first time.
