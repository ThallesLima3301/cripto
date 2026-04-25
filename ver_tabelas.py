import sqlite3

conn = sqlite3.connect("data/crypto_monitor.db")
cur = conn.cursor()

cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
for row in cur.fetchall():
    print(row[0])

conn.close()