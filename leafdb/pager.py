import os
from collections import OrderedDict

PAGE_SIZE = 4096
EMPTY_PAGE = bytes(PAGE_SIZE)


class BufferPool:
    def __init__(self, capacity=64):
        self.capacity = capacity
        self.pages = OrderedDict()
        self.dirty = set()

    def __len__(self):
        return len(self.pages)

    def get(self, n):
        if n in self.pages:
            self.pages.move_to_end(n)
            return self.pages[n]
        return None

    def put(self, n, data, dirty=True):
        self.pages[n] = data
        self.pages.move_to_end(n)
        if dirty:
            self.dirty.add(n)

    def discard_clean(self, n):
        if n not in self.dirty:
            self.pages.pop(n, None)


class Pager:
    def __init__(self, path, cache_size=64):
        self.path = path
        if not os.path.exists(path) or os.path.getsize(path) == 0:
            with open(path, "wb"):
                pass
        self.file = open(path, "r+b")
        size = self.file.seek(0, os.SEEK_END)
        self.num_pages = max(1, size // PAGE_SIZE)
        self.pool = BufferPool(cache_size)
        self.touched = None
        self.shadow = {}
        self.hits = 0
        self.misses = 0

    def _file_size(self):
        pos = self.file.tell()
        size = self.file.seek(0, os.SEEK_END)
        self.file.seek(pos)
        return size

    def _read_page(self, n):
        self.file.seek(n * PAGE_SIZE)
        data = self.file.read(PAGE_SIZE)
        if len(data) < PAGE_SIZE:
            data += bytes(PAGE_SIZE - len(data))
        return data

    def _write_page(self, n, data):
        self.file.seek(n * PAGE_SIZE)
        self.file.write(data)

    def _evict_if_full(self):
        if self.touched is not None:
            return
        while len(self.pool.pages) >= self.pool.capacity:
            old, data = self.pool.pages.popitem(last=False)
            if old in self.pool.dirty:
                self._write_page(old, data)
                self.pool.dirty.discard(old)

    def get(self, n):
        if n < 0 or (n >= self.num_pages and n != 0):
            raise IndexError(f"page {n} out of range ({self.num_pages} pages)")
        data = self.pool.get(n)
        if data is not None:
            self.hits += 1
            return data
        self.misses += 1
        data = self._read_page(n)
        self._evict_if_full()
        self.pool.put(n, data, dirty=False)
        return data

    def write(self, n, data):
        if len(data) > PAGE_SIZE:
            raise ValueError(f"page {n} overflow: {len(data)} > {PAGE_SIZE} bytes")
        if self.touched is not None and n not in self.shadow:
            current = self.pool.get(n)
            if current is None and (n + 1) * PAGE_SIZE <= self._file_size():
                current = self._read_page(n)
            self.shadow[n] = current
        padded = data + bytes(PAGE_SIZE - len(data))
        self._evict_if_full()
        self.pool.put(n, padded, dirty=True)
        if n >= self.num_pages:
            self.num_pages = n + 1
        if self.touched is not None:
            self.touched.add(n)

    def allocate(self):
        n = self.num_pages
        self.num_pages += 1
        self.write(n, EMPTY_PAGE)
        return n

    def begin_txn(self):
        if self.touched is not None:
            raise RuntimeError("transaction already active on pager")
        self.touched = set()
        self.shadow = {}

    def rollback_txn(self):
        for n, image in self.shadow.items():
            if image is None:
                self.pool.pages.pop(n, None)
                self.pool.dirty.discard(n)
            else:
                self.pool.put(n, image, dirty=False)
                self.pool.dirty.discard(n)
        self.shadow = {}
        self.touched = None

    def collect_commit(self):
        pages = sorted(self.touched or ())
        images = {p: self.pool.pages[p] for p in pages}
        self.touched = None
        self.shadow = {}
        return pages, self.num_pages, images

    def flush(self, fsync=False):
        for n in sorted(self.pool.dirty):
            self._write_page(n, self.pool.pages[n])
        self.pool.dirty.clear()
        if fsync:
            self.sync()

    def sync(self):
        self.file.flush()
        if os.environ.get("LEAFDB_FSYNC", "1") != "0":
            os.fsync(self.file.fileno())

    def close(self):
        self.flush()
        self.file.close()

    def stats(self):
        total = self.hits + self.misses
        rate = (self.hits / total * 100.0) if total else 0.0
        return {
            "cache_hits": self.hits,
            "cache_misses": self.misses,
            "hit_rate": f"{rate:.1f}%",
            "cached_pages": len(self.pool.pages),
            "dirty_pages": len(self.pool.dirty),
            "file_pages": self.num_pages,
        }
