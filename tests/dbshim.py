"""SQLite stand-in for MySQL (pymysql) used by the tests. No network, no MySQL.

    from dbshim import setup
    Conn = setup("name")          # fresh database file; patch module.get_db_connection = Conn
"""
import os
import re
import sqlite3
import sys
import tempfile
import types
from datetime import datetime

pm = types.ModuleType("pymysql")
pm.cursors = types.ModuleType("pymysql.cursors")
pm.cursors.DictCursor = object
pm.connect = lambda **kw: None
sys.modules.setdefault("pymysql", pm)
sys.modules.setdefault("pymysql.cursors", pm.cursors)
sqlite3.register_adapter(datetime, lambda d: d.strftime("%Y-%m-%d %H:%M:%S"))


def translate(sql):
    s = sql.strip()
    if "GET_LOCK" in s or "RELEASE_LOCK" in s:
        return "SELECT 1 AS got"
    m = re.match(r"SHOW COLUMNS FROM (\w+)(?: LIKE '(\w+)')?$", s, re.I)
    if m:
        t, col = m.group(1), m.group(2)
        q = f"SELECT name AS Field, type AS Type FROM pragma_table_info('{t}')"
        return q + (f" WHERE name='{col}'" if col else "")
    if re.match(r"ALTER TABLE \w+ (MODIFY|CHANGE|DROP INDEX|ADD (UNIQUE )?(INDEX|KEY))", s, re.I):
        return "SELECT 1"
    if s.upper().startswith("CREATE TABLE"):
        lines = [ln for ln in s.splitlines() if not ln.strip().startswith(("INDEX ", "FOREIGN KEY", "KEY "))]
        s = "\n".join(lines).replace("INT PRIMARY KEY AUTO_INCREMENT", "INTEGER PRIMARY KEY AUTOINCREMENT")
        s = re.sub(r"UNIQUE KEY \w+ ", "UNIQUE ", s)
        s = re.sub(r"ENUM\([^)]*\)", "TEXT", s)
        s = re.sub(r"\bON UPDATE CURRENT_TIMESTAMP\b", "", s)
        s = re.sub(r"\)\s*ENGINE=.*$", ")", s, flags=re.S)
        s = re.sub(r",\s*\)\s*$", "\n)", s)
    s = re.sub(r"\bNOW\(\)", "CURRENT_TIMESTAMP", s)
    return s.replace("%s", "?")


def setup(name, fresh=True):
    path = os.path.join(tempfile.gettempdir(), f"{name}_test.db")
    if fresh and os.path.exists(path):
        os.remove(path)

    class Cur:
        is_sqlite = True

        def __init__(self, conn):
            self.c = conn.cursor()
            self.rowcount = -1

        def execute(self, sql, params=()):
            t = translate(sql)
            self.c.execute(t, tuple(params or ()) if "?" in t else ())
            self.rowcount = self.c.rowcount

        def fetchone(self):
            r = self.c.fetchone()
            return dict(r) if r else None

        def fetchall(self):
            return [dict(r) for r in self.c.fetchall()]

        def close(self):
            self.c.close()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            self.close()
            return False

        @property
        def lastrowid(self):
            return self.c.lastrowid

    class Conn:
        def __init__(self):
            self.conn = sqlite3.connect(path, isolation_level=None)
            self.conn.row_factory = sqlite3.Row

        def cursor(self):
            return Cur(self.conn)

        def commit(self):
            pass

        def close(self):
            self.conn.close()

        def ping(self, reconnect=False):
            pass

    return Conn
