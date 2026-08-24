import json

from .pager import PAGE_SIZE
from .rows import SUPPORTED_TYPES

CATALOG_PAGE = 0


class CatalogError(Exception):
    pass


class TableMeta:
    def __init__(self, name, columns, root_page, row_count=0, next_rowid=1, indexes=None):
        self.name = name
        self.columns = columns
        self.root_page = root_page
        self.row_count = row_count
        self.next_rowid = next_rowid
        self.indexes = dict(indexes or {})

    def colnames(self):
        return [c["name"] for c in self.columns]

    def types(self):
        return [c["type"] for c in self.columns]

    def pk_column(self):
        for c in self.columns:
            if c.get("pk"):
                return c["name"]
        return None

    def pk_index(self):
        for i, c in enumerate(self.columns):
            if c.get("pk"):
                return i
        return None

    def column_index(self, name):
        try:
            return self.colnames().index(name)
        except ValueError:
            raise CatalogError(f"table {self.name!r} has no column {name!r}")

    def to_dict(self):
        return {
            "name": self.name,
            "columns": self.columns,
            "root_page": self.root_page,
            "row_count": self.row_count,
            "next_rowid": self.next_rowid,
            "indexes": self.indexes,
        }

    @classmethod
    def from_dict(cls, d):
        return cls(
            name=d["name"],
            columns=d["columns"],
            root_page=d["root_page"],
            row_count=d.get("row_count", 0),
            next_rowid=d.get("next_rowid", 1),
            indexes=d.get("indexes", {}),
        )


class Catalog:
    def __init__(self):
        self.tables = {}
        self.free_pages: list[int] = []

    def get(self, name):
        meta = self.tables.get(name.lower())
        if meta is None:
            raise CatalogError(f"no such table: {name}")
        return meta

    def add(self, meta):
        if meta.name in self.tables:
            raise CatalogError(f"table {meta.name!r} already exists")
        self.tables[meta.name] = meta

    def to_json(self):
        return json.dumps({
            "tables": {n: m.to_dict() for n, m in self.tables.items()},
            "free_pages": self.free_pages,
        })

    def save(self, pager):
        raw = self.to_json().encode("utf-8")
        if len(raw) > PAGE_SIZE - 8:
            raise CatalogError("catalog overflow: too many tables/columns")
        pager.write(CATALOG_PAGE, raw)

    @classmethod
    def load(cls, pager):
        cat = cls()
        raw = pager.get(CATALOG_PAGE).rstrip(b"\x00")
        if not raw:
            return cat
        data = json.loads(raw.decode("utf-8"))
        for name, m in data["tables"].items():
            cat.tables[name] = TableMeta.from_dict(m)
        return cat

    def validate_column_def(self, col):
        name = col["name"].lower()
        if col["type"] not in SUPPORTED_TYPES:
            raise CatalogError(f"unsupported type {col['type']!r} (use INT or TEXT)")
        return {"name": name, "type": col["type"], "pk": bool(col.get("pk"))}
