import flet as ft
import flet.canvas as cv
import sqlite3
import calendar
import asyncio
import csv
import json
import shutil
import subprocess
import sys

from datetime import date, timedelta
from pathlib import Path


DB_PATH = Path(__file__).with_name("pulse.db")
SERIES_NAME_MAX_LENGTH = 50


def connect_database():
    connection = sqlite3.connect(DB_PATH)
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def initialize_database():
    with connect_database() as connection:
        # Series tablosu
        connection.execute(
"""
            CREATE TABLE IF NOT EXISTS series (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        series_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(series)").fetchall()
        }
        series_migrations = {
            "description": "TEXT NOT NULL DEFAULT ''",
            "goal": "TEXT NOT NULL DEFAULT ''",
            "schedule_type": "TEXT NOT NULL DEFAULT 'daily'",
            "schedule_days": "TEXT NOT NULL DEFAULT '0,1,2,3,4,5,6'",
            "weekly_target": "INTEGER NOT NULL DEFAULT 7",
            "archived": "INTEGER NOT NULL DEFAULT 0",
            "archived_at": "TEXT",
        }
        for column_name, definition in series_migrations.items():
            if column_name not in series_columns:
                connection.execute(
                    f"ALTER TABLE series ADD COLUMN {column_name} {definition}"
                )

        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS app_settings (
                setting_key TEXT PRIMARY KEY,
                setting_value TEXT NOT NULL
            )
            """
        )

        # Notlar pulse kaydından bağımsız tutulur. Böylece görev
        # yapılmayan REST / FLATLINE günlerine de not eklenebilir.
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS daily_notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                series_id INTEGER NOT NULL,
                note_date TEXT NOT NULL,
                note TEXT NOT NULL DEFAULT '',
                FOREIGN KEY (series_id)
                    REFERENCES series(id)
                    ON DELETE CASCADE,
                UNIQUE (series_id, note_date)
            )
            """
        )

        # Eski pulse_entries yapısını kontrol et
        columns = connection.execute(
            """
            PRAGMA table_info(pulse_entries)
            """
        ).fetchall()

        # pulse_entries hiç yoksa yeni yapıyı oluştur
        if not columns:
            connection.execute(
                """
                CREATE TABLE pulse_entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    series_id INTEGER NOT NULL,
                    pulse_date TEXT NOT NULL,
                    note TEXT NOT NULL DEFAULT '',
                    FOREIGN KEY (series_id)
                        REFERENCES series(id)
                        ON DELETE CASCADE,
                    UNIQUE (series_id, pulse_date)
                )
                """
            )

            connection.execute(
                """
                INSERT OR IGNORE INTO series (name)
                VALUES ('Daily Development')
                """
            )

            return

        column_names = [column[1] for column in columns]

        # Eski veritabanını yeni multi-series yapısına migrate et
        if "series_id" not in column_names:
            connection.execute(
                """
                INSERT OR IGNORE INTO series (name)
                VALUES ('Daily Development')
                """
            )

            default_series_id = connection.execute(
                """
                SELECT id
                FROM series
                WHERE name = 'Daily Development'
                """
            ).fetchone()[0]

            connection.execute(
                """
                ALTER TABLE pulse_entries
                RENAME TO pulse_entries_legacy
                """
            )

            connection.execute(
                """
                CREATE TABLE pulse_entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    series_id INTEGER NOT NULL,
                    pulse_date TEXT NOT NULL,
                    note TEXT NOT NULL DEFAULT '',
                    FOREIGN KEY (series_id)
                        REFERENCES series(id)
                        ON DELETE CASCADE,
                    UNIQUE (series_id, pulse_date)
                )
                """
            )

            connection.execute(
                """
                INSERT INTO pulse_entries (
                    series_id,
                    pulse_date,
                    note
                )
                SELECT ?, pulse_date, note
                FROM pulse_entries_legacy
                """,
                (default_series_id,),
            )

            connection.execute(
                """
                DROP TABLE pulse_entries_legacy
                """
            )

        # Önceki sürümlerde pulse_entries içinde saklanan notları
        # yeni bağımsız not tablosuna kayıpsız ve idempotent taşı.
        connection.execute(
            """
            INSERT INTO daily_notes (series_id, note_date, note)
            SELECT series_id, pulse_date, note
            FROM pulse_entries
            WHERE note <> ''
            ON CONFLICT(series_id, note_date) DO NOTHING
            """
        )


def get_series(include_archived=False):
    with connect_database() as connection:
        return connection.execute(
            f"""
            SELECT id, name
            FROM series
            {'' if include_archived else 'WHERE archived = 0'}
            ORDER BY id
            """
        ).fetchall()


def get_series_details(series_id):
    with connect_database() as connection:
        row = connection.execute(
            """
            SELECT id, name, description, goal, schedule_type,
                   schedule_days, weekly_target, archived, created_at
            FROM series WHERE id = ?
            """,
            (series_id,),
        ).fetchone()
    return row


def update_series_profile(
    series_id, description, goal, schedule_type, schedule_days, weekly_target
):
    with connect_database() as connection:
        connection.execute(
            """
            UPDATE series
            SET description = ?, goal = ?, schedule_type = ?,
                schedule_days = ?, weekly_target = ?
            WHERE id = ?
            """,
            (
                description, goal, schedule_type,
                schedule_days, weekly_target, series_id,
            ),
        )


def set_series_archived(series_id, archived):
    with connect_database() as connection:
        connection.execute(
            """
            UPDATE series
            SET archived = ?,
                archived_at = CASE WHEN ? = 1 THEN CURRENT_TIMESTAMP ELSE NULL END
            WHERE id = ?
            """,
            (int(archived), int(archived), series_id),
        )


def create_backup():
    backup_dir = DB_PATH.with_name("backups")
    backup_dir.mkdir(exist_ok=True)
    stamp = __import__("datetime").datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup_path = backup_dir / f"pulse-backup-{stamp}.db"
    with connect_database() as source, sqlite3.connect(backup_path) as target:
        source.backup(target)
    return backup_path


def get_latest_backup():
    backup_dir = DB_PATH.with_name("backups")
    backups = sorted(backup_dir.glob("pulse-backup-*.db"), reverse=True)
    return backups[0] if backups else None


def restore_latest_backup():
    latest = get_latest_backup()
    if latest is None:
        return None
    safety_copy = create_backup()
    shutil.copy2(latest, DB_PATH)
    initialize_database()
    return latest, safety_copy


def export_series_data(series_id):
    details = get_series_details(series_id)
    export_dir = DB_PATH.with_name("exports")
    export_dir.mkdir(exist_ok=True)
    safe_name = "".join(
        character if character.isalnum() or character in "-_" else "_"
        for character in details[1]
    )[:40] or "series"
    pulse_dates = set(get_pulse_dates(series_id))
    notes = get_daily_notes(series_id)
    first_day = min(pulse_dates | set(notes)) if pulse_dates or notes else date.today()
    rows = []
    for offset in range((date.today() - first_day).days + 1):
        day_value = first_day + timedelta(days=offset)
        rows.append((day_value.isoformat(), int(day_value in pulse_dates), notes.get(day_value, "")))

    csv_path = export_dir / f"{safe_name}.csv"
    md_path = export_dir / f"{safe_name}.md"
    with csv_path.open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.writer(stream)
        writer.writerow(["Date", "Pulse", "Note"])
        writer.writerows(rows)
    with md_path.open("w", encoding="utf-8") as stream:
        stream.write(f"# {details[1]}\n\n")
        for day_text, has_pulse, note in reversed(rows):
            if note or has_pulse:
                stream.write(
                    f"## {day_text} — {'PULSE' if has_pulse else 'NO PULSE'}\n\n"
                    f"{note or 'No note recorded.'}\n\n"
                )
    return csv_path, md_path


def _safe_export_name(value):
    return "".join(
        character if character.isalnum() or character in "-_" else "_"
        for character in value
    )[:50] or "the-pulse"


def create_portable_export(series_id=None):
    """Create a versioned, shareable export that can be imported later."""
    export_dir = DB_PATH.with_name("exports")
    export_dir.mkdir(exist_ok=True)
    series_rows = []
    with connect_database() as connection:
        if series_id is None:
            rows = connection.execute(
                """
                SELECT id, name, description, goal, schedule_type,
                       schedule_days, weekly_target, archived, created_at
                FROM series ORDER BY id
                """
            ).fetchall()
        else:
            rows = connection.execute(
                """
                SELECT id, name, description, goal, schedule_type,
                       schedule_days, weekly_target, archived, created_at
                FROM series WHERE id = ?
                """,
                (series_id,),
            ).fetchall()

        for row in rows:
            pulses = [
                pulse_date
                for (pulse_date,) in connection.execute(
                    "SELECT pulse_date FROM pulse_entries WHERE series_id = ? "
                    "ORDER BY pulse_date",
                    (row[0],),
                ).fetchall()
            ]
            notes = [
                {"date": note_date, "note": note}
                for note_date, note in connection.execute(
                    "SELECT note_date, note FROM daily_notes WHERE series_id = ? "
                    "ORDER BY note_date",
                    (row[0],),
                ).fetchall()
            ]
            series_rows.append(
                {
                    "name": row[1],
                    "description": row[2] or "",
                    "goal": row[3] or "",
                    "schedule": {
                        "type": row[4],
                        "days": row[5],
                        "weekly_target": row[6],
                    },
                    "archived": bool(row[7]),
                    "created_at": row[8],
                    "pulses": pulses,
                    "notes": notes,
                }
            )

    stamp = __import__("datetime").datetime.now().strftime("%Y%m%d-%H%M%S")
    label = series_rows[0]["name"] if len(series_rows) == 1 else "all-series"
    export_path = export_dir / f"{_safe_export_name(label)}-{stamp}.pulse.json"
    payload = {
        "format": "the-pulse-portable-data",
        "schema_version": 1,
        "exported_at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
        "series": series_rows,
    }
    export_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return export_path


def _unique_series_name(connection, requested_name):
    base_name = (requested_name or "Imported series").strip()[:SERIES_NAME_MAX_LENGTH]
    candidate = base_name
    counter = 2
    while connection.execute(
        "SELECT 1 FROM series WHERE name = ?", (candidate,)
    ).fetchone():
        suffix = f" (Imported {counter})"
        candidate = base_name[:SERIES_NAME_MAX_LENGTH - len(suffix)] + suffix
        counter += 1
    return candidate


def import_portable_export(file_path):
    source_path = Path(file_path).expanduser()
    if not source_path.is_file():
        raise ValueError("The selected file could not be found.")
    try:
        payload = json.loads(source_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("This is not a readable The Pulse export file.") from error
    if (
        payload.get("format") != "the-pulse-portable-data"
        or payload.get("schema_version") != 1
        or not isinstance(payload.get("series"), list)
    ):
        raise ValueError("This file is not a supported The Pulse export.")

    imported_names = []
    with connect_database() as connection:
        for item in payload["series"]:
            if not isinstance(item, dict):
                raise ValueError("The export contains invalid series data.")
            schedule = item.get("schedule") or {}
            imported_name = _unique_series_name(connection, item.get("name"))
            cursor = connection.execute(
                """
                INSERT INTO series (
                    name, description, goal, schedule_type, schedule_days,
                    weekly_target, archived
                ) VALUES (?, ?, ?, ?, ?, ?, 0)
                """,
                (
                    imported_name,
                    str(item.get("description") or ""),
                    str(item.get("goal") or ""),
                    schedule.get("type") or "daily",
                    schedule.get("days") or "0,1,2,3,4,5,6",
                    int(schedule.get("weekly_target") or 7),
                ),
            )
            new_series_id = cursor.lastrowid
            for pulse_date in item.get("pulses") or []:
                date.fromisoformat(str(pulse_date))
                connection.execute(
                    "INSERT OR IGNORE INTO pulse_entries "
                    "(series_id, pulse_date, note) VALUES (?, ?, '')",
                    (new_series_id, str(pulse_date)),
                )
            for note_item in item.get("notes") or []:
                note_date = str(note_item.get("date"))
                date.fromisoformat(note_date)
                connection.execute(
                    """
                    INSERT INTO daily_notes (series_id, note_date, note)
                    VALUES (?, ?, ?)
                    ON CONFLICT(series_id, note_date) DO UPDATE SET
                        note = excluded.note
                    """,
                    (new_series_id, note_date, str(note_item.get("note") or "")),
                )
            imported_names.append(imported_name)
    return imported_names


def open_exports_folder():
    export_dir = DB_PATH.with_name("exports")
    export_dir.mkdir(exist_ok=True)
    try:
        if sys.platform == "win32":
            __import__("os").startfile(export_dir)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(export_dir)])
        else:
            subprocess.Popen(["xdg-open", str(export_dir)])
        return True
    except (OSError, AttributeError):
        return False


def send_native_notification(title, message):
    if sys.platform != "win32":
        return False
    try:
        from winotify import Notification
        Notification(app_id="The Pulse", title=title, msg=message).show()
        return True
    except (ImportError, OSError):
        return False


def create_series(name):
    if len(name) > SERIES_NAME_MAX_LENGTH:
        raise ValueError("Series name is too long.")

    with connect_database() as connection:
        cursor = connection.execute(
            """
            INSERT INTO series (name)
            VALUES (?)
            """,
            (name,),
        )

        return cursor.lastrowid


def rename_series(series_id, name):
    if len(name) > SERIES_NAME_MAX_LENGTH:
        raise ValueError("Series name is too long.")

    with connect_database() as connection:
        connection.execute(
            """
            UPDATE series
            SET name = ?
            WHERE id = ?
            """,
            (name, series_id),
        )


def delete_series(series_id):
    with connect_database() as connection:
        connection.execute(
            """
            DELETE FROM series
            WHERE id = ?
            """,
            (series_id,),
        )


def get_setting(setting_key):
    with connect_database() as connection:
        row = connection.execute(
            """
            SELECT setting_value
            FROM app_settings
            WHERE setting_key = ?
            """,
            (setting_key,),
        ).fetchone()

    return row[0] if row else None


def save_setting(setting_key, setting_value):
    with connect_database() as connection:
        connection.execute(
            """
            INSERT INTO app_settings (
                setting_key,
                setting_value
            )
            VALUES (?, ?)
            ON CONFLICT(setting_key) DO UPDATE SET
                setting_value = excluded.setting_value
            """,
            (setting_key, str(setting_value)),
        )


def get_pulse_dates(series_id):
    with connect_database() as connection:
        rows = connection.execute(
            """
            SELECT pulse_date
            FROM pulse_entries
            WHERE series_id = ?
            ORDER BY pulse_date
            """,
            (series_id,),
        ).fetchall()

    return [date.fromisoformat(row[0]) for row in rows]


def get_pulse_entries(series_id):
    with connect_database() as connection:
        rows = connection.execute(
            """
            SELECT pulse_date, note
            FROM pulse_entries
            WHERE series_id = ?
            ORDER BY pulse_date
            """,
            (series_id,),
        ).fetchall()

    return [
        (date.fromisoformat(pulse_date), note or "")
        for pulse_date, note in rows
    ]


def get_daily_notes(series_id):
    with connect_database() as connection:
        rows = connection.execute(
            """
            SELECT note_date, note
            FROM daily_notes
            WHERE series_id = ?
            ORDER BY note_date
            """,
            (series_id,),
        ).fetchall()

    return {
        date.fromisoformat(note_date): note or ""
        for note_date, note in rows
    }


def get_today_note(series_id):
    note = get_note_for_date(series_id, date.today())
    return note or ""


def get_note_for_date(series_id, pulse_date):
    with connect_database() as connection:
        row = connection.execute(
            """
            SELECT note
            FROM daily_notes
            WHERE series_id = ?
              AND note_date = ?
            """,
            (series_id, pulse_date.isoformat()),
        ).fetchone()

    return row[0] if row else None


def update_pulse_note(series_id, pulse_date, note):
    with connect_database() as connection:
        connection.execute(
            """
            INSERT INTO daily_notes (series_id, note_date, note)
            VALUES (?, ?, ?)
            ON CONFLICT(series_id, note_date) DO UPDATE SET
                note = excluded.note
            """,
            (series_id, pulse_date.isoformat(), note),
        )


def pulse_exists_today(series_id):
    today = date.today().isoformat()

    with connect_database() as connection:
        row = connection.execute(
            """
            SELECT 1
            FROM pulse_entries
            WHERE series_id = ?
              AND pulse_date = ?
            """,
            (series_id, today),
        ).fetchone()

    return row is not None


def undo_today_pulse(series_id, keep_note=True):
    with connect_database() as connection:
        if not keep_note:
            connection.execute(
                "DELETE FROM daily_notes WHERE series_id = ? AND note_date = ?",
                (series_id, date.today().isoformat()),
            )
        connection.execute(
            "DELETE FROM pulse_entries WHERE series_id = ? AND pulse_date = ?",
            (series_id, date.today().isoformat()),
        )


def is_scheduled_day(series_id, day_value, pulse_dates=None):
    details = get_series_details(series_id)
    if not details:
        return True
    schedule_type = details[4]
    if schedule_type == "daily":
        return True
    if schedule_type == "weekdays":
        selected = {int(value) for value in (details[5] or "").split(",") if value}
        return day_value.weekday() in selected
    target = max(1, min(7, int(details[6] or 1)))
    pulse_dates = pulse_dates if pulse_dates is not None else get_pulse_dates(series_id)
    week_start = day_value - timedelta(days=day_value.weekday())
    pulses_through_day = sum(
        week_start <= pulse_day <= day_value
        for pulse_day in pulse_dates
    )
    return pulses_through_day < target or day_value in set(pulse_dates)


def scheduled_misses_between(series_id, start_day, end_day, pulse_dates):
    if end_day <= start_day:
        return 0
    pulse_set = set(pulse_dates)
    return sum(
        1
        for offset in range(1, (end_day - start_day).days)
        if (
            (candidate := start_day + timedelta(days=offset)) not in pulse_set
            and is_scheduled_day(series_id, candidate, pulse_dates)
        )
    )


def calculate_stats(pulse_dates, series_id=None):
    if not pulse_dates:
        return 0, 0, 0

    total_pulse = len(pulse_dates)

    longest_pulse = 1
    active_run = 1

    for previous_date, current_date in zip(
        pulse_dates,
        pulse_dates[1:],
    ):
        gap = (current_date - previous_date).days
        missed_required = (
            scheduled_misses_between(
                series_id, previous_date, current_date, pulse_dates
            )
            if series_id is not None
            else max(0, gap - 1)
        )

        # 1 REST günü pulse'ı bozmaz.
        if missed_required <= 1:
            active_run += 1
        else:
            active_run = 1

        longest_pulse = max(longest_pulse, active_run)

    days_since_last_pulse = (date.today() - pulse_dates[-1]).days
    missed_required = (
        scheduled_misses_between(
            series_id, pulse_dates[-1], date.today(), pulse_dates
        )
        if series_id is not None
        else max(0, days_since_last_pulse - 1)
    )

    # İki veya daha fazla ardışık REST -> FLATLINE
    if missed_required <= 1:
        current_pulse = active_run
    else:
        current_pulse = 0

    return current_pulse, longest_pulse, total_pulse

def get_pulse_status(pulse_dates, series_id=None):
    if not pulse_dates:
        return "NO PULSE", "This series has not received a pulse yet."

    today = date.today()
    last_pulse_date = pulse_dates[-1]

    if last_pulse_date == today:
        return "ALIVE", "Today's pulse is alive."

    if series_id is not None and not is_scheduled_day(series_id, today, pulse_dates):
        return "ALIVE", "No pulse is scheduled today."

    # Bugün henüz bitmediği için yalnızca tamamen kaçırılmış
    # günleri sayıyoruz.
    missed_days = (
        scheduled_misses_between(
            series_id, last_pulse_date, today, pulse_dates
        )
        if series_id is not None
        else max(0, (today - last_pulse_date).days - 1)
    )

    if missed_days == 0:
        return "ALIVE", "Waiting for today's pulse."

    if missed_days == 1:
        return "REST", "One rest day. The pulse is still alive."

    return (
        "FLATLINE",
        f"{missed_days} days without a pulse.",
    )

def main(page: ft.Page):
    initialize_database()

    page.title = "The Pulse"
    page.theme_mode = ft.ThemeMode.DARK
    page.bgcolor = "#090B10"
    page.padding = 24
    page.scroll = ft.ScrollMode.AUTO
    page.horizontal_alignment = ft.CrossAxisAlignment.CENTER
    page.vertical_alignment = ft.MainAxisAlignment.START
    page.window.width = 900
    page.window.height = 900
    page.window.min_width = 760
    page.window.min_height = 650

    current_text = ft.Text(
        size=30,
        weight=ft.FontWeight.BOLD,
    )
    longest_text = ft.Text(
        size=30,
        weight=ft.FontWeight.BOLD,
        color="#FFC857",
    )
    total_text = ft.Text(
        size=30,
        weight=ft.FontWeight.BOLD,
        color="#56C8FF",
    )
    status_text = ft.Text("", color="#FF5D73")

    pulse_heart = ft.Text(
        "♥",
        size=72,
        weight=ft.FontWeight.BOLD,
    )

    pulse_state_text = ft.Text(
        size=22,
        weight=ft.FontWeight.BOLD,
    )

    pulse_state_detail = ft.Text(
        size=14,
        color="#A9B0BF",
        text_align=ft.TextAlign.CENTER,
    )

    state_panel = ft.Container(
        content=ft.Column(
            controls=[
                pulse_heart,
                pulse_state_text,
                pulse_state_detail,
            ],
            spacing=8,
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
        ),
        width=360,
        padding=20,
        bgcolor="#15171B",
        border=ft.Border.all(1, "#252B38"),
        border_radius=20,
    )

    series_title = ft.Text(
        size=22,
        weight=ft.FontWeight.BOLD,
        color="#F4F6FA",
    )

    note_field = ft.TextField(
        hint_text="What did you accomplish today?",
        multiline=True,
        min_lines=3,
        max_lines=5,
        width=520,
        border_color="#343A48",
        focused_border_color="#FF3158",
    )

    note_title = ft.Text(
        "TODAY'S NOTE",
        size=13,
        color="#8D95A5",
        weight=ft.FontWeight.BOLD,
    )

    edit_note_button = ft.Button(
        content="EDIT NOTE",
        visible=False,
    )

    save_note_button = ft.Button(
        content="SAVE NOTE",
        visible=False,
    )

    undo_pulse_button = ft.Button(
        content="UNDO PULSE",
        visible=False,
    )

    record_button = ft.Button(
        content="♥ RECORD PULSE",
        width=240,
        height=48,
    )

    series_dropdown = ft.Dropdown(
        width=480,
        label="Series",
        border_color="#343A48",
        focused_border_color="#FF3158",
    )

    new_series_field = ft.TextField(
        label="Series name",
        hint_text="Reading",
        max_length=SERIES_NAME_MAX_LENGTH,
    )

    new_series_error = ft.Text("")

    rename_series_field = ft.TextField(
        label="New series name",
        max_length=SERIES_NAME_MAX_LENGTH,
    )
    rename_series_error = ft.Text("")
    delete_series_message = ft.Text("")

    def stat_card(label, value_control, accent_color):
        return ft.Container(
            content=ft.Column(
                controls=[
                    ft.Container(
                        width=28,
                        height=3,
                        bgcolor=accent_color,
                        border_radius=2,
                    ),
                    ft.Text(
                        label,
                        size=12,
                        color="#8D95A5",
                        weight=ft.FontWeight.BOLD,
                    ),
                    value_control,
                ],
                spacing=7,
                horizontal_alignment=(
                    ft.CrossAxisAlignment.CENTER
                ),
            ),
            width=170,
            padding=20,
            bgcolor="#151923",
            border=ft.Border.all(1, "#252B38"),
            border_radius=16,
        )

    stats_row = ft.Row(
        controls=[
            stat_card("CURRENT", current_text, "#FF3158"),
            stat_card("LONGEST", longest_text, "#FFC857"),
            stat_card("TOTAL", total_text, "#56C8FF"),
        ],
        alignment=ft.MainAxisAlignment.CENTER,
        spacing=14,
        wrap=True,
    )

    history_title = ft.Text(
        "LAST 14 DAYS",
        size=13,
        color="#8D95A5",
        weight=ft.FontWeight.BOLD,
    )

    history_hint = ft.Text(
        "Click a day to view progress",
        size=11,
        color="#72798A",
    )

    history_canvas = cv.Canvas(
        width=700,
        height=112,
    )

    history_labels = ft.Row(
        width=700,
        spacing=0,
        alignment=ft.MainAxisAlignment.CENTER,
    )

    history_monitor = ft.Container(
        content=ft.Column(
            controls=[
                history_canvas,
                history_labels,
            ],
            spacing=2,
        ),
        width=700,
        padding=0,
        bgcolor="#0B0E14",
        border=ft.Border.all(1, "#252B38"),
        border_radius=14,
        clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
    )

    history_detail_series = ft.Text(
        size=14,
        color="#8D95A5",
        weight=ft.FontWeight.BOLD,
    )
    history_detail_date = ft.Text(
        size=18,
        weight=ft.FontWeight.BOLD,
    )
    history_detail_state = ft.Text(
        size=15,
        weight=ft.FontWeight.BOLD,
    )
    history_detail_note = ft.Text(
        size=14,
        color="#A9B0BF",
    )

    history_detail_dialog = ft.AlertDialog(
        modal=True,
        title=ft.Text("Daily progress"),
        content=ft.Column(
            controls=[
                history_detail_series,
                history_detail_date,
                history_detail_state,
                ft.Divider(),
                ft.Text(
                    "NOTE",
                    size=11,
                    color="#8D95A5",
                    weight=ft.FontWeight.BOLD,
                ),
                history_detail_note,
            ],
            width=360,
            tight=True,
            spacing=10,
        ),
        actions=[
            ft.Button(
                content="Close",
                on_click=lambda e: page.pop_dialog(),
            ),
        ],
    )

    calendar_state = {
        "month": date.today().replace(day=1),
        "selected_day": date.today(),
    }

    calendar_month_title = ft.Text(
        size=20,
        weight=ft.FontWeight.BOLD,
        color="#F4F6FA",
    )
    calendar_month_picker = ft.Dropdown(
        width=145,
        value=str(date.today().month),
        options=[
            ft.DropdownOption(key=str(month), text=calendar.month_name[month])
            for month in range(1, 13)
        ],
    )
    calendar_year_picker = ft.Dropdown(
        width=105,
        value=str(date.today().year),
        options=[
            ft.DropdownOption(key=str(year), text=str(year))
            for year in range(date.today().year - 10, date.today().year + 11)
        ],
    )
    calendar_grid = ft.Column(spacing=0)
    selected_day_title = ft.Text(
        size=19,
        weight=ft.FontWeight.BOLD,
        color="#F4F6FA",
    )
    selected_day_state = ft.Text(
        size=13,
        weight=ft.FontWeight.BOLD,
    )
    selected_day_note = ft.TextField(
        label="NOTE",
        multiline=True,
        min_lines=2,
        max_lines=6,
        width=660,
        border_color="#343A48",
        focused_border_color="#FF3158",
    )
    selected_day_message = ft.Text(
        size=12,
        color="#8D95A5",
    )
    save_history_note_button = ft.Button(
        content="SAVE NOTE",
    )

    def get_history_day_state(selected_date, pulse_dates):
        if selected_date > date.today():
            return "FUTURE", "#343A48"

        if not pulse_dates:
            if selected_date == date.today():
                return "NO PULSE", "#72798A"
            return "NOT STARTED", "#4B5261"

        if selected_date < pulse_dates[0]:
            return "NOT STARTED", "#4B5261"

        pulse_date_set = set(pulse_dates)
        previous_pulses = [
            pulse_day
            for pulse_day in pulse_dates
            if pulse_day < selected_date
        ]
        previous_pulse = (
            previous_pulses[-1]
            if previous_pulses
            else None
        )

        if selected_date in pulse_date_set:
            if (
                previous_pulse is not None
                and scheduled_misses_between(
                    selected_series_id(),
                    previous_pulse,
                    selected_date,
                    pulse_dates,
                ) >= 2
            ):
                return "REVIVE", "#35D07F"

            return "PULSE RECORDED", "#FF3158"

        if (
            previous_pulse is not None
            and not is_scheduled_day(
                selected_series_id(), selected_date, pulse_dates
            )
        ):
            return "OFF DAY", "#56C8FF"

        if selected_date == date.today():
            return "ALIVE — WAITING TODAY", "#FF3158"

        if previous_pulse is None:
            return "NOT STARTED", "#4B5261"

        missed_required = scheduled_misses_between(
            selected_series_id(),
            previous_pulse,
            selected_date + timedelta(days=1),
            pulse_dates,
        )

        if missed_required == 1:
            return "REST", "#F5A623"

        return "FLATLINE", "#72798A"

    def history_line_for_day(day_value, pulse_dates, cell_width=96):
        pulse_set = set(pulse_dates)
        baseline = 48
        path_elements = [cv.Path.MoveTo(x=0, y=baseline)]

        if day_value in pulse_set:
            points = [
                (18, baseline),
                (28, baseline),
                (34, baseline - 9),
                (40, baseline + 12),
                (47, baseline - 30),
                (54, baseline + 22),
                (64, baseline),
                (cell_width, baseline),
            ]
        else:
            points = [(cell_width, baseline)]

        for x, y in points:
            path_elements.append(cv.Path.LineTo(x=x, y=y))

        day_state, day_color = get_history_day_state(
            day_value,
            pulse_dates,
        )
        if day_value > date.today():
            day_color = "#252B38"

        return cv.Path(
            elements=path_elements,
            paint=ft.Paint(
                color=day_color,
                stroke_width=2.5,
                style=ft.PaintingStyle.STROKE,
                stroke_cap=ft.StrokeCap.ROUND,
                stroke_join=ft.StrokeJoin.ROUND,
            ),
        )

    def select_calendar_day(selected_date, update_page=True):
        calendar_state["selected_day"] = selected_date
        pulse_dates = get_pulse_dates(selected_series_id())
        state_name, state_color = get_history_day_state(
            selected_date,
            pulse_dates,
        )
        note = get_note_for_date(
            selected_series_id(),
            selected_date,
        )

        selected_day_title.value = selected_date.strftime(
            "%A, %d %B %Y"
        )
        selected_day_state.value = state_name
        selected_day_state.color = state_color
        selected_day_note.value = note or ""
        first_pulse_date = pulse_dates[0] if pulse_dates else None
        selected_day_note.read_only = (
            selected_date > date.today()
            or first_pulse_date is None
            or selected_date < first_pulse_date
        )
        save_history_note_button.visible = not selected_day_note.read_only

        if selected_date > date.today():
            selected_day_message.value = (
                "Future days cannot be edited."
            )
        elif first_pulse_date is None or selected_date < first_pulse_date:
            selected_day_message.value = (
                "Notes begin with this series' first pulse."
            )
        elif selected_date not in set(pulse_dates):
            selected_day_message.value = (
                "No pulse was recorded. You can still add a note."
            )
        else:
            selected_day_message.value = (
                "Edit the note and save your changes."
            )

        build_calendar()
        if update_page:
            page.update()

    def pick_calendar_day(selected_date):
        shown_month = calendar_state["month"]
        if (
            selected_date.year != shown_month.year
            or selected_date.month != shown_month.month
        ):
            calendar_state["month"] = selected_date.replace(day=1)
        select_calendar_day(selected_date)

    def move_selected_day(offset):
        picked_day = calendar_state["selected_day"] + timedelta(days=offset)
        pick_calendar_day(picked_day)

    def build_calendar():
        shown_month = calendar_state["month"]
        selected_date = calendar_state["selected_day"]
        pulse_dates = get_pulse_dates(selected_series_id())
        notes_by_date = get_daily_notes(selected_series_id())
        calendar_month_title.value = shown_month.strftime("%B %Y").upper()

        first_weekday = 6 if get_setting("week_start") == "sunday" else 0
        month_calendar = calendar.Calendar(firstweekday=first_weekday)
        weekday_names = (
            ["SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT"]
            if first_weekday == 6
            else ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]
        )
        weekday_header.controls = [
            ft.Container(
                content=ft.Text(
                    weekday,
                    size=10,
                    color="#8D95A5",
                    weight=ft.FontWeight.BOLD,
                    text_align=ft.TextAlign.CENTER,
                ),
                width=96,
                alignment=ft.Alignment.CENTER,
            )
            for weekday in weekday_names
        ]
        weeks = month_calendar.monthdatescalendar(
            shown_month.year,
            shown_month.month,
        )
        while len(weeks) < 6:
            last_day = weeks[-1][-1]
            weeks.append([
                last_day + timedelta(days=offset)
                for offset in range(1, 8)
            ])

        rows = []
        for week in weeks:
            day_cells = []
            for day_value in week:
                in_month = day_value.month == shown_month.month
                is_today = day_value == date.today()
                is_selected = day_value == selected_date
                day_state, day_color = get_history_day_state(
                    day_value,
                    pulse_dates,
                )
                cell_state_labels = {
                    "PULSE RECORDED": "PULSE",
                    "ALIVE — WAITING TODAY": "WAITING",
                    "NOT STARTED": "NOT STARTED",
                    "NO PULSE": "NO PULSE",
                    "REVIVE": "REVIVE",
                    "REST": "REST",
                    "FLATLINE": "FLATLINE",
                    "FUTURE": "FUTURE",
                    "OFF DAY": "OFF DAY",
                }
                cell_state = cell_state_labels.get(day_state, day_state)

                cell_canvas = cv.Canvas(
                    shapes=[history_line_for_day(day_value, pulse_dates)],
                    width=96,
                    height=68,
                )
                day_cells.append(
                    ft.Container(
                        content=ft.Stack(
                            controls=[
                                cell_canvas,
                                ft.Container(
                                    content=ft.Text(
                                        str(day_value.day),
                                        size=12,
                                        weight=(
                                            ft.FontWeight.BOLD
                                            if is_today or is_selected
                                            else ft.FontWeight.NORMAL
                                        ),
                                        color=(
                                            "#F4F6FA"
                                            if in_month
                                            else "#555D6D"
                                        ),
                                    ),
                                    left=8,
                                    top=6,
                                ),
                                ft.Container(
                                    content=ft.Text(
                                        cell_state,
                                        size=8,
                                        color=day_color,
                                        weight=ft.FontWeight.BOLD,
                                    ),
                                    left=8,
                                    bottom=4,
                                ),
                                ft.Icon(
                                    ft.Icons.NOTE_ALT_OUTLINED,
                                    size=11,
                                    color="#56C8FF",
                                    right=6,
                                    top=6,
                                    visible=bool(notes_by_date.get(day_value, "").strip()),
                                ),
                            ]
                        ),
                        width=96,
                        height=78,
                        bgcolor=(
                            "#171D28"
                            if is_selected
                            else "#0B0E14"
                        ),
                        border=ft.Border.all(
                            2 if is_selected else 1,
                            "#F4F6FA" if is_selected else "#1B2230",
                        ),
                        tooltip=day_value.isoformat(),
                        ink=True,
                        on_click=(
                            lambda e, picked_day=day_value:
                            pick_calendar_day(picked_day)
                        ),
                    )
                )
            rows.append(ft.Row(controls=day_cells, spacing=0))

        calendar_grid.controls = rows

    def change_calendar_month(offset):
        current_month = calendar_state["month"]
        month_index = current_month.year * 12 + current_month.month - 1 + offset
        new_month = date(
            month_index // 12,
            month_index % 12 + 1,
            1,
        )
        calendar_state["month"] = new_month
        calendar_month_picker.value = str(new_month.month)
        calendar_year_picker.value = str(new_month.year)
        select_calendar_day(new_month, update_page=False)
        page.update()

    def jump_calendar_date(e):
        target = date(
            int(calendar_year_picker.value),
            int(calendar_month_picker.value),
            1,
        )
        calendar_state["month"] = target
        select_calendar_day(target)

    def return_calendar_to_today(e):
        calendar_state["month"] = date.today().replace(day=1)
        calendar_month_picker.value = str(date.today().month)
        calendar_year_picker.value = str(date.today().year)
        select_calendar_day(date.today())

    def save_history_note(e):
        selected_date = calendar_state["selected_day"]
        update_pulse_note(
            selected_series_id(),
            selected_date,
            selected_day_note.value or "",
        )
        selected_day_message.value = "Note updated."
        if selected_date == date.today():
            note_field.value = selected_day_note.value or ""
        build_notes()
        build_dashboard()
        page.update()

    def open_history_day(selected_date):
        series_id = selected_series_id()
        pulse_dates = get_pulse_dates(series_id)
        day_state, state_color = get_history_day_state(
            selected_date,
            pulse_dates,
        )
        note = get_note_for_date(series_id, selected_date)

        history_detail_series.value = series_title.value
        history_detail_date.value = selected_date.strftime(
            "%A, %d %B %Y"
        )
        history_detail_state.value = day_state
        history_detail_state.color = state_color

        if note is None:
            history_detail_note.value = (
                "No pulse or note was recorded on this day."
            )
        elif note.strip():
            history_detail_note.value = note
        else:
            history_detail_note.value = "No note recorded."

        page.show_dialog(history_detail_dialog)

    def build_history(pulse_dates, line_color):
        pulse_date_set = set(pulse_dates)
        today = date.today()
        first_day = today - timedelta(days=13)
        slot_width = 50
        baseline_y = 58
        shapes = []
        labels = []

        grid_paint = ft.Paint(
            color="#1B2230",
            stroke_width=1,
            style=ft.PaintingStyle.STROKE,
        )

        for separator in range(1, 14):
            x = separator * slot_width
            shapes.append(
                cv.Line(
                    x1=x,
                    y1=0,
                    x2=x,
                    y2=112,
                    paint=grid_paint,
                )
            )

        for offset in range(14):
            current_day = first_day + timedelta(days=offset)
            has_pulse = current_day in pulse_date_set
            is_today = current_day == today
            start_x = offset * slot_width
            end_x = start_x + slot_width

            path_elements = [
                cv.Path.MoveTo(x=start_x, y=baseline_y)
            ]

            if has_pulse:
                points = [
                    (start_x + 8, baseline_y),
                    (start_x + 14, baseline_y),
                    (start_x + 18, baseline_y - 10),
                    (start_x + 22, baseline_y + 13),
                    (start_x + 27, baseline_y - 36),
                    (start_x + 32, baseline_y + 27),
                    (start_x + 38, baseline_y),
                    (end_x, baseline_y),
                ]
            else:
                points = [(end_x, baseline_y)]

            for x, y in points:
                path_elements.append(
                    cv.Path.LineTo(x=x, y=y)
                )

            shapes.append(
                cv.Path(
                    elements=path_elements,
                    paint=ft.Paint(
                        color=line_color,
                        stroke_width=3,
                        style=ft.PaintingStyle.STROKE,
                        stroke_cap=ft.StrokeCap.ROUND,
                        stroke_join=ft.StrokeJoin.ROUND,
                    ),
                )
            )

            if is_today:
                shapes.append(
                    cv.Rect(
                        x=start_x + 1,
                        y=1,
                        width=slot_width - 2,
                        height=110,
                        paint=ft.Paint(
                            color="#72798A",
                            stroke_width=1,
                            style=ft.PaintingStyle.STROKE,
                        ),
                    )
                )

            labels.append(
                ft.Container(
                    content=ft.Column(
                        controls=[
                            ft.Text(
                                current_day.strftime("%a").upper(),
                                size=8,
                                color="#72798A",
                            ),
                            ft.Text(
                                str(current_day.day),
                                size=10,
                                color=(
                                    "#F4F6FA"
                                    if is_today
                                    else "#8D95A5"
                                ),
                                weight=(
                                    ft.FontWeight.BOLD
                                    if is_today
                                    else ft.FontWeight.NORMAL
                                ),
                            ),
                        ],
                        spacing=0,
                        horizontal_alignment=(
                            ft.CrossAxisAlignment.CENTER
                        ),
                    ),
                    width=slot_width,
                    tooltip=current_day.isoformat(),
                    on_hover=lambda e: (
                        setattr(
                            e.control,
                            "bgcolor",
                            "#171C26"
                            if str(e.data).lower() == "true"
                            else None,
                        ),
                        e.control.update(),
                    ),
                    on_click=(
                        lambda e, selected_date=current_day:
                        open_history_day(selected_date)
                    ),
                    ink=True,
                )
            )

        return shapes, labels

    def selected_series_id():
        return int(series_dropdown.value)

    def refresh_series_options():
        all_series = get_series()

        series_dropdown.options = [
            ft.DropdownOption(
                key=str(series_id),
                text=series_name,
            )
            for series_id, series_name in all_series
        ]

        return all_series

    def refresh_screen():
        series_id = selected_series_id()

        all_series = get_series()

        series_name = next(
            name
            for current_id, name in all_series
            if current_id == series_id
        )

        series_title.value = series_name

        pulse_dates = get_pulse_dates(series_id)

        current_pulse, longest_pulse, total_pulse = (
            calculate_stats(pulse_dates, series_id)
        )

        pulse_state, pulse_state_message = (
            get_pulse_status(pulse_dates, series_id)
        )

        state_colors = {
            "ALIVE": "#FF3158",
            "REST": "#FFC857",
            "FLATLINE": "#72798A",
            "NO PULSE": "#72798A",
        }
        state_color = state_colors[pulse_state]
        pulse_heart.color = state_color
        pulse_state_text.color = state_color

        revived_today = (
            len(pulse_dates) >= 2
            and pulse_dates[-1] == date.today()
            and scheduled_misses_between(
                series_id, pulse_dates[-2], pulse_dates[-1], pulse_dates
            ) >= 2
        )

        if revived_today:
            display_state = "REVIVED"
            display_message = "Revived today. Alive now."
            display_color = "#35D07F"
        else:
            display_state = pulse_state
            display_message = pulse_state_message
            display_color = state_color

        pulse_state_text.value = display_state
        pulse_state_detail.value = display_message
        pulse_heart.color = display_color
        pulse_state_text.color = display_color
        current_text.color = display_color
        history_line_color = display_color

        panel_backgrounds = {
            "ALIVE": "#1A1117",
            "REST": "#1A1710",
            "FLATLINE": "#15171B",
            "NO PULSE": "#15171B",
            "REVIVED": "#101A16",
        }
        state_panel.bgcolor = panel_backgrounds[display_state]
        panel_borders = {
            "ALIVE": "#4A1D2A",
            "REST": "#4A3B1A",
            "FLATLINE": "#303542",
            "NO PULSE": "#303542",
            "REVIVED": "#1D4A35",
        }
        state_panel.border = ft.Border.all(
            1,
            panel_borders[display_state],
        )

        current_text.value = str(current_pulse)
        longest_text.value = str(longest_pulse)
        total_text.value = str(total_pulse)
        history_shapes, history_day_labels = (
            build_history(pulse_dates, history_line_color)
        )
        history_canvas.shapes = history_shapes
        history_labels.controls = history_day_labels

        if pulse_exists_today(series_id):
            note_field.value = get_today_note(series_id)
            note_field.read_only = True
            edit_note_button.visible = True
            save_note_button.visible = False
            undo_pulse_button.visible = True

            record_button.disabled = True
            record_button.content = "♥ PULSE RECORDED"
            record_button.style = ft.ButtonStyle(
                bgcolor="#303542",
                color="#8D95A5",
            )

        else:
            note_field.value = ""
            note_field.read_only = False
            edit_note_button.visible = False
            save_note_button.visible = False
            undo_pulse_button.visible = False
            record_button.disabled = False

            if pulse_state == "FLATLINE":
                record_button.content = "♥ REVIVE"
            else:
                record_button.content = "♥ RECORD PULSE"

            record_button.style = ft.ButtonStyle(
                bgcolor=(
                    "#FF3158"
                    if pulse_state == "NO PULSE"
                    else state_color
                ),
                color="#FFFFFF",
            )

        status_text.value = ""

    async def clear_status_later(expected_message):
        await asyncio.sleep(3)
        if status_text.value == expected_message:
            status_text.value = ""
            page.update()

    def schedule_status_clear():
        if status_text.value:
            page.run_task(clear_status_later, status_text.value)

    def record_pulse(e):
        series_id = selected_series_id()
        today = date.today().isoformat()
        note = note_field.value or ""

        pulse_dates_before = get_pulse_dates(series_id)

        was_flatline = (
            get_pulse_status(pulse_dates_before, series_id)[0]
            == "FLATLINE"
        )

        try:
            with connect_database() as connection:
                connection.execute(
                    """
                    INSERT INTO pulse_entries (
                        series_id,
                        pulse_date,
                        note
                    )
                    VALUES (?, ?, ?)
                    """,
                    (
                        series_id,
                        today,
                        note,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO daily_notes (series_id, note_date, note)
                    VALUES (?, ?, ?)
                    ON CONFLICT(series_id, note_date) DO UPDATE SET
                        note = excluded.note
                    """,
                    (series_id, today, note),
                )

        except sqlite3.IntegrityError:
            status_text.value = (
                "Today's pulse has already been recorded."
            )
            return

        refresh_screen()
        build_notes()
        build_dashboard()

        if was_flatline:
            status_text.value = "♥ Pulse revived."
        else:
            status_text.value = "♥ Pulse recorded."

        page.update()
        schedule_status_clear()

    def start_note_edit(e):
        note_field.read_only = False
        edit_note_button.visible = False
        save_note_button.visible = True
        status_text.value = ""
        select_calendar_day(
            calendar_state["selected_day"],
            update_page=False,
        )
        page.update()

    def save_note_changes(e):
        update_pulse_note(
            selected_series_id(),
            date.today(),
            note_field.value or "",
        )
        note_field.read_only = True
        edit_note_button.visible = True
        save_note_button.visible = False
        status_text.value = "Note updated."
        build_notes()
        build_dashboard()
        page.update()
        schedule_status_clear()

    keep_undo_note = ft.Checkbox(
        label="Keep today's note", value=True
    )

    def confirm_undo_pulse(e):
        undo_today_pulse(selected_series_id(), keep_undo_note.value)
        page.pop_dialog()
        refresh_screen()
        build_dashboard()
        build_calendar()
        build_notes()
        status_text.value = "Today's pulse was removed."
        page.update()
        schedule_status_clear()

    undo_pulse_dialog = ft.AlertDialog(
        modal=True,
        title=ft.Text("Undo today's pulse?"),
        content=ft.Column(
            controls=[
                ft.Text(
                    "CURRENT, LONGEST, TOTAL and the EKG will be recalculated."
                ),
                keep_undo_note,
            ],
            tight=True,
        ),
        actions=[
            ft.TextButton(content="Cancel", on_click=lambda e: page.pop_dialog()),
            ft.Button(content="Undo pulse", on_click=confirm_undo_pulse),
        ],
    )

    def open_undo_pulse(e):
        keep_undo_note.value = True
        page.show_dialog(undo_pulse_dialog)

    def change_series(e):
        save_setting(
            "selected_series_id",
            selected_series_id(),
        )
        refresh_screen()
        build_calendar()
        select_calendar_day(
            calendar_state["selected_day"],
            update_page=False,
        )
        build_notes()
        build_dashboard()
        page.update()

    def save_new_series(e):
        name = (new_series_field.value or "").strip()

        if not name:
            new_series_error.value = (
                "Please enter a series name."
            )
            return

        if len(name) > SERIES_NAME_MAX_LENGTH:
            new_series_error.value = (
                f"Series names can contain at most "
                f"{SERIES_NAME_MAX_LENGTH} characters."
            )
            page.update()
            return

        try:
            new_series_id = create_series(name)

        except sqlite3.IntegrityError:
            new_series_error.value = (
                "A series with this name already exists."
            )
            return

        refresh_series_options()

        series_dropdown.value = str(new_series_id)
        save_setting("selected_series_id", new_series_id)

        page.pop_dialog()

        refresh_screen()
        build_notes()
        build_dashboard()
        page.update()

    new_series_dialog = ft.AlertDialog(
        modal=True,
        title=ft.Text("Start a new series"),
        content=ft.Column(
            controls=[
                new_series_field,
                new_series_error,
            ],
            tight=True,
        ),
        actions=[
            ft.TextButton(
                content="Cancel",
                on_click=lambda e: page.pop_dialog(),
            ),
            ft.Button(
                content="Create",
                on_click=save_new_series,
            ),
        ],
    )

    def open_new_series_dialog(e):
        new_series_field.value = ""
        new_series_error.value = ""

        page.show_dialog(new_series_dialog)

    def save_renamed_series(e):
        name = (rename_series_field.value or "").strip()

        if not name:
            rename_series_error.value = (
                "Please enter a series name."
            )
            page.update()
            return

        if len(name) > SERIES_NAME_MAX_LENGTH:
            rename_series_error.value = (
                f"Series names can contain at most "
                f"{SERIES_NAME_MAX_LENGTH} characters."
            )
            page.update()
            return

        try:
            rename_series(selected_series_id(), name)
        except sqlite3.IntegrityError:
            rename_series_error.value = (
                "A series with this name already exists."
            )
            page.update()
            return

        refresh_series_options()
        page.pop_dialog()
        refresh_screen()
        build_notes()
        build_dashboard()
        page.update()

    rename_series_dialog = ft.AlertDialog(
        modal=True,
        title=ft.Text("Rename series"),
        content=ft.Column(
            controls=[
                rename_series_field,
                rename_series_error,
            ],
            tight=True,
        ),
        actions=[
            ft.TextButton(
                content="Cancel",
                on_click=lambda e: page.pop_dialog(),
            ),
            ft.Button(
                content="Save",
                on_click=save_renamed_series,
            ),
        ],
    )

    def open_rename_series_dialog(e):
        rename_series_field.value = series_title.value
        rename_series_error.value = ""
        page.show_dialog(rename_series_dialog)

    def confirm_delete_series(e):
        delete_series(selected_series_id())
        all_series = refresh_series_options()

        if not all_series:
            next_series_id = create_series(
                "Daily Development"
            )
            all_series = refresh_series_options()
        else:
            next_series_id = all_series[0][0]

        series_dropdown.value = str(next_series_id)
        save_setting("selected_series_id", next_series_id)

        page.pop_dialog()
        refresh_screen()
        build_notes()
        build_dashboard()
        page.update()

    delete_series_dialog = ft.AlertDialog(
        modal=True,
        title=ft.Text("Delete series?"),
        content=delete_series_message,
        actions=[
            ft.TextButton(
                content="Cancel",
                on_click=lambda e: page.pop_dialog(),
            ),
            ft.Button(
                content="Delete",
                on_click=confirm_delete_series,
            ),
        ],
    )

    def open_delete_series_dialog(e):
        delete_series_message.value = (
            f'"{series_title.value}" and all of its pulses '
            "will be permanently deleted."
        )
        page.show_dialog(delete_series_dialog)

    def archive_selected_series(e):
        active_series = get_series()
        if len(active_series) <= 1:
            status_text.value = "Keep at least one active series."
            status_text.color = "#FF5D73"
            page.update()
            return
        archived_id = selected_series_id()
        export_path = create_portable_export(archived_id)
        set_series_archived(archived_id, True)
        remaining = refresh_series_options()
        series_dropdown.value = str(remaining[0][0])
        save_setting("selected_series_id", series_dropdown.value)
        refresh_screen()
        build_calendar()
        build_notes()
        build_dashboard()
        status_text.value = f"Archived and exported: {export_path.name}"
        status_text.color = "#35D07F"
        page.update()

    profile_description = ft.TextField(
        label="Description", multiline=True, min_lines=2, max_lines=4
    )
    profile_goal = ft.TextField(label="Goal", hint_text="Read at least 20 minutes")
    profile_schedule = ft.Dropdown(
        label="Schedule",
        value="daily",
        options=[
            ft.DropdownOption(key="daily", text="Every day"),
            ft.DropdownOption(key="weekdays", text="Selected weekdays"),
            ft.DropdownOption(key="weekly", text="Times per week"),
        ],
    )
    weekday_checks = [
        ft.Checkbox(label=label, value=True)
        for label in ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    ]
    weekly_target_field = ft.TextField(
        label="Times per week", value="3", keyboard_type=ft.KeyboardType.NUMBER
    )
    profile_message = ft.Text(size=12)

    def save_series_profile(e):
        try:
            weekly_target = int(weekly_target_field.value or "0")
        except ValueError:
            weekly_target = 0
        if profile_schedule.value == "weekly" and not 1 <= weekly_target <= 7:
            profile_message.value = "Weekly target must be from 1 to 7."
            profile_message.color = "#FF5D73"
            page.update()
            return
        selected_days = [
            str(index) for index, checkbox in enumerate(weekday_checks)
            if checkbox.value
        ]
        if profile_schedule.value == "weekdays" and not selected_days:
            profile_message.value = "Select at least one weekday."
            profile_message.color = "#FF5D73"
            page.update()
            return
        update_series_profile(
            selected_series_id(),
            profile_description.value or "",
            profile_goal.value or "",
            profile_schedule.value,
            ",".join(selected_days),
            weekly_target if weekly_target else 7,
        )
        page.pop_dialog()
        refresh_screen()
        build_calendar()
        build_dashboard()
        page.update()

    series_profile_dialog = ft.AlertDialog(
        modal=True,
        title=ft.Text("Series settings"),
        content=ft.Column(
            controls=[
                profile_description,
                profile_goal,
                profile_schedule,
                ft.Row(controls=weekday_checks, wrap=True),
                weekly_target_field,
                profile_message,
            ],
            width=500,
            tight=True,
            spacing=10,
        ),
        actions=[
            ft.TextButton(content="Cancel", on_click=lambda e: page.pop_dialog()),
            ft.Button(content="Save", on_click=save_series_profile),
        ],
    )

    def open_series_profile(e):
        details = get_series_details(selected_series_id())
        profile_description.value = details[2]
        profile_goal.value = details[3]
        profile_schedule.value = details[4]
        selected_days = set((details[5] or "").split(","))
        for index, checkbox in enumerate(weekday_checks):
            checkbox.value = str(index) in selected_days
        weekly_target_field.value = str(details[6])
        profile_message.value = ""
        page.show_dialog(series_profile_dialog)

    record_button.on_click = record_pulse
    edit_note_button.on_click = start_note_edit
    save_note_button.on_click = save_note_changes
    undo_pulse_button.on_click = open_undo_pulse
    save_history_note_button.on_click = save_history_note
    series_dropdown.on_select = change_series

    series_menu = ft.PopupMenuButton(
        icon=ft.Icons.MORE_HORIZ,
        icon_color="#A9B0BF",
        tooltip="Series options",
        menu_position=ft.PopupMenuPosition.UNDER,
        items=[
            ft.PopupMenuItem(
                content="Rename series",
                icon=ft.Icons.EDIT_OUTLINED,
                on_click=open_rename_series_dialog,
            ),
            ft.PopupMenuItem(
                content="Series settings",
                icon=ft.Icons.TUNE,
                on_click=open_series_profile,
            ),
            ft.PopupMenuItem(
                content="Archive series",
                icon=ft.Icons.ARCHIVE_OUTLINED,
                on_click=archive_selected_series,
            ),
            ft.PopupMenuItem(),
            ft.PopupMenuItem(
                content=ft.Text(
                    "Delete series",
                    color="#FF5D73",
                ),
                icon=ft.Icons.DELETE_OUTLINE,
                on_click=open_delete_series_dialog,
            ),
        ],
    )

    all_series = refresh_series_options()

    if not all_series:
        default_id = create_series(
            "Daily Development"
        )
        refresh_series_options()
        series_dropdown.value = str(default_id)
        save_setting("selected_series_id", default_id)
    else:
        saved_series_id = get_setting(
            "selected_series_id"
        )
        available_ids = {
            str(series_id)
            for series_id, _ in all_series
        }

        if saved_series_id in available_ids:
            series_dropdown.value = saved_series_id
        else:
            series_dropdown.value = str(all_series[0][0])
            save_setting(
                "selected_series_id",
                series_dropdown.value,
            )

    refresh_screen()

    today_view = ft.Column(
        controls=[
                state_panel,

                stats_row,

                note_title,
                note_field,

                ft.Row(
                    controls=[
                        edit_note_button,
                        save_note_button,
                        undo_pulse_button,
                        record_button,
                    ],
                    alignment=ft.MainAxisAlignment.CENTER,
                    spacing=10,
                    wrap=True,
                ),

                status_text,

                ft.Column(
                    controls=[
                        history_title,
                        history_hint,
                    ],
                    spacing=2,
                    horizontal_alignment=(
                        ft.CrossAxisAlignment.CENTER
                    ),
                ),
                history_monitor,
        ],
        horizontal_alignment=ft.CrossAxisAlignment.CENTER,
        spacing=18,
    )

    weekday_header = ft.Row(
        controls=[
            ft.Container(
                content=ft.Text(
                    weekday,
                    size=10,
                    color="#8D95A5",
                    weight=ft.FontWeight.BOLD,
                    text_align=ft.TextAlign.CENTER,
                ),
                width=96,
                alignment=ft.Alignment.CENTER,
            )
            for weekday in ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]
        ],
        spacing=0,
    )
    calendar_month_picker.on_select = jump_calendar_date
    calendar_year_picker.on_select = jump_calendar_date

    history_legend = ft.Row(
        controls=[
            ft.Text("● Pulse", color="#FF3158", size=11),
            ft.Text("● Rest", color="#F5A623", size=11),
            ft.Text("● Flatline", color="#72798A", size=11),
            ft.Text("● Revive", color="#35D07F", size=11),
            ft.Text("▣ Note", color="#56C8FF", size=11),
        ],
        alignment=ft.MainAxisAlignment.CENTER,
        spacing=12,
        wrap=True,
    )

    history_view = ft.Column(
        controls=[
            ft.Row(
                controls=[
                    ft.IconButton(
                        icon=ft.Icons.CHEVRON_LEFT,
                        tooltip="Previous month",
                        on_click=lambda e: change_calendar_month(-1),
                    ),
                    calendar_month_title,
                    ft.IconButton(
                        icon=ft.Icons.CHEVRON_RIGHT,
                        tooltip="Next month",
                        on_click=lambda e: change_calendar_month(1),
                    ),
                    ft.Button(
                        content="TODAY",
                        on_click=return_calendar_to_today,
                    ),
                    calendar_month_picker,
                    calendar_year_picker,
                ],
                alignment=ft.MainAxisAlignment.CENTER,
                wrap=True,
            ),
            ft.Text(
                "Select a day to view its status and note.",
                size=12,
                color="#72798A",
            ),
            history_legend,
            ft.Container(
                content=ft.Column(
                    controls=[weekday_header, calendar_grid],
                    spacing=0,
                ),
                width=674,
                bgcolor="#0B0E14",
                border=ft.Border.all(1, "#252B38"),
                border_radius=14,
                clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
            ),
            ft.Container(
                content=ft.Column(
                    controls=[
                        selected_day_title,
                        selected_day_state,
                        selected_day_note,
                        ft.Row(
                            controls=[
                                ft.Row(
                                    controls=[
                                        ft.IconButton(
                                            icon=ft.Icons.CHEVRON_LEFT,
                                            tooltip="Previous day",
                                            on_click=lambda e: move_selected_day(-1),
                                        ),
                                        ft.IconButton(
                                            icon=ft.Icons.CHEVRON_RIGHT,
                                            tooltip="Next day",
                                            on_click=lambda e: move_selected_day(1),
                                        ),
                                        selected_day_message,
                                    ],
                                    spacing=4,
                                    wrap=True,
                                ),
                                save_history_note_button,
                            ],
                            alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                            wrap=True,
                        ),
                    ],
                    spacing=10,
                ),
                width=674,
                padding=18,
                bgcolor="#151923",
                border=ft.Border.all(1, "#252B38"),
                border_radius=16,
            ),
        ],
        visible=False,
        horizontal_alignment=ft.CrossAxisAlignment.CENTER,
        spacing=16,
    )

    notes_filter = ft.Dropdown(
        label="Show",
        width=220,
        value="all_days",
        options=[
            ft.DropdownOption(key="all_days", text="All days"),
            ft.DropdownOption(key="written_notes", text="Days with notes"),
            ft.DropdownOption(key="pulse_days", text="Pulse days"),
        ],
    )
    notes_search = ft.TextField(
        label="Search notes",
        hint_text="Search words in your notes",
        prefix_icon=ft.Icons.SEARCH,
        width=590,
    )
    notes_status_filter = ft.Dropdown(
        label="Status",
        width=180,
        value="all",
        options=[
            ft.DropdownOption(key="all", text="All statuses"),
            ft.DropdownOption(key="alive", text="Alive"),
            ft.DropdownOption(key="revive", text="Revive"),
            ft.DropdownOption(key="rest", text="Rest"),
            ft.DropdownOption(key="flatline", text="Flatline"),
        ],
    )
    notes_sort = ft.Dropdown(
        label="Order",
        width=190,
        value="newest",
        options=[
            ft.DropdownOption(key="newest", text="Newest to oldest"),
            ft.DropdownOption(key="oldest", text="Oldest to newest"),
        ],
    )
    notes_summary = ft.Text(size=12, color="#8D95A5")
    notes_list = ft.Column(spacing=10)
    notes_empty_state = ft.Text(
        "No days match the selected filters.",
        color="#8D95A5",
        visible=False,
    )
    note_editor_state = {"date": None}
    past_note_editor = ft.TextField(
        label="NOTE",
        multiline=True,
        min_lines=5,
        max_lines=9,
        width=460,
        border_color="#343A48",
        focused_border_color="#FF3158",
    )
    past_note_editor_date = ft.Text(
        size=14,
        color="#8D95A5",
        weight=ft.FontWeight.BOLD,
    )

    def notes_status_for_day(day_value, pulse_dates):
        raw_state, color = get_history_day_state(day_value, pulse_dates)
        if raw_state == "PULSE RECORDED":
            return "ALIVE", color
        if raw_state == "ALIVE — WAITING TODAY":
            return "ALIVE", color
        if raw_state == "NO PULSE":
            return "FLATLINE", color
        return raw_state, color

    def save_past_note(e):
        note_date = note_editor_state["date"]
        if note_date is None:
            return
        update_pulse_note(
            selected_series_id(),
            note_date,
            past_note_editor.value or "",
        )
        page.pop_dialog()
        if calendar_state["selected_day"] == note_date:
            selected_day_note.value = past_note_editor.value or ""
        if note_date == date.today():
            note_field.value = past_note_editor.value or ""
        build_notes()
        build_dashboard()
        page.update()

    past_note_dialog = ft.AlertDialog(
        modal=True,
        title=ft.Text("Edit note"),
        content=ft.Column(
            controls=[past_note_editor_date, past_note_editor],
            width=470,
            tight=True,
            spacing=12,
        ),
        actions=[
            ft.TextButton(
                content="Cancel",
                on_click=lambda e: page.pop_dialog(),
            ),
            ft.Button(content="Save", on_click=save_past_note),
        ],
    )

    def open_past_note_editor(day_value, note_value):
        note_editor_state["date"] = day_value
        past_note_editor_date.value = day_value.strftime("%A, %d %B %Y")
        past_note_editor.value = note_value
        page.show_dialog(past_note_dialog)

    def build_note_card(day_value, status_name, status_color, note_value, has_pulse):
        note_display = (
            note_value
            if note_value.strip()
            else "No note recorded."
        )
        actions = [
            ft.IconButton(
                icon=ft.Icons.EDIT_OUTLINED,
                tooltip="Add or edit note",
                on_click=(
                    lambda e, selected_day=day_value, current_note=note_value:
                    open_past_note_editor(selected_day, current_note)
                ),
            )
        ]

        search_query = (notes_search.value or "").strip()
        note_control = ft.Text(
            note_display,
            size=14,
            color="#C9CED8" if note_value.strip() else "#72798A",
        )
        if search_query and search_query.lower() in note_display.lower():
            spans = []
            remaining = note_display
            query_lower = search_query.lower()
            while query_lower in remaining.lower():
                index = remaining.lower().index(query_lower)
                if index:
                    spans.append(ft.TextSpan(remaining[:index]))
                spans.append(
                    ft.TextSpan(
                        remaining[index:index + len(search_query)],
                        style=ft.TextStyle(
                            bgcolor="#4A3B1A",
                            color="#FFFFFF",
                        ),
                    )
                )
                remaining = remaining[index + len(search_query):]
            if remaining:
                spans.append(ft.TextSpan(remaining))
            note_control = ft.Text(spans=spans, size=14)

        return ft.Container(
            content=ft.Column(
                controls=[
                    ft.Row(
                        controls=[
                            ft.Column(
                                controls=[
                                    ft.Text(
                                        day_value.strftime("%A, %d %B %Y"),
                                        size=15,
                                        weight=ft.FontWeight.BOLD,
                                        color="#F4F6FA",
                                    ),
                                    ft.Text(
                                        status_name,
                                        size=11,
                                        weight=ft.FontWeight.BOLD,
                                        color=status_color,
                                    ),
                                ],
                                spacing=2,
                            ),
                            ft.Row(controls=actions),
                        ],
                        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                    ),
                    ft.Divider(color="#252B38"),
                    note_control,
                ],
                spacing=8,
            ),
            width=674,
            padding=16,
            bgcolor="#151923",
            border=ft.Border.all(1, "#252B38"),
            border_radius=14,
            ink=True,
            on_click=(
                lambda e, selected_day=day_value, current_note=note_value:
                open_past_note_editor(selected_day, current_note)
            ),
        )

    def build_notes():
        pulse_dates = get_pulse_dates(selected_series_id())
        if not pulse_dates:
            notes_list.controls = []
            notes_summary.value = "This series has no interaction yet."
            notes_empty_state.visible = True
            return

        notes_by_date = get_daily_notes(selected_series_id())
        pulse_date_set = set(pulse_dates)
        first_day = pulse_dates[0]
        last_day = date.today()
        all_days = [
            first_day + timedelta(days=offset)
            for offset in range((last_day - first_day).days + 1)
        ]

        rows = []
        for day_value in all_days:
            has_pulse = day_value in pulse_date_set
            note_value = notes_by_date.get(day_value, "")
            status_name, status_color = notes_status_for_day(
                day_value,
                pulse_dates,
            )

            if notes_filter.value == "written_notes" and not note_value.strip():
                continue
            if notes_filter.value == "pulse_days" and not has_pulse:
                continue
            search_query = (notes_search.value or "").strip().lower()
            if search_query and search_query not in note_value.lower():
                continue
            if (
                notes_status_filter.value != "all"
                and status_name.lower() != notes_status_filter.value
            ):
                continue

            rows.append(
                (day_value, status_name, status_color, note_value, has_pulse)
            )

        rows.sort(
            key=lambda row: row[0],
            reverse=notes_sort.value == "newest",
        )
        grouped_controls = []
        active_month = None
        for row in rows:
            month_key = (row[0].year, row[0].month)
            if month_key != active_month:
                grouped_controls.append(
                    ft.Container(
                        content=ft.Text(
                            row[0].strftime("%B %Y").upper(),
                            size=12,
                            color="#56C8FF",
                            weight=ft.FontWeight.BOLD,
                        ),
                        padding=ft.Padding.only(top=10, left=4),
                    )
                )
                active_month = month_key
            grouped_controls.append(build_note_card(*row))
        notes_list.controls = grouped_controls
        notes_empty_state.visible = not rows
        notes_summary.value = (
            (
                f"{len(rows)} results for “{(notes_search.value or '').strip()}” · "
                if (notes_search.value or "").strip()
                else f"{len(rows)} of {len(all_days)} days shown · "
            )
            + f"Since {first_day.strftime('%d %B %Y')}"
        )

    def clear_notes_filters(e):
        notes_filter.value = "all_days"
        notes_status_filter.value = "all"
        notes_sort.value = "newest"
        notes_search.value = ""
        build_notes()
        page.update()

    def refresh_notes_filters(e):
        build_notes()
        page.update()

    notes_filter.on_select = refresh_notes_filters
    notes_status_filter.on_select = refresh_notes_filters
    notes_sort.on_select = refresh_notes_filters
    notes_search.on_change = refresh_notes_filters

    notes_view = ft.Column(
        controls=[
            ft.Row(
                controls=[
                    ft.Icon(ft.Icons.NOTES, size=30, color="#56C8FF"),
                    ft.Column(
                        controls=[
                            ft.Text(
                                "NOTES",
                                size=24,
                                weight=ft.FontWeight.BOLD,
                            ),
                            notes_summary,
                        ],
                        spacing=1,
                    ),
                ],
                alignment=ft.MainAxisAlignment.CENTER,
            ),
            ft.Row(
                controls=[notes_filter, notes_status_filter, notes_sort],
                alignment=ft.MainAxisAlignment.CENTER,
                spacing=10,
                wrap=True,
            ),
            notes_search,
            ft.Button(content="CLEAR FILTERS", on_click=clear_notes_filters),
            notes_empty_state,
            notes_list,
        ],
        visible=False,
        horizontal_alignment=ft.CrossAxisAlignment.CENTER,
        spacing=14,
    )

    dashboard_filter = ft.Dropdown(
        label="Show",
        width=190,
        value="all",
        options=[
            ft.DropdownOption(key="all", text="All series"),
            ft.DropdownOption(key="waiting", text="Waiting today"),
            ft.DropdownOption(key="completed", text="Completed today"),
        ],
    )
    dashboard_sort = ft.Dropdown(
        label="Order",
        width=210,
        value="attention",
        options=[
            ft.DropdownOption(key="attention", text="Needs attention"),
            ft.DropdownOption(key="name", text="Series name"),
            ft.DropdownOption(key="newest", text="Newest series"),
        ],
    )
    dashboard_summary = ft.Text(size=13, color="#8D95A5")
    dashboard_completion_message = ft.Text(
        size=13,
        color="#35D07F",
        weight=ft.FontWeight.BOLD,
    )
    dashboard_progress = ft.ProgressBar(
        width=420,
        value=0,
        color="#35D07F",
        bgcolor="#252B38",
    )
    dashboard_cards = ft.Column(spacing=12)
    dashboard_card_notes = {}

    def dashboard_display_state(series_id, pulse_dates):
        pulse_state, pulse_message = get_pulse_status(pulse_dates, series_id)
        revived_today = (
            len(pulse_dates) >= 2
            and pulse_dates[-1] == date.today()
            and scheduled_misses_between(
                series_id, pulse_dates[-2], pulse_dates[-1], pulse_dates
            ) >= 2
        )
        if revived_today:
            return "REVIVED", "Revived today. Alive now.", "#35D07F"
        colors = {
            "ALIVE": "#FF3158",
            "REST": "#F5A623",
            "FLATLINE": "#72798A",
            "NO PULSE": "#72798A",
        }
        return pulse_state, pulse_message, colors[pulse_state]

    def save_dashboard_note(series_id, note_control):
        update_pulse_note(
            series_id,
            date.today(),
            note_control.value or "",
        )
        if series_id == selected_series_id():
            note_field.value = note_control.value or ""
            selected_day_note.value = note_control.value or ""
        build_notes()
        page.update()

    def record_dashboard_pulse(series_id, note_control):
        if pulse_exists_today(series_id):
            return
        note = note_control.value or ""
        with connect_database() as connection:
            connection.execute(
                """
                INSERT INTO pulse_entries (series_id, pulse_date, note)
                VALUES (?, ?, ?)
                """,
                (series_id, date.today().isoformat(), note),
            )
            connection.execute(
                """
                INSERT INTO daily_notes (series_id, note_date, note)
                VALUES (?, ?, ?)
                ON CONFLICT(series_id, note_date) DO UPDATE SET
                    note = excluded.note
                """,
                (series_id, date.today().isoformat(), note),
            )
        refresh_screen()
        build_calendar()
        build_notes()
        build_dashboard()
        page.update()

    def open_series_today(series_id):
        series_dropdown.value = str(series_id)
        save_setting("selected_series_id", series_id)
        refresh_screen()
        build_calendar()
        build_notes()
        show_section("today")

    def undo_dashboard_pulse(series_id):
        undo_today_pulse(series_id, True)
        refresh_screen()
        build_calendar()
        build_notes()
        build_dashboard()
        page.update()

    def build_dashboard_card(series_id, series_name):
        compact = (get_setting("compact_dashboard") or "1") == "1"
        series_details = get_series_details(series_id)
        series_context = series_details[3] or series_details[2]
        pulse_dates = get_pulse_dates(series_id)
        current_value, longest_value, total_value = calculate_stats(
            pulse_dates, series_id
        )
        state_name, state_message, state_color = dashboard_display_state(
            series_id, pulse_dates
        )
        completed_today = pulse_exists_today(series_id)
        note_control = ft.TextField(
            hint_text="What did you accomplish today?",
            value=get_today_note(series_id),
            multiline=True,
            min_lines=1 if compact else 2,
            max_lines=2 if compact else 4,
            width=620,
            read_only=False,
            border_color="#343A48",
            focused_border_color=state_color,
            visible=not compact,
        )
        dashboard_card_notes[series_id] = note_control

        action_button = ft.Button(
            content=(
                "♥ PULSE RECORDED"
                if completed_today
                else "♥ REVIVE"
                if state_name == "FLATLINE"
                else "♥ RECORD PULSE"
            ),
            disabled=completed_today,
            on_click=(
                lambda e, sid=series_id, field=note_control:
                record_dashboard_pulse(sid, field)
            ),
        )
        if not completed_today:
            action_button.style = ft.ButtonStyle(
                bgcolor="#FF3158" if state_name == "NO PULSE" else state_color,
                color="#FFFFFF",
            )

        return ft.Container(
            content=ft.Column(
                controls=[
                    ft.Row(
                        controls=[
                            ft.Column(
                                controls=[
                                    ft.Text(
                                        series_name,
                                        size=19,
                                        weight=ft.FontWeight.BOLD,
                                    ),
                                    ft.Text(
                                        state_name,
                                        size=12,
                                        weight=ft.FontWeight.BOLD,
                                        color=state_color,
                                    ),
                                ],
                                spacing=2,
                            ),
                            ft.IconButton(
                                icon=ft.Icons.OPEN_IN_NEW,
                                tooltip="Open series",
                                on_click=lambda e, sid=series_id: open_series_today(sid),
                            ),
                        ],
                        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                    ),
                    ft.Text(
                        state_message,
                        size=12,
                        color="#8D95A5",
                        visible=not compact,
                    ),
                    ft.Text(
                        series_context,
                        size=12,
                        color="#A9B0BF",
                        visible=bool(series_context) and not compact,
                    ),
                    ft.Row(
                        controls=[
                            ft.Text(f"CURRENT  {current_value}", color=state_color),
                            ft.Text(f"LONGEST  {longest_value}", color="#FFC857"),
                            ft.Text(f"TOTAL  {total_value}", color="#56C8FF"),
                        ],
                        spacing=20,
                        wrap=True,
                    ),
                    note_control,
                    ft.Row(
                        controls=[
                            ft.Button(
                                content="SAVE NOTE",
                                visible=completed_today and not compact,
                                on_click=(
                                    lambda e, sid=series_id, field=note_control:
                                    save_dashboard_note(sid, field)
                                ),
                            ),
                            ft.Button(
                                content="UNDO",
                                visible=completed_today,
                                on_click=(
                                    lambda e, sid=series_id:
                                    undo_dashboard_pulse(sid)
                                ),
                            ),
                            action_button,
                        ],
                        alignment=ft.MainAxisAlignment.END,
                        wrap=True,
                    ),
                ],
                spacing=10,
            ),
            width=674,
            padding=18,
            bgcolor="#151923",
            border=ft.Border.all(1, "#252B38"),
            border_radius=16,
        )

    def build_dashboard():
        series_rows = get_series()
        dashboard_data = []
        completed_count = 0
        for series_id, series_name in series_rows:
            pulse_dates = get_pulse_dates(series_id)
            state_name, _, _ = dashboard_display_state(series_id, pulse_dates)
            completed = pulse_exists_today(series_id)
            completed_count += int(completed)
            if dashboard_filter.value == "waiting" and completed:
                continue
            if dashboard_filter.value == "completed" and not completed:
                continue
            priorities = {
                "FLATLINE": 0,
                "REST": 1,
                "ALIVE": 2,
                "REVIVED": 3,
                "NO PULSE": 4,
            }
            priority = priorities.get(state_name, 5)
            if completed:
                priority = 3
            dashboard_data.append(
                (series_id, series_name, priority)
            )

        if dashboard_sort.value == "attention":
            dashboard_data.sort(key=lambda row: (row[2], row[1].lower()))
        elif dashboard_sort.value == "name":
            dashboard_data.sort(key=lambda row: row[1].lower())
        else:
            dashboard_data.sort(key=lambda row: row[0], reverse=True)

        dashboard_cards.controls = [
            build_dashboard_card(series_id, series_name)
            for series_id, series_name, _ in dashboard_data
        ]
        dashboard_summary.value = (
            f"{completed_count} of {len(series_rows)} series completed today · "
            f"{len(series_rows) - completed_count} waiting"
        )
        dashboard_completion_message.value = (
            "All pulses recorded for today."
            if series_rows and completed_count == len(series_rows)
            else ""
        )
        dashboard_progress.value = (
            completed_count / len(series_rows) if series_rows else 0
        )
        if not series_rows:
            dashboard_cards.controls = [
                ft.Column(
                    controls=[
                        ft.Text("No active series yet.", color="#8D95A5"),
                        ft.Button(
                            content="CREATE YOUR FIRST SERIES",
                            on_click=open_new_series_dialog,
                        ),
                    ],
                    horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                )
            ]

    def refresh_dashboard_controls(e):
        build_dashboard()
        page.update()

    dashboard_filter.on_select = refresh_dashboard_controls
    dashboard_sort.on_select = refresh_dashboard_controls

    dashboard_view = ft.Column(
        controls=[
            ft.Text(
                "TODAY'S DASHBOARD",
                size=24,
                weight=ft.FontWeight.BOLD,
            ),
            dashboard_summary,
            dashboard_completion_message,
            dashboard_progress,
            ft.Row(
                controls=[dashboard_filter, dashboard_sort],
                alignment=ft.MainAxisAlignment.CENTER,
                spacing=10,
                wrap=True,
            ),
            dashboard_cards,
        ],
        horizontal_alignment=ft.CrossAxisAlignment.CENTER,
        spacing=14,
    )

    help_content = ft.Column(
        controls=[
            ft.Text("THE PULSE BASICS", size=18, weight=ft.FontWeight.BOLD),
            ft.Text(
                "A series is a task or habit you want to keep alive. "
                "Record one pulse on every day you complete it."
            ),
            ft.Divider(),
            ft.Text("PULSE", weight=ft.FontWeight.BOLD, color="#FF3158"),
            ft.Text("The task was completed on that day."),
            ft.Text("CURRENT", weight=ft.FontWeight.BOLD, color="#FF3158"),
            ft.Text(
                "The number of pulses in the currently living run. "
                "One REST day does not end the run; two consecutive missed "
                "days cause FLATLINE and reset CURRENT to 0."
            ),
            ft.Text("LONGEST", weight=ft.FontWeight.BOLD, color="#FFC857"),
            ft.Text("The highest number of pulses achieved in one living run."),
            ft.Text("TOTAL", weight=ft.FontWeight.BOLD, color="#56C8FF"),
            ft.Text("Every pulse ever recorded for the selected series."),
            ft.Text("REST", weight=ft.FontWeight.BOLD, color="#F5A623"),
            ft.Text("One fully missed day. The series is still alive."),
            ft.Text("FLATLINE", weight=ft.FontWeight.BOLD, color="#8D95A5"),
            ft.Text("Two or more consecutive fully missed days."),
            ft.Text("REVIVE", weight=ft.FontWeight.BOLD, color="#35D07F"),
            ft.Text("The first new pulse after a FLATLINE."),
            ft.Divider(),
            ft.Text("SCREENS", size=18, weight=ft.FontWeight.BOLD),
            ft.Text(
                "Dashboard: view every series and record today's work quickly.\n"
                "Today: focus on one selected series.\n"
                "History: explore the calendar, EKG and any day's note.\n"
                "Notes: search, filter, sort and edit the complete journal."
            ),
            ft.Text("SERIES MANAGEMENT", size=18, weight=ft.FontWeight.BOLD),
            ft.Text(
                "Use + NEW SERIES to start another task. The menu beside it "
                "renames or permanently deletes the selected series. Changing "
                "the selector updates Today, History and Notes together."
            ),
            ft.Text("HISTORY & NOTES", size=18, weight=ft.FontWeight.BOLD),
            ft.Text(
                "Calendar pulse peaks mark completed days; flat lines mark days "
                "without a pulse. Select a day to view or edit its note. In "
                "Notes, use Show, Status, Order and Search to control the journal."
            ),
            ft.Text("SHARING & ARCHIVE", size=18, weight=ft.FontWeight.BOLD),
            ft.Text(
                "Archiving hides a series and automatically creates a portable "
                ".pulse.json file. In Settings, export all data, open the export "
                "folder, or import a file received from another device. Imported "
                "series never overwrite an existing series with the same name."
            ),
            ft.Divider(),
            ft.Text("IMPORTANT", size=18, weight=ft.FontWeight.BOLD),
            ft.Text(
                "Adding a note to a REST or FLATLINE day does not create a "
                "pulse or change statistics. Future dates and dates before a "
                "series began cannot be edited. A pulse can be recorded only "
                "once per series per day."
            ),
        ],
        width=520,
        height=560,
        scroll=ft.ScrollMode.AUTO,
        spacing=8,
    )

    help_dialog = ft.AlertDialog(
        modal=True,
        title=ft.Text("How The Pulse works"),
        content=help_content,
        actions=[
            ft.Button(content="Close", on_click=lambda e: page.pop_dialog()),
        ],
    )

    def open_help(e):
        page.show_dialog(help_dialog)

    onboarding_dialog = ft.AlertDialog(
        modal=True,
        title=ft.Text("Welcome to The Pulse"),
        content=ft.Column(
            controls=[
                ft.Text(
                    "Create a series for each task or habit you want to keep alive."
                ),
                ft.Text(
                    "Record a PULSE on every day you complete the task. "
                    "One missed day is REST; two consecutive missed days cause "
                    "FLATLINE; completing it again creates a REVIVE."
                ),
                ft.Text(
                    "CURRENT is your living run, LONGEST is your best run, and "
                    "TOTAL is every pulse you have recorded."
                ),
                ft.Text(
                    "Start from Dashboard to see and update all series. "
                    "You can reopen the complete guide anytime with the ? button."
                ),
            ],
            width=470,
            tight=True,
            spacing=12,
        ),
        actions=[
            ft.Button(
                content="Get started",
                on_click=lambda e: (
                    save_setting("onboarding_completed", "1"),
                    page.pop_dialog(),
                ),
            ),
        ],
    )

    settings_message = ft.Text(size=12, color="#56C8FF")
    default_screen_setting = ft.Dropdown(
        label="Default screen",
        value=get_setting("default_screen") or "dashboard",
        options=[
            ft.DropdownOption(key="dashboard", text="Dashboard"),
            ft.DropdownOption(key="today", text="Today"),
            ft.DropdownOption(key="history", text="History"),
            ft.DropdownOption(key="notes", text="Notes"),
        ],
    )
    week_start_setting = ft.Dropdown(
        label="Week starts on",
        value=get_setting("week_start") or "monday",
        options=[
            ft.DropdownOption(key="monday", text="Monday"),
            ft.DropdownOption(key="sunday", text="Sunday"),
        ],
    )
    reminder_time_setting = ft.TextField(
        label="Daily reminder time",
        hint_text="21:00",
        value=get_setting("reminder_time") or "21:00",
    )
    reminders_enabled_setting = ft.Switch(
        label="Daily reminders",
        value=(get_setting("reminders_enabled") or "1") == "1",
    )
    reminder_time_setting.disabled = not reminders_enabled_setting.value

    def toggle_reminders(e):
        reminder_time_setting.disabled = not reminders_enabled_setting.value
        page.update()

    reminders_enabled_setting.on_change = toggle_reminders
    compact_dashboard_setting = ft.Switch(
        label="Compact Dashboard cards",
        value=(get_setting("compact_dashboard") or "1") == "1",
    )
    archived_series_list = ft.Column(spacing=6)
    export_scope_setting = ft.Dropdown(
        label="What do you want to export?",
        value="all",
        options=[ft.DropdownOption(key="all", text="All series")],
    )
    import_path_setting = ft.TextField(
        label="Import a shared .pulse.json file",
        hint_text=r"C:\Users\YourName\Downloads\series.pulse.json",
    )

    def export_one_series(series_id):
        export_path = create_portable_export(series_id)
        settings_message.value = f"Shareable file created: {export_path.name}"
        settings_message.color = "#35D07F"
        open_exports_folder()
        page.update()

    def refresh_export_options():
        with connect_database() as connection:
            rows = connection.execute(
                "SELECT id, name, archived FROM series ORDER BY archived, name"
            ).fetchall()
        export_scope_setting.options = [
            ft.DropdownOption(key="all", text="All series (one file)")
        ] + [
            ft.DropdownOption(
                key=f"series:{series_id}",
                text=f"{name}{' — archived' if archived else ''}",
            )
            for series_id, name, archived in rows
        ]
        available_values = {"all"} | {
            f"series:{series_id}" for series_id, _, _ in rows
        }
        if export_scope_setting.value not in available_values:
            export_scope_setting.value = "all"

    def choose_import_file(e):
        try:
            import tkinter as tk
            from tkinter import filedialog

            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            selected_path = filedialog.askopenfilename(
                title="Import The Pulse data",
                filetypes=[
                    ("The Pulse exports", "*.pulse.json"),
                    ("JSON files", "*.json"),
                    ("All files", "*.*"),
                ],
            )
            root.destroy()
            if selected_path:
                import_path_setting.value = selected_path
                settings_message.value = "File selected. Press IMPORT FILE."
                settings_message.color = "#56C8FF"
        except (ImportError, OSError, RuntimeError):
            settings_message.value = (
                "The file window could not open. Paste the downloaded file path above."
            )
            settings_message.color = "#FF5D73"
        page.update()

    def refresh_archived_series():
        archived = []
        with connect_database() as connection:
            archived = connection.execute(
                "SELECT id, name FROM series WHERE archived = 1 ORDER BY name"
            ).fetchall()
        archived_series_list.controls = [
            ft.Row(
                controls=[
                    ft.Text(name, expand=True),
                    ft.Button(
                        content="MAKE ACTIVE",
                        on_click=lambda e, sid=series_id: restore_archived(sid),
                    ),
                    ft.Button(
                        content="EXPORT FILE",
                        on_click=lambda e, sid=series_id: export_one_series(sid),
                    ),
                ]
            )
            for series_id, name in archived
        ] or [ft.Text("No archived series.", color="#8D95A5")]

    def restore_archived(series_id):
        set_series_archived(series_id, False)
        refresh_series_options()
        refresh_archived_series()
        build_dashboard()
        page.update()

    def save_settings(e):
        reminder_value = (reminder_time_setting.value or "").strip()
        try:
            hour_text, minute_text = reminder_value.split(":")
            valid_time = 0 <= int(hour_text) <= 23 and 0 <= int(minute_text) <= 59
        except (ValueError, AttributeError):
            valid_time = False
        if reminders_enabled_setting.value and not valid_time:
            settings_message.value = "Use a valid 24-hour time such as 21:00."
            settings_message.color = "#FF5D73"
            page.update()
            return
        save_setting("default_screen", default_screen_setting.value)
        save_setting("week_start", week_start_setting.value)
        save_setting("reminder_time", reminder_value)
        save_setting("reminders_enabled", int(reminders_enabled_setting.value))
        save_setting("compact_dashboard", int(compact_dashboard_setting.value))
        settings_message.value = "Settings saved."
        settings_message.color = "#35D07F"
        build_dashboard()
        page.update()

    def export_chosen_data(e):
        selected_export = export_scope_setting.value or "all"
        if selected_export == "all":
            export_path = create_portable_export()
            description = "All series"
        else:
            try:
                export_series_id = int(selected_export.split(":", 1)[1])
            except (ValueError, IndexError):
                settings_message.value = "Choose a valid export option."
                settings_message.color = "#FF5D73"
                page.update()
                return
            export_path = create_portable_export(export_series_id)
            details = get_series_details(export_series_id)
            description = details[1] if details else "Selected series"
        settings_message.value = f"{description} exported: {export_path.name}"
        settings_message.color = "#35D07F"
        open_exports_folder()
        page.update()

    def import_shared_data(e):
        try:
            imported_names = import_portable_export(
                (import_path_setting.value or "").strip().strip('"')
            )
        except (ValueError, OSError, TypeError) as error:
            settings_message.value = str(error)
            settings_message.color = "#FF5D73"
            page.update()
            return
        refresh_series_options()
        build_dashboard()
        build_calendar()
        build_notes()
        settings_message.value = (
            f"Imported {len(imported_names)} series: " + ", ".join(imported_names)
        )
        settings_message.color = "#35D07F"
        page.update()

    def open_data_folder(e):
        if not open_exports_folder():
            settings_message.value = str(DB_PATH.with_name("exports"))
            settings_message.color = "#56C8FF"
            page.update()

    def show_onboarding_again(e):
        page.pop_dialog()
        page.show_dialog(onboarding_dialog)

    settings_dialog = ft.AlertDialog(
        modal=True,
        title=ft.Text("Settings"),
        content=ft.Column(
            controls=[
                default_screen_setting,
                week_start_setting,
                ft.Row(
                    controls=[reminders_enabled_setting, reminder_time_setting],
                    spacing=12,
                ),
                compact_dashboard_setting,
                ft.Divider(),
                ft.Text("EXPORT & IMPORT", weight=ft.FontWeight.BOLD),
                ft.Text(
                    "EXPORT: Choose one series or all series. The created "
                    ".pulse.json file includes pulses, notes and schedules and "
                    "can be shared through WhatsApp, e-mail or cloud storage.",
                    size=12,
                    color="#A9B0BF",
                ),
                export_scope_setting,
                ft.Row(
                    controls=[
                        ft.Button(content="EXPORT FILE", on_click=export_chosen_data),
                        ft.Button(content="OPEN EXPORT FOLDER", on_click=open_data_folder),
                    ],
                    wrap=True,
                ),
                ft.Text(
                    "IMPORT: Select a previously exported .pulse.json file. "
                    "Its series will be added to the application without "
                    "overwriting your existing data.",
                    size=12,
                    color="#A9B0BF",
                ),
                import_path_setting,
                ft.Row(
                    controls=[
                        ft.Button(content="CHOOSE FILE", on_click=choose_import_file),
                        ft.Button(content="IMPORT FILE", on_click=import_shared_data),
                    ],
                    wrap=True,
                ),
                ft.Divider(),
                ft.Text("ARCHIVED SERIES", weight=ft.FontWeight.BOLD),
                ft.Text(
                    "Archived series are hidden from daily screens. Archiving "
                    "automatically creates a shareable file. Make Active brings "
                    "the series back to Dashboard; Export File creates another copy.",
                    size=12,
                    color="#A9B0BF",
                ),
                archived_series_list,
                ft.Button(content="SHOW INTRODUCTION", on_click=show_onboarding_again),
                settings_message,
            ],
            width=520,
            height=570,
            scroll=ft.ScrollMode.AUTO,
            spacing=10,
        ),
        actions=[
            ft.TextButton(content="Close", on_click=lambda e: page.pop_dialog()),
            ft.Button(content="Save", on_click=save_settings),
        ],
    )

    def open_settings(e):
        settings_message.value = ""
        refresh_export_options()
        refresh_archived_series()
        page.show_dialog(settings_dialog)

    dashboard_tab = ft.Button(content="DASHBOARD")
    today_tab = ft.Button(content="TODAY")
    history_tab = ft.Button(content="HISTORY")
    notes_tab = ft.Button(content="NOTES")

    series_management_row = ft.Row(
        controls=[
            series_dropdown,
            ft.Button(
                content="+ NEW SERIES",
                on_click=open_new_series_dialog,
            ),
            series_menu,
        ],
        alignment=ft.MainAxisAlignment.CENTER,
        spacing=10,
        wrap=True,
    )

    def show_section(section_name):
        series_management_row.visible = section_name != "dashboard"
        dashboard_view.visible = section_name == "dashboard"
        today_view.visible = section_name == "today"
        history_view.visible = section_name == "history"
        notes_view.visible = section_name == "notes"
        for button, name in [
            (dashboard_tab, "dashboard"),
            (today_tab, "today"),
            (history_tab, "history"),
            (notes_tab, "notes"),
        ]:
            button.style = ft.ButtonStyle(
                bgcolor="#FF3158" if name == section_name else "#151923",
                color="#FFFFFF" if name == section_name else "#A9B0BF",
            )
        if section_name == "history":
            build_calendar()
            select_calendar_day(
                calendar_state["selected_day"],
                update_page=False,
            )
        if section_name == "notes":
            build_notes()
        if section_name == "dashboard":
            build_dashboard()
        page.update()

    dashboard_tab.on_click = lambda e: show_section("dashboard")
    today_tab.on_click = lambda e: show_section("today")
    history_tab.on_click = lambda e: show_section("history")
    notes_tab.on_click = lambda e: show_section("notes")

    build_notes()
    build_dashboard()

    page.add(
        ft.Container(
            content=ft.Column(
                controls=[
                    ft.Row(
                        controls=[
                            ft.Container(width=44),
                            ft.Text(
                                "THE PULSE",
                                size=36,
                                weight=ft.FontWeight.BOLD,
                                color="#F4F6FA",
                            ),
                            ft.IconButton(
                                icon=ft.Icons.HELP_OUTLINE,
                                tooltip="Help",
                                on_click=open_help,
                            ),
                            ft.IconButton(
                                icon=ft.Icons.SETTINGS_OUTLINED,
                                tooltip="Settings",
                                on_click=open_settings,
                            ),
                        ],
                        alignment=ft.MainAxisAlignment.CENTER,
                    ),
                    ft.Text(
                        "Keep it alive.",
                        size=14,
                        color="#72798A",
                    ),
                    series_management_row,
                    ft.Row(
                        controls=[dashboard_tab, today_tab, history_tab, notes_tab],
                        alignment=ft.MainAxisAlignment.CENTER,
                        spacing=8,
                        wrap=True,
                    ),
                    ft.Divider(color="#252B38"),
                    dashboard_view,
                    today_view,
                    history_view,
                    notes_view,
                ],
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                spacing=18,
            ),
            width=760,
            padding=24,
            bgcolor="#0F121A",
            border_radius=24,
        )
    )
    initial_screen = get_setting("default_screen") or "dashboard"
    show_section(initial_screen)

    waiting_names = [
        name
        for series_id, name in get_series()
        if (
            is_scheduled_day(series_id, date.today(), get_pulse_dates(series_id))
            and not pulse_exists_today(series_id)
        )
    ]
    reminder_dialog = ft.AlertDialog(
        modal=False,
        title=ft.Text("Today's pulses are waiting"),
        content=ft.Text(
            "Still waiting: " + ", ".join(waiting_names)
            if waiting_names
            else "All scheduled pulses are complete."
        ),
        actions=[
            ft.Button(content="Open Dashboard", on_click=lambda e: (
                page.pop_dialog(), show_section("dashboard")
            )),
        ],
    )

    def reminder_is_due():
        if (get_setting("reminders_enabled") or "1") != "1":
            return False
        reminder_text = get_setting("reminder_time") or "21:00"
        try:
            hour_value, minute_value = [int(value) for value in reminder_text.split(":")]
        except ValueError:
            return False
        now = __import__("datetime").datetime.now()
        return (
            (now.hour, now.minute) >= (hour_value, minute_value)
            and bool(waiting_names)
            and get_setting("last_reminder_date") != date.today().isoformat()
        )

    async def reminder_monitor():
        while True:
            await asyncio.sleep(60)
            if (get_setting("reminders_enabled") or "1") != "1":
                continue
            current_waiting = [
                name
                for series_id, name in get_series()
                if (
                    is_scheduled_day(
                        series_id, date.today(), get_pulse_dates(series_id)
                    )
                    and not pulse_exists_today(series_id)
                )
            ]
            if not current_waiting:
                continue
            reminder_text = get_setting("reminder_time") or "21:00"
            now = __import__("datetime").datetime.now()
            try:
                hour_value, minute_value = [
                    int(value) for value in reminder_text.split(":")
                ]
            except ValueError:
                continue
            if (
                (now.hour, now.minute) >= (hour_value, minute_value)
                and get_setting("last_reminder_date") != date.today().isoformat()
            ):
                save_setting("last_reminder_date", date.today().isoformat())
                send_native_notification(
                    "Today's pulses are waiting",
                    f"{len(current_waiting)} series still waiting: "
                    + ", ".join(current_waiting),
                )

    if get_setting("onboarding_completed") != "1":
        page.show_dialog(onboarding_dialog)
    elif reminder_is_due():
        save_setting("last_reminder_date", date.today().isoformat())
        notification_sent = send_native_notification(
            "Today's pulses are waiting",
            f"{len(waiting_names)} series still waiting: "
            + ", ".join(waiting_names),
        )
        if not notification_sent:
            page.show_dialog(reminder_dialog)
    page.run_task(reminder_monitor)


if __name__ == "__main__":
    ft.run(main)