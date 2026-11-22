#!/usr/bin/env python3
"""Validate a SQL task folder end to end against a fresh SQLite database.

Usage: python3 tools/sql_check.py sql/tasks/<task>

The folder holds schema.sql, seed.sql (test data) and queries.sql.
Each query in queries.sql may start with a "-- name: <label>" line.

Checks:
  1. schema + seed load with foreign keys enforced (a bad row fails the load)
  2. PRAGMA integrity_check and foreign_key_check
  3. every table has a primary key; every foreign key is backed by an index
  4. normalization hints from the seed data:
       - non-key functional dependencies A -> B   (3NF / BCNF)
       - multivalued dependencies in all-key tables (4NF)
       - join dependencies of 3-column all-key tables (5NF)
  5. EXPLAIN QUERY PLAN per query: full scans and temp B-trees
  6. runs each query and prints its result as a table

Exit code is 1 if any ERROR was found, otherwise 0. WARN/HINT never fail.
"""
import itertools
import re
import sqlite3
import sys
from pathlib import Path

NAME_RE = re.compile(r"^\s*--\s*name:\s*(.+)$", re.MULTILINE)


def quote(identifier):
    return '"' + identifier.replace('"', '""') + '"'


def split_statements(sql_text):
    """Split on statement boundaries the way SQLite itself parses them."""
    statements, buffer = [], ""
    for line in sql_text.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            if buffer.strip():
                statements.append(buffer.strip())
            buffer = ""
    if buffer.strip() and not all(
        ln.strip().startswith("--") or not ln.strip() for ln in buffer.splitlines()
    ):
        statements.append(buffer.strip())
    return statements


class Report:
    def __init__(self):
        self.errors = 0

    def error(self, message):
        self.errors += 1
        print(f"  ERROR  {message}")

    @staticmethod
    def warn(message):
        print(f"  WARN   {message}")

    @staticmethod
    def hint(message):
        print(f"  HINT   {message}")

    @staticmethod
    def ok(message):
        print(f"  OK     {message}")


def user_tables(conn):
    return [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_schema WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name")]


def primary_key(conn, table):
    cols = [(r[5], r[1]) for r in conn.execute(f"PRAGMA table_info({quote(table)})") if r[5]]
    return [name for _, name in sorted(cols)]


def unique_column_sets(conn, table):
    """PK plus every UNIQUE index, each as a frozenset of columns."""
    sets = [frozenset(primary_key(conn, table))] if primary_key(conn, table) else []
    for idx in conn.execute(f"PRAGMA index_list({quote(table)})"):
        if idx[2]:  # unique
            sets.append(frozenset(r[2] for r in conn.execute(f"PRAGMA index_info({quote(idx[1])})")))
    return sets


def index_prefixes(conn, table):
    prefixes = []
    pk = primary_key(conn, table)
    if pk:
        prefixes.append(pk)
    for idx in conn.execute(f"PRAGMA index_list({quote(table)})"):
        prefixes.append([r[2] for r in conn.execute(f"PRAGMA index_info({quote(idx[1])})")])
    return prefixes


def check_structure(conn, report):
    print("\n[2] Integrity and foreign keys")
    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    (report.ok if integrity == "ok" else report.error)(f"integrity_check: {integrity}")
    violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    for table, rowid, parent, _ in violations:
        report.error(f"{table} rowid {rowid} references a missing row in {parent}")
    if not violations:
        report.ok("foreign_key_check: no orphan rows")

    print("\n[3] Keys and indexes")
    for table in user_tables(conn):
        if not primary_key(conn, table):
            report.error(f"{table}: no PRIMARY KEY")
        fk_groups = {}
        for row in conn.execute(f"PRAGMA foreign_key_list({quote(table)})"):
            fk_groups.setdefault(row[0], (row[2], []))[1].append((row[1], row[3], row[4]))
        indexed = index_prefixes(conn, table)
        for parent, columns in fk_groups.values():
            child_cols = [c for _, c, _ in sorted(columns)]
            parent_cols = [p for _, _, p in sorted(columns)]
            label = f"{table}({', '.join(child_cols)}) -> {parent}({', '.join(c or 'PK' for c in parent_cols)})"
            if any(sorted(ix[:len(child_cols)]) == sorted(child_cols) for ix in indexed):
                report.ok(f"FK {label} is indexed")
            else:
                report.warn(f"FK {label} has no index; joins/deletes on it scan {table}")


def check_normalization(conn, report):
    print("\n[4] Normalization hints (inferred from seed data, so only as good as the data)")
    found = False
    for table in user_tables(conn):
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({quote(table)})")]
        keys = unique_column_sets(conn, table)
        pk = primary_key(conn, table)
        row_count = conn.execute(f"SELECT COUNT(*) FROM {quote(table)}").fetchone()[0]
        if row_count < 3:
            continue
        key_cols = set().union(*keys) if keys else set()

        # 3NF/BCNF: single non-key determinant A that repeats yet fixes B.
        for a, b in itertools.permutations([c for c in cols if c not in key_cols], 2):
            repeats, violations = conn.execute(
                f"SELECT COUNT(*) - COUNT(DISTINCT {quote(a)}), "
                f"(SELECT COUNT(*) FROM (SELECT 1 FROM {quote(table)} WHERE {quote(a)} IS NOT NULL "
                f"GROUP BY {quote(a)} HAVING COUNT(DISTINCT {quote(b)}) > 1)) "
                f"FROM {quote(table)} WHERE {quote(a)} IS NOT NULL").fetchone()
            if repeats > 0 and violations == 0:
                found = True
                report.hint(f"{table}: {a} -> {b} holds in data; if real, move {b} to a table keyed by {a}")

        # 4NF / 5NF only matter for all-key tables of 3+ columns.
        if len(pk) < 3 or set(pk) != set(cols):
            continue
        t = quote(table)
        for x in pk:
            rest = [c for c in pk if c != x]
            for y_size in range(1, len(rest)):
                for y in itertools.combinations(rest, y_size):
                    if rest[0] not in y:  # X ->> Z is the complement of X ->> Y; report once
                        continue
                    z = [c for c in rest if c not in y]
                    sel_y = ", ".join(quote(c) for c in (x, *y))
                    sel_z = ", ".join(quote(c) for c in (x, *z))
                    joined = conn.execute(
                        f"SELECT COUNT(*) FROM (SELECT DISTINCT {sel_y} FROM {t}) p "
                        f"JOIN (SELECT DISTINCT {sel_z} FROM {t}) q USING ({quote(x)})").fetchone()[0]
                    if joined == row_count and row_count > conn.execute(
                            f"SELECT COUNT(*) FROM (SELECT DISTINCT {sel_y} FROM {t})").fetchone()[0]:
                        found = True
                        report.hint(f"{table}: {x} ->> {', '.join(y)} (4NF); split into "
                                    f"({sel_y.replace(chr(34), '')}) and ({sel_z.replace(chr(34), '')})")
        if len(pk) == 3:
            a, b, c = (quote(col) for col in pk)
            joined = conn.execute(
                f"SELECT COUNT(*) FROM (SELECT DISTINCT {a},{b} FROM {t}) ab "
                f"JOIN (SELECT DISTINCT {b},{c} FROM {t}) bc USING ({b}) "
                f"JOIN (SELECT DISTINCT {a},{c} FROM {t}) ac USING ({a},{c})").fetchone()[0]
            if joined == row_count:
                pairs = [conn.execute(f"SELECT COUNT(*) FROM (SELECT DISTINCT {p},{q} FROM {t})").fetchone()[0]
                         for p, q in ((a, b), (b, c), (a, c))]
                if max(pairs) < row_count:
                    found = True
                    report.hint(f"{table}: lossless 3-way decomposition exists (5NF); "
                                f"consider tables {pk[0]}-{pk[1]}, {pk[1]}-{pk[2]}, {pk[0]}-{pk[2]}")
    if not found:
        report.ok("no FD/MVD/JD violations visible in the data")


def check_queries(conn, queries_file, report):
    statements = split_statements(queries_file.read_text())
    for position, statement in enumerate(statements, 1):
        match = NAME_RE.search(statement)
        label = match.group(1).strip() if match else f"query {position}"
        print(f"\n[5/6] {label}")
        try:
            plan = conn.execute(f"EXPLAIN QUERY PLAN {statement}").fetchall()
        except sqlite3.Error as exc:
            report.error(f"does not compile: {exc}")
            continue
        for _, _, _, detail in plan:
            if detail.startswith("SCAN") and "INDEX" not in detail and "CONSTANT ROW" not in detail:
                report.warn(f"plan: {detail}  (full scan; fine only for a driving table read once)")
            elif "TEMP B-TREE" in detail:
                report.warn(f"plan: {detail}  (extra sort; an index on those columns removes it)")
            else:
                print(f"         plan: {detail}")
        cursor = conn.execute(statement)
        if cursor.description is None:
            continue
        headers = [d[0] for d in cursor.description]
        rows = [["NULL" if v is None else str(v) for v in row] for row in cursor.fetchall()]
        widths = [max(len(h), *(len(r[i]) for r in rows)) if rows else len(h)
                  for i, h in enumerate(headers)]
        print("  " + " | ".join(h.ljust(w) for h, w in zip(headers, widths)))
        print("  " + "-+-".join("-" * w for w in widths))
        for row in rows:
            print("  " + " | ".join(v.ljust(w) for v, w in zip(row, widths)))
        print(f"  ({len(rows)} row{'s' if len(rows) != 1 else ''})")


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    task_dir = Path(sys.argv[1])
    report = Report()
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")

    print(f"SQLite {sqlite3.sqlite_version} | task: {task_dir}")
    print("\n[1] Load schema and seed data (foreign keys enforced)")
    for name in ("schema.sql", "seed.sql"):
        path = task_dir / name
        if not path.exists():
            report.error(f"{name} missing")
            continue
        try:
            conn.executescript(path.read_text())
            report.ok(f"{name} loaded")
        except sqlite3.Error as exc:
            report.error(f"{name}: {exc}")
    conn.execute("ANALYZE")

    check_structure(conn, report)
    check_normalization(conn, report)
    queries = task_dir / "queries.sql"
    if queries.exists():
        check_queries(conn, queries, report)

    print(f"\n{'FAILED' if report.errors else 'PASSED'}: {report.errors} error(s)")
    sys.exit(1 if report.errors else 0)


if __name__ == "__main__":
    main()
