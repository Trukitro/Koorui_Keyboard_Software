"""Foto de solo lectura de la configuracion del teclado KOORUI (320F:50C3).

Uso: python snapshot.py <etiqueta>      guarda snap_<etiqueta>.json
     python snapshot.py diff <a> <b>    muestra los bytes que cambian
"""
import json
import sys
import time

import hid

READ_CMDS = (0x03, 0x05, 0x12, 0x13, 0x14, 0x15, 0x1B, 0x1F)
PAGE, CHUNKS, PAGES = 0x40, ((0, 0x18), (0x18, 0x18), (0x30, 0x10)), 5


def open_dev():
    for d in hid.enumerate(0x320F, 0x50C3):
        if (d["usage_page"], d["usage"]) == (0xFF1C, 0x92):
            h = hid.device()
            h.open_path(d["path"])
            h.set_nonblocking(1)
            return h
    raise SystemExit("receptor no encontrado")


def tx(dev, cmd, ln=0, addr=0):
    p = bytearray(64)
    p[0], p[3], p[4], p[5], p[6] = 4, cmd, ln, addr & 0xFF, addr >> 8
    s = sum(p[3:])
    p[1], p[2] = s & 0xFF, s >> 8
    dev.write(bytes(p))
    end = time.time() + 0.5
    while time.time() < end:
        r = dev.read(64)
        if r and r[3] == cmd:
            return bytes(r)
        time.sleep(0.002)
    return None


def snapshot():
    dev = open_dev()
    out = {}
    tx(dev, 0x01)
    for cmd in READ_CMDS:
        data = b""
        for page in range(PAGES):
            for off, ln in CHUNKS:
                r = tx(dev, cmd, ln, page * PAGE + off)
                data += r[8:8 + ln] if r else b"\xEE" * ln
        out["%02x" % cmd] = data.hex()
    tx(dev, 0x02)
    return out


def diff(a, b):
    sa, sb = (json.load(open(f"snap_{n}.json")) for n in (a, b))
    for cmd in sa:
        da, db = bytes.fromhex(sa[cmd]), bytes.fromhex(sb[cmd])
        for i, (x, y) in enumerate(zip(da, db)):
            if x != y:
                print(f"cmd {cmd} dir 0x{i:03x}: {x:02x} -> {y:02x}")
    print("fin diff")


if __name__ == "__main__":
    if sys.argv[1] == "diff":
        diff(sys.argv[2], sys.argv[3])
    else:
        snap = snapshot()
        json.dump(snap, open(f"snap_{sys.argv[1]}.json", "w"), indent=1)
        for cmd, data in snap.items():
            print(cmd, data[:96])
