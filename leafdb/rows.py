import struct

INT = "INT"
TEXT = "TEXT"
SUPPORTED_TYPES = (INT, TEXT)

_NULL_HEADER = struct.Struct(">I")
_INT_VALUE = struct.Struct(">q")
_TEXT_LEN = struct.Struct(">H")


def encode_row(types, values):
    if len(values) != len(types):
        raise ValueError(f"row has {len(values)} values, schema expects {len(types)}")
    nulls = 0
    parts = []
    for i, (t, v) in enumerate(zip(types, values)):
        if v is None:
            nulls |= 1 << i
            continue
        if t == INT:
            parts.append(_INT_VALUE.pack(v))
        elif t == TEXT:
            raw = v.encode("utf-8")
            if len(raw) > 0xFFFF:
                raise ValueError("text value exceeds 65535 bytes")
            parts.append(_TEXT_LEN.pack(len(raw)))
            parts.append(raw)
        else:
            raise ValueError(f"unknown column type {t!r}")
    return _NULL_HEADER.pack(nulls) + b"".join(parts)


def decode_row(types, buf):
    (nulls,) = _NULL_HEADER.unpack_from(buf, 0)
    off = _NULL_HEADER.size
    out = []
    for i, t in enumerate(types):
        if nulls & (1 << i):
            out.append(None)
        elif t == INT:
            (v,) = _INT_VALUE.unpack_from(buf, off)
            off += _INT_VALUE.size
            out.append(v)
        elif t == TEXT:
            (n,) = _TEXT_LEN.unpack_from(buf, off)
            off += _TEXT_LEN.size
            out.append(buf[off:off + n].decode("utf-8"))
            off += n
        else:
            raise ValueError(f"unknown column type {t!r}")
    return tuple(out)


def coerce_value(col_type, col_name, value):
    if value is None:
        return None
    if col_type == INT:
        if isinstance(value, bool):
            raise TypeError(f"column {col_name!r} is INT, got boolean")
        if isinstance(value, float):
            if not value.is_integer():
                raise TypeError(f"column {col_name!r} is INT, got non-integral {value}")
            return int(value)
        if isinstance(value, int):
            return value
        raise TypeError(f"column {col_name!r} is INT, got {type(value).__name__}")
    if col_type == TEXT:
        if isinstance(value, str):
            return value
        raise TypeError(f"column {col_name!r} is TEXT, got {type(value).__name__}")
    raise TypeError(f"column {col_name!r} has unknown type {col_type!r}")
