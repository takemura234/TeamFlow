from __future__ import annotations

import json
import os
import sqlite3
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory


BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("TEAMFLOW_DB", BASE_DIR / "teamflow.db"))

app = Flask(__name__, static_folder="static", static_url_path="/static")


def db() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def rows_to_dicts(rows):
    return [dict(row) for row in rows]


def init_db() -> None:
    schema = """
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT 'member'
    );
    CREATE TABLE IF NOT EXISTS projects (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        description TEXT DEFAULT '',
        start_date TEXT NOT NULL,
        end_date TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id INTEGER NOT NULL,
        title TEXT NOT NULL,
        assignee_id INTEGER,
        start_date TEXT NOT NULL,
        due_date TEXT NOT NULL,
        priority TEXT NOT NULL DEFAULT 'medium',
        status TEXT NOT NULL DEFAULT 'todo',
        progress INTEGER NOT NULL DEFAULT 0 CHECK(progress BETWEEN 0 AND 100),
        description TEXT DEFAULT '',
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(project_id) REFERENCES projects(id),
        FOREIGN KEY(assignee_id) REFERENCES users(id)
    );
    CREATE TABLE IF NOT EXISTS comments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id INTEGER NOT NULL,
        author TEXT NOT NULL,
        body TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS subtasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id INTEGER NOT NULL,
        title TEXT NOT NULL,
        done INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS notifications (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        type TEXT NOT NULL,
        body TEXT NOT NULL,
        is_read INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS ai_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        prompt TEXT NOT NULL,
        response TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    """
    with db() as connection:
        connection.executescript(schema)
        if connection.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
            seed(connection)


def seed(connection: sqlite3.Connection) -> None:
    today = date.today()
    users = [("武田 TL", "admin"), ("田中", "member"), ("横井", "member"), ("佐藤", "member"), ("鈴木", "member")]
    connection.executemany("INSERT INTO users(name, role) VALUES (?, ?)", users)
    connection.execute(
        "INSERT INTO projects(name, description, start_date, end_date) VALUES (?, ?, ?, ?)",
        ("車載リフト開発", "設計から試作評価までの開発プロジェクト", today.isoformat(), (today + timedelta(days=75)).isoformat()),
    )
    tasks = [
        (1, "油圧ユニット仕様確定", 2, -5, 3, "high", "in_progress", 65, "主要仕様と安全率を確定する"),
        (1, "フレーム強度解析", 3, 0, 10, "high", "in_progress", 35, "解析条件と荷重ケースをレビュー"),
        (1, "制御盤レイアウト設計", 4, 3, 17, "medium", "todo", 10, "配線性と保守性を考慮する"),
        (1, "試作部品の手配", 5, 8, 26, "medium", "todo", 0, "長納期品を優先して発注"),
        (1, "安全レビュー", 2, 25, 34, "high", "todo", 0, "リスクアセスメントを実施"),
        (1, "基本構想レビュー", 1, -14, -6, "low", "done", 100, "関係者レビュー済み"),
    ]
    connection.executemany(
        """INSERT INTO tasks(project_id, title, assignee_id, start_date, due_date, priority, status, progress, description)
           VALUES (?, ?, ?, date('now', ? || ' days'), date('now', ? || ' days'), ?, ?, ?, ?)""",
        tasks,
    )
    connection.executemany(
        "INSERT INTO notifications(type, body, is_read) VALUES (?, ?, ?)",
        [
            ("risk", "「油圧ユニット仕様確定」の期限まで3日です。進捗を確認してください。", 0),
            ("update", "横井さんが「フレーム強度解析」の進捗を35%に更新しました。", 0),
            ("milestone", "基本構想レビューが完了しました。", 1),
        ],
    )


TASK_SELECT = """
SELECT t.*, u.name AS assignee_name, p.name AS project_name,
       CAST(julianday(t.due_date) - julianday(date('now')) AS INTEGER) AS days_left
FROM tasks t
JOIN projects p ON p.id = t.project_id
LEFT JOIN users u ON u.id = t.assignee_id
"""


@app.get("/")
def index():
    return send_from_directory(BASE_DIR / "static", "index.html")


@app.get("/api/bootstrap")
def bootstrap():
    with db() as connection:
        tasks = rows_to_dicts(connection.execute(TASK_SELECT + " ORDER BY t.due_date").fetchall())
        projects = rows_to_dicts(connection.execute("SELECT * FROM projects ORDER BY id").fetchall())
        users = rows_to_dicts(connection.execute("SELECT * FROM users ORDER BY id").fetchall())
        notifications = rows_to_dicts(connection.execute("SELECT * FROM notifications ORDER BY created_at DESC, id DESC LIMIT 20").fetchall())
    return jsonify(tasks=tasks, projects=projects, users=users, notifications=notifications)


@app.get("/api/tasks")
def list_tasks():
    status = request.args.get("status")
    query = TASK_SELECT
    params = []
    if status and status != "all":
        query += " WHERE t.status = ?"
        params.append(status)
    query += " ORDER BY t.due_date"
    with db() as connection:
        return jsonify(rows_to_dicts(connection.execute(query, params).fetchall()))


@app.post("/api/tasks")
def create_task():
    data = request.get_json(force=True)
    required = ["title", "project_id", "start_date", "due_date"]
    if any(not data.get(field) for field in required):
        return jsonify(error="必須項目を入力してください"), 400
    if data["due_date"] < data["start_date"]:
        return jsonify(error="期限は開始日以降にしてください"), 400
    values = (
        int(data["project_id"]), data["title"].strip(), data.get("assignee_id") or None,
        data["start_date"], data["due_date"], data.get("priority", "medium"),
        data.get("status", "todo"), int(data.get("progress", 0)), data.get("description", "").strip(),
    )
    with db() as connection:
        cursor = connection.execute(
            """INSERT INTO tasks(project_id, title, assignee_id, start_date, due_date, priority, status, progress, description)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""", values,
        )
        task_id = cursor.lastrowid
        if data.get("priority") == "high":
            connection.execute("INSERT INTO notifications(type, body) VALUES ('urgent', ?)", (f"緊急タスク「{data['title']}」が追加されました。",))
        task = dict(connection.execute(TASK_SELECT + " WHERE t.id = ?", (task_id,)).fetchone())
    return jsonify(task), 201


@app.put("/api/tasks/<int:task_id>")
def update_task(task_id: int):
    data = request.get_json(force=True)
    allowed = {"title", "assignee_id", "start_date", "due_date", "priority", "status", "progress", "description"}
    updates = {key: value for key, value in data.items() if key in allowed}
    if not updates:
        return jsonify(error="更新項目がありません"), 400
    if "progress" in updates:
        updates["progress"] = max(0, min(100, int(updates["progress"])))
        if updates["progress"] == 100:
            updates["status"] = "done"
    assignments = ", ".join(f"{key} = ?" for key in updates)
    values = list(updates.values()) + [datetime.now().isoformat(timespec="seconds"), task_id]
    with db() as connection:
        cursor = connection.execute(f"UPDATE tasks SET {assignments}, updated_at = ? WHERE id = ?", values)
        if cursor.rowcount == 0:
            return jsonify(error="タスクが見つかりません"), 404
        task = dict(connection.execute(TASK_SELECT + " WHERE t.id = ?", (task_id,)).fetchone())
    return jsonify(task)


@app.delete("/api/tasks/<int:task_id>")
def delete_task(task_id: int):
    with db() as connection:
        cursor = connection.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    return ("", 204) if cursor.rowcount else (jsonify(error="タスクが見つかりません"), 404)


@app.get("/api/tasks/<int:task_id>")
def task_detail(task_id: int):
    with db() as connection:
        task = connection.execute(TASK_SELECT + " WHERE t.id = ?", (task_id,)).fetchone()
        if task is None:
            return jsonify(error="タスクが見つかりません"), 404
        subtasks = rows_to_dicts(connection.execute("SELECT * FROM subtasks WHERE task_id = ? ORDER BY id", (task_id,)).fetchall())
        comments = rows_to_dicts(connection.execute("SELECT * FROM comments WHERE task_id = ? ORDER BY created_at DESC, id DESC", (task_id,)).fetchall())
    return jsonify(task=dict(task), subtasks=subtasks, comments=comments)


@app.post("/api/tasks/<int:task_id>/subtasks")
def add_subtask(task_id: int):
    data = request.get_json(force=True)
    title = data.get("title", "").strip()
    if not title:
        return jsonify(error="サブタスク名を入力してください"), 400
    with db() as connection:
        if connection.execute("SELECT 1 FROM tasks WHERE id = ?", (task_id,)).fetchone() is None:
            return jsonify(error="タスクが見つかりません"), 404
        count = connection.execute("SELECT COUNT(*) FROM subtasks WHERE task_id = ?", (task_id,)).fetchone()[0]
        if count >= 10:
            return jsonify(error="サブタスクは10件までです"), 400
        cursor = connection.execute("INSERT INTO subtasks(task_id, title) VALUES (?, ?)", (task_id, title))
        subtask = dict(connection.execute("SELECT * FROM subtasks WHERE id = ?", (cursor.lastrowid,)).fetchone())
    return jsonify(subtask), 201


@app.put("/api/subtasks/<int:subtask_id>")
def update_subtask(subtask_id: int):
    data = request.get_json(force=True)
    with db() as connection:
        cursor = connection.execute("UPDATE subtasks SET done = ? WHERE id = ?", (1 if data.get("done") else 0, subtask_id))
        if cursor.rowcount == 0:
            return jsonify(error="サブタスクが見つかりません"), 404
        subtask = dict(connection.execute("SELECT * FROM subtasks WHERE id = ?", (subtask_id,)).fetchone())
    return jsonify(subtask)


@app.delete("/api/subtasks/<int:subtask_id>")
def delete_subtask(subtask_id: int):
    with db() as connection:
        cursor = connection.execute("DELETE FROM subtasks WHERE id = ?", (subtask_id,))
    return ("", 204) if cursor.rowcount else (jsonify(error="サブタスクが見つかりません"), 404)


@app.post("/api/tasks/<int:task_id>/comments")
def add_comment(task_id: int):
    data = request.get_json(force=True)
    body = data.get("body", "").strip()
    if not body:
        return jsonify(error="コメントを入力してください"), 400
    with db() as connection:
        cursor = connection.execute("INSERT INTO comments(task_id, author, body) VALUES (?, ?, ?)", (task_id, data.get("author", "武田 TL"), body))
        comment = dict(connection.execute("SELECT * FROM comments WHERE id = ?", (cursor.lastrowid,)).fetchone())
    return jsonify(comment), 201


@app.get("/api/dashboard")
def dashboard():
    with db() as connection:
        summary = dict(connection.execute("""
            SELECT COUNT(*) AS total,
                   SUM(status = 'done') AS completed,
                   SUM(status != 'done' AND due_date < date('now')) AS overdue,
                   SUM(status != 'done' AND due_date BETWEEN date('now') AND date('now', '+3 days') AND progress < 50) AS at_risk,
                   ROUND(AVG(progress)) AS avg_progress
            FROM tasks
        """).fetchone())
        risks = rows_to_dicts(connection.execute(TASK_SELECT + " WHERE t.status != 'done' AND (t.due_date < date('now', '+4 days') OR t.progress < 25) ORDER BY t.due_date LIMIT 5").fetchall())
    return jsonify(summary=summary, risks=risks)


def local_advice(prompt: str, tasks: list[dict]) -> str:
    active = [task for task in tasks if task["status"] != "done"]
    overdue = [task for task in active if task["days_left"] < 0]
    risks = sorted(active, key=lambda item: (item["days_left"], item["progress"]))[:3]
    lines = ["現在のタスク状況から、次の順で対応することをおすすめします。"]
    if overdue:
        lines.append(f"1. 期限超過の「{overdue[0]['title']}」を最優先で再計画してください。")
    elif risks:
        lines.append(f"1. 期限が近い「{risks[0]['title']}」の残作業と担当を今日中に確認してください。")
    if len(risks) > 1:
        lines.append(f"2. 「{risks[1]['title']}」は進捗{risks[1]['progress']}%です。30分の短いレビューで障害を洗い出してください。")
    lines.append("3. 高優先度タスクは、期限・担当・次の具体的な一手をチームで共有してください。")
    return "\n".join(lines)


def gemini_advice(prompt: str, tasks: list[dict]) -> str | None:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return None
    context = json.dumps(tasks, ensure_ascii=False)
    payload = json.dumps({"contents": [{"parts": [{"text": f"あなたは日本語のプロジェクト管理アドバイザーです。簡潔に回答してください。\nタスク: {context}\n質問: {prompt}"}]}]}).encode()
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={api_key}"
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            result = json.load(response)
        return result["candidates"][0]["content"]["parts"][0]["text"]
    except (urllib.error.URLError, KeyError, IndexError, TimeoutError):
        return None


@app.post("/api/ai-advice")
def ai_advice():
    data = request.get_json(force=True)
    prompt = data.get("prompt", "").strip()
    if not prompt:
        return jsonify(error="相談内容を入力してください"), 400
    with db() as connection:
        tasks = rows_to_dicts(connection.execute(TASK_SELECT + " ORDER BY t.due_date").fetchall())
        answer = gemini_advice(prompt, tasks) or local_advice(prompt, tasks)
        connection.execute("INSERT INTO ai_logs(prompt, response) VALUES (?, ?)", (prompt, answer))
    return jsonify(answer=answer, provider="gemini" if os.environ.get("GEMINI_API_KEY") else "local")


@app.get("/api/health")
def health():
    return jsonify(status="ok", service="TeamFlow")


if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")), debug=os.environ.get("FLASK_DEBUG") == "1")
