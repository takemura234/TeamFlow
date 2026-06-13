from __future__ import annotations

import json
import os
import secrets
import sqlite3
import csv
import io
import urllib.error
import urllib.request
from functools import wraps
from datetime import date, datetime, timedelta
from pathlib import Path

from flask import Flask, Response, jsonify, redirect, request, send_file, send_from_directory, session
from werkzeug.exceptions import HTTPException
from werkzeug.security import check_password_hash, generate_password_hash

try:
    from apscheduler.schedulers.background import BackgroundScheduler
except ImportError:
    BackgroundScheduler = None


BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("TEAMFLOW_DB", BASE_DIR / "teamflow.db"))
BACKUP_DIR = Path(os.environ.get("TEAMFLOW_BACKUP_DIR", BASE_DIR / "backups"))

app = Flask(__name__, static_folder="static", static_url_path="/static")
app.config.update(
    SECRET_KEY=os.environ.get("TEAMFLOW_SECRET_KEY", "teamflow-development-secret-change-me"),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("TEAMFLOW_COOKIE_SECURE") == "1",
)


def db() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 30000")
    return connection


@app.errorhandler(Exception)
def handle_unexpected_error(error):
    if isinstance(error, HTTPException):
        return error
    app.logger.exception("Unhandled error on %s", request.path)
    if request.path.startswith("/api/"):
        return jsonify(error="サーバー処理に失敗しました。少し待ってから再度お試しください"), 500
    return "Internal Server Error", 500


def rows_to_dicts(rows):
    return [dict(row) for row in rows]


def migrate_users(connection: sqlite3.Connection) -> None:
    columns = {row[1] for row in connection.execute("PRAGMA table_info(users)")}
    if "username" not in columns:
        connection.execute("ALTER TABLE users ADD COLUMN username TEXT")
    if "password_hash" not in columns:
        connection.execute("ALTER TABLE users ADD COLUMN password_hash TEXT")
    if "line_user_id" not in columns:
        connection.execute("ALTER TABLE users ADD COLUMN line_user_id TEXT")

    initial_password = os.environ.get("TEAMFLOW_INITIAL_PASSWORD", "TeamFlow2026!")
    users = connection.execute("SELECT id, role, username, password_hash FROM users ORDER BY id").fetchall()
    for user in users:
        username = user["username"] or ("admin" if user["role"] == "admin" else f"member{user['id']}")
        password_hash = user["password_hash"] or generate_password_hash(initial_password)
        connection.execute(
            "UPDATE users SET username = ?, password_hash = ? WHERE id = ?",
            (username, password_hash, user["id"]),
        )
    connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_username ON users(username)")


def migrate_team_members(connection: sqlite3.Connection) -> None:
    team = [
        (1, "武村TL", "admin", "admin"),
        (2, "中村", "member", "nakamura"),
        (3, "岡田", "member", "okada"),
        (4, "磯貝", "member", "isogai"),
        (5, "水野", "member", "mizuno"),
        (6, "淵田", "member", "fuchida"),
        (7, "榊原", "member", "sakakibara"),
    ]
    initial_password = os.environ.get("TEAMFLOW_INITIAL_PASSWORD", "TeamFlow2026!")
    for user_id, name, role, username in team:
        existing = connection.execute("SELECT id, password_hash FROM users WHERE id = ?", (user_id,)).fetchone()
        if existing:
            connection.execute(
                "UPDATE users SET name = ?, role = ?, username = ? WHERE id = ?",
                (name, role, username, user_id),
            )
        else:
            connection.execute(
                "INSERT INTO users(id, name, role, username, password_hash) VALUES (?, ?, ?, ?, ?)",
                (user_id, name, role, username, generate_password_hash(initial_password)),
            )


def migrate_notifications(connection: sqlite3.Connection) -> None:
    columns = {row[1] for row in connection.execute("PRAGMA table_info(notifications)")}
    additions = {
        "user_id": "INTEGER",
        "task_id": "INTEGER",
        "event_key": "TEXT",
        "read_at": "TEXT",
    }
    for name, definition in additions.items():
        if name not in columns:
            connection.execute(f"ALTER TABLE notifications ADD COLUMN {name} {definition}")
    connection.execute("UPDATE notifications SET user_id = 1 WHERE user_id IS NULL")
    connection.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_notifications_event_user ON notifications(event_key, user_id) WHERE event_key IS NOT NULL"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_notifications_user_created ON notifications(user_id, created_at DESC)"
    )


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
    CREATE TABLE IF NOT EXISTS milestones (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        due_date TEXT NOT NULL,
        achieved_at TEXT,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS user_notification_settings (
        user_id INTEGER PRIMARY KEY,
        in_app_enabled INTEGER NOT NULL DEFAULT 1,
        line_enabled INTEGER NOT NULL DEFAULT 0,
        weekly_summary_enabled INTEGER NOT NULL DEFAULT 1,
        FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
    );
    """
    with db() as connection:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.executescript(schema)
        migrate_users(connection)
        if connection.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
            seed(connection)
            migrate_users(connection)
        migrate_team_members(connection)
        migrate_notifications(connection)
        connection.execute(
            """INSERT OR IGNORE INTO user_notification_settings(user_id)
               SELECT id FROM users WHERE id BETWEEN 1 AND 7"""
        )


def add_notification(
    connection: sqlite3.Connection,
    user_id: int,
    notification_type: str,
    body: str,
    event_key: str,
    task_id: int | None = None,
) -> bool:
    enabled = connection.execute(
        "SELECT in_app_enabled FROM user_notification_settings WHERE user_id = ?", (user_id,)
    ).fetchone()
    if enabled is not None and not enabled["in_app_enabled"]:
        return False
    cursor = connection.execute(
        """INSERT OR IGNORE INTO notifications(user_id, task_id, type, body, event_key)
           VALUES (?, ?, ?, ?, ?)""",
        (user_id, task_id, notification_type, body, event_key),
    )
    if cursor.rowcount > 0:
        line = connection.execute(
            """SELECT u.line_user_id, s.line_enabled
               FROM users u JOIN user_notification_settings s ON s.user_id = u.id
               WHERE u.id = ?""",
            (user_id,),
        ).fetchone()
        if line and line["line_enabled"] and line["line_user_id"]:
            send_line_message(line["line_user_id"], body)
    return cursor.rowcount > 0


def send_line_message(line_user_id: str, message: str) -> bool:
    token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
    if not token:
        return False
    payload = json.dumps({
        "to": line_user_id,
        "messages": [{"type": "text", "text": message[:5000]}],
    }).encode()
    req = urllib.request.Request(
        "https://api.line.me/v2/bot/message/push",
        data=payload,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            return 200 <= response.status < 300
    except (urllib.error.URLError, TimeoutError):
        return False


def run_notification_checks(connection: sqlite3.Connection) -> int:
    today = date.today().isoformat()
    created = 0
    admin_id = 1
    tasks = connection.execute(
        """SELECT t.id, t.title, t.assignee_id, t.due_date, t.progress, t.updated_at, u.name AS assignee_name,
                  CAST(julianday(t.due_date) - julianday(date('now')) AS INTEGER) AS days_left
           FROM tasks t LEFT JOIN users u ON u.id = t.assignee_id
           WHERE t.status != 'done'"""
    ).fetchall()
    for task in tasks:
        assignee_id = task["assignee_id"]
        if 0 <= task["days_left"] <= 3 and task["progress"] < 50:
            body = f"「{task['title']}」は期限まで{task['days_left']}日、進捗{task['progress']}%です。遅延リスクを確認してください。"
            for user_id in {admin_id, assignee_id} - {None}:
                created += add_notification(
                    connection, user_id, "risk", body, f"risk:{task['id']}:{today}", task["id"]
                )
        if task["days_left"] < 0:
            body = f"「{task['title']}」が期限を{abs(task['days_left'])}日超過しています。担当：{task['assignee_name'] or '未割当'}"
            created += add_notification(
                connection, admin_id, "overdue", body, f"overdue:{task['id']}:{today}", task["id"]
            )
        stale = connection.execute(
            "SELECT datetime(?) <= datetime('now', '-7 days')", (task["updated_at"],)
        ).fetchone()[0]
        if stale and assignee_id:
            body = f"「{task['title']}」は7日以上更新されていません。進捗を更新してください。"
            created += add_notification(
                connection, assignee_id, "stale", body, f"stale:{task['id']}:{today}", task["id"]
            )
    return created


def seed(connection: sqlite3.Connection) -> None:
    today = date.today()
    users = [("武村TL", "admin"), ("中村", "member"), ("岡田", "member"), ("磯貝", "member"), ("水野", "member")]
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


def current_user():
    user_id = session.get("user_id")
    if not user_id:
        return None
    with db() as connection:
        return connection.execute(
            "SELECT id, name, username, role FROM users WHERE id = ?", (user_id,)
        ).fetchone()


def require_roles(*roles):
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            user = current_user()
            if user is None:
                return jsonify(error="ログインが必要です"), 401
            if roles and user["role"] not in roles:
                return jsonify(error="この操作を行う権限がありません"), 403
            return view(*args, **kwargs)
        return wrapped
    return decorator


def can_edit_task(connection: sqlite3.Connection, task_id: int) -> bool:
    user = current_user()
    if user is None:
        return False
    if user["role"] == "admin":
        return True
    task = connection.execute("SELECT assignee_id FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return bool(task and user["role"] == "member" and task["assignee_id"] == user["id"])


TASK_PRIORITIES = {"high", "medium", "low"}
TASK_STATUSES = {"todo", "in_progress", "review", "done", "hold"}


def validate_task_data(connection: sqlite3.Connection, data: dict, partial: bool = False):
    required = ("title", "project_id", "start_date", "due_date")
    if not partial and any(not data.get(field) for field in required):
        return "必須項目を入力してください"
    if "title" in data:
        data["title"] = str(data["title"]).strip()
        if not data["title"]:
            return "タスク名を入力してください"
        if len(data["title"]) > 120:
            return "タスク名は120文字以内で入力してください"
    if "project_id" in data:
        try:
            data["project_id"] = int(data["project_id"])
        except (TypeError, ValueError):
            return "プロジェクトが正しくありません"
        if connection.execute("SELECT 1 FROM projects WHERE id = ?", (data["project_id"],)).fetchone() is None:
            return "プロジェクトが見つかりません"
    if "assignee_id" in data:
        data["assignee_id"] = data["assignee_id"] or None
        if data["assignee_id"] is not None:
            try:
                data["assignee_id"] = int(data["assignee_id"])
            except (TypeError, ValueError):
                return "担当者が正しくありません"
            if connection.execute("SELECT 1 FROM users WHERE id = ?", (data["assignee_id"],)).fetchone() is None:
                return "担当者が見つかりません"
    for field in ("start_date", "due_date"):
        if field in data:
            try:
                date.fromisoformat(data[field])
            except (TypeError, ValueError):
                return "日付が正しくありません"
    if "priority" in data and data["priority"] not in TASK_PRIORITIES:
        return "優先度が正しくありません"
    if "status" in data and data["status"] not in TASK_STATUSES:
        return "ステータスが正しくありません"
    if "progress" in data:
        try:
            data["progress"] = int(data["progress"])
        except (TypeError, ValueError):
            return "進捗が正しくありません"
        if not 0 <= data["progress"] <= 100:
            return "進捗は0%から100%で入力してください"
    if "description" in data:
        data["description"] = str(data["description"] or "").strip()
    if "update_comment" in data:
        data["update_comment"] = str(data["update_comment"] or "").strip()
        if len(data["update_comment"]) > 1000:
            return "更新コメントは1000文字以内で入力してください"
    return None


@app.before_request
def protect_api():
    if not request.path.startswith("/api/") or request.path in {"/api/session", "/api/health"}:
        return None
    if current_user() is None:
        return jsonify(error="ログインが必要です"), 401
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        token = request.headers.get("X-CSRF-Token")
        if not token or not secrets.compare_digest(token, session.get("csrf_token", "")):
            return jsonify(error="セキュリティトークンが無効です"), 403
    return None


TASK_SELECT = """
SELECT t.*, u.name AS assignee_name, p.name AS project_name,
       CAST(julianday(t.due_date) - julianday(date('now')) AS INTEGER) AS days_left
FROM tasks t
JOIN projects p ON p.id = t.project_id
LEFT JOIN users u ON u.id = t.assignee_id
"""


@app.get("/")
def index():
    if current_user() is None:
        return redirect("/login")
    response = send_from_directory(BASE_DIR / "static", "index.html")
    response.headers["Cache-Control"] = "no-cache"
    return response


@app.get("/login")
def login_page():
    response = send_from_directory(BASE_DIR / "static", "login.html")
    response.headers["Cache-Control"] = "no-cache"
    return response


@app.get("/sw.js")
def service_worker():
    response = send_from_directory(BASE_DIR / "static", "sw.js")
    response.headers["Service-Worker-Allowed"] = "/"
    response.headers["Cache-Control"] = "no-cache"
    return response


@app.route("/api/session", methods=["GET", "POST", "DELETE"])
def user_session():
    if request.method == "GET":
        user = current_user()
        if user is None:
            return jsonify(authenticated=False), 401
        return jsonify(authenticated=True, user=dict(user), csrf_token=session["csrf_token"])

    if request.method == "DELETE":
        session.clear()
        return "", 204

    data = request.get_json(silent=True) or {}
    username = data.get("username", "").strip()
    password = data.get("password", "")
    with db() as connection:
        user = connection.execute(
            "SELECT id, name, username, role, password_hash FROM users WHERE username = ?", (username,)
        ).fetchone()
    if user is None or not check_password_hash(user["password_hash"], password):
        return jsonify(error="ログインIDまたはパスワードが違います"), 401
    session.clear()
    session["user_id"] = user["id"]
    session["csrf_token"] = secrets.token_urlsafe(32)
    return jsonify(
        user={key: user[key] for key in ("id", "name", "username", "role")},
        csrf_token=session["csrf_token"],
    )


@app.put("/api/account/password")
def change_password():
    data = request.get_json(force=True)
    current_password = data.get("current_password", "")
    new_password = data.get("new_password", "")
    if len(new_password) < 10 or not any(c.isalpha() for c in new_password) or not any(c.isdigit() for c in new_password):
        return jsonify(error="新しいパスワードは英字と数字を含む10文字以上にしてください"), 400
    user = current_user()
    with db() as connection:
        account = connection.execute("SELECT password_hash FROM users WHERE id = ?", (user["id"],)).fetchone()
        if not check_password_hash(account["password_hash"], current_password):
            return jsonify(error="現在のパスワードが違います"), 400
        connection.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (generate_password_hash(new_password), user["id"]),
        )
    return jsonify(ok=True)


@app.put("/api/account/line")
def update_line_account():
    data = request.get_json(force=True)
    line_user_id = str(data.get("line_user_id", "")).strip()
    if line_user_id and (len(line_user_id) > 64 or not line_user_id.startswith("U")):
        return jsonify(error="LINE User IDが正しくありません"), 400
    user = current_user()
    with db() as connection:
        connection.execute("UPDATE users SET line_user_id = ? WHERE id = ?", (line_user_id or None, user["id"]))
    return jsonify(line_user_id=line_user_id)


@app.post("/api/notify/line/test")
def test_line_notification():
    user = current_user()
    with db() as connection:
        account = connection.execute("SELECT line_user_id FROM users WHERE id = ?", (user["id"],)).fetchone()
    if not account["line_user_id"]:
        return jsonify(error="LINE User IDを設定してください"), 400
    if not os.environ.get("LINE_CHANNEL_ACCESS_TOKEN"):
        return jsonify(error="LINE_CHANNEL_ACCESS_TOKENが未設定です"), 503
    if not send_line_message(account["line_user_id"], "TeamFlowからのテスト通知です。"):
        return jsonify(error="LINE通知の送信に失敗しました"), 502
    return jsonify(ok=True)


@app.put("/api/admin/users/<int:user_id>/password")
@require_roles("admin")
def admin_reset_password(user_id: int):
    data = request.get_json(force=True)
    new_password = data.get("new_password", "")
    if len(new_password) < 10 or not any(c.isalpha() for c in new_password) or not any(c.isdigit() for c in new_password):
        return jsonify(error="仮パスワードは英字と数字を含む10文字以上にしてください"), 400
    with db() as connection:
        cursor = connection.execute(
            "UPDATE users SET password_hash = ? WHERE id = ? AND id BETWEEN 1 AND 7",
            (generate_password_hash(new_password), user_id),
        )
        if cursor.rowcount == 0:
            return jsonify(error="ユーザーが見つかりません"), 404
    return jsonify(ok=True)


def create_database_backup() -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    target = BACKUP_DIR / f"teamflow-{datetime.now():%Y%m%d-%H%M%S-%f}.db"
    with sqlite3.connect(DB_PATH) as source, sqlite3.connect(target) as destination:
        source.backup(destination)
    return target


@app.post("/api/admin/backup")
@require_roles("admin")
def create_backup():
    target = create_database_backup()
    return jsonify(filename=target.name, size=target.stat().st_size)


@app.get("/api/admin/backup/<path:filename>")
@require_roles("admin")
def download_backup(filename: str):
    safe_name = Path(filename).name
    target = BACKUP_DIR / safe_name
    if not target.exists() or target.suffix != ".db":
        return jsonify(error="バックアップが見つかりません"), 404
    return send_file(target, as_attachment=True, download_name=safe_name)


CSV_FIELDS = ["title", "project", "assignee", "start_date", "due_date", "priority", "status", "progress", "description"]
CSV_ALIASES = {
    "title": ("title", "タスク名"), "project": ("project", "プロジェクト"),
    "assignee": ("assignee", "担当者"), "start_date": ("start_date", "開始日"),
    "due_date": ("due_date", "期限"), "priority": ("priority", "優先度"),
    "status": ("status", "ステータス", "状態"), "progress": ("progress", "進捗"),
    "description": ("description", "説明"),
}
PRIORITY_IMPORT = {"高": "high", "中": "medium", "低": "low", "high": "high", "medium": "medium", "low": "low"}
STATUS_IMPORT = {
    "未着手": "todo", "進行中": "in_progress", "レビュー待ち": "review", "完了": "done", "保留": "hold",
    "todo": "todo", "in_progress": "in_progress", "review": "review", "done": "done", "hold": "hold",
}


def csv_safe(value) -> str:
    text = str(value or "")
    return f"'{text}" if text.startswith(("=", "+", "-", "@")) else text


@app.get("/api/admin/tasks/export")
@require_roles("admin")
def export_tasks_csv():
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=CSV_FIELDS)
    writer.writeheader()
    with db() as connection:
        tasks = connection.execute(TASK_SELECT + " ORDER BY t.due_date, t.id").fetchall()
    for task in tasks:
        writer.writerow({
            "title": csv_safe(task["title"]), "project": csv_safe(task["project_name"]),
            "assignee": csv_safe(task["assignee_name"]), "start_date": task["start_date"],
            "due_date": task["due_date"], "priority": task["priority"], "status": task["status"],
            "progress": task["progress"], "description": csv_safe(task["description"]),
        })
    filename = f"teamflow-tasks-{date.today().isoformat()}.csv"
    return Response(
        "\ufeff" + output.getvalue(),
        content_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/api/admin/tasks/import")
@require_roles("admin")
def import_tasks_csv():
    uploaded = request.files.get("file")
    if uploaded is None or not uploaded.filename.lower().endswith(".csv"):
        return jsonify(error="CSVファイルを選択してください"), 400
    raw = uploaded.read(2_000_001)
    if len(raw) > 2_000_000:
        return jsonify(error="CSVファイルは2MB以下にしてください"), 413
    try:
        reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig")))
    except UnicodeDecodeError:
        return jsonify(error="CSVはUTF-8形式で保存してください"), 400
    if not reader.fieldnames:
        return jsonify(error="CSVのヘッダーがありません"), 400
    headers = {name.strip(): name for name in reader.fieldnames if name}
    columns = {field: next((headers[name] for name in aliases if name in headers), None) for field, aliases in CSV_ALIASES.items()}
    if not all(columns[field] for field in ("title", "project", "start_date", "due_date")):
        return jsonify(error="必須列は title, project, start_date, due_date です"), 400

    prepared = []
    errors = []
    with db() as connection:
        projects = {row["name"]: row["id"] for row in connection.execute("SELECT id, name FROM projects")}
        users = {}
        for row in connection.execute("SELECT id, name, username FROM users WHERE id BETWEEN 1 AND 7"):
            users[row["name"]] = row["id"]
            users[row["username"]] = row["id"]
        for line_number, row in enumerate(reader, start=2):
            if line_number > 1001:
                errors.append("1000行を超えるCSVは読み込めません")
                break
            value = lambda field: str(row.get(columns[field], "") or "").strip() if columns[field] else ""
            project_name = value("project")
            data = {
                "title": value("title"), "project_id": projects.get(project_name),
                "assignee_id": users.get(value("assignee")) if value("assignee") else None,
                "start_date": value("start_date"), "due_date": value("due_date"),
                "priority": PRIORITY_IMPORT.get(value("priority") or "medium"),
                "status": STATUS_IMPORT.get(value("status") or "todo"),
                "progress": value("progress") or 0, "description": value("description"),
            }
            if project_name not in projects:
                errors.append(f"{line_number}行目: プロジェクト「{project_name}」が見つかりません")
                continue
            assignee = value("assignee")
            if assignee and assignee not in users:
                errors.append(f"{line_number}行目: 担当者「{assignee}」が見つかりません")
                continue
            if data["priority"] is None or data["status"] is None:
                errors.append(f"{line_number}行目: 優先度またはステータスが正しくありません")
                continue
            error = validate_task_data(connection, data)
            if error or data["due_date"] < data["start_date"]:
                errors.append(f"{line_number}行目: {error or '期限は開始日以降にしてください'}")
                continue
            if data["status"] == "done" or data["progress"] == 100:
                data["status"], data["progress"] = "done", 100
            prepared.append(data)
        if errors:
            return jsonify(error="CSVを読み込めません", details=errors[:20]), 400
        if not prepared:
            return jsonify(error="登録できるタスクがありません"), 400
        connection.executemany(
            """INSERT INTO tasks(project_id, title, assignee_id, start_date, due_date, priority, status, progress, description)
               VALUES (:project_id, :title, :assignee_id, :start_date, :due_date, :priority, :status, :progress, :description)""",
            prepared,
        )
    return jsonify(imported=len(prepared)), 201


@app.get("/api/bootstrap")
def bootstrap():
    user = current_user()
    with db() as connection:
        run_notification_checks(connection)
        tasks = rows_to_dicts(connection.execute(TASK_SELECT + " ORDER BY t.due_date").fetchall())
        projects = rows_to_dicts(connection.execute(
            """SELECT p.*,
                      (SELECT COUNT(*) FROM tasks t WHERE t.project_id = p.id) AS task_count,
                      (SELECT COUNT(*) FROM milestones m WHERE m.project_id = p.id) AS milestone_count
               FROM projects p ORDER BY p.start_date, p.id"""
        ).fetchall())
        user_fields = "id, name, role, username" if user["role"] == "admin" else "id, name, role"
        users = rows_to_dicts(connection.execute(
            f"SELECT {user_fields} FROM users WHERE id BETWEEN 1 AND 7 ORDER BY id"
        ).fetchall())
        notifications = rows_to_dicts(connection.execute(
            "SELECT * FROM notifications WHERE user_id = ? ORDER BY created_at DESC, id DESC LIMIT 50",
            (user["id"],),
        ).fetchall())
        milestones = rows_to_dicts(connection.execute(
            """SELECT m.*, p.name AS project_name,
                      CAST(julianday(m.due_date) - julianday(date('now')) AS INTEGER) AS days_left
               FROM milestones m JOIN projects p ON p.id = m.project_id
               ORDER BY m.due_date, m.id"""
        ).fetchall())
        notification_settings = dict(connection.execute(
            "SELECT * FROM user_notification_settings WHERE user_id = ?", (user["id"],)
        ).fetchone())
        account = dict(connection.execute(
            "SELECT line_user_id FROM users WHERE id = ?", (user["id"],)
        ).fetchone())
    return jsonify(
        tasks=tasks,
        projects=projects,
        users=users,
        notifications=notifications,
        milestones=milestones,
        notification_settings=notification_settings,
        account=account,
        current_user=dict(user),
        csrf_token=session["csrf_token"],
    )


@app.get("/api/notifications")
def list_notifications():
    user = current_user()
    with db() as connection:
        notifications = rows_to_dicts(connection.execute(
            "SELECT * FROM notifications WHERE user_id = ? ORDER BY created_at DESC, id DESC LIMIT 50",
            (user["id"],),
        ).fetchall())
    return jsonify(notifications)


@app.put("/api/notifications/<int:notification_id>/read")
def read_notification(notification_id: int):
    user = current_user()
    with db() as connection:
        cursor = connection.execute(
            """UPDATE notifications SET is_read = 1, read_at = ?
               WHERE id = ? AND user_id = ?""",
            (datetime.now().isoformat(timespec="seconds"), notification_id, user["id"]),
        )
        if cursor.rowcount == 0:
            return jsonify(error="通知が見つかりません"), 404
        notification = dict(connection.execute(
            "SELECT * FROM notifications WHERE id = ?", (notification_id,)
        ).fetchone())
    return jsonify(notification)


@app.put("/api/notifications/read-all")
def read_all_notifications():
    user = current_user()
    with db() as connection:
        connection.execute(
            """UPDATE notifications SET is_read = 1, read_at = ?
               WHERE user_id = ? AND is_read = 0""",
            (datetime.now().isoformat(timespec="seconds"), user["id"]),
        )
    return jsonify(ok=True)


@app.post("/api/notifications/check")
@require_roles("admin")
def check_notifications():
    with db() as connection:
        created = run_notification_checks(connection)
        notifications = rows_to_dicts(connection.execute(
            "SELECT * FROM notifications WHERE user_id = ? ORDER BY created_at DESC, id DESC LIMIT 50",
            (current_user()["id"],),
        ).fetchall())
    return jsonify(created=created, notifications=notifications)


@app.get("/api/users")
@require_roles("admin")
def list_users():
    with db() as connection:
        users = rows_to_dicts(connection.execute(
            "SELECT id, name, username, role FROM users WHERE id BETWEEN 1 AND 7 ORDER BY id"
        ).fetchall())
    return jsonify(users)


def validate_project_data(data: dict, partial: bool = False):
    if not partial or "name" in data:
        data["name"] = str(data.get("name", "")).strip()
        if not data["name"]:
            return "プロジェクト名を入力してください"
        if len(data["name"]) > 120:
            return "プロジェクト名は120文字以内で入力してください"
    if "description" in data:
        data["description"] = str(data["description"] or "").strip()
    for field in ("start_date", "end_date"):
        if not partial or field in data:
            try:
                date.fromisoformat(data.get(field, ""))
            except (TypeError, ValueError):
                return "日付が正しくありません"
    return None


@app.get("/api/projects")
def list_projects():
    with db() as connection:
        projects = rows_to_dicts(connection.execute(
            """SELECT p.*,
                      (SELECT COUNT(*) FROM tasks t WHERE t.project_id = p.id) AS task_count,
                      (SELECT COUNT(*) FROM milestones m WHERE m.project_id = p.id) AS milestone_count
               FROM projects p ORDER BY p.start_date, p.id"""
        ).fetchall())
    return jsonify(projects)


@app.post("/api/projects")
@require_roles("admin")
def create_project():
    data = request.get_json(force=True)
    error = validate_project_data(data)
    if error:
        return jsonify(error=error), 400
    if data["end_date"] < data["start_date"]:
        return jsonify(error="終了日は開始日以降にしてください"), 400
    with db() as connection:
        cursor = connection.execute(
            """INSERT INTO projects(name, description, start_date, end_date)
               VALUES (?, ?, ?, ?)""",
            (data["name"], data.get("description", ""), data["start_date"], data["end_date"]),
        )
        project = dict(connection.execute(
            """SELECT p.*, 0 AS task_count, 0 AS milestone_count
               FROM projects p WHERE p.id = ?""",
            (cursor.lastrowid,),
        ).fetchone())
    return jsonify(project), 201


@app.put("/api/projects/<int:project_id>")
@require_roles("admin")
def update_project(project_id: int):
    data = request.get_json(force=True)
    allowed = {key: value for key, value in data.items() if key in {"name", "description", "start_date", "end_date"}}
    if not allowed:
        return jsonify(error="更新項目がありません"), 400
    with db() as connection:
        existing = connection.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        if existing is None:
            return jsonify(error="プロジェクトが見つかりません"), 404
        error = validate_project_data(allowed, partial=True)
        if error:
            return jsonify(error=error), 400
        start_date = allowed.get("start_date", existing["start_date"])
        end_date = allowed.get("end_date", existing["end_date"])
        if end_date < start_date:
            return jsonify(error="終了日は開始日以降にしてください"), 400
        assignments = ", ".join(f"{key} = ?" for key in allowed)
        connection.execute(
            f"UPDATE projects SET {assignments} WHERE id = ?", (*allowed.values(), project_id)
        )
        project = dict(connection.execute(
            """SELECT p.*,
                      (SELECT COUNT(*) FROM tasks t WHERE t.project_id = p.id) AS task_count,
                      (SELECT COUNT(*) FROM milestones m WHERE m.project_id = p.id) AS milestone_count
               FROM projects p WHERE p.id = ?""",
            (project_id,),
        ).fetchone())
    return jsonify(project)


@app.delete("/api/projects/<int:project_id>")
@require_roles("admin")
def delete_project(project_id: int):
    cascade = request.args.get("cascade") == "1"
    with db() as connection:
        project = connection.execute("SELECT id FROM projects WHERE id = ?", (project_id,)).fetchone()
        if project is None:
            return jsonify(error="プロジェクトが見つかりません"), 404
        task_count = connection.execute("SELECT COUNT(*) FROM tasks WHERE project_id = ?", (project_id,)).fetchone()[0]
        milestone_count = connection.execute("SELECT COUNT(*) FROM milestones WHERE project_id = ?", (project_id,)).fetchone()[0]
        if (task_count or milestone_count) and not cascade:
            return jsonify(error="タスクまたはマイルストーンがあるプロジェクトは削除できません"), 409
        if cascade:
            task_ids = [row["id"] for row in connection.execute("SELECT id FROM tasks WHERE project_id = ?", (project_id,))]
            if task_ids:
                placeholders = ",".join("?" for _ in task_ids)
                connection.execute(f"DELETE FROM notifications WHERE task_id IN ({placeholders})", task_ids)
            connection.execute("DELETE FROM tasks WHERE project_id = ?", (project_id,))
            connection.execute("DELETE FROM milestones WHERE project_id = ?", (project_id,))
        connection.execute("DELETE FROM projects WHERE id = ?", (project_id,))
    return "", 204


@app.get("/api/milestones")
def list_milestones():
    with db() as connection:
        milestones = rows_to_dicts(connection.execute(
            """SELECT m.*, p.name AS project_name,
                      CAST(julianday(m.due_date) - julianday(date('now')) AS INTEGER) AS days_left
               FROM milestones m JOIN projects p ON p.id = m.project_id
               ORDER BY m.due_date, m.id"""
        ).fetchall())
    return jsonify(milestones)


@app.post("/api/milestones")
@require_roles("admin")
def create_milestone():
    data = request.get_json(force=True)
    name = str(data.get("name", "")).strip()
    due_date = data.get("due_date", "")
    try:
        project_id = int(data.get("project_id"))
        date.fromisoformat(due_date)
    except (TypeError, ValueError):
        return jsonify(error="入力内容が正しくありません"), 400
    if not name:
        return jsonify(error="マイルストーン名を入力してください"), 400
    with db() as connection:
        if connection.execute("SELECT 1 FROM projects WHERE id = ?", (project_id,)).fetchone() is None:
            return jsonify(error="プロジェクトが見つかりません"), 404
        cursor = connection.execute(
            "INSERT INTO milestones(project_id, name, due_date) VALUES (?, ?, ?)",
            (project_id, name, due_date),
        )
        milestone = dict(connection.execute(
            """SELECT m.*, p.name AS project_name,
                      CAST(julianday(m.due_date) - julianday(date('now')) AS INTEGER) AS days_left
               FROM milestones m JOIN projects p ON p.id = m.project_id WHERE m.id = ?""",
            (cursor.lastrowid,),
        ).fetchone())
    return jsonify(milestone), 201


@app.put("/api/milestones/<int:milestone_id>")
@require_roles("admin")
def update_milestone(milestone_id: int):
    data = request.get_json(force=True)
    achieved = data.get("achieved")
    if not isinstance(achieved, bool):
        return jsonify(error="達成状態が正しくありません"), 400
    with db() as connection:
        existing = connection.execute(
            "SELECT achieved_at FROM milestones WHERE id = ?", (milestone_id,)
        ).fetchone()
        if existing is None:
            return jsonify(error="マイルストーンが見つかりません"), 404
        cursor = connection.execute(
            "UPDATE milestones SET achieved_at = ? WHERE id = ?",
            (datetime.now().isoformat(timespec="seconds") if achieved else None, milestone_id),
        )
        if cursor.rowcount == 0:
            return jsonify(error="マイルストーンが見つかりません"), 404
        milestone = dict(connection.execute(
            """SELECT m.*, p.name AS project_name,
                      CAST(julianday(m.due_date) - julianday(date('now')) AS INTEGER) AS days_left
               FROM milestones m JOIN projects p ON p.id = m.project_id WHERE m.id = ?""",
            (milestone_id,),
        ).fetchone())
        if achieved and not existing["achieved_at"]:
            for recipient in connection.execute("SELECT id FROM users WHERE id BETWEEN 1 AND 7"):
                add_notification(
                    connection,
                    recipient["id"],
                    "milestone",
                    f"マイルストーン「{milestone['name']}」を達成しました。",
                    f"milestone:{milestone_id}:achieved",
                )
    return jsonify(milestone)


@app.delete("/api/milestones/<int:milestone_id>")
@require_roles("admin")
def delete_milestone(milestone_id: int):
    with db() as connection:
        cursor = connection.execute("DELETE FROM milestones WHERE id = ?", (milestone_id,))
    return ("", 204) if cursor.rowcount else (jsonify(error="マイルストーンが見つかりません"), 404)


@app.put("/api/settings/notifications")
def update_notification_settings():
    data = request.get_json(force=True)
    fields = ("in_app_enabled", "line_enabled", "weekly_summary_enabled")
    values = []
    for field in fields:
        if not isinstance(data.get(field), bool):
            return jsonify(error="通知設定が正しくありません"), 400
        values.append(1 if data[field] else 0)
    user = current_user()
    with db() as connection:
        connection.execute(
            """UPDATE user_notification_settings
               SET in_app_enabled = ?, line_enabled = ?, weekly_summary_enabled = ?
               WHERE user_id = ?""",
            (*values, user["id"]),
        )
        settings = dict(connection.execute(
            "SELECT * FROM user_notification_settings WHERE user_id = ?", (user["id"],)
        ).fetchone())
    return jsonify(settings)


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
@require_roles("admin", "member")
def create_task():
    data = request.get_json(force=True)
    user = current_user()
    if user["role"] == "member":
        data["assignee_id"] = user["id"]
    with db() as connection:
        error = validate_task_data(connection, data)
        if error:
            return jsonify(error=error), 400
        if data["due_date"] < data["start_date"]:
            return jsonify(error="期限は開始日以降にしてください"), 400
        progress = data.get("progress", 0)
        status = data.get("status", "todo")
        if status == "done" or progress == 100:
            progress, status = 100, "done"
        values = (
            data["project_id"], data["title"], data.get("assignee_id"),
            data["start_date"], data["due_date"], data.get("priority", "medium"),
            status, progress, data.get("description", ""),
        )
        cursor = connection.execute(
            """INSERT INTO tasks(project_id, title, assignee_id, start_date, due_date, priority, status, progress, description)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""", values,
        )
        task_id = cursor.lastrowid
        if data.get("priority") == "high":
            for recipient in connection.execute("SELECT id FROM users WHERE id BETWEEN 1 AND 7"):
                add_notification(
                    connection,
                    recipient["id"],
                    "urgent",
                    f"緊急タスクが追加されました：「{data['title']}」",
                    f"urgent:{task_id}",
                    task_id,
                )
        task = dict(connection.execute(TASK_SELECT + " WHERE t.id = ?", (task_id,)).fetchone())
    return jsonify(task), 201


@app.put("/api/tasks/<int:task_id>")
def update_task(task_id: int):
    data = request.get_json(force=True)
    allowed = {"project_id", "title", "assignee_id", "start_date", "due_date", "priority", "status", "progress", "description", "update_comment"}
    user = current_user()
    if user["role"] == "member":
        allowed = {"status", "progress", "update_comment"}
    updates = {key: value for key, value in data.items() if key in allowed}
    if not updates:
        return jsonify(error="更新項目がありません"), 400
    with db() as connection:
        if not can_edit_task(connection, task_id):
            return jsonify(error="このタスクを編集する権限がありません"), 403
        existing = connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if existing is None:
            return jsonify(error="タスクが見つかりません"), 404
        error = validate_task_data(connection, updates, partial=True)
        if error:
            return jsonify(error=error), 400
        start_date = updates.get("start_date", existing["start_date"])
        due_date = updates.get("due_date", existing["due_date"])
        if due_date < start_date:
            return jsonify(error="期限は開始日以降にしてください"), 400
        comment = updates.pop("update_comment", "")
        progress = updates.get("progress", existing["progress"])
        status = updates.get("status", existing["status"])
        if updates.get("status") == "done" or ("progress" in updates and progress == 100):
            updates["progress"], updates["status"] = 100, "done"
        elif "status" in updates and status != "done" and progress == 100:
            updates["progress"] = 95
        if not updates and not comment:
            return jsonify(error="更新項目がありません"), 400
        if updates:
            assignments = ", ".join(f"{key} = ?" for key in updates)
            values = list(updates.values()) + [datetime.now().isoformat(timespec="seconds"), task_id]
            cursor = connection.execute(f"UPDATE tasks SET {assignments}, updated_at = ? WHERE id = ?", values)
        else:
            cursor = connection.execute(
                "UPDATE tasks SET updated_at = ? WHERE id = ?",
                (datetime.now().isoformat(timespec="seconds"), task_id),
            )
        if cursor.rowcount == 0:
            return jsonify(error="タスクが見つかりません"), 404
        if comment:
            connection.execute(
                "INSERT INTO comments(task_id, author, body) VALUES (?, ?, ?)",
                (task_id, user["name"], comment),
            )
        task = dict(connection.execute(TASK_SELECT + " WHERE t.id = ?", (task_id,)).fetchone())
    return jsonify(task)


@app.put("/api/tasks/<int:task_id>/schedule")
@require_roles("admin")
def update_task_schedule(task_id: int):
    data = request.get_json(force=True)
    try:
        start_date = date.fromisoformat(data.get("start_date", ""))
        due_date = date.fromisoformat(data.get("due_date", ""))
    except (TypeError, ValueError):
        return jsonify(error="日付が正しくありません"), 400
    if due_date < start_date:
        return jsonify(error="期限は開始日以降にしてください"), 400
    with db() as connection:
        task = connection.execute("SELECT title, start_date, due_date FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if task is None:
            return jsonify(error="タスクが見つかりません"), 404
        old_start, old_due = task["start_date"], task["due_date"]
        connection.execute(
            """UPDATE tasks SET start_date = ?, due_date = ?, updated_at = ? WHERE id = ?""",
            (start_date.isoformat(), due_date.isoformat(), datetime.now().isoformat(timespec="seconds"), task_id),
        )
        if old_start != start_date.isoformat() or old_due != due_date.isoformat():
            connection.execute(
                "INSERT INTO comments(task_id, author, body) VALUES (?, ?, ?)",
                (
                    task_id,
                    current_user()["name"],
                    f"ガントチャートで期間を変更：{old_start} ～ {old_due} → {start_date.isoformat()} ～ {due_date.isoformat()}",
                ),
            )
        updated = dict(connection.execute(TASK_SELECT + " WHERE t.id = ?", (task_id,)).fetchone())
    return jsonify(updated)


@app.delete("/api/tasks/<int:task_id>")
@require_roles("admin")
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
        if not can_edit_task(connection, task_id):
            return jsonify(error="このタスクを編集する権限がありません"), 403
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
        subtask = connection.execute("SELECT task_id FROM subtasks WHERE id = ?", (subtask_id,)).fetchone()
        if subtask is None:
            return jsonify(error="サブタスクが見つかりません"), 404
        if not can_edit_task(connection, subtask["task_id"]):
            return jsonify(error="このタスクを編集する権限がありません"), 403
        cursor = connection.execute("UPDATE subtasks SET done = ? WHERE id = ?", (1 if data.get("done") else 0, subtask_id))
        if cursor.rowcount == 0:
            return jsonify(error="サブタスクが見つかりません"), 404
        subtask = dict(connection.execute("SELECT * FROM subtasks WHERE id = ?", (subtask_id,)).fetchone())
    return jsonify(subtask)


@app.delete("/api/subtasks/<int:subtask_id>")
def delete_subtask(subtask_id: int):
    with db() as connection:
        subtask = connection.execute("SELECT task_id FROM subtasks WHERE id = ?", (subtask_id,)).fetchone()
        if subtask is None:
            return jsonify(error="サブタスクが見つかりません"), 404
        if not can_edit_task(connection, subtask["task_id"]):
            return jsonify(error="このタスクを編集する権限がありません"), 403
        cursor = connection.execute("DELETE FROM subtasks WHERE id = ?", (subtask_id,))
    return ("", 204) if cursor.rowcount else (jsonify(error="サブタスクが見つかりません"), 404)


@app.post("/api/tasks/<int:task_id>/comments")
def add_comment(task_id: int):
    data = request.get_json(force=True)
    body = data.get("body", "").strip()
    if not body:
        return jsonify(error="コメントを入力してください"), 400
    with db() as connection:
        if connection.execute("SELECT 1 FROM tasks WHERE id = ?", (task_id,)).fetchone() is None:
            return jsonify(error="タスクが見つかりません"), 404
        cursor = connection.execute(
            "INSERT INTO comments(task_id, author, body) VALUES (?, ?, ?)",
            (task_id, current_user()["name"], body),
        )
        comment = dict(connection.execute("SELECT * FROM comments WHERE id = ?", (cursor.lastrowid,)).fetchone())
    return jsonify(comment), 201


@app.get("/api/dashboard")
def dashboard():
    project_id = request.args.get("project_id", type=int)
    where = "WHERE project_id = ?" if project_id else ""
    params = (project_id,) if project_id else ()
    with db() as connection:
        summary = dict(connection.execute(f"""
            SELECT COUNT(*) AS total,
                   SUM(status = 'done') AS completed,
                   SUM(status != 'done' AND due_date < date('now')) AS overdue,
                   SUM(status != 'done' AND due_date BETWEEN date('now') AND date('now', '+3 days') AND progress < 50) AS at_risk,
                   ROUND(AVG(progress)) AS avg_progress
            FROM tasks {where}
        """, params).fetchone())
        risk_where = "t.status != 'done' AND (t.due_date < date('now', '+4 days') OR t.progress < 25)"
        risk_params = []
        if project_id:
            risk_where += " AND t.project_id = ?"
            risk_params.append(project_id)
        risks = rows_to_dicts(connection.execute(
            TASK_SELECT + f" WHERE {risk_where} ORDER BY t.due_date LIMIT 5", risk_params
        ).fetchall())
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


def gemini_generate(prompt: str) -> str | None:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return None
    model = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
    payload = json.dumps({"contents": [{"parts": [{"text": prompt}]}]}).encode()
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            result = json.load(response)
        return result["candidates"][0]["content"]["parts"][0]["text"]
    except (urllib.error.URLError, KeyError, IndexError, TimeoutError):
        return None


def gemini_advice(prompt: str, tasks: list[dict]) -> str | None:
    context = json.dumps(tasks, ensure_ascii=False)
    return gemini_generate(
        f"あなたは日本語のプロジェクト管理アドバイザーです。簡潔に回答してください。\nタスク: {context}\n質問: {prompt}"
    )


def build_weekly_report(tasks: list[dict]) -> str:
    completed = [task for task in tasks if task["status"] == "done"]
    active = [task for task in tasks if task["status"] != "done"]
    overdue = [task for task in active if task["days_left"] < 0]
    important = sorted(active, key=lambda task: (task["priority"] != "high", task["days_left"]))[:5]
    lines = ["【TeamFlow 週次レポート】", f"完了タスク: {len(completed)}件", f"進行中タスク: {len(active)}件", f"期限超過: {len(overdue)}件"]
    if important:
        lines.append("今週の重要タスク:")
        lines.extend(f"・{task['title']}（{task['assignee_name'] or '未割当'} / {task['progress']}%）" for task in important)
    if overdue:
        lines.append("最優先で期限超過タスクの再計画を行ってください。")
    return "\n".join(lines)


@app.post("/api/ai-advice")
@require_roles("admin")
def ai_advice():
    data = request.get_json(force=True)
    prompt = data.get("prompt", "").strip()
    if not prompt:
        return jsonify(error="相談内容を入力してください"), 400
    with db() as connection:
        tasks = rows_to_dicts(connection.execute(TASK_SELECT + " ORDER BY t.due_date").fetchall())
        gemini_answer = gemini_advice(prompt, tasks)
        answer = gemini_answer or local_advice(prompt, tasks)
        connection.execute("INSERT INTO ai_logs(prompt, response) VALUES (?, ?)", (prompt, answer))
    return jsonify(answer=answer, provider="gemini" if gemini_answer else "local")


@app.post("/api/ai-replan/<int:task_id>")
@require_roles("admin")
def ai_replan(task_id: int):
    with db() as connection:
        task = connection.execute(TASK_SELECT + " WHERE t.id = ?", (task_id,)).fetchone()
        if task is None:
            return jsonify(error="タスクが見つかりません"), 404
        workloads = connection.execute(
            """SELECT u.id, u.name, COUNT(t.id) AS active_count
               FROM users u LEFT JOIN tasks t ON t.assignee_id = u.id AND t.status != 'done'
               WHERE u.role = 'member' GROUP BY u.id, u.name ORDER BY active_count, u.id"""
        ).fetchall()
        assignee = workloads[0] if workloads else None
        delay = max(3, abs(task["days_left"]) + 2 if task["days_left"] < 0 else 3)
        suggested_due = max(
            date.fromisoformat(task["start_date"]), date.today() + timedelta(days=delay)
        ).isoformat()
        context = f"タスク「{task['title']}」は進捗{task['progress']}%、期限{task['due_date']}です。担当候補は{assignee['name'] if assignee else '現担当'}、新期限候補は{suggested_due}です。理由を80文字以内で説明してください。"
        reason = gemini_generate(context) or "期限と現在の進捗、メンバーの担当件数をもとに、負荷の少ない担当者と現実的な期限を提案しました。"
    return jsonify(
        task_id=task_id,
        assignee_id=assignee["id"] if assignee else task["assignee_id"],
        assignee_name=assignee["name"] if assignee else task["assignee_name"],
        due_date=suggested_due,
        reason=reason,
    )


@app.post("/api/ai-replan/<int:task_id>/apply")
@require_roles("admin")
def apply_ai_replan(task_id: int):
    data = request.get_json(force=True)
    with db() as connection:
        task = connection.execute("SELECT start_date FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if task is None:
            return jsonify(error="タスクが見つかりません"), 404
        try:
            assignee_id = int(data["assignee_id"])
            due_date = date.fromisoformat(data["due_date"])
        except (KeyError, TypeError, ValueError):
            return jsonify(error="提案内容が正しくありません"), 400
        if due_date.isoformat() < task["start_date"]:
            return jsonify(error="期限は開始日以降にしてください"), 400
        if connection.execute("SELECT 1 FROM users WHERE id = ? AND role = 'member'", (assignee_id,)).fetchone() is None:
            return jsonify(error="担当者が見つかりません"), 404
        connection.execute(
            "UPDATE tasks SET assignee_id = ?, due_date = ?, updated_at = ? WHERE id = ?",
            (assignee_id, due_date.isoformat(), datetime.now().isoformat(timespec="seconds"), task_id),
        )
        connection.execute(
            "INSERT INTO comments(task_id, author, body) VALUES (?, ?, ?)",
            (task_id, current_user()["name"], f"AI再計画を承認：担当者と期限を{due_date.isoformat()}へ更新しました。"),
        )
        updated = dict(connection.execute(TASK_SELECT + " WHERE t.id = ?", (task_id,)).fetchone())
    return jsonify(updated)


@app.post("/api/ai-weekly-report")
@require_roles("admin")
def ai_weekly_report():
    with db() as connection:
        tasks = rows_to_dicts(connection.execute(TASK_SELECT + " ORDER BY t.due_date").fetchall())
        base = build_weekly_report(tasks)
        gemini_report = gemini_generate(f"次の週次レポートに短い改善コメントを追加してください。\n{base}")
        report = gemini_report or base
        connection.execute("INSERT INTO ai_logs(prompt, response) VALUES (?, ?)", ("weekly-report", report))
        add_notification(connection, current_user()["id"], "weekly", report, f"weekly:{date.today().isoformat()}")
    return jsonify(report=report, provider="gemini" if gemini_report else "local")


def scheduled_daily_check() -> None:
    with db() as connection:
        run_notification_checks(connection)


def scheduled_weekly_report() -> None:
    with db() as connection:
        tasks = rows_to_dicts(connection.execute(TASK_SELECT + " ORDER BY t.due_date").fetchall())
        base = build_weekly_report(tasks)
        report = gemini_generate(f"次の週次レポートに短い改善コメントを追加してください。\n{base}") or base
        connection.execute("INSERT INTO ai_logs(prompt, response) VALUES (?, ?)", ("weekly-report", report))
        add_notification(connection, 1, "weekly", report, f"weekly:{date.today().isoformat()}")


@app.get("/api/health")
def health():
    try:
        with db() as connection:
            connection.execute("SELECT 1").fetchone()
        database = "ok"
        status = "ok"
    except sqlite3.Error:
        database = "error"
        status = "degraded"
    return jsonify(
        status=status,
        service="TeamFlow",
        database=database,
        scheduler_enabled=os.environ.get("TEAMFLOW_ENABLE_SCHEDULER") == "1",
        gemini_configured=bool(os.environ.get("GEMINI_API_KEY")),
        gemini_model=os.environ.get("GEMINI_MODEL", "gemini-2.5-flash"),
        line_configured=bool(os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")),
    ), 200 if status == "ok" else 503


init_db()

if os.environ.get("TEAMFLOW_ENABLE_SCHEDULER") == "1" and BackgroundScheduler:
    scheduler = BackgroundScheduler(timezone="Asia/Tokyo")
    scheduler.add_job(scheduled_daily_check, "cron", hour=8, minute=5, id="daily-notifications", replace_existing=True)
    scheduler.add_job(scheduled_weekly_report, "cron", day_of_week="mon", hour=8, minute=0, id="weekly-report", replace_existing=True)
    scheduler.start()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")), debug=os.environ.get("FLASK_DEBUG") == "1")
