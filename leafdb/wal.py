import os
import struct
import zlib

from .pager import PAGE_SIZE

WAL_MAGIC = b"LFDB"
WAL_HEADER = b"LEAFWAL01\n"
COMMIT_PAGE = 0xFFFFFFFF
_FRAME_HEADER = struct.Struct(">4sIII")


def _fsync_enabled():
    return os.environ.get("LEAFDB_FSYNC", "1") != "0"


def _frame(page_no, payload=b""):
    crc = zlib.crc32(payload) & 0xFFFFFFFF
    return _FRAME_HEADER.pack(WAL_MAGIC, page_no, len(payload), crc) + payload


def _commit_frame(num_pages):
    return _frame(COMMIT_PAGE, struct.pack(">I", num_pages))


class WAL:
    def __init__(self, path):
        self.path = path
        fresh = not (os.path.exists(path) and os.path.getsize(path) >= len(WAL_HEADER))
        self.file = open(path, "wb" if fresh else "r+b")
        if fresh:
            self.file.write(WAL_HEADER)
            self.file.flush()
            os.fsync(self.file.fileno())
        else:
            self.file.seek(0, os.SEEK_END)

    def append_batch(self, pages, num_pages):
        self.file.seek(0, os.SEEK_END)
        blob = b"".join(_frame(p, pages[p]) for p in sorted(pages))
        blob += _commit_frame(num_pages)
        self.file.write(blob)
        self.file.flush()
        if _fsync_enabled():
            os.fsync(self.file.fileno())
        return self.file.tell()

    def pending_bytes(self):
        size = self.file.seek(0, os.SEEK_END)
        self.file.seek(0, os.SEEK_END)
        return max(0, size - len(WAL_HEADER))

    def size(self):
        return os.path.getsize(self.path) if os.path.exists(self.path) else len(WAL_HEADER)

    def read_batches_from(self, offset, end=None):
        """Parse committed batches appended after `offset` (MVCC read path).

        Returns a list of (new_offset, images, num_pages); stops at the first
        torn/invalid frame so partial batches from an in-flight writer are
        never observed. Uses a cached read handle; pass `end` (known file size)
        to skip the stat entirely.
        """
        if end is None:
            end = self.size()
        if end <= offset:
            return []
        rh = getattr(self, "_read_handle", None)
        if rh is None:
            rh = self._read_handle = open(self.path, "rb")
        rh.seek(offset)
        data = rh.read(end - offset)
        batches = []
        base = offset
        pos = 0
        batch = {}
        n = len(data)
        while pos + _FRAME_HEADER.size <= n:
            magic, page_no, length, crc = _FRAME_HEADER.unpack_from(data, pos)
            if magic != WAL_MAGIC:
                break
            start = pos + _FRAME_HEADER.size
            stop = start + length
            if stop > n:
                break
            payload = data[start:stop]
            if zlib.crc32(payload) & 0xFFFFFFFF != crc:
                break
            pos = stop
            if page_no == COMMIT_PAGE:
                (num_pages,) = struct.unpack(">I", payload)
                batches.append((base + pos, dict(batch), num_pages))
                batch = {}
            else:
                batch[page_no] = payload
        return batches

    def reset(self):
        self.file.close()
        self.file = open(self.path, "wb")
        self.file.write(WAL_HEADER)
        self.file.flush()
        if _fsync_enabled():
            os.fsync(self.file.fileno())
        rh = getattr(self, "_read_handle", None)
        if rh is not None:
            rh.close()
            self._read_handle = None

    def close(self):
        rh = getattr(self, "_read_handle", None)
        if rh is not None:
            rh.close()
            self._read_handle = None
        self.file.close()


def recover(wal_path, db_path):
    if not os.path.exists(db_path):
        with open(db_path, "wb"):
            pass
    if not os.path.exists(wal_path) or os.path.getsize(wal_path) <= len(WAL_HEADER):
        return 0
    with open(wal_path, "rb") as f:
        data = f.read()
    pos = len(WAL_HEADER)
    applied_batches = 0
    batch = {}
    with open(db_path, "r+b") as db:
        while pos + _FRAME_HEADER.size <= len(data):
            magic, page_no, length, crc = _FRAME_HEADER.unpack_from(data, pos)
            if magic != WAL_MAGIC:
                break
            start = pos + _FRAME_HEADER.size
            end = start + length
            if end > len(data):
                break
            payload = data[start:end]
            if zlib.crc32(payload) & 0xFFFFFFFF != crc:
                break
            pos = end
            if page_no == COMMIT_PAGE:
                (num_pages,) = struct.unpack(">I", payload)
                for pg in sorted(batch):
                    db.seek(pg * PAGE_SIZE)
                    db.write(batch[pg])
                need = num_pages * PAGE_SIZE
                db_end = db.seek(0, os.SEEK_END)
                if db_end < need:
                    db.seek(need - 1)
                    db.write(b"\x00")
                applied_batches += 1
                batch = {}
            else:
                batch[page_no] = payload
        db.flush()
        os.fsync(db.fileno())
    return applied_batches
