import sqlite3

conn = sqlite3.connect("data/crypto_monitor.db")
conn.row_factory = sqlite3.Row
cur = conn.cursor()

for table in ["sell_tracking", "sell_signals", "buys"]:
    print(f"\n=== {table} ===")
    cur.execute(f"SELECT * FROM {table} ORDER BY 1 DESC LIMIT 10")
    rows = cur.fetchall()
    if not rows:
        print("(sem registros)")
    else:
        for row in rows:
            print(dict(row))

conn.close()