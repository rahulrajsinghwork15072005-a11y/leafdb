"""Free-page list: tracks pages orphaned by DROP TABLE so they can be
reused instead of growing the file forever.

The list is persisted inside the catalog (page 0) and managed as part of
normal transactions, so crash safety applies to it automatically.
"""

import json


class FreeList:
    def __init__(self, pages=None):
        self.pages: list[int] = list(pages or [])

    def push(self, *page_numbers: int):
        for p in page_numbers:
            if p > 0 and p not in self.pages:
                self.pages.append(p)

    def pop(self) -> int | None:
        return self.pages.pop() if self.pages else None

    def __len__(self):
        return len(self.pages)

    def __contains__(self, page):
        return page in self.pages


def collect_tree_pages(btree, root_page) -> set:
    """Walk a B+ tree and return every page number it occupies."""
    from .btree import Leaf
    visited = set()
    stack = [root_page]
    while stack:
        pg = stack.pop()
        if pg in visited:
            continue
        visited.add(pg)
        node = btree._load(pg)
        if not isinstance(node, Leaf):
            stack.extend(node.children)
    return visited
