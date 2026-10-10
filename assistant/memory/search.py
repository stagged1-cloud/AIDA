r"""Search everything you and AIDA have said.  Usage:  python memory\search.py "words to find" [--days 30]"""
import sys
import sqlite3
import pathlib
import datetime

args = sys.argv[1:]
days = None
if "--days" in args:
    i = args.index("--days")
    days = int(args[i + 1])
    del args[i:i + 2]
q = " ".join(args).strip()
if not q:
    print(__doc__)
    sys.exit(0)
db = sqlite3.connect(str(pathlib.Path(__file__).with_name("memory.db")))
since = (datetime.date.today() - datetime.timedelta(days=days)).isoformat() if days else "0000"
try:
    rows = db.execute(
        "SELECT t.day, t.ts, t.who, t.text FROM turns_fts f JOIN turns t ON t.id = f.rowid "
        "WHERE turns_fts MATCH ? AND t.day >= ? ORDER BY t.id DESC LIMIT 40", (q, since)).fetchall()
except sqlite3.OperationalError:
    rows = db.execute("SELECT day, ts, who, text FROM turns WHERE text LIKE ? AND day >= ? "
                      "ORDER BY id DESC LIMIT 40", (f"%{q}%", since)).fetchall()
for day, ts, who, text in rows:
    print(f"{day} {ts} {who}: {text[:400]}")
if not rows:
    print("No matches.")
