import re


class SQLError(Exception):
    pass


class ParseError(SQLError):
    pass


KEYWORDS = {
    "SELECT", "FROM", "WHERE", "GROUP", "BY", "HAVING", "ORDER", "LIMIT",
    "OFFSET", "ASC", "DESC", "AND", "OR", "NOT", "NULL", "IS", "IN",
    "DISTINCT", "JOIN", "INNER", "LEFT", "RIGHT", "FULL", "OUTER", "ON",
    "AS", "INSERT", "INTO", "VALUES", "CREATE", "TABLE", "INDEX", "DELETE",
    "UPDATE", "SET", "BEGIN", "COMMIT", "ROLLBACK", "EXPLAIN", "PRIMARY",
    "KEY", "INT", "TEXT", "COUNT", "SUM", "AVG", "MIN", "MAX",
    "BETWEEN", "LIKE", "DROP",
}

AGGREGATES = {"COUNT", "SUM", "AVG", "MIN", "MAX"}

IDENT_OK_KEYWORDS = {"INT", "TEXT"} | AGGREGATES

_TOKEN_RE = re.compile(
    r"""(?P<ws>\s+)
      | (?P<string>'(?:[^']|'')*')
      | (?P<number>\d+\.\d+|\d+)
      | (?P<ident>[A-Za-z_][A-Za-z_0-9]*)
      | (?P<op><=|>=|<>|!=|=|<|>|;|,|\(|\)|\*|\.|\+|-|/)
    """,
    re.VERBOSE,
)


class Token:
    __slots__ = ("kind", "value", "pos", "end")

    def __init__(self, kind, value, pos, end):
        self.kind = kind
        self.value = value
        self.pos = pos
        self.end = end

    def __repr__(self):
        return f"Token({self.kind},{self.value!r})"


def tokenize(sql):
    tokens = []
    pos = 0
    while pos < len(sql):
        m = _TOKEN_RE.match(sql, pos)
        if m is None:
            raise ParseError(f"unexpected character {sql[pos]!r} at position {pos}")
        kind = m.lastgroup
        text = m.group()
        if kind != "ws":
            if kind == "string":
                tokens.append(Token("str", text[1:-1].replace("''", "'"), pos, m.end()))
            elif kind == "number":
                val = float(text) if "." in text else int(text)
                tokens.append(Token("num", val, pos, m.end()))
            elif kind == "ident":
                upper = text.upper()
                if upper in KEYWORDS:
                    tokens.append(Token("kw", upper, pos, m.end()))
                else:
                    tokens.append(Token("ident", text.lower(), pos, m.end()))
            else:
                tokens.append(Token("op", text, pos, m.end()))
        pos = m.end()
    tokens.append(Token("eof", None, len(sql), len(sql)))
    return tokens


class Literal:
    def __init__(self, value):
        self.value = value


class Column:
    def __init__(self, name, table=None):
        self.name = name
        self.table = table


class Star:
    def __init__(self, table=None):
        self.table = table


class BinOp:
    def __init__(self, op, left, right):
        self.op = op
        self.left = left
        self.right = right


class UnaryOp:
    def __init__(self, op, operand):
        self.op = op
        self.operand = operand


class IsNull:
    def __init__(self, operand, negated=False):
        self.operand = operand
        self.negated = negated


class Between:
    def __init__(self, operand, lo, hi, negated=False):
        self.operand = operand
        self.lo = lo
        self.hi = hi
        self.negated = negated


class InList:
    def __init__(self, operand, items, negated=False):
        self.operand = operand
        self.items = items
        self.negated = negated


class Like:
    def __init__(self, operand, pattern, negated=False):
        self.operand = operand
        self.pattern = pattern
        self.negated = negated


class Func:
    def __init__(self, name, arg=None, star=False, distinct=False):
        self.name = name
        self.arg = arg
        self.star = star
        self.distinct = distinct


class JoinClause:
    def __init__(self, jtype, table, alias, on):
        self.jtype = jtype
        self.table = table
        self.alias = alias
        self.on = on


class SelectStmt:
    def __init__(self):
        self.distinct = False
        self.items = []
        self.table = None
        self.alias = None
        self.joins = []
        self.where = None
        self.group_by = []
        self.having = None
        self.order_by = []
        self.limit = None
        self.offset = None


class InsertStmt:
    def __init__(self, table, columns, rows):
        self.table = table
        self.columns = columns
        self.rows = rows


class CreateTableStmt:
    def __init__(self, name, columns):
        self.name = name
        self.columns = columns


class CreateIndexStmt:
    def __init__(self, name, table, column):
        self.name = name
        self.table = table
        self.column = column


class DeleteStmt:
    def __init__(self, table, where):
        self.table = table
        self.where = where


class UpdateStmt:
    def __init__(self, table, assignments, where):
        self.table = table
        self.assignments = assignments
        self.where = where


class BeginStmt:
    pass


class CommitStmt:
    pass


class RollbackStmt:
    pass


class ExplainStmt:
    def __init__(self, stmt):
        self.stmt = stmt


class DropTableStmt:
    def __init__(self, name):
        self.name = name


class DropIndexStmt:
    def __init__(self, name):
        self.name = name


def walk_func_nodes(expr):
    found = []

    def go(e):
        if isinstance(e, BinOp):
            go(e.left)
            go(e.right)
        elif isinstance(e, UnaryOp):
            go(e.operand)
        elif isinstance(e, IsNull):
            go(e.operand)
        elif isinstance(e, Func):
            found.append(e)

    go(expr)
    return found


class Parser:
    def __init__(self, sql):
        self.sql = sql
        self.tokens = tokenize(sql)
        self.i = 0

    def peek(self, ahead=0):
        return self.tokens[min(self.i + ahead, len(self.tokens) - 1)]

    def next(self):
        tok = self.tokens[self.i]
        if tok.kind != "eof":
            self.i += 1
        return tok

    def at_kw(self, *kws):
        t = self.peek()
        return t.kind == "kw" and t.value in kws

    def at_op(self, *ops):
        t = self.peek()
        return t.kind == "op" and t.value in ops

    def accept_kw(self, *kws):
        if self.at_kw(*kws):
            return self.next()
        return None

    def accept_op(self, *ops):
        if self.at_op(*ops):
            return self.next()
        return None

    def expect_kw(self, kw):
        if not self.at_kw(kw):
            raise ParseError(f"expected {kw}, found {self._describe(self.peek())}")
        return self.next()

    def expect_op(self, op):
        if not self.at_op(op):
            raise ParseError(f"expected {op!r}, found {self._describe(self.peek())}")
        return self.next()

    def expect_ident(self, what="identifier"):
        t = self.peek()
        if t.kind == "ident" or (t.kind == "kw" and t.value in IDENT_OK_KEYWORDS):
            self.next()
            return t.value.lower()
        raise ParseError(f"expected {what}, found {self._describe(t)}")

    @staticmethod
    def _describe(tok):
        if tok.kind == "eof":
            return "end of input"
        return f"{tok.value!r}"

    def _text_slice(self, start_tok, end_tok):
        return self.sql[start_tok.pos:end_tok.end].strip()

    def parse_statement(self):
        if self.accept_kw("EXPLAIN"):
            inner = self.parse_statement()
            if isinstance(inner, ExplainStmt):
                raise ParseError("nested EXPLAIN is not supported")
            return ExplainStmt(inner)
        if self.at_kw("SELECT"):
            return self.parse_select()
        if self.at_kw("INSERT"):
            return self.parse_insert()
        if self.at_kw("CREATE"):
            return self.parse_create()
        if self.at_kw("DELETE"):
            return self.parse_delete()
        if self.at_kw("UPDATE"):
            return self.parse_update()
        if self.at_kw("DROP"):
            self.next()
            if self.accept_kw("TABLE"):
                return DropTableStmt(self.expect_ident("table name"))
            if self.accept_kw("INDEX"):
                return DropIndexStmt(self.expect_ident("index name"))
            raise ParseError("expected TABLE or INDEX after DROP")
        if self.accept_kw("BEGIN"):
            return BeginStmt()
        if self.accept_kw("COMMIT"):
            return CommitStmt()
        if self.accept_kw("ROLLBACK"):
            return RollbackStmt()
        raise ParseError(f"unsupported statement starting with {self._describe(self.peek())}")

    def parse_select(self):
        start = self.expect_kw("SELECT")
        sel = SelectStmt()
        sel.distinct = bool(self.accept_kw("DISTINCT"))
        while True:
            if self.at_op("*"):
                star_tok = self.next()
                sel.items.append((Star(None), "*"))
            elif self.at_kw("COUNT", "SUM", "AVG", "MIN", "MAX") and self.peek(1).kind == "op" and self.peek(1).value == "(":
                expr = self.parse_expression()
                label = self._default_label(expr)
                if self.accept_kw("AS"):
                    label = self.expect_ident("alias")
                elif self.peek().kind == "ident":
                    label = self.next().value
                sel.items.append((expr, label))
            else:
                item_start = self.peek()
                expr = self.parse_expression()
                item_end = self.tokens[self.i - 1]
                label = None
                if self.accept_kw("AS"):
                    label = self.expect_ident("alias")
                elif self.peek().kind == "ident":
                    label = self.next().value
                if label is None:
                    label = self._text_slice(item_start, item_end)
                sel.items.append((expr, label))
            if not self.accept_op(","):
                break
        self.expect_kw("FROM")
        sel.table = self.expect_ident("table name")
        sel.alias = self._parse_alias()
        while True:
            join = self._parse_join_opt()
            if join is None:
                break
            sel.joins.append(join)
        if self.accept_kw("WHERE"):
            sel.where = self.parse_expression()
        if self.accept_kw("GROUP"):
            self.expect_kw("BY")
            sel.group_by.append(self.parse_expression())
            while self.accept_op(","):
                sel.group_by.append(self.parse_expression())
        if self.accept_kw("HAVING"):
            sel.having = self.parse_expression()
        if self.accept_kw("ORDER"):
            self.expect_kw("BY")
            sel.order_by.append(self._parse_order_item())
            while self.accept_op(","):
                sel.order_by.append(self._parse_order_item())
        if self.accept_kw("LIMIT"):
            sel.limit = self._expect_int("LIMIT")
            if self.accept_kw("OFFSET"):
                sel.offset = self._expect_int("OFFSET")
        elif self.accept_kw("OFFSET"):
            sel.offset = self._expect_int("OFFSET")
        return sel

    def _default_label(self, expr):
        if isinstance(expr, Func):
            if expr.star:
                return f"{expr.name}(*)"
            arg = expr.arg
            arg_text = arg.name if isinstance(arg, Column) and arg.table is None else "?"
            return f"{expr.name}({arg_text})"
        return "expr"

    def _parse_alias(self):
        if self.accept_kw("AS"):
            return self.expect_ident("alias")
        if self.peek().kind == "ident":
            return self.next().value
        return None

    def _parse_join_opt(self):
        jtype = None
        if self.at_kw("INNER", "LEFT", "RIGHT", "FULL"):
            word = self.next().value
            self.accept_kw("OUTER")
            jtype = {"INNER": "INNER", "LEFT": "LEFT", "RIGHT": "RIGHT", "FULL": "FULL"}[word]
            self.expect_kw("JOIN")
        elif self.at_kw("JOIN"):
            self.next()
            jtype = "INNER"
        else:
            return None
        table = self.expect_ident("join table")
        alias = self._parse_alias()
        self.expect_kw("ON")
        on = self.parse_expression()
        return JoinClause(jtype, table, alias, on)

    def _parse_order_item(self):
        expr = self.parse_expression()
        desc = False
        if self.accept_kw("DESC"):
            desc = True
        else:
            self.accept_kw("ASC")
        return (expr, desc)

    def _expect_int(self, what):
        t = self.peek()
        if t.kind == "num" and isinstance(t.value, int) and t.value >= 0:
            return self.next().value
        raise ParseError(f"{what} expects a non-negative integer")

    def parse_insert(self):
        self.expect_kw("INSERT")
        self.expect_kw("INTO")
        table = self.expect_ident("table name")
        columns = None
        if self.at_op("("):
            self.next()
            columns = [self.expect_ident("column name")]
            while self.accept_op(","):
                columns.append(self.expect_ident("column name"))
            self.expect_op(")")
        self.expect_kw("VALUES")
        rows = []
        while True:
            self.expect_op("(")
            row = [self.parse_expression()]
            while self.accept_op(","):
                row.append(self.parse_expression())
            self.expect_op(")")
            rows.append(row)
            if not self.accept_op(","):
                break
        return InsertStmt(table, columns, rows)

    def parse_create(self):
        self.expect_kw("CREATE")
        if self.accept_kw("TABLE"):
            name = self.expect_ident("table name")
            self.expect_op("(")
            cols = []
            while True:
                cname = self.expect_ident("column name")
                ctype = self.expect_ident_or_type()
                col = {"name": cname, "type": ctype, "pk": False}
                if self.accept_kw("PRIMARY"):
                    self.expect_kw("KEY")
                    col["pk"] = True
                cols.append(col)
                if not self.accept_op(","):
                    break
            self.expect_op(")")
            return CreateTableStmt(name, cols)
        if self.accept_kw("INDEX"):
            iname = self.expect_ident("index name")
            self.expect_kw("ON")
            table = self.expect_ident("table name")
            self.expect_op("(")
            column = self.expect_ident("column name")
            self.expect_op(")")
            return CreateIndexStmt(iname, table, column)
        raise ParseError("expected TABLE or INDEX after CREATE")

    def expect_ident_or_type(self):
        t = self.peek()
        if t.kind == "kw" and t.value in ("INT", "TEXT"):
            return self.next().value
        if t.kind == "ident":
            name = self.next().value
            raise ParseError(f"unknown type {name!r} (use INT or TEXT)")
        raise ParseError(f"expected type, found {self._describe(t)}")

    def parse_delete(self):
        self.expect_kw("DELETE")
        self.expect_kw("FROM")
        table = self.expect_ident("table name")
        where = None
        if self.accept_kw("WHERE"):
            where = self.parse_expression()
        return DeleteStmt(table, where)

    def parse_update(self):
        self.expect_kw("UPDATE")
        table = self.expect_ident("table name")
        self.expect_kw("SET")
        assignments = []
        while True:
            col = self.expect_ident("column name")
            self.expect_op("=")
            assignments.append((col, self.parse_expression()))
            if not self.accept_op(","):
                break
        where = None
        if self.accept_kw("WHERE"):
            where = self.parse_expression()
        return UpdateStmt(table, assignments, where)

    def parse_expression(self):
        return self._parse_or()

    def _parse_or(self):
        left = self._parse_and()
        while self.accept_kw("OR"):
            left = BinOp("OR", left, self._parse_and())
        return left

    def _parse_and(self):
        left = self._parse_not()
        while self.accept_kw("AND"):
            left = BinOp("AND", left, self._parse_not())
        return left

    def _parse_not(self):
        if self.accept_kw("NOT"):
            return UnaryOp("NOT", self._parse_not())
        return self._parse_predicate()

    def _parse_predicate(self):
        left = self._parse_additive()
        if self.at_kw("IS"):
            self.next()
            negated = bool(self.accept_kw("NOT"))
            self.expect_kw("NULL")
            return IsNull(left, negated)
        negated = bool(self.accept_kw("NOT"))
        if self.at_kw("BETWEEN"):
            self.next()
            lo = self._parse_additive()
            self.expect_kw("AND")
            hi = self._parse_additive()
            return Between(left, lo, hi, negated)
        if self.at_kw("IN"):
            self.next()
            self.expect_op("(")
            items = [self._parse_additive()]
            while self.accept_op(","):
                items.append(self._parse_additive())
            self.expect_op(")")
            return InList(left, items, negated)
        if self.at_kw("LIKE"):
            self.next()
            pattern = self._parse_additive()
            return Like(left, pattern, negated)
        if negated:
            raise ParseError("dangling NOT before predicate")
        for op in ("=", "<>", "!=", "<=", ">=", "<", ">"):
            if self.at_op(op):
                self.next()
                right = self._parse_additive()
                norm = "<>" if op == "!=" else op
                return BinOp(norm, left, right)
        return left

    def _parse_additive(self):
        left = self._parse_multiplicative()
        while self.at_op("+", "-"):
            op = self.next().value
            left = BinOp(op, left, self._parse_multiplicative())
        return left

    def _parse_multiplicative(self):
        left = self._parse_unary()
        while self.at_op("*", "/"):
            op = self.next().value
            left = BinOp(op, left, self._parse_unary())
        return left

    def _parse_unary(self):
        if self.at_op("-"):
            self.next()
            return UnaryOp("NEG", self._parse_unary())
        if self.at_op("+"):
            self.next()
            return self._parse_unary()
        return self._parse_primary()

    def _parse_primary(self):
        t = self.peek()
        if t.kind == "num":
            self.next()
            return Literal(t.value)
        if t.kind == "str":
            self.next()
            return Literal(t.value)
        if t.kind == "kw":
            if t.value == "NULL":
                self.next()
                return Literal(None)
            if t.value in AGGREGATES and self.peek(1).kind == "op" and self.peek(1).value == "(":
                name = self.next().value
                self.next()
                if name == "COUNT" and self.at_op("*"):
                    self.next()
                    self.expect_op(")")
                    return Func("COUNT", star=True)
                distinct = bool(self.accept_kw("DISTINCT"))
                arg = self.parse_expression()
                self.expect_op(")")
                node = Func(name, arg=arg)
                node.distinct = distinct
                return node
            if t.value not in IDENT_OK_KEYWORDS:
                raise ParseError(f"unexpected keyword {t.value} in expression")
        if t.kind == "ident" or (t.kind == "kw" and t.value in IDENT_OK_KEYWORDS):
            self.next()
            if self.at_op(".") and self.peek(1).kind == "ident":
                self.next()
                col = self.next().value
                return Column(col, table=t.value.lower())
            return Column(t.value.lower())
        if t.kind == "op" and t.value == "(":
            self.next()
            expr = self.parse_expression()
            self.expect_op(")")
            return expr
        raise ParseError(f"unexpected {self._describe(t)} in expression")


def parse_statement(sql):
    p = Parser(sql)
    stmt = p.parse_statement()
    p.accept_op(";")
    if p.peek().kind != "eof":
        raise ParseError(f"unexpected trailing input: {p._describe(p.peek())}")
    return stmt


def parse_script(sql):
    p = Parser(sql)
    stmts = []
    while True:
        while p.accept_op(";"):
            pass
        if p.peek().kind == "eof":
            break
        stmts.append(p.parse_statement())
        if not p.accept_op(";"):
            if p.peek().kind != "eof":
                raise ParseError(f"expected ';' between statements, found {p._describe(p.peek())}")
    return stmts
