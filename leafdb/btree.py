import bisect
import struct

from .pager import PAGE_SIZE

MAX_VALUE_SIZE = PAGE_SIZE // 2


def _min_bytes():
    return PAGE_SIZE // 3

_LEAF = 0
_INTERNAL = 1
_NO_PAGE = 0xFFFFFFFF
_LEAF_HEADER = struct.Struct(">BII")
_INT_KEY = struct.Struct(">q")
_U32 = struct.Struct(">I")


class BTreeError(Exception):
    pass


class DuplicateKey(BTreeError):
    def __init__(self, key):
        super().__init__(f"duplicate key: {key}")
        self.key = key


class Node:
    __slots__ = ()

    def to_bytes(self):
        raise NotImplementedError

    def serialized_size(self):
        return len(self.to_bytes())


class Leaf(Node):
    __slots__ = ("cells", "next_leaf")

    def __init__(self, cells=None, next_leaf=None):
        self.cells = cells if cells is not None else []
        self.next_leaf = next_leaf

    def to_bytes(self):
        parts = [_LEAF_HEADER.pack(
            _LEAF,
            _NO_PAGE if self.next_leaf is None else self.next_leaf,
            len(self.cells),
        )]
        for k, v in self.cells:
            parts.append(_INT_KEY.pack(k))
            parts.append(_U32.pack(len(v)))
            parts.append(v)
        return b"".join(parts)


class Internal(Node):
    __slots__ = ("keys", "children")

    def __init__(self, keys=None, children=None, is_root=False):
        self.keys = keys if keys is not None else []
        self.children = children if children is not None else []

    def to_bytes(self):
        parts = [struct.pack(">BII", _INTERNAL, len(self.keys), len(self.children))]
        for k in self.keys:
            parts.append(_INT_KEY.pack(k))
        for c in self.children:
            parts.append(_U32.pack(c))
        return b"".join(parts)


def node_from_bytes(buf):
    kind = buf[0]
    if kind == _LEAF:
        _, nxt, n = _LEAF_HEADER.unpack_from(buf, 0)
        off = _LEAF_HEADER.size
        cells = []
        for _ in range(n):
            (k,) = _INT_KEY.unpack_from(buf, off)
            off += _INT_KEY.size
            (vlen,) = _U32.unpack_from(buf, off)
            off += _U32.size
            cells.append((k, buf[off:off + vlen]))
            off += vlen
        return Leaf(cells, None if nxt == _NO_PAGE else nxt)
    if kind == _INTERNAL:
        nk, nc = struct.unpack_from(">II", buf, 1)
        off = 9
        keys = list(struct.unpack_from(">" + "q" * nk, buf, off)) if nk else []
        off += 8 * nk
        children = list(struct.unpack_from(">" + "I" * nc, buf, off)) if nc else []
        return Internal(keys, children)
    raise ValueError(f"bad node kind {kind}")


class BTree:
    """Disk-backed B+ tree over fixed-size pages.

    Leaves hold sorted (key, value-blob) cells chained for range scans;
    internal nodes route searches by separator keys.
    """

    def __init__(self, pager):
        self.pager = pager
        self._ncache = {}

    def _load(self, n):
        data = self.pager.get(n)
        hit = self._ncache.get(n)
        if hit is not None and hit[0] is data:
            return hit[1]
        node = node_from_bytes(data)
        if len(self._ncache) > 512:
            self._ncache.clear()
        self._ncache[n] = (data, node)
        return node

    @staticmethod
    def _child_index(node, key):
        return bisect.bisect_right(node.keys, key)

    @staticmethod
    def _underflow(node):
        if isinstance(node, Leaf):
            return len(node.cells) == 0 or node.serialized_size() < _min_bytes()
        return len(node.children) < 2 or node.serialized_size() < _min_bytes()

    @staticmethod
    def _can_spare(node):
        return node.serialized_size() - _min_bytes() > 64

    def search(self, root, key):
        n = root
        while True:
            node = self._load(n)
            if isinstance(node, Leaf):
                i = bisect.bisect_left([c[0] for c in node.cells], key)
                if i < len(node.cells) and node.cells[i][0] == key:
                    return node.cells[i][1]
                return None
            n = node.children[self._child_index(node, key)]

    def insert(self, root, key, value):
        """Insert (key, value); returns the (possibly new) root page."""
        if not isinstance(key, int) or isinstance(key, bool):
            raise TypeError(f"btree keys must be ints, got {type(key).__name__}")
        if len(value) > MAX_VALUE_SIZE:
            raise ValueError(f"value of {len(value)} bytes exceeds MAX_VALUE_SIZE ({MAX_VALUE_SIZE})")
        split = self._insert(root, key, value)
        if split is None:
            return root
        sep, right = split
        new_root = Internal(keys=[sep], children=[root, right], is_root=True)
        n = self.pager.allocate()
        self.pager.write(n, new_root.to_bytes())
        return n

    def _insert(self, n, key, value):
        node = self._load(n)
        if isinstance(node, Leaf):
            keys = [c[0] for c in node.cells]
            i = bisect.bisect_left(keys, key)
            if i < len(keys) and keys[i] == key:
                raise DuplicateKey(key)
            node.cells.insert(i, (key, value))
            if node.serialized_size() <= PAGE_SIZE:
                self.pager.write(n, node.to_bytes())
                return None
            return self._split_leaf(n, node)
        i = self._child_index(node, key)
        split = self._insert(node.children[i], key, value)
        if split is None:
            return None
        sep, right = split
        node.keys.insert(i, sep)
        node.children.insert(i + 1, right)
        if node.serialized_size() <= PAGE_SIZE:
            self.pager.write(n, node.to_bytes())
            return None
        return self._split_internal(n, node)

    def _split_leaf(self, n, node):
        mid = len(node.cells) // 2
        if mid == 0:
            raise ValueError("row too large to fit in a page")
        right_page = self.pager.allocate()
        right = Leaf(cells=node.cells[mid:], next_leaf=node.next_leaf)
        left = Leaf(cells=node.cells[:mid], next_leaf=right_page)
        self.pager.write(right_page, right.to_bytes())
        self.pager.write(n, left.to_bytes())
        return (right.cells[0][0], right_page)

    def _split_internal(self, n, node):
        mid = len(node.keys) // 2
        if mid == 0:
            raise ValueError("internal node too small to split")
        sep = node.keys[mid]
        right_page = self.pager.allocate()
        right = Internal(keys=node.keys[mid + 1:], children=node.children[mid + 1:])
        left = Internal(keys=node.keys[:mid], children=node.children[:mid + 1])
        self.pager.write(right_page, right.to_bytes())
        self.pager.write(n, left.to_bytes())
        return (sep, right_page)

    def upsert(self, root, key, value):
        if len(value) > MAX_VALUE_SIZE:
            raise ValueError(f"value of {len(value)} bytes exceeds MAX_VALUE_SIZE ({MAX_VALUE_SIZE})")
        try:
            return self.insert(root, key, value)
        except DuplicateKey:
            n = root
            while True:
                node = self._load(n)
                if isinstance(node, Leaf):
                    keys = [c[0] for c in node.cells]
                    i = bisect.bisect_left(keys, key)
                    node.cells[i] = (key, value)
                    self.pager.write(n, node.to_bytes())
                    return root
                n = node.children[self._child_index(node, key)]

    def delete(self, root, key):
        """Remove key with borrow/merge rebalancing; returns (new_root, found)."""
        found = self._delete(root, key)
        if not found:
            return root, False
        node = self._load(root)
        while isinstance(node, Internal) and len(node.keys) == 0:
            root = node.children[0]
            node = self._load(root)
        return root, True

    def _delete(self, n, key):
        node = self._load(n)
        if isinstance(node, Leaf):
            keys = [c[0] for c in node.cells]
            i = bisect.bisect_left(keys, key)
            if i < len(keys) and keys[i] == key:
                node.cells.pop(i)
                self.pager.write(n, node.to_bytes())
                return True
            return False
        i = self._child_index(node, key)
        deleted = self._delete(node.children[i], key)
        if not deleted:
            return False
        child = self._load(node.children[i])
        if self._underflow(child):
            self._rebalance(n, node, i)
            self.pager.write(n, node.to_bytes())
        return True

    def _rebalance(self, pn, pnode, i):
        child_page = pnode.children[i]
        child = self._load(child_page)

        if i > 0:
            left_page = pnode.children[i - 1]
            left = self._load(left_page)
            if self._can_spare(left):
                self._borrow_from_left(pnode, i, left, child)
                self.pager.write(left_page, left.to_bytes())
                self.pager.write(child_page, child.to_bytes())
                self.pager.write(pn, pnode.to_bytes())
                return
        if i < len(pnode.children) - 1:
            right_page = pnode.children[i + 1]
            right = self._load(right_page)
            if self._can_spare(right):
                self._borrow_from_right(pnode, i, child, right)
                self.pager.write(right_page, right.to_bytes())
                self.pager.write(child_page, child.to_bytes())
                self.pager.write(pn, pnode.to_bytes())
                return

        if i > 0:
            left = self._load(pnode.children[i - 1])
            merged = self._merge(left, child, sep=pnode.keys[i - 1])
            self.pager.write(pnode.children[i - 1], merged.to_bytes())
            del pnode.children[i]
            del pnode.keys[i - 1]
        else:
            right = self._load(pnode.children[i + 1])
            merged = self._merge(child, right, sep=pnode.keys[i])
            self.pager.write(child_page, merged.to_bytes())
            del pnode.children[i + 1]
            del pnode.keys[i]

    def _borrow_from_left(self, pnode, i, left, child):
        if isinstance(child, Leaf):
            child.cells.insert(0, left.cells.pop())
            pnode.keys[i - 1] = child.cells[0][0]
        else:
            child.keys.insert(0, pnode.keys[i - 1])
            pnode.keys[i - 1] = left.keys.pop()
            child.children.insert(0, left.children.pop())

    def _borrow_from_right(self, pnode, i, child, right):
        if isinstance(child, Leaf):
            child.cells.append(right.cells.pop(0))
            pnode.keys[i] = right.cells[0][0]
        else:
            child.keys.append(pnode.keys[i])
            pnode.keys[i] = right.keys.pop(0)
            child.children.append(right.children.pop(0))

    def _merge(self, low, high, sep):
        if isinstance(low, Leaf):
            return Leaf(cells=low.cells + high.cells, next_leaf=high.next_leaf)
        low.keys = low.keys + [sep] + high.keys
        low.children = low.children + high.children
        return low

    def range_scan(self, root, lo=None, hi=None):
        n = root
        while True:
            node = self._load(n)
            if isinstance(node, Leaf):
                break
            n = node.children[0] if lo is None else node.children[self._child_index(node, lo)]
        while True:
            node = self._load(n)
            for k, v in node.cells:
                if lo is not None and k < lo:
                    continue
                if hi is not None and k > hi:
                    return
                yield k, v
            if node.next_leaf is None:
                return
            n = node.next_leaf

    def items(self, root):
        return self.range_scan(root)

    def stats(self, root):
        depth = 1
        leaves = 0
        internals = 0
        keys = 0
        level = [(root, self._load(root))]
        while level:
            nxt = []
            for page, nd in level:
                if isinstance(nd, Leaf):
                    leaves += 1
                    keys += len(nd.cells)
                else:
                    internals += 1
                    for c in nd.children:
                        nxt.append((c, self._load(c)))
            if nxt:
                depth += 1
            level = nxt
        fill = (keys / leaves) if leaves else 0.0
        return {"depth": depth, "leaves": leaves, "internal_nodes": internals,
                "keys": keys, "avg_fill": round(fill, 1)}

    def check(self, root):
        """Validate full B+ tree invariants; raises AssertionError on violation."""
        depths = set()
        walked = []

        def walk(pg, depth):
            nd = self._load(pg)
            if isinstance(nd, Leaf):
                depths.add(depth)
                ks = [c[0] for c in nd.cells]
                assert ks == sorted(set(ks)), f"leaf {pg} keys unsorted/dup"
                walked.extend(ks)
                return (ks[0], ks[-1]) if ks else (None, None)
            assert len(nd.children) == len(nd.keys) + 1, \
                f"internal {pg}: {len(nd.children)} children vs {len(nd.keys)} separators"
            assert nd.keys == sorted(set(nd.keys)), f"internal {pg} separators unsorted"
            bounds = [walk(cp, depth + 1) for cp in nd.children]
            for j in range(len(nd.keys)):
                right_min = bounds[j + 1][0]
                assert nd.keys[j] <= right_min, \
                    f"unsafe separator in {pg}: keys[{j}]={nd.keys[j]} > right-min {right_min}"
            lows = [b[0] for b in bounds if b[0] is not None]
            highs = [b[1] for b in bounds if b[1] is not None]
            lo = min(lows) if lows else None
            hi = max(highs) if highs else None
            return (lo, hi)

        walk(root, 1)

        page = root
        node = self._load(page)
        while isinstance(node, Internal):
            page = node.children[0]
            node = self._load(page)
        linked = []
        seen_pages = set()
        while True:
            assert isinstance(node, Leaf), "leaf chain hit a non-leaf"
            assert page not in seen_pages, "cycle in leaf chain"
            seen_pages.add(page)
            linked.extend(c[0] for c in node.cells)
            if node.next_leaf is None:
                break
            page = node.next_leaf
            node = self._load(page)
        assert linked == walked, "leaf chain disagrees with tree order"

        def occupancy(pg, is_root):
            nd = self._load(pg)
            if not is_root:
                assert not self._underflow(nd), \
                    f"node {pg} underfull ({nd.serialized_size()} bytes)"
            if hasattr(nd, "children"):
                for cp in nd.children:
                    occupancy(cp, False)

        occupancy(root, True)
        assert len(depths) == 1, f"unbalanced tree: leaves at depths {sorted(depths)}"
        return {"ok": True, "keys": len(walked), "balanced": len(depths) == 1}

    def bulk_load(self, pairs):
        data = sorted(pairs)
        if not data:
            page = self.pager.allocate()
            self.pager.write(page, Leaf().to_bytes())
            return page
        leaf_nodes = []
        cur = []
        for k, v in data:
            cur.append((k, v))
            probe = Leaf(cur).serialized_size()
            if probe > PAGE_SIZE - 16:
                if len(cur) == 1:
                    raise ValueError("row too large to fit in a page")
                leaf_nodes.append(Leaf(cur))
                cur = []
        if cur:
            leaf_nodes.append(Leaf(cur))
        pages = []
        for ln in leaf_nodes:
            p = self.pager.allocate()
            self.pager.write(p, ln.to_bytes())
            pages.append((p, ln.cells[0][0]))
        for i, (p, _) in enumerate(pages):
            nxt = pages[i + 1][0] if i + 1 < len(pages) else None
            ln = self._load(p)
            ln.next_leaf = nxt
            self.pager.write(p, ln.to_bytes())
        level = pages
        fanout = 32
        while len(level) > 1:
            parents = []
            for i in range(0, len(level), fanout):
                group = level[i:i + fanout]
                kids = [p for p, _ in group]
                seps = [k for _, k in group[1:]]
                p = self.pager.allocate()
                self.pager.write(p, Internal(seps, kids).to_bytes())
                parents.append((p, group[0][1]))
            level = parents
        return level[0][0]

    def vacuum(self, root):
        data = list(self.items(root))
        new_root = self.bulk_load(data)
        return new_root, len(data)
