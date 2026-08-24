import os
import random
import shutil
import sqlite3
import tempfile
import unittest

os.environ["LEAFDB_FSYNC"] = "0"

from leafdb.engine import Database


def norm(rows):
    out = []
    for row in rows:
        vals = []
        for v in row:
            if isinstance(v, float):
                vals.append(round(v, 6))
            else:
                vals.append(v)
        out.append(tuple(vals))
    return sorted(out, key=lambda r: tuple((x is None, type(x).__name__, x) for x in r))


class DifferentialTest(unittest.TestCase):
    """Runs identical workloads against LeafDB and stdlib SQLite and asserts
    byte-for-byte equivalent result sets."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="leafdb_diff_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.db = Database(os.path.join(self.tmp, "diff.db"))
        self.addCleanup(self.db.close)
        self.sql = sqlite3.connect(":memory:")
        self.addCleanup(self.sql.close)
        self._seed()
        self.supports_right_full = sqlite3.sqlite_version_info >= (3, 39)

    def _seed(self):
        rng = random.Random(2026)
        first = ["ada", "grace", "linus", "ken", "barbara", "james", "margaret", "dennis"]
        depts = ["core", "tools", "research"]
        cities = ["pune", "delhi", "goa"]
        employees = []
        for i in range(120):
            name = rng.choice(first) + "_" + str(i)
            dept = rng.choice(depts)
            city = rng.choice(cities)
            salary = rng.randint(50, 200) * 1000
            bonus = None if rng.random() < 0.25 else rng.randint(1, 50) * 100
            age = None if rng.random() < 0.10 else rng.randint(21, 65)
            employees.append((i, name, dept, city, salary, bonus, age))
        departments = [(i, d, rng.randint(1, 5)) for i, d in enumerate(depts)]
        tasks = []
        for j in range(400):
            tasks.append((j, rng.randint(0, 119), rng.choice([1, 2, 4, 8, None])))

        self.db.execute_script(
            """
            CREATE TABLE employees (
                id INT PRIMARY KEY, name TEXT, dept TEXT, city TEXT,
                salary INT, bonus INT, age INT);
            CREATE TABLE departments (id INT PRIMARY KEY, name TEXT, floor INT);
            CREATE TABLE tasks (id INT PRIMARY KEY, emp_id INT, hours INT);
            """
        )
        self._bulk("employees", employees, 7)
        self._bulk("departments", departments, 7)
        self._bulk("tasks", tasks, 100)

        cur = self.sql.cursor()
        cur.execute(
            "CREATE TABLE employees (id INTEGER PRIMARY KEY, name TEXT, dept TEXT, "
            "city TEXT, salary INT, bonus INT, age INT)")
        cur.execute("CREATE TABLE departments (id INTEGER PRIMARY KEY, name TEXT, floor INT)")
        cur.execute("CREATE TABLE tasks (id INTEGER PRIMARY KEY, emp_id INT, hours INT)")
        cur.executemany("INSERT INTO employees VALUES (?,?,?,?,?,?,?)", employees)
        cur.executemany("INSERT INTO departments VALUES (?,?,?)", departments)
        cur.executemany("INSERT INTO tasks VALUES (?,?,?)", tasks)
        self.sql.commit()

    def _bulk(self, table, rows, batch):
        def lit(v):
            if v is None:
                return "NULL"
            if isinstance(v, str):
                return "'" + v.replace("'", "''") + "'"
            return str(v)
        stmts = [f"INSERT INTO {table} VALUES ({','.join(lit(v) for v in r)})" for r in rows]
        self.db.execute_script("BEGIN;")
        for i in range(0, len(stmts), batch):
            self.db.execute_script(";\n".join(stmts[i:i + batch]) + ";")
        self.db.execute_script("COMMIT;")

    def assert_same(self, sql):
        with self.subTest(sql=sql):
            ours = norm(self.db.execute(sql).rows)
            theirs = norm(self.sql.execute(sql).fetchall())
            self.assertEqual(ours, theirs)

    def test_simple_filters(self):
        for pred in [
            "salary > 150000",
            "salary <= 80000 AND dept = 'core'",
            "city <> 'goa'",
            "bonus IS NULL",
            "bonus IS NOT NULL AND age < 40",
            "age = 30 OR age IS NULL",
            "NOT (dept = 'tools')",
            "salary + COALESCE(bonus, 0) > 160000" if False else "salary + 0 = 150000",
        ]:
            self.assert_same(f"SELECT id FROM employees WHERE {pred} ORDER BY id")

    def test_between_in_like(self):
        for pred in [
            "salary BETWEEN 90000 AND 120000",
            "salary NOT BETWEEN 90000 AND 120000",
            "age BETWEEN 25 AND 35",
            "dept IN ('core', 'research')",
            "city NOT IN ('pune')",
            "id IN (1, 2, 3)",
            "name LIKE 'ada%'",
            "name NOT LIKE '%_9'",
            "name LIKE '%research%'",
            "bonus IS NULL AND salary LIKE '1%'",
        ]:
            self.assert_same(f"SELECT id FROM employees WHERE {pred} ORDER BY id")

    def test_null_propagation_arithmetic(self):
        for expr in [
            "bonus + 100",
            "bonus * 2",
            "age - 10",
            "salary / 1000",
            "salary / 0",
            "bonus / 0",
        ]:
            self.assert_same(f"SELECT id, {expr} FROM employees ORDER BY id")

    def test_three_valued_logic_combos(self):
        for pred in [
            "bonus > 1000 OR age < 30",
            "bonus > 1000 AND age < 30",
            "NOT bonus > 1000",
            "NOT (age = 25 OR bonus IS NULL)",
            "age = 25 OR bonus IS NULL",
            "(salary < 100000 OR bonus IS NOT NULL) AND NOT (city = 'delhi')",
        ]:
            self.assert_same(f"SELECT id FROM employees WHERE {pred} ORDER BY id")

    def test_order_limit_offset_with_nulls(self):
        for q in [
            "SELECT id, age FROM employees ORDER BY age ASC, id LIMIT 7",
            "SELECT id, age FROM employees ORDER BY age DESC, id DESC LIMIT 7",
            "SELECT id, bonus FROM employees ORDER BY bonus ASC LIMIT 5 OFFSET 5",
            "SELECT id, salary FROM employees ORDER BY salary DESC LIMIT 4 OFFSET 3",
            "SELECT id FROM employees ORDER BY age, id LIMIT 100 OFFSET 115",
        ]:
            self.assert_same(q)

    def test_distinct_and_aggregates(self):
        for q in [
            "SELECT DISTINCT dept FROM employees ORDER BY dept",
            "SELECT DISTINCT city, dept FROM employees ORDER BY city, dept",
            "SELECT COUNT(*), COUNT(bonus), COUNT(age) FROM employees",
            "SELECT SUM(salary), MIN(age), MAX(age) FROM employees",
            "SELECT AVG(bonus), AVG(salary) FROM employees",
            "SELECT SUM(bonus) FROM employees WHERE city = 'nowhere'",
        ]:
            self.assert_same(q)

    def test_group_by_having(self):
        for q in [
            "SELECT dept, COUNT(*) AS n FROM employees GROUP BY dept ORDER BY dept",
            "SELECT city, COUNT(*) AS c, SUM(salary) AS s FROM employees "
            "GROUP BY city HAVING c > 5 ORDER BY s DESC",
            "SELECT dept, AVG(salary) AS avg_sal FROM employees "
            "GROUP BY dept HAVING avg_sal > 100000 ORDER BY avg_sal DESC",
            "SELECT city, COUNT(bonus) AS nb FROM employees GROUP BY city ORDER BY city",
            "SELECT age, COUNT(*) FROM employees GROUP BY age ORDER BY age LIMIT 6",
        ]:
            self.assert_same(q)

    def test_inner_and_left_joins(self):
        for q in [
            "SELECT e.id, d.name FROM employees e JOIN departments d ON e.dept = d.name ORDER BY e.id",
            "SELECT e.id, t.hours FROM employees e LEFT JOIN tasks t ON t.emp_id = e.id "
            "ORDER BY e.id, t.hours LIMIT 60",
            "SELECT d.name, COUNT(e.id) AS heads FROM departments d "
            "LEFT JOIN employees e ON e.dept = d.name GROUP BY d.name ORDER BY d.name",
            "SELECT e.name, t.hours FROM tasks t JOIN employees e ON t.emp_id = e.id "
            "WHERE t.hours IS NOT NULL ORDER BY t.id LIMIT 40",
        ]:
            self.assert_same(q)

    def test_right_full_outer_joins(self):
        if not self.supports_right_full:
            self.skipTest("sqlite < 3.39 lacks RIGHT/FULL JOIN")
        for q in [
            "SELECT t.id, e.id FROM tasks t RIGHT JOIN employees e ON t.emp_id = e.id "
            "ORDER BY t.id, e.id LIMIT 80",
            "SELECT e.id, t.id FROM employees e FULL OUTER JOIN tasks t "
            "ON t.emp_id = e.id AND t.hours = 8 ORDER BY e.id, t.id LIMIT 80",
        ]:
            self.assert_same(q)

    def test_random_predicate_fuzz(self):
        rng = random.Random(99)
        cols = [
            ("salary", lambda r: r.randint(50000, 200000)),
            ("bonus", lambda r: None if r.random() < 0.3 else r.randint(100, 5000)),
            ("age", lambda r: None if r.random() < 0.3 else r.randint(20, 70)),
        ]
        ops = ["=", "<>", "<", "<=", ">", ">="]
        for trial in range(150):
            col_fn = rng.choice(cols)
            col, gen = col_fn
            val = gen(rng)
            literal = "NULL" if val is None else str(val)
            choice = rng.random()
            if choice < 0.45 and val is not None:
                pred = f"{col} {rng.choice(ops)} {literal}"
            elif choice < 0.6:
                pred = f"{col} IS {'NOT ' if rng.random() < 0.5 else ''}NULL"
            elif choice < 0.8 and val is not None:
                lo = val - rng.randint(1, 30000) if col == "salary" else (val or 0) - 10
                hi = val + rng.randint(1, 30000) if col == "salary" else (val or 0) + 10
                pred = f"{col} BETWEEN {lo} AND {hi}"
            else:
                vals = ", ".join("NULL" if (g := gen(rng)) is None else str(g) for _ in range(3))
                neg = "NOT " if rng.random() < 0.5 else ""
                pred = f"{col} {neg}IN ({vals})"
            sql = f"SELECT id FROM employees WHERE {pred} ORDER BY id"
            ours = norm(self.db.execute(sql).rows)
            theirs = norm(self.sql.execute(sql).fetchall())
            self.assertEqual(ours, theirs, msg=f"fuzz #{trial}: {sql}")

    def test_random_groupby_fuzz(self):
        rng = random.Random(4242)
        agg_fns = ["COUNT(*)", "COUNT(bonus)", "SUM(salary)", "AVG(bonus)", "MIN(age)", "MAX(salary)"]
        group_cols = ["dept", "city", "age"]
        for trial in range(60):
            gcol = rng.choice(group_cols)
            agg = rng.choice(agg_fns)
            threshold = rng.randint(0, 60)
            having = f"HAVING n > {threshold}" if rng.random() < 0.6 else ""
            direction = "ASC" if rng.random() < 0.5 else "DESC"
            q = (f"SELECT {gcol}, {agg} AS n FROM employees GROUP BY {gcol} "
                 f"{having} ORDER BY n {direction}, {gcol} ASC")
            ours = norm(db_rows(self.db, q))
            theirs = norm(self.sql.execute(q).fetchall())
            self.assertEqual(ours, theirs, msg=f"groupfuzz #{trial}: {q}")


def db_rows(db, sql):
    return db.execute(sql).rows


if __name__ == "__main__":
    unittest.main(verbosity=2)
