import sqlite3
from datetime import datetime, timedelta

USAGE_DB_PATH = 'usage.db'

def init_db():
    conn = sqlite3.connect(USAGE_DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS usage (
                    user_id TEXT,
                    timestamp DATETIME
                )''')
    conn.commit()
    conn.close()

def check_limits(user_id):
    conn = sqlite3.connect(USAGE_DB_PATH)
    c = conn.cursor()
    now = datetime.now()
    one_day_ago = now - timedelta(days=1)

    # ユーザーごとのチェック
    c.execute("SELECT COUNT(*) FROM usage WHERE user_id = ? AND timestamp > ?", (user_id, one_day_ago))
    user_count = c.fetchone()[0]
    if user_count >= 20:
        conn.close()
        return False, "1日の実行回数の上限（20回）に達しました。"

    # 全ユーザー全体のチェック
    c.execute("SELECT COUNT(*) FROM usage WHERE timestamp > ?", (one_day_ago,))
    total_count = c.fetchone()[0]
    if total_count >= 1000:
        conn.close()
        return False, "全ユーザーの実行回数の上限（1000回）に達しました。"

    # 実行記録をデータベースに追加
    c.execute("INSERT INTO usage (user_id, timestamp) VALUES (?, ?)", (user_id, now))
    conn.commit()
    conn.close()
    return True, None