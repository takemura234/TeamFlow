from __future__ import annotations

import sys
from datetime import date

from server import TASK_SELECT, add_notification, build_weekly_report, db, gemini_generate, rows_to_dicts, run_notification_checks


def daily() -> None:
    with db() as connection:
        created = run_notification_checks(connection)
    print(f"created notifications: {created}")


def weekly() -> None:
    with db() as connection:
        tasks = rows_to_dicts(connection.execute(TASK_SELECT + " ORDER BY t.due_date").fetchall())
        base = build_weekly_report(tasks)
        report = gemini_generate(f"次の週次レポートに短い改善コメントを追加してください。\n{base}") or base
        connection.execute("INSERT INTO ai_logs(prompt, response) VALUES (?, ?)", ("weekly-report", report))
        add_notification(connection, 1, "weekly", report, f"weekly:{date.today().isoformat()}")
    print(report)


if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else "daily"
    {"daily": daily, "weekly": weekly}.get(command, daily)()
