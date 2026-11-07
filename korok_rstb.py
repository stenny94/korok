"""Minimal pure-Python Yaz0 + BotW ResourceSizeTable (RSTB) support.

Wii U format is big-endian:
  "RSTB", u32 crc_count, u32 name_count,
  crc_count  * (u32 crc32(name), u32 size)      sorted by crc
  name_count * (char[128] name,  u32 size)      for crc collisions
"""
import pathlib
import struct
import zlib


def yaz0_decompress(data: bytes) -> bytes:
    if data[:4] != b"Yaz0":
        raise ValueError("Not a Yaz0 stream")
    size = struct.unpack(">I", data[4:8])[0]
    out = bytearray()
    src = 16
    while len(out) < size:
        header = data[src]
        src += 1
        for _ in range(8):
            if len(out) >= size:
                break
            if header & 0x80:
                out.append(data[src])
                src += 1
            else:
                b1, b2 = data[src], data[src + 1]
                src += 2
                copy_from = len(out) - ((((b1 & 0x0F) << 8) | b2) + 1)
                n = b1 >> 4
                if n == 0:
                    n = data[src] + 0x12
                    src += 1
                else:
                    n += 2
                for _ in range(n):
                    out.append(out[copy_from])
                    copy_from += 1
            header = (header << 1) & 0xFF
    return bytes(out)


def yaz0_compress_stored(data: bytes) -> bytes:
    """Valid Yaz0 stream using only literal bytes (no compression).
    Larger on disk, but any Yaz0 reader (including the game) accepts it."""
    out = bytearray(b"Yaz0")
    out += struct.pack(">I", len(data)) + bytes(8)
    for i in range(0, len(data), 8):
        chunk = data[i:i + 8]
        out.append((0xFF << (8 - len(chunk))) & 0xFF)
        out += chunk
    return bytes(out)


def crc_of(name: str) -> int:
    return zlib.crc32(name.encode("utf-8")) & 0xFFFFFFFF


class SizeTable:
    def __init__(self, raw: bytes):
        if raw[:4] != b"RSTB":
            raise ValueError("Not an RSTB file")
        crc_n, name_n = struct.unpack(">II", raw[4:12])
        off = 12
        self.crc = dict(struct.iter_unpack(">II", raw[off:off + 8 * crc_n]))
        off += 8 * crc_n
        self.names = {}
        for _ in range(name_n):
            name = raw[off:off + 128].split(b"\0", 1)[0].decode("utf-8", "replace")
            self.names[name] = struct.unpack_from(">I", raw, off + 128)[0]
            off += 132

    @classmethod
    def from_file(cls, path):
        raw = pathlib.Path(path).read_bytes()
        if raw[:4] == b"Yaz0":
            raw = yaz0_decompress(raw)
        return cls(raw)

    def raise_size(self, name: str, size: int):
        """Set entry to max(current, size). Returns 'added', 'updated' or None."""
        size = min(int(size), 0xFFFFFFFF)
        if name in self.names:
            if size > self.names[name]:
                self.names[name] = size
                return "updated"
            return None
        crc = crc_of(name)
        if crc in self.crc:
            if size > self.crc[crc]:
                self.crc[crc] = size
                return "updated"
            return None
        self.crc[crc] = size
        return "added"

    def merge_larger(self, other: "SizeTable"):
        """Take every entry of `other` that is new or bigger than ours.
        Returns (added, updated)."""
        added = updated = 0
        for crc, size in other.crc.items():
            cur = self.crc.get(crc)
            if cur is None:
                self.crc[crc] = size
                added += 1
            elif size > cur:
                self.crc[crc] = size
                updated += 1
        for name, size in other.names.items():
            cur = self.names.get(name)
            if cur is None:
                self.names[name] = size
                added += 1
            elif size > cur:
                self.names[name] = size
                updated += 1
        return added, updated

    def delete(self, name: str):
        """Remove an entry. Returns 'deleted' or None if there was none."""
        if name in self.names:
            del self.names[name]
            return "deleted"
        crc = crc_of(name)
        if crc in self.crc:
            del self.crc[crc]
            return "deleted"
        return None

    def to_binary(self) -> bytes:
        out = bytearray(b"RSTB")
        out += struct.pack(">II", len(self.crc), len(self.names))
        for crc in sorted(self.crc):
            out += struct.pack(">II", crc, self.crc[crc])
        for name, size in self.names.items():
            out += name.encode("utf-8")[:128].ljust(128, b"\0") + struct.pack(">I", size)
        return bytes(out)

    def save(self, path):
        pathlib.Path(path).write_bytes(yaz0_compress_stored(self.to_binary()))
