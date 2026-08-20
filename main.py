import flet as ft
import flet.canvas as cv
import sqlite3
import calendar
import asyncio
import csv
import json
import random
import shutil
import subprocess
import sys

from datetime import date, datetime, timedelta
from pathlib import Path


DB_PATH = Path(__file__).with_name("pulse.db")
SERIES_NAME_MAX_LENGTH = 50
IMPORT_MAX_SERIES = 500
IMPORT_MAX_ITEMS = 100000
IMPORT_TEXT_MAX_LENGTH = 10000

TROPHY_DEFINITIONS = {
    "pulse_heart": ("Pulse Heart", "favorite", "#FF3158"),
    "phoenix": ("Phoenix", "local_fire_department", "#FF7A45"),
    "north_star": ("North Star", "star", "#FFC857"),
    "guardian": ("Guardian Shield", "shield", "#56C8FF"),
    "crystal": ("Signal Crystal", "auto_awesome", "#B58CFF"),
    "classic": ("Classic Trophy", "emoji_events", "#F4F6FA"),
}


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

        # Eski "haftada X kez" seçeneği hangi günün zorunlu olduğunu
        # belirleyemediği için kaldırıldı. Daha önce bu modu kullanan serilerde
        # işaretli günler korunarak açık gün planına dönüştürülür.
        connection.execute(
            """
            UPDATE series
            SET schedule_type = CASE
                    WHEN TRIM(COALESCE(schedule_days, '')) <> ''
                        THEN 'weekdays'
                    ELSE 'daily'
                END,
                schedule_days = CASE
                    WHEN TRIM(COALESCE(schedule_days, '')) <> ''
                        THEN schedule_days
                    ELSE '0,1,2,3,4,5,6'
                END
            WHERE schedule_type = 'weekly'
            """
        )

        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS app_settings (
                setting_key TEXT PRIMARY KEY,
                setting_value TEXT NOT NULL
            )
            """
        )

        # A schedule change must never reinterpret older pulse history. Each
        # row describes the weekly plan starting on effective_from, until a
        # newer row takes over.
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS series_schedule_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                series_id INTEGER NOT NULL,
                effective_from TEXT NOT NULL,
                schedule_type TEXT NOT NULL CHECK (
                    schedule_type IN ('daily', 'weekdays')
                ),
                schedule_days TEXT NOT NULL DEFAULT '0,1,2,3,4,5,6',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (series_id) REFERENCES series(id) ON DELETE CASCADE,
                UNIQUE (series_id, effective_from)
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_schedule_versions_lookup
            ON series_schedule_versions (series_id, effective_from DESC)
            """
        )

        # A date override has priority over the weekly plan. It can turn one
        # future date into either a required pulse day or an OFF DAY.
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS series_date_overrides (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                series_id INTEGER NOT NULL,
                override_date TEXT NOT NULL,
                is_scheduled INTEGER NOT NULL CHECK (is_scheduled IN (0, 1)),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (series_id) REFERENCES series(id) ON DELETE CASCADE,
                UNIQUE (series_id, override_date)
            )
            """
        )

        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS trophy_targets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                series_id INTEGER NOT NULL,
                series_name_snapshot TEXT NOT NULL,
                start_date TEXT NOT NULL,
                target_date TEXT NOT NULL,
                trophy_key TEXT NOT NULL,
                random_choice INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'active' CHECK (
                    status IN ('active', 'earned', 'failed', 'cancelled')
                ),
                completed_at TEXT,
                cancelled_at TEXT,
                celebrated_at TEXT,
                rest_count INTEGER,
                pulse_count INTEGER,
                planned_count INTEGER,
                frame TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (series_id) REFERENCES series(id) ON DELETE CASCADE
            )
            """
        )
        trophy_table_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'trophy_targets'"
        ).fetchone()[0]
        if "cancelled" not in trophy_table_sql.lower():
            connection.execute("DROP INDEX IF EXISTS idx_trophy_targets_series_status")
            connection.execute("ALTER TABLE trophy_targets RENAME TO trophy_targets_legacy")
            connection.execute(
                """
                CREATE TABLE trophy_targets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    series_id INTEGER NOT NULL,
                    series_name_snapshot TEXT NOT NULL,
                    start_date TEXT NOT NULL,
                    target_date TEXT NOT NULL,
                    trophy_key TEXT NOT NULL,
                    random_choice INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'active' CHECK (
                        status IN ('active', 'earned', 'failed', 'cancelled')
                    ),
                    completed_at TEXT,
                    cancelled_at TEXT,
                    celebrated_at TEXT,
                    rest_count INTEGER,
                    pulse_count INTEGER,
                    planned_count INTEGER,
                    frame TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (series_id) REFERENCES series(id) ON DELETE CASCADE
                )
                """
            )
            connection.execute(
                """
                INSERT INTO trophy_targets (
                    id, series_id, series_name_snapshot, start_date, target_date,
                    trophy_key, random_choice, status, completed_at, cancelled_at,
                    celebrated_at,
                    rest_count, pulse_count, planned_count, frame, created_at
                )
                SELECT id, series_id, series_name_snapshot, start_date, target_date,
                       trophy_key, random_choice, status, completed_at, NULL, NULL,
                       rest_count, pulse_count, planned_count, frame, created_at
                FROM trophy_targets_legacy
                """
            )
            connection.execute("DROP TABLE trophy_targets_legacy")
        trophy_columns = {
            row[1] for row in connection.execute(
                "PRAGMA table_info(trophy_targets)"
            ).fetchall()
        }
        if "celebrated_at" not in trophy_columns:
            connection.execute(
                "ALTER TABLE trophy_targets ADD COLUMN celebrated_at TEXT"
            )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_trophy_targets_series_status
            ON trophy_targets (series_id, status, target_date)
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS trophy_target_days (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                target_id INTEGER NOT NULL,
                required_date TEXT NOT NULL,
                FOREIGN KEY (target_id) REFERENCES trophy_targets(id) ON DELETE CASCADE,
                UNIQUE (target_id, required_date)
            )
            """
        )

        # Existing installations receive one historical baseline. Subsequent
        # changes are added as new versions instead of overwriting this row.
        connection.execute(
            """
            INSERT OR IGNORE INTO series_schedule_versions (
                series_id, effective_from, schedule_type, schedule_days
            )
            SELECT id,
                   DATE(created_at),
                   CASE WHEN schedule_type = 'weekdays' THEN 'weekdays' ELSE 'daily' END,
                   CASE
                       WHEN schedule_type = 'weekdays' THEN schedule_days
                       ELSE '0,1,2,3,4,5,6'
                   END
            FROM series
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
            connection.execute(
                """
                INSERT OR IGNORE INTO series_schedule_versions (
                    series_id, effective_from, schedule_type, schedule_days
                )
                SELECT id, ?, 'daily', '0,1,2,3,4,5,6'
                FROM series WHERE name = 'Daily Development'
                """,
                (date.today().isoformat(),),
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
            connection.execute(
                """
                INSERT OR IGNORE INTO series_schedule_versions (
                    series_id, effective_from, schedule_type, schedule_days
                )
                SELECT id, ?, 'daily', '0,1,2,3,4,5,6'
                FROM series WHERE name = 'Daily Development'
                """,
                (date.today().isoformat(),),
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


def get_schedule_for_day(series_id, day_value):
    """Return the schedule version that was active on a particular date."""
    with connect_database() as connection:
        row = connection.execute(
            """
            SELECT schedule_type, schedule_days, effective_from
            FROM series_schedule_versions
            WHERE series_id = ? AND effective_from <= ?
            ORDER BY effective_from DESC
            LIMIT 1
            """,
            (series_id, day_value.isoformat()),
        ).fetchone()
    if row:
        return row

    details = get_series_details(series_id)
    if not details:
        return "daily", "0,1,2,3,4,5,6", day_value.isoformat()
    schedule_type = "weekdays" if details[4] == "weekdays" else "daily"
    schedule_days = details[5] if schedule_type == "weekdays" else "0,1,2,3,4,5,6"
    return schedule_type, schedule_days, day_value.isoformat()


def save_series_schedule(series_id, effective_from, schedule_type, schedule_days):
    if schedule_type not in {"daily", "weekdays"}:
        raise ValueError("Choose a valid schedule type.")
    parsed_date = date.fromisoformat(str(effective_from))
    if parsed_date < date.today():
        raise ValueError("A new schedule cannot start in the past.")
    normalized_days = "0,1,2,3,4,5,6" if schedule_type == "daily" else schedule_days
    if schedule_type == "weekdays" and not normalized_days:
        raise ValueError("Select at least one weekday.")

    with connect_database() as connection:
        connection.execute(
            """
            INSERT INTO series_schedule_versions (
                series_id, effective_from, schedule_type, schedule_days
            ) VALUES (?, ?, ?, ?)
            ON CONFLICT(series_id, effective_from) DO UPDATE SET
                schedule_type = excluded.schedule_type,
                schedule_days = excluded.schedule_days,
                created_at = CURRENT_TIMESTAMP
            """,
            (series_id, parsed_date.isoformat(), schedule_type, normalized_days),
        )
        # Keep legacy columns populated for backward-compatible exports and
        # older app versions. Date-aware calculations use the version table.
        connection.execute(
            """
            UPDATE series
            SET schedule_type = ?, schedule_days = ?, weekly_target = 7
            WHERE id = ?
            """,
            (schedule_type, normalized_days, series_id),
        )


def get_schedule_versions(series_id):
    with connect_database() as connection:
        return connection.execute(
            """
            SELECT effective_from, schedule_type, schedule_days
            FROM series_schedule_versions
            WHERE series_id = ?
            ORDER BY effective_from DESC
            """,
            (series_id,),
        ).fetchall()


def set_date_override(series_id, override_date, is_scheduled):
    parsed_date = date.fromisoformat(str(override_date))
    if parsed_date < date.today():
        raise ValueError("Past days cannot be changed.")
    with connect_database() as connection:
        connection.execute(
            """
            INSERT INTO series_date_overrides (
                series_id, override_date, is_scheduled
            ) VALUES (?, ?, ?)
            ON CONFLICT(series_id, override_date) DO UPDATE SET
                is_scheduled = excluded.is_scheduled,
                created_at = CURRENT_TIMESTAMP
            """,
            (series_id, parsed_date.isoformat(), int(bool(is_scheduled))),
        )


def set_date_overrides(series_id, override_dates, is_scheduled):
    parsed_dates = sorted({
        date.fromisoformat(str(override_date))
        for override_date in override_dates
    })
    if not parsed_dates:
        raise ValueError("Select at least one exception date.")
    if parsed_dates[0] < date.today():
        raise ValueError("Past days cannot be changed.")
    with connect_database() as connection:
        connection.executemany(
            """
            INSERT INTO series_date_overrides (
                series_id, override_date, is_scheduled
            ) VALUES (?, ?, ?)
            ON CONFLICT(series_id, override_date) DO UPDATE SET
                is_scheduled = excluded.is_scheduled,
                created_at = CURRENT_TIMESTAMP
            """,
            [
                (series_id, picked_date.isoformat(), int(bool(is_scheduled)))
                for picked_date in parsed_dates
            ],
        )

def set_mixed_date_overrides(series_id, assignments):
    """Save required and free-day exceptions together in one transaction."""
    if not assignments:
        raise ValueError("Mark at least one date before saving.")
    parsed = []
    for raw_date, is_scheduled in assignments.items():
        parsed_date = date.fromisoformat(str(raw_date))
        if parsed_date < date.today():
            raise ValueError("Past days cannot be changed.")
        parsed.append(
            (series_id, parsed_date.isoformat(), int(bool(is_scheduled)))
        )
    with connect_database() as connection:
        connection.executemany(
            """
            INSERT INTO series_date_overrides (
                series_id, override_date, is_scheduled
            ) VALUES (?, ?, ?)
            ON CONFLICT(series_id, override_date) DO UPDATE SET
                is_scheduled = excluded.is_scheduled,
                created_at = CURRENT_TIMESTAMP
            """,
            parsed,
        )


def create_trophy_target(series_id, target_date, trophy_key):
    parsed_target = date.fromisoformat(str(target_date))
    if parsed_target <= date.today():
        raise ValueError("Choose a trophy target after today.")
    random_choice = trophy_key == "random"
    if random_choice:
        trophy_key = random.choice(list(TROPHY_DEFINITIONS))
    if trophy_key not in TROPHY_DEFINITIONS:
        raise ValueError("Choose a valid trophy.")

    start_day = date.today()
    required_dates = []
    current_day = start_day
    while current_day <= parsed_target:
        if is_scheduled_day(series_id, current_day):
            required_dates.append(current_day.isoformat())
        current_day += timedelta(days=1)
    if not required_dates:
        raise ValueError("This target has no planned Pulse days.")

    with connect_database() as connection:
        active = connection.execute(
            "SELECT 1 FROM trophy_targets WHERE series_id = ? AND status = 'active'",
            (series_id,),
        ).fetchone()
        if active:
            raise ValueError("This series already has an active trophy target.")
        series_row = connection.execute(
            "SELECT name FROM series WHERE id = ?",
            (series_id,),
        ).fetchone()
        if series_row is None:
            raise ValueError("Series not found.")
        cursor = connection.execute(
            """
            INSERT INTO trophy_targets (
                series_id, series_name_snapshot, start_date, target_date,
                trophy_key, random_choice
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                series_id,
                series_row[0],
                start_day.isoformat(),
                parsed_target.isoformat(),
                trophy_key,
                int(random_choice),
            ),
        )
        connection.executemany(
            """
            INSERT INTO trophy_target_days (target_id, required_date)
            VALUES (?, ?)
            """,
            [(cursor.lastrowid, required_date) for required_date in required_dates],
        )


def cancel_active_trophy_target(target_id, series_id):
    with connect_database() as connection:
        connection.execute(
            """
            UPDATE trophy_targets
            SET status = 'cancelled', cancelled_at = CURRENT_TIMESTAMP,
                completed_at = CURRENT_TIMESTAMP
            WHERE id = ? AND series_id = ? AND status = 'active'
            """,
            (target_id, series_id),
        )


def get_target_required_dates(target_id, series_id, start_date, target_date):
    with connect_database() as connection:
        rows = connection.execute(
            """
            SELECT required_date FROM trophy_target_days
            WHERE target_id = ? ORDER BY required_date
            """,
            (target_id,),
        ).fetchall()
    if rows:
        return [date.fromisoformat(row[0]) for row in rows]

    # Existing targets created before schema v3 receive a one-time snapshot.
    required_dates = []
    current_day = start_date
    while current_day <= target_date:
        if is_scheduled_day(series_id, current_day):
            required_dates.append(current_day)
        current_day += timedelta(days=1)
    with connect_database() as connection:
        connection.executemany(
            """
            INSERT OR IGNORE INTO trophy_target_days (target_id, required_date)
            VALUES (?, ?)
            """,
            [(target_id, day_value.isoformat()) for day_value in required_dates],
        )
    return required_dates


def _trophy_target_metrics(series_id, start_date, end_date, target_id=None):
    pulses = set(get_pulse_dates(series_id, include_inactive=True))
    frozen_required_dates = (
        set(get_target_required_dates(target_id, series_id, start_date, end_date))
        if target_id is not None else None
    )
    planned_count = 0
    pulse_count = 0
    rest_count = 0
    consecutive_rests = 0
    flatlined = False
    current_day = start_date
    while current_day <= end_date:
        is_required = (
            current_day in frozen_required_dates
            if frozen_required_dates is not None
            else is_scheduled_day(series_id, current_day, pulses)
        )
        if is_required:
            planned_count += 1
            if current_day in pulses:
                pulse_count += 1
                consecutive_rests = 0
            else:
                rest_count += 1
                consecutive_rests += 1
                if consecutive_rests >= 2:
                    flatlined = True
        current_day += timedelta(days=1)
    return planned_count, pulse_count, rest_count, flatlined


def refresh_trophy_target_states(series_id=None):
    with connect_database() as connection:
        if series_id is None:
            rows = connection.execute(
                """
                SELECT id, series_id, start_date, target_date
                FROM trophy_targets WHERE status = 'active'
                """
            ).fetchall()
        else:
            rows = connection.execute(
                """
                SELECT id, series_id, start_date, target_date
                FROM trophy_targets
                WHERE status = 'active' AND series_id = ?
                """,
                (series_id,),
            ).fetchall()

    today = date.today()
    for target_id, target_series_id, start_text, target_text in rows:
        start_day = date.fromisoformat(start_text)
        target_day = date.fromisoformat(target_text)
        frozen_required_dates = set(
            get_target_required_dates(
                target_id, target_series_id, start_day, target_day
            )
        )
        raw_pulses = set(
            get_pulse_dates(target_series_id, include_inactive=True)
        )
        last_finished_day = min(target_day, today - timedelta(days=1))
        target_complete_today = (
            target_day == today
            and (
                target_day not in frozen_required_dates
                or today in raw_pulses
            )
        )
        evaluation_end = target_day if target_complete_today else last_finished_day
        if evaluation_end < start_day:
            continue

        planned, pulses, rests, flatlined = _trophy_target_metrics(
            target_series_id, start_day, evaluation_end, target_id
        )
        if flatlined:
            status = "failed"
            frame = None
        elif target_day < today or target_complete_today:
            status = "earned"
            frame = (
                "gold" if rests == 0
                else "silver" if rests == 1
                else "bronze" if rests == 2
                else "none"
            )
        else:
            continue

        with connect_database() as connection:
            connection.execute(
                """
                UPDATE trophy_targets
                SET status = ?, completed_at = CURRENT_TIMESTAMP,
                    rest_count = ?, pulse_count = ?, planned_count = ?, frame = ?
                WHERE id = ? AND status = 'active'
                """,
                (status, rests, pulses, planned, frame, target_id),
            )


def get_trophy_targets(series_id):
    refresh_trophy_target_states(series_id)
    with connect_database() as connection:
        return connection.execute(
            """
            SELECT id, series_name_snapshot, start_date, target_date,
                   trophy_key, random_choice, status, completed_at,
                   rest_count, pulse_count, planned_count, frame
            FROM trophy_targets
            WHERE series_id = ?
            ORDER BY CASE status WHEN 'active' THEN 0 ELSE 1 END,
                     target_date DESC, id DESC
            """,
            (series_id,),
        ).fetchall()


def get_earned_trophies(series_id=None):
    query = """
        SELECT id, series_id, series_name_snapshot, start_date, target_date,
               trophy_key, completed_at, rest_count, pulse_count,
               planned_count, frame
        FROM trophy_targets
        WHERE status = 'earned'
    """
    parameters = []
    if series_id is not None:
        query += " AND series_id = ?"
        parameters.append(series_id)
    query += " ORDER BY COALESCE(completed_at, target_date) DESC, id DESC"
    with connect_database() as connection:
        return connection.execute(query, parameters).fetchall()


def get_uncelebrated_earned_trophy():
    with connect_database() as connection:
        return connection.execute(
            """
            SELECT id, series_id, series_name_snapshot, start_date, target_date,
                   trophy_key, completed_at, rest_count, pulse_count,
                   planned_count, frame
            FROM trophy_targets
            WHERE status = 'earned' AND celebrated_at IS NULL
            ORDER BY COALESCE(completed_at, target_date), id
            LIMIT 1
            """
        ).fetchone()


def mark_trophy_celebrated(target_id):
    with connect_database() as connection:
        connection.execute(
            """
            UPDATE trophy_targets
            SET celebrated_at = CURRENT_TIMESTAMP
            WHERE id = ? AND status = 'earned'
            """,
            (target_id,),
        )


def create_trophy_share_card(target_id):
    """Render an earned trophy as a social-media friendly PNG card."""
    with connect_database() as connection:
        row = connection.execute(
            """
            SELECT series_name_snapshot, start_date, target_date, trophy_key,
                   rest_count, pulse_count, planned_count, frame
            FROM trophy_targets
            WHERE id = ? AND status = 'earned'
            """,
            (target_id,),
        ).fetchone()
    if row is None:
        raise ValueError("Only earned trophies can be shared.")

    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError as error:
        raise ValueError(
            "PNG share cards require Pillow. Install it with: pip install pillow"
        ) from error

    series_name, start_text, target_text, trophy_key, rests, pulses, planned, frame = row
    trophy_name, _, trophy_color = TROPHY_DEFINITIONS.get(
        trophy_key, TROPHY_DEFINITIONS["classic"]
    )
    frame_colors = {
        "gold": "#FFD45C", "silver": "#C9D0DA",
        "bronze": "#C88752", "none": "#343A48", None: "#343A48",
    }
    image = Image.new("RGB", (1200, 630), "#090B10")
    draw = ImageDraw.Draw(image)
    font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    bold_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    title_font = ImageFont.truetype(bold_path, 54)
    trophy_font = ImageFont.truetype(bold_path, 44)
    body_font = ImageFont.truetype(font_path, 28)
    small_font = ImageFont.truetype(bold_path, 22)

    draw.rounded_rectangle((35, 35, 1165, 595), radius=38, fill="#11151F",
                           outline=frame_colors.get(frame, "#343A48"), width=8)
    draw.text((75, 70), "THE PULSE", font=title_font, fill="#F4F6FA")
    draw.text((78, 139), "TROPHY EARNED", font=small_font, fill="#56C8FF")

    color = trophy_color
    draw.ellipse((105, 225, 325, 445), fill="#151923", outline=color, width=6)
    draw.rounded_rectangle((165, 260, 265, 355), radius=15, fill=color)
    draw.polygon([(165, 280), (125, 265), (135, 335), (180, 355)], fill=color)
    draw.polygon([(265, 280), (305, 265), (295, 335), (250, 355)], fill=color)
    draw.rectangle((202, 350, 228, 395), fill=color)
    draw.rounded_rectangle((165, 392, 265, 418), radius=10, fill=color)

    draw.text((375, 225), trophy_name, font=trophy_font, fill="#F4F6FA")
    draw.text((378, 292), series_name, font=body_font, fill="#A9B0BF")
    frame_label = "FRAMELESS" if frame in {None, "none"} else f"{frame.upper()} FRAME"
    draw.text((378, 348), frame_label, font=small_font,
              fill=frame_colors.get(frame, "#A9B0BF"))
    draw.text((378, 397), f"{pulses or 0}/{planned or 0} PULSES  •  {rests or 0} REST",
              font=body_font, fill="#F4F6FA")
    draw.text((378, 452), f"{start_text}  →  {target_text}", font=body_font,
              fill="#8D95A5")
    draw.text((78, 540), "Keep it alive.", font=body_font, fill="#FF3158")

    export_dir = DB_PATH.with_name("exports")
    export_dir.mkdir(exist_ok=True)
    path = export_dir / f"trophy-{target_id}-{_safe_export_name(trophy_name)}.png"
    image.save(path, "PNG")
    return path


def get_active_trophy_target(series_id):
    rows = get_trophy_targets(series_id)
    return next((row for row in rows if row[6] == "active"), None)


def get_active_trophy_progress(series_id):
    target = get_active_trophy_target(series_id)
    if target is None:
        return None

    (
        target_id, series_snapshot, start_text, target_text,
        trophy_key, random_choice, status, completed_at,
        stored_rests, stored_pulses, stored_planned, stored_frame,
    ) = target
    start_day = date.fromisoformat(start_text)
    target_day = date.fromisoformat(target_text)
    pulse_set = set(get_pulse_dates(series_id, include_inactive=True))
    required_date_set = set(
        get_target_required_dates(target_id, series_id, start_day, target_day)
    )
    today = date.today()

    planned_total = 0
    pulse_count = 0
    rest_count = 0
    current_day = start_day
    while current_day <= target_day:
        if current_day in required_date_set:
            planned_total += 1
            if current_day <= today and current_day in pulse_set:
                pulse_count += 1
            elif current_day < today:
                rest_count += 1
        current_day += timedelta(days=1)

    required_pulses = max(pulse_count, planned_total - rest_count)
    remaining_pulses = max(0, required_pulses - pulse_count)
    progress = (
        min(1.0, pulse_count / required_pulses)
        if required_pulses > 0 else 0.0
    )
    frame = (
        "gold" if rest_count == 0
        else "silver" if rest_count == 1
        else "bronze" if rest_count == 2
        else "none"
    )
    return {
        "id": target_id,
        "series_name": series_snapshot,
        "start_date": start_day,
        "target_date": target_day,
        "trophy_key": trophy_key,
        "random_choice": bool(random_choice),
        "planned_total": planned_total,
        "pulse_count": pulse_count,
        "rest_count": rest_count,
        "required_pulses": required_pulses,
        "remaining_pulses": remaining_pulses,
        "calendar_days_left": max(0, (target_day - today).days),
        "progress": progress,
        "frame": frame,
    }


def delete_date_override(series_id, override_date):
    parsed_date = date.fromisoformat(str(override_date))
    if parsed_date < date.today():
        raise ValueError("Past date exceptions are locked and cannot be removed.")
    with connect_database() as connection:
        connection.execute(
            """
            DELETE FROM series_date_overrides
            WHERE series_id = ? AND override_date = ?
            """,
            (series_id, parsed_date.isoformat()),
        )


def get_date_overrides(series_id, from_date=None):
    with connect_database() as connection:
        if from_date is None:
            return connection.execute(
                """
                SELECT override_date, is_scheduled
                FROM series_date_overrides
                WHERE series_id = ?
                ORDER BY override_date
                """,
                (series_id,),
            ).fetchall()
        return connection.execute(
            """
            SELECT override_date, is_scheduled
            FROM series_date_overrides
            WHERE series_id = ? AND override_date >= ?
            ORDER BY override_date
            """,
            (series_id, from_date.isoformat()),
        ).fetchall()


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
                    f"## {day_text} - {'PULSE' if has_pulse else 'NO PULSE'}\n\n"
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
            schedule_history = [
                {
                    "effective_from": effective_from,
                    "type": schedule_type,
                    "days": schedule_days,
                }
                for effective_from, schedule_type, schedule_days
                in connection.execute(
                    """
                    SELECT effective_from, schedule_type, schedule_days
                    FROM series_schedule_versions
                    WHERE series_id = ?
                    ORDER BY effective_from
                    """,
                    (row[0],),
                ).fetchall()
            ]
            date_overrides = [
                {
                    "date": override_date,
                    "is_scheduled": bool(is_scheduled),
                }
                for override_date, is_scheduled in connection.execute(
                    """
                    SELECT override_date, is_scheduled
                    FROM series_date_overrides
                    WHERE series_id = ?
                    ORDER BY override_date
                    """,
                    (row[0],),
                ).fetchall()
            ]
            trophy_targets = []
            for target in connection.execute(
                """
                SELECT id, series_name_snapshot, start_date, target_date,
                       trophy_key, random_choice, status, completed_at,
                       cancelled_at, rest_count, pulse_count, planned_count,
                       frame, created_at
                FROM trophy_targets
                WHERE series_id = ? ORDER BY id
                """,
                (row[0],),
            ).fetchall():
                required_dates = [
                    required_date
                    for (required_date,) in connection.execute(
                        """
                        SELECT required_date FROM trophy_target_days
                        WHERE target_id = ? ORDER BY required_date
                        """,
                        (target[0],),
                    ).fetchall()
                ]
                trophy_targets.append(
                    {
                        "series_name_snapshot": target[1],
                        "start_date": target[2],
                        "target_date": target[3],
                        "trophy_key": target[4],
                        "random_choice": bool(target[5]),
                        "status": target[6],
                        "completed_at": target[7],
                        "cancelled_at": target[8],
                        "rest_count": target[9],
                        "pulse_count": target[10],
                        "planned_count": target[11],
                        "frame": target[12],
                        "created_at": target[13],
                        "required_dates": required_dates,
                    }
                )
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
                    "schedule_history": schedule_history,
                    "date_overrides": date_overrides,
                    "trophy_targets": trophy_targets,
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
        "schema_version": 3,
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


def _validated_import_date(value, label, allow_future=True):
    try:
        parsed = date.fromisoformat(str(value))
    except (TypeError, ValueError) as error:
        raise ValueError(f"The export contains an invalid {label}.") from error
    if not allow_future and parsed > date.today():
        raise ValueError("The export contains a pulse dated in the future.")
    return parsed


def _validated_schedule(schedule_type, schedule_days):
    if schedule_type not in {"daily", "weekdays"}:
        raise ValueError("The export contains an invalid schedule type.")
    raw_days = str(schedule_days or "0,1,2,3,4,5,6").split(",")
    try:
        days = [int(value.strip()) for value in raw_days]
    except ValueError as error:
        raise ValueError("The export contains invalid pulse days.") from error
    if not days or len(days) != len(set(days)) or any(day not in range(7) for day in days):
        raise ValueError("The export contains invalid pulse days.")
    return ",".join(str(day) for day in sorted(days))


def _bounded_import_text(value, label, maximum=IMPORT_TEXT_MAX_LENGTH):
    text = str(value or "")
    if len(text) > maximum:
        raise ValueError(f"The export contains a {label} that is too long.")
    return text


def _validated_import_timestamp(value, label):
    if not value:
        return None
    text = str(value)
    try:
        datetime.fromisoformat(text)
    except ValueError as error:
        raise ValueError(f"The export contains an invalid {label}.") from error
    return text


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
        or payload.get("schema_version") not in {1, 2, 3}
        or not isinstance(payload.get("series"), list)
    ):
        raise ValueError("This file is not a supported The Pulse export.")
    if not payload["series"] or len(payload["series"]) > IMPORT_MAX_SERIES:
        raise ValueError("The export contains an invalid number of series.")

    item_count = 0
    for item in payload["series"]:
        if not isinstance(item, dict):
            raise ValueError("The export contains invalid series data.")
        for key in ("schedule_history", "date_overrides", "pulses", "notes", "trophy_targets"):
            values = item.get(key) or []
            if not isinstance(values, list):
                raise ValueError(f"The export contains invalid {key} data.")
            item_count += len(values)
    if item_count > IMPORT_MAX_ITEMS:
        raise ValueError("The export is too large to import safely.")

    imported_names = []
    with connect_database() as connection:
        for item in payload["series"]:
            if not isinstance(item, dict):
                raise ValueError("The export contains invalid series data.")
            schedule = item.get("schedule") or {}
            if not isinstance(schedule, dict):
                raise ValueError("The export contains invalid schedule data.")
            schedule_type = schedule.get("type") or "daily"
            schedule_days = _validated_schedule(
                schedule_type, schedule.get("days") or "0,1,2,3,4,5,6"
            )
            try:
                weekly_target = int(schedule.get("weekly_target") or len(schedule_days.split(",")))
            except (TypeError, ValueError) as error:
                raise ValueError("The export contains an invalid weekly target.") from error
            if weekly_target not in range(1, 8):
                raise ValueError("The export contains an invalid weekly target.")
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
                    _bounded_import_text(item.get("description"), "description"),
                    _bounded_import_text(item.get("goal"), "goal"),
                    schedule_type,
                    schedule_days,
                    weekly_target,
                ),
            )
            new_series_id = cursor.lastrowid
            schedule_history = item.get("schedule_history") or []
            if not schedule_history:
                pulse_values = item.get("pulses") or []
                fallback_start = (
                    min(str(value) for value in pulse_values)
                    if pulse_values
                    else date.today().isoformat()
                )
                schedule_history = [
                    {
                        "effective_from": fallback_start,
                        "type": schedule.get("type") or "daily",
                        "days": schedule.get("days") or "0,1,2,3,4,5,6",
                    }
                ]
            for version in schedule_history:
                if not isinstance(version, dict):
                    raise ValueError("The export contains invalid schedule history.")
                effective_from = _validated_import_date(
                    version.get("effective_from"), "schedule date"
                ).isoformat()
                version_type = version.get("type") or "daily"
                version_days = _validated_schedule(
                    version_type, version.get("days") or "0,1,2,3,4,5,6"
                )
                connection.execute(
                    """
                    INSERT OR REPLACE INTO series_schedule_versions (
                        series_id, effective_from, schedule_type, schedule_days
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (new_series_id, effective_from, version_type, version_days),
                )
            for override in item.get("date_overrides") or []:
                if not isinstance(override, dict):
                    raise ValueError("The export contains invalid date exceptions.")
                override_date = _validated_import_date(
                    override.get("date"), "exception date"
                ).isoformat()
                connection.execute(
                    """
                    INSERT OR REPLACE INTO series_date_overrides (
                        series_id, override_date, is_scheduled
                    ) VALUES (?, ?, ?)
                    """,
                    (
                        new_series_id,
                        override_date,
                        int(bool(override.get("is_scheduled"))),
                    ),
                )
            for pulse_date in item.get("pulses") or []:
                pulse_date = _validated_import_date(
                    pulse_date, "pulse date", allow_future=False
                ).isoformat()
                connection.execute(
                    "INSERT OR IGNORE INTO pulse_entries "
                    "(series_id, pulse_date, note) VALUES (?, ?, '')",
                    (new_series_id, pulse_date),
                )
            for note_item in item.get("notes") or []:
                if not isinstance(note_item, dict):
                    raise ValueError("The export contains invalid notes.")
                note_date = _validated_import_date(
                    note_item.get("date"), "note date"
                ).isoformat()
                connection.execute(
                    """
                    INSERT INTO daily_notes (series_id, note_date, note)
                    VALUES (?, ?, ?)
                    ON CONFLICT(series_id, note_date) DO UPDATE SET
                        note = excluded.note
                    """,
                    (
                        new_series_id,
                        note_date,
                        _bounded_import_text(note_item.get("note"), "note"),
                    ),
                )
            active_target_seen = False
            for target in item.get("trophy_targets") or []:
                if not isinstance(target, dict):
                    raise ValueError("The export contains invalid trophy target data.")
                start_date = _validated_import_date(
                    target.get("start_date"), "trophy start date"
                )
                target_date = _validated_import_date(
                    target.get("target_date"), "trophy target date"
                )
                if target_date < start_date:
                    raise ValueError("A trophy target ends before it starts.")
                trophy_key = target.get("trophy_key")
                if trophy_key not in TROPHY_DEFINITIONS:
                    raise ValueError("The export contains an unknown trophy.")
                status = target.get("status") or "failed"
                if status not in {"active", "earned", "failed", "cancelled"}:
                    raise ValueError("The export contains an invalid trophy status.")
                if status == "active" and active_target_seen:
                    raise ValueError("A series cannot contain two active trophy targets.")
                active_target_seen = active_target_seen or status == "active"
                frame = target.get("frame")
                if frame not in {None, "gold", "silver", "bronze", "none"}:
                    raise ValueError("The export contains an invalid trophy frame.")
                numeric_values = []
                for key in ("rest_count", "pulse_count", "planned_count"):
                    value = target.get(key)
                    if value is None:
                        numeric_values.append(None)
                    else:
                        try:
                            value = int(value)
                        except (TypeError, ValueError) as error:
                            raise ValueError("The export contains invalid trophy totals.") from error
                        if value < 0:
                            raise ValueError("The export contains invalid trophy totals.")
                        numeric_values.append(value)
                completed_at = _validated_import_timestamp(
                    target.get("completed_at"), "completion time"
                )
                cancelled_at = _validated_import_timestamp(
                    target.get("cancelled_at"), "cancellation time"
                )
                cursor = connection.execute(
                    """
                    INSERT INTO trophy_targets (
                        series_id, series_name_snapshot, start_date, target_date,
                        trophy_key, random_choice, status, completed_at,
                        cancelled_at, rest_count, pulse_count, planned_count,
                        frame, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        new_series_id,
                        _bounded_import_text(
                            target.get("series_name_snapshot") or imported_name,
                            "series name snapshot", SERIES_NAME_MAX_LENGTH
                        ),
                        start_date.isoformat(), target_date.isoformat(), trophy_key,
                        int(bool(target.get("random_choice"))), status, completed_at,
                        cancelled_at, *numeric_values, frame,
                        str(target.get("created_at") or datetime.now().isoformat(timespec="seconds")),
                    ),
                )
                required_dates = target.get("required_dates") or []
                if not isinstance(required_dates, list):
                    raise ValueError("The export contains invalid trophy target days.")
                normalized_dates = []
                for required_date in required_dates:
                    parsed = _validated_import_date(required_date, "trophy target day")
                    if parsed < start_date or parsed > target_date:
                        raise ValueError("A trophy target day is outside its target range.")
                    normalized_dates.append(parsed.isoformat())
                if len(normalized_dates) != len(set(normalized_dates)):
                    raise ValueError("The export contains duplicate trophy target days.")
                connection.executemany(
                    "INSERT INTO trophy_target_days (target_id, required_date) VALUES (?, ?)",
                    [(cursor.lastrowid, value) for value in normalized_dates],
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

        series_id = cursor.lastrowid
        connection.execute(
            """
            INSERT INTO series_schedule_versions (
                series_id, effective_from, schedule_type, schedule_days
            ) VALUES (?, ?, 'daily', '0,1,2,3,4,5,6')
            """,
            (series_id, date.today().isoformat()),
        )
        return series_id


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


def get_pulse_dates(series_id, include_inactive=False):
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

    pulse_dates = [date.fromisoformat(row[0]) for row in rows]
    if include_inactive:
        return pulse_dates

    # Keep the physical record when a completed day is later changed to an
    # OFF DAY, but exclude it from every visible statistic and timeline. If
    # that date becomes a pulse day again, the stored completion reappears.
    return [
        pulse_day
        for pulse_day in pulse_dates
        if is_scheduled_day(series_id, pulse_day)
    ]


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
    if not is_scheduled_day(series_id, date.today()):
        return False
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
    with connect_database() as connection:
        override = connection.execute(
            """
            SELECT is_scheduled
            FROM series_date_overrides
            WHERE series_id = ? AND override_date = ?
            """,
            (series_id, day_value.isoformat()),
        ).fetchone()
    if override is not None:
        return bool(override[0])

    schedule_type, schedule_days, _ = get_schedule_for_day(series_id, day_value)
    if schedule_type == "daily":
        return True
    if schedule_type == "weekdays":
        selected = {int(value) for value in (schedule_days or "").split(",") if value}
        return day_value.weekday() in selected
    return True


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
        if (
            series_id is not None
            and not is_scheduled_day(series_id, date.today(), pulse_dates)
        ):
            return "ALIVE", "OFF DAY - no pulse is planned for today."
        return "NO PULSE", "This series has not received a pulse yet."

    today = date.today()
    last_pulse_date = pulse_dates[-1]

    if last_pulse_date == today:
        return "ALIVE", "Today's pulse is alive."

    # Bugün henüz bitmediği için yalnızca tamamen kaçırılmış
    # günleri sayıyoruz.
    missed_days = (
        scheduled_misses_between(
            series_id, last_pulse_date, today, pulse_dates
        )
        if series_id is not None
        else max(0, (today - last_pulse_date).days - 1)
    )

    today_is_scheduled = (
        series_id is None
        or is_scheduled_day(series_id, today, pulse_dates)
    )

    # OFF DAY zinciri kendi başına değiştirmez. Ancak daha önce kaçırılmış
    # planlı günler varsa mevcut REST / FLATLINE durumu korunur.
    if missed_days == 0 and not today_is_scheduled:
        return "ALIVE", "OFF DAY - your chain is safe today."

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

    def heart_button_content(label):
        return ft.Row(
            controls=[
                ft.Icon(ft.Icons.FAVORITE, size=18),
                ft.Text(label, weight=ft.FontWeight.BOLD),
            ],
            tight=True,
            spacing=7,
            alignment=ft.MainAxisAlignment.CENTER,
        )

    pulse_heart = ft.Icon(
        ft.Icons.FAVORITE,
        size=72,
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

    frame_colors = {
        "gold": "#FFD45C",
        "silver": "#C9D0DA",
        "bronze": "#C88752",
        "none": "#343A48",
    }

    def target_trophy_icon(icon_name):
        icons = {
            "favorite": ft.Icons.FAVORITE,
            "local_fire_department": ft.Icons.LOCAL_FIRE_DEPARTMENT,
            "star": ft.Icons.STAR,
            "shield": ft.Icons.SHIELD_OUTLINED,
            "auto_awesome": ft.Icons.AUTO_AWESOME,
            "emoji_events": ft.Icons.EMOJI_EVENTS,
        }
        return icons.get(icon_name, ft.Icons.EMOJI_EVENTS)

    today_target_icon = ft.Icon(ft.Icons.EMOJI_EVENTS, size=42, color="#FFC857")
    today_target_name = ft.Text(weight=ft.FontWeight.BOLD, size=15)
    today_target_details = ft.Text(size=12, color="#A9B0BF")
    today_target_card = ft.Container(
        content=ft.Row(
            controls=[
                today_target_icon,
                ft.Column(
                    controls=[today_target_name, today_target_details],
                    spacing=3,
                    expand=True,
                ),
            ],
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        ),
        width=700,
        padding=14,
        bgcolor="#151923",
        border=ft.Border.all(1, "#343A48"),
        border_radius=14,
        visible=False,
    )

    history_target_icon = ft.Icon(ft.Icons.EMOJI_EVENTS, size=64, color="#72798A")
    history_target_title = ft.Text(
        "NO ACTIVE TROPHY TARGET", size=16, weight=ft.FontWeight.BOLD
    )
    history_target_status = ft.Text(size=12, color="#A9B0BF")
    history_target_numbers = ft.Text(size=12, color="#D7DBE5")
    history_target_frame = ft.Text(size=11, weight=ft.FontWeight.BOLD)
    history_target_progress = ft.ProgressBar(
        value=0,
        color="#FFC857",
        bgcolor="#252B38",
        height=8,
    )
    history_create_target_button = ft.Button(
        content="CREATE TROPHY TARGET",
        icon=ft.Icons.EMOJI_EVENTS,
        on_click=lambda e: open_series_profile(e),
    )
    history_target_card = ft.Container(
        content=ft.Column(
            controls=[
                ft.Row(
                    controls=[
                        history_target_icon,
                        ft.Column(
                            controls=[
                                history_target_title,
                                history_target_status,
                                history_target_frame,
                            ],
                            spacing=3,
                            expand=True,
                        ),
                    ],
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                history_target_progress,
                history_target_numbers,
                history_create_target_button,
            ],
            spacing=10,
        ),
        width=674,
        padding=18,
        bgcolor="#151923",
        border=ft.Border.all(1, "#343A48"),
        border_radius=16,
    )

    def refresh_target_cards():
        progress = get_active_trophy_progress(selected_series_id())
        if progress is None:
            today_target_card.visible = False
            history_target_icon.name = ft.Icons.EMOJI_EVENTS
            history_target_icon.color = "#72798A"
            history_target_title.value = "NO ACTIVE TROPHY TARGET"
            history_target_status.value = (
                "Choose a future date and keep the signal alive to earn a trophy."
            )
            history_target_frame.value = ""
            history_target_numbers.value = ""
            history_target_progress.value = 0
            history_target_progress.visible = False
            history_create_target_button.visible = True
            history_target_card.border = ft.Border.all(1, "#343A48")
            return

        trophy_name, icon_name, trophy_color = TROPHY_DEFINITIONS.get(
            progress["trophy_key"], TROPHY_DEFINITIONS["classic"]
        )
        frame = progress["frame"]
        frame_color = frame_colors[frame]
        frame_label = "FRAMELESS" if frame == "none" else f"{frame.upper()} FRAME"

        today_target_card.visible = True
        today_target_card.border = ft.Border.all(2, frame_color)
        today_target_icon.name = target_trophy_icon(icon_name)
        today_target_icon.color = trophy_color
        today_target_name.value = trophy_name
        today_target_details.value = (
            f"{progress['calendar_days_left']} calendar days left | "
            f"{progress['pulse_count']}/{progress['required_pulses']} Pulses | "
            f"{progress['remaining_pulses']} remaining\n"
            f"{progress['planned_total']} planned days | "
            f"{progress['rest_count']} REST | {frame_label}"
        )

        history_target_icon.name = target_trophy_icon(icon_name)
        history_target_icon.color = trophy_color
        history_target_title.value = trophy_name
        history_target_status.value = (
            f"{progress['start_date'].isoformat()} to "
            f"{progress['target_date'].isoformat()} | "
            f"{progress['calendar_days_left']} calendar days left"
        )
        history_target_frame.value = (
            f"CURRENT REWARD: {frame_label} | "
            f"{progress['rest_count']} REST"
        )
        history_target_frame.color = frame_color
        history_target_numbers.value = (
            f"Progress: {progress['pulse_count']} of "
            f"{progress['required_pulses']} required Pulses "
            f"({round(progress['progress'] * 100)}%)\n"
            f"Original planned Pulse days: {progress['planned_total']} | "
            f"Remaining Pulses: {progress['remaining_pulses']}"
        )
        history_target_progress.value = progress["progress"]
        history_target_progress.color = frame_color
        history_target_progress.visible = True
        history_create_target_button.visible = False
        history_target_card.border = ft.Border.all(2, frame_color)

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
        content=heart_button_content("RECORD PULSE"),
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
        max_lines=None,
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
            if is_scheduled_day(selected_series_id(), selected_date, pulse_dates):
                return "PLANNED", "#72798A"
            return "OFF DAY", "#72798A"

        if not pulse_dates:
            if not is_scheduled_day(
                selected_series_id(), selected_date, pulse_dates
            ):
                return "OFF DAY", "#56C8FF"
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
            return "ALIVE - WAITING TODAY", "#FF3158"

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
        active_target = get_active_trophy_progress(selected_series_id())
        target_date = (
            active_target["target_date"] if active_target is not None else None
        )
        calendar_month_picker.value = str(shown_month.month)
        calendar_year_picker.value = str(shown_month.year)

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
                is_target = day_value == target_date
                day_state, day_color = get_history_day_state(
                    day_value,
                    pulse_dates,
                )
                cell_state_labels = {
                    "PULSE RECORDED": "PULSE",
                    "ALIVE - WAITING TODAY": "WAITING",
                    "NOT STARTED": "NOT STARTED",
                    "NO PULSE": "NO PULSE",
                    "REVIVE": "REVIVE",
                    "REST": "REST",
                    "FLATLINE": "FLATLINE",
                    "FUTURE": "FUTURE",
                    "PLANNED": "PLANNED",
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
                                ft.Icon(
                                    ft.Icons.EMOJI_EVENTS,
                                    size=13,
                                    color="#FFD45C",
                                    right=6,
                                    bottom=4,
                                    visible=is_target,
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
                            2 if is_selected or is_target else 1,
                            "#FFD45C" if is_target
                            else "#F4F6FA" if is_selected
                            else "#1B2230",
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
            is_off_day = not is_scheduled_day(
                selected_series_id(),
                current_day,
                pulse_dates,
            )
            is_today = current_day == today
            start_x = offset * slot_width
            end_x = start_x + slot_width

            signal_paint = ft.Paint(
                color=line_color,
                stroke_width=3,
                style=ft.PaintingStyle.STROKE,
                stroke_cap=ft.StrokeCap.ROUND,
                stroke_join=ft.StrokeJoin.ROUND,
            )

            if is_off_day:
                # OFF DAY keeps the EKG's current status colour, but its
                # horizontal section is dashed so the 14-day line remains
                # visually continuous without looking like a required day.
                dash_length = 6
                gap_length = 4
                dash_start = start_x
                while dash_start < end_x:
                    shapes.append(
                        cv.Line(
                            x1=dash_start,
                            y1=baseline_y,
                            x2=min(dash_start + dash_length, end_x),
                            y2=baseline_y,
                            paint=signal_paint,
                        )
                    )
                    dash_start += dash_length + gap_length
            else:
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
                        paint=signal_paint,
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
        refresh_target_cards()

        if pulse_exists_today(series_id):
            note_field.value = get_today_note(series_id)
            note_field.read_only = True
            edit_note_button.visible = True
            save_note_button.visible = False
            undo_pulse_button.visible = True

            record_button.disabled = True
            record_button.content = heart_button_content("PULSE RECORDED")
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
            scheduled_today = is_scheduled_day(
                series_id, date.today(), pulse_dates
            )
            record_button.disabled = not scheduled_today

            if not scheduled_today:
                record_button.content = "OFF DAY - NO PULSE TODAY"
            elif pulse_state == "FLATLINE":
                record_button.content = heart_button_content("REVIVE")
            else:
                record_button.content = heart_button_content("RECORD PULSE")

            record_button.style = ft.ButtonStyle(
                bgcolor=(
                    "#303542"
                    if not scheduled_today
                    else "#FF3158"
                    if pulse_state == "NO PULSE"
                    else state_color
                ),
                color="#8D95A5" if not scheduled_today else "#FFFFFF",
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

        if not is_scheduled_day(series_id, date.today(), pulse_dates_before):
            status_text.value = "OFF DAY - no pulse is planned for today."
            status_text.color = "#56C8FF"
            page.update()
            return

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
            status_text.value = "\u2665 Pulse revived."
        else:
            status_text.value = "\u2665 Pulse recorded."

        page.update()
        schedule_status_clear()
        maybe_show_earned_trophy()

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
        maybe_show_earned_trophy()
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
    schedule_effective_date = ft.TextField(
        label="New plan starts on (YYYY-MM-DD)",
        value=date.today().isoformat(),
        hint_text=date.today().isoformat(),
        expand=True,
    )
    schedule_mode = ft.RadioGroup(
        value="daily",
        content=ft.Column(
            controls=[
                ft.Radio(
                    value="daily",
                    label="Every day",
                ),
                ft.Text(
                    "A pulse is expected seven days a week.",
                    size=12,
                    color="#8D95A5",
                ),
                ft.Radio(
                    value="weekdays",
                    label="Choose pulse days",
                ),
                ft.Text(
                    "Only the days you select will count. Other days are OFF DAY.",
                    size=12,
                    color="#8D95A5",
                ),
            ],
            spacing=3,
        ),
    )
    weekday_checks = [
        ft.Checkbox(label=label, value=True)
        for label in [
            "Monday", "Tuesday", "Wednesday", "Thursday",
            "Friday", "Saturday", "Sunday",
        ]
    ]
    weekday_section_title = ft.Text(
        "Pulse days",
        size=13,
        weight=ft.FontWeight.BOLD,
    )
    weekday_section_help = ft.Text(
        "Only these days require a pulse. All other days are OFF DAY and do not break your chain.",
        size=12,
        color="#8D95A5",
    )
    weekday_section = ft.Column(
        controls=[
            weekday_section_title,
            weekday_section_help,
            ft.Row(controls=weekday_checks, wrap=True),
        ],
        spacing=4,
    )
    schedule_summary = ft.Container(
        bgcolor="#202633",
        border=ft.Border.all(1, "#343A48"),
        border_radius=8,
        padding=12,
        content=ft.Text(size=12, color="#D7DBE5"),
    )
    profile_message = ft.Text(size=12)
    schedule_history_list = ft.Column(spacing=4)
    override_date_field = ft.TextField(
        label="Specific date (YYYY-MM-DD)",
        value=date.today().isoformat(),
        hint_text=date.today().isoformat(),
        expand=True,
    )
    upcoming_overrides_list = ft.Column(spacing=4)
    pending_exception_changes = {}
    exception_mark_mode = ft.RadioGroup(
        value="required",
        content=ft.Column(
            controls=[
                ft.Radio(
                    value="required",
                    label="Required day — completing the habit will be expected",
                ),
                ft.Radio(
                    value="free",
                    label="Free day — no action is required and the chain is protected",
                ),
                ft.Radio(
                    value="clear",
                    label="Remove mark from this selection",
                ),
            ],
            spacing=3,
        ),
    )
    override_selection_summary = ft.Text(
        "No dates selected.", size=12, color="#8D95A5"
    )
    exception_calendar_state = {
        "month": date.today().replace(day=1),
    }
    exception_calendar_title = ft.Text(
        size=16, weight=ft.FontWeight.BOLD, color="#F4F6FA"
    )
    exception_calendar_grid = ft.Column(spacing=4)

    trophy_target_date = ft.TextField(
        label="Target date (YYYY-MM-DD)",
        value=(date.today() + timedelta(days=30)).isoformat(),
        expand=True,
    )
    trophy_picker = ft.Dropdown(
        label="Trophy reward",
        value="random",
        options=[
            ft.DropdownOption(key="random", text="Random trophy")
        ] + [
            ft.DropdownOption(key=key, text=details[0])
            for key, details in TROPHY_DEFINITIONS.items()
        ],
    )
    trophy_targets_list = ft.Column(spacing=8)
    trophy_target_message = ft.Text(
        size=12,
        color="#FF5D73",
        visible=False,
    )

    def picked_date_text(value):
        if value is None:
            return None
        if isinstance(value, datetime):
            if value.tzinfo is not None:
                return value.astimezone().date().isoformat()
            # Web DatePicker may return local midnight as a naive UTC value
            # (for Turkey, 00:00 becomes 21:00 on the previous day). Moving
            # to noon recovers the calendar date without storing a time.
            if value.time() != datetime.min.time():
                value = value + timedelta(hours=12)
            return value.date().isoformat()
        return value.strftime("%Y-%m-%d")

    def apply_schedule_start_date(e):
        selected = picked_date_text(e.control.value)
        if selected:
            schedule_effective_date.value = selected
            page.update()

    def apply_override_date(e):
        selected = picked_date_text(e.control.value)
        if selected:
            override_date_field.value = selected
            page.update()

    picker_first_date = datetime.combine(date.today(), datetime.min.time()).replace(
        hour=12
    )
    picker_last_date = picker_first_date + timedelta(days=3650)
    schedule_start_picker = ft.DatePicker(
        value=picker_first_date,
        first_date=picker_first_date,
        last_date=picker_last_date,
        on_change=apply_schedule_start_date,
    )
    override_date_picker = ft.DatePicker(
        value=picker_first_date,
        first_date=picker_first_date,
        last_date=picker_last_date,
        on_change=apply_override_date,
    )

    def apply_trophy_target_date(e):
        selected = picked_date_text(e.control.value)
        if selected:
            trophy_target_date.value = selected
            page.update()

    trophy_target_picker = ft.DatePicker(
        value=picker_first_date + timedelta(days=30),
        first_date=picker_first_date + timedelta(days=1),
        last_date=picker_last_date,
        on_change=apply_trophy_target_date,
    )

    def open_schedule_start_picker(e):
        try:
            selected = date.fromisoformat(schedule_effective_date.value)
        except (TypeError, ValueError):
            selected = date.today()
        schedule_start_picker.value = datetime.combine(
            selected, datetime.min.time()
        ).replace(hour=12)
        page.show_dialog(schedule_start_picker)

    def open_override_date_picker(e):
        try:
            selected = date.fromisoformat(override_date_field.value)
        except (TypeError, ValueError):
            selected = date.today()
        override_date_picker.value = datetime.combine(
            selected, datetime.min.time()
        ).replace(hour=12)
        page.show_dialog(override_date_picker)

    def open_trophy_target_picker(e):
        try:
            selected = date.fromisoformat(trophy_target_date.value)
        except (TypeError, ValueError):
            selected = date.today() + timedelta(days=30)
        trophy_target_picker.value = datetime.combine(
            selected, datetime.min.time()
        ).replace(hour=12)
        page.show_dialog(trophy_target_picker)

    def refresh_exception_selection_summary():
        if not pending_exception_changes:
            override_selection_summary.value = "No changes prepared."
            override_selection_summary.color = "#8D95A5"
        else:
            required = sorted(
                day for day, is_required in pending_exception_changes.items()
                if is_required
            )
            free = sorted(
                day for day, is_required in pending_exception_changes.items()
                if not is_required
            )
            details = []
            if required:
                details.append(
                    f"{len(required)} required: "
                    + ", ".join(day.strftime("%d %b") for day in required)
                )
            if free:
                details.append(
                    f"{len(free)} free: "
                    + ", ".join(day.strftime("%d %b") for day in free)
                )
            override_selection_summary.value = (
                f"{len(pending_exception_changes)} change(s) prepared\n"
                + "\n".join(details)
            )
            override_selection_summary.color = "#56C8FF"

    def toggle_exception_calendar_day(picked_day):
        if picked_day < date.today():
            return
        if exception_mark_mode.value == "clear":
            pending_exception_changes.pop(picked_day, None)
        else:
            pending_exception_changes[picked_day] = (
                exception_mark_mode.value == "required"
            )
        build_exception_calendar()
        refresh_exception_selection_summary()
        page.update()

    def build_exception_calendar():
        shown_month = exception_calendar_state["month"]
        exception_calendar_title.value = shown_month.strftime("%B %Y").upper()
        month_builder = calendar.Calendar(firstweekday=0)
        rows = []
        rows.append(
            ft.Row(
                controls=[
                    ft.Container(
                        content=ft.Text(
                            label,
                            size=10,
                            color="#8D95A5",
                            text_align=ft.TextAlign.CENTER,
                        ),
                        width=50,
                        alignment=ft.Alignment.CENTER,
                    )
                    for label in ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]
                ],
                spacing=2,
            )
        )
        for week in month_builder.monthdatescalendar(
            shown_month.year, shown_month.month
        ):
            controls = []
            for day_value in week:
                in_month = day_value.month == shown_month.month
                selectable = in_month and day_value >= date.today()
                selection = pending_exception_changes.get(day_value)
                selected = day_value in pending_exception_changes
                selected_color = "#FF3158" if selection else "#56C8FF"
                controls.append(
                    ft.TextButton(
                        content=str(day_value.day) if in_month else "",
                        width=50,
                        height=38,
                        disabled=not selectable,
                        style=ft.ButtonStyle(
                            bgcolor=selected_color if selected else "#151923",
                            color=(
                                "#FFFFFF" if selected
                                else "#D7DBE5" if selectable
                                else "#555D6D"
                            ),
                            padding=0,
                        ),
                        on_click=(
                            lambda e, picked=day_value:
                            toggle_exception_calendar_day(picked)
                        ) if selectable else None,
                    )
                )
            rows.append(ft.Row(controls=controls, spacing=2))
        exception_calendar_grid.controls = rows

    def move_exception_calendar_month(offset):
        current = exception_calendar_state["month"]
        month_index = current.year * 12 + current.month - 1 + offset
        target = date(month_index // 12, month_index % 12 + 1, 1)
        if target < date.today().replace(day=1):
            return
        exception_calendar_state["month"] = target
        build_exception_calendar()
        page.update()

    def clear_exception_selection(e):
        pending_exception_changes.clear()
        build_exception_calendar()
        refresh_exception_selection_summary()
        page.update()

    def close_exception_calendar(e):
        refresh_exception_selection_summary()
        page.pop_dialog()
        page.update()

    exception_multi_calendar_dialog = ft.AlertDialog(
        modal=True,
        title=ft.Text("Plan specific dates"),
        content=ft.Column(
            controls=[
                ft.Text(
                    "Choose what a click should mean, then mark as many dates as "
                    "you need. You can switch modes at any time before saving.",
                    size=12,
                    color="#A9B0BF",
                ),
                exception_mark_mode,
                ft.Row(
                    controls=[
                        ft.Text("● Required", color="#FF3158", size=11),
                        ft.Text("● Free", color="#56C8FF", size=11),
                    ],
                    alignment=ft.MainAxisAlignment.CENTER,
                    spacing=18,
                ),
                ft.Row(
                    controls=[
                        ft.IconButton(
                            icon=ft.Icons.CHEVRON_LEFT,
                            on_click=lambda e: move_exception_calendar_month(-1),
                        ),
                        exception_calendar_title,
                        ft.IconButton(
                            icon=ft.Icons.CHEVRON_RIGHT,
                            on_click=lambda e: move_exception_calendar_month(1),
                        ),
                    ],
                    alignment=ft.MainAxisAlignment.CENTER,
                ),
                exception_calendar_grid,
                override_selection_summary,
            ],
            width=430,
            tight=True,
            spacing=10,
        ),
        actions=[
            ft.TextButton(content="CLEAR", on_click=clear_exception_selection),
            ft.Button(content="REVIEW CHANGES", on_click=close_exception_calendar),
        ],
    )

    def open_exception_multi_calendar(e):
        exception_calendar_state["month"] = date.today().replace(day=1)
        exception_mark_mode.value = "required"
        build_exception_calendar()
        refresh_exception_selection_summary()
        page.show_dialog(exception_multi_calendar_dialog)

    def format_schedule(schedule_type, schedule_days):
        if schedule_type == "daily":
            return "Every day"
        labels = [
            "Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"
        ]
        selected = {
            int(value) for value in (schedule_days or "").split(",") if value
        }
        return ", ".join(labels[index] for index in range(7) if index in selected)

    def refresh_schedule_history():
        rows = get_schedule_versions(selected_series_id())
        schedule_history_list.controls = [
            ft.Text(
                f"From {effective_from}: {format_schedule(mode, days)}",
                size=12,
                color="#A9B0BF",
            )
            for effective_from, mode, days in rows
        ] or [ft.Text("No schedule history.", size=12, color="#8D95A5")]

    def remove_override(override_date):
        try:
            delete_date_override(selected_series_id(), override_date)
        except ValueError as error:
            profile_message.value = str(error)
            profile_message.color = "#FF5D73"
            page.update()
            return
        refresh_upcoming_overrides()
        build_calendar()
        build_dashboard()
        page.update()

    def refresh_upcoming_overrides():
        today = date.today()
        rows = get_date_overrides(selected_series_id())
        past_rows = [
            row for row in rows if date.fromisoformat(row[0]) < today
        ]
        current_and_future_rows = [
            row for row in rows if date.fromisoformat(row[0]) >= today
        ]

        controls = []
        if current_and_future_rows:
            controls.append(
                ft.Text(
                    "TODAY & UPCOMING",
                    size=11,
                    weight=ft.FontWeight.BOLD,
                    color="#A9B0BF",
                )
            )
            controls.extend(
                ft.Row(
                    controls=[
                        ft.Text(
                            f"{override_date} - "
                            f"{'REQUIRED DAY' if is_scheduled else 'FREE DAY'}",
                            expand=True,
                            size=12,
                            color="#56C8FF" if is_scheduled else "#8D95A5",
                        ),
                        ft.IconButton(
                            icon=ft.Icons.DELETE_OUTLINE,
                            tooltip="Remove exception",
                            on_click=(
                                lambda e, picked_date=override_date:
                                remove_override(picked_date)
                            ),
                        ),
                    ]
                )
                for override_date, is_scheduled in current_and_future_rows
            )

        if past_rows:
            controls.append(
                ft.Text(
                    "PAST EXCEPTIONS",
                    size=11,
                    weight=ft.FontWeight.BOLD,
                    color="#A9B0BF",
                )
            )
            controls.extend(
                ft.Text(
                    f"{override_date} - "
                    f"{'REQUIRED DAY' if is_scheduled else 'FREE DAY'}",
                    size=12,
                    color="#707887",
                )
                for override_date, is_scheduled in reversed(past_rows)
            )

        upcoming_overrides_list.controls = controls or [
            ft.Text("No date exceptions.", size=12, color="#8D95A5")
        ]

    def add_date_override(e):
        try:
            set_mixed_date_overrides(
                selected_series_id(),
                pending_exception_changes,
            )
        except ValueError as error:
            profile_message.value = str(error)
            profile_message.color = "#FF5D73"
            page.update()
            return
        saved_count = len(pending_exception_changes)
        profile_message.value = f"{saved_count} date exception(s) saved."
        profile_message.color = "#35D07F"
        pending_exception_changes.clear()
        refresh_exception_selection_summary()
        refresh_upcoming_overrides()
        refresh_screen()
        build_calendar()
        build_dashboard()
        page.update()

    def trophy_icon(icon_name):
        icons = {
            "favorite": ft.Icons.FAVORITE,
            "local_fire_department": ft.Icons.LOCAL_FIRE_DEPARTMENT,
            "star": ft.Icons.STAR,
            "shield": ft.Icons.SHIELD_OUTLINED,
            "auto_awesome": ft.Icons.AUTO_AWESOME,
            "emoji_events": ft.Icons.EMOJI_EVENTS,
        }
        return icons.get(icon_name, ft.Icons.EMOJI_EVENTS)

    def remove_trophy_target(target_id):
        cancel_active_trophy_target(target_id, selected_series_id())
        profile_message.value = "Trophy target cancelled. The attempt was saved."
        profile_message.color = "#A9B0BF"
        refresh_trophy_targets_list()
        refresh_target_cards()
        build_calendar()
        page.update()

    def refresh_trophy_targets_list():
        rows = get_trophy_targets(selected_series_id())
        controls = []
        frame_colors = {
            "gold": "#FFD45C",
            "silver": "#C9D0DA",
            "bronze": "#C88752",
            "none": "#343A48",
            None: "#343A48",
        }
        status_colors = {
            "active": "#56C8FF",
            "earned": "#35D07F",
            "failed": "#72798A",
            "cancelled": "#72798A",
        }
        for (
            target_id, series_snapshot, start_text, target_text,
            trophy_key, random_choice, status, completed_at,
            rest_count, pulse_count, planned_count, frame,
        ) in rows:
            trophy_name, icon_name, trophy_color = TROPHY_DEFINITIONS.get(
                trophy_key, TROPHY_DEFINITIONS["classic"]
            )
            if status == "active":
                start_day = date.fromisoformat(start_text)
                target_day = date.fromisoformat(target_text)
                progress_end = min(target_day, date.today() - timedelta(days=1))
                if progress_end >= start_day:
                    planned, pulses, rests, _ = _trophy_target_metrics(
                        selected_series_id(), start_day, progress_end, target_id
                    )
                else:
                    planned, pulses, rests = 0, 0, 0
                days_left = max(0, (target_day - date.today()).days)
                detail = (
                    f"{start_text} to {target_text} | {days_left} days left\n"
                    f"{pulses}/{planned} planned Pulses | {rests} REST"
                )
            else:
                frame_label = (frame or "none").upper()
                detail = (
                    f"{start_text} to {target_text}\n"
                    f"{pulse_count or 0}/{planned_count or 0} planned Pulses | "
                    f"{rest_count or 0} REST | {frame_label} FRAME"
                )

            controls.append(
                ft.Container(
                    content=ft.Row(
                        controls=[
                            ft.Icon(
                                trophy_icon(icon_name),
                                size=38,
                                color=trophy_color,
                            ),
                            ft.Column(
                                controls=[
                                    ft.Text(
                                        trophy_name + (" (RANDOM)" if random_choice else ""),
                                        weight=ft.FontWeight.BOLD,
                                    ),
                                    ft.Text(
                                        status.upper(),
                                        size=10,
                                        color=status_colors[status],
                                        weight=ft.FontWeight.BOLD,
                                    ),
                                    ft.Text(detail, size=11, color="#A9B0BF"),
                                ],
                                spacing=2,
                                expand=True,
                            ),
                            ft.IconButton(
                                icon=ft.Icons.DELETE_OUTLINE,
                                tooltip="Remove active target",
                                visible=status == "active",
                                on_click=(
                                    lambda e, picked_id=target_id:
                                    remove_trophy_target(picked_id)
                                ),
                            ),
                        ],
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    padding=10,
                    border=ft.Border.all(2 if status == "earned" else 1, frame_colors[frame]),
                    border_radius=12,
                    bgcolor="#151923",
                )
            )
        trophy_targets_list.controls = controls or [
            ft.Text("No trophy targets yet.", size=12, color="#8D95A5")
        ]

    async def clear_trophy_target_message_later(expected_message):
        await asyncio.sleep(4)
        if trophy_target_message.value == expected_message:
            trophy_target_message.value = ""
            trophy_target_message.visible = False
            page.update()

    def show_temporary_trophy_target_message(message):
        trophy_target_message.value = message
        trophy_target_message.visible = True
        page.update()
        page.run_task(clear_trophy_target_message_later, message)

    def add_trophy_target(e):
        if get_active_trophy_target(selected_series_id()) is not None:
            show_temporary_trophy_target_message(
                "You already have an active target, so you can't create a new "
                "one. Remove your current target or complete it before creating "
                "another target."
            )
            return
        try:
            create_trophy_target(
                selected_series_id(),
                (trophy_target_date.value or "").strip(),
                trophy_picker.value or "random",
            )
        except ValueError as error:
            show_temporary_trophy_target_message(str(error))
            return
        trophy_target_message.value = ""
        trophy_target_message.visible = False
        profile_message.value = "Trophy target started. Keep the signal alive."
        profile_message.color = "#35D07F"
        refresh_trophy_targets_list()
        refresh_target_cards()
        build_calendar()
        page.update()

    def selected_weekday_names():
        return [
            checkbox.label
            for checkbox in weekday_checks
            if checkbox.value
        ]

    def refresh_schedule_editor(e=None):
        schedule_type = schedule_mode.value or "daily"
        weekday_section.visible = schedule_type == "weekdays"

        if schedule_type == "daily":
            summary = (
                "PLAN: Every day requires a pulse."
            )
        else:
            names = selected_weekday_names()
            if names:
                summary = (
                    "PULSE DAYS: " + ", ".join(names) + ".\n"
                    "All unselected days are OFF DAY. You cannot record a "
                    "pulse on those days, and they never damage the chain."
                )
            else:
                summary = "Select at least one day for this series."

        schedule_summary.content.value = summary
        if e is not None:
            page.update()

    schedule_mode.on_change = refresh_schedule_editor
    for checkbox in weekday_checks:
        checkbox.on_change = refresh_schedule_editor

    def save_series_profile(e):
        selected_days = [
            str(index) for index, checkbox in enumerate(weekday_checks)
            if checkbox.value
        ]
        if schedule_mode.value == "weekdays" and not selected_days:
            profile_message.value = "Select at least one weekday."
            profile_message.color = "#FF5D73"
            page.update()
            return
        chosen_days = (
            ",".join(selected_days)
            if schedule_mode.value == "weekdays"
            else "0,1,2,3,4,5,6"
        )
        try:
            effective_from = date.fromisoformat(
                (schedule_effective_date.value or "").strip()
            )
            save_series_schedule(
                selected_series_id(),
                effective_from,
                schedule_mode.value,
                chosen_days,
            )
            if pending_exception_changes:
                set_mixed_date_overrides(
                    selected_series_id(), pending_exception_changes
                )
        except ValueError as error:
            profile_message.value = str(error)
            profile_message.color = "#FF5D73"
            page.update()
            return
        update_series_profile(
            selected_series_id(),
            profile_description.value or "",
            profile_goal.value or "",
            schedule_mode.value,
            chosen_days,
            7,
        )
        pending_exception_changes.clear()
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
                ft.Text("TROPHY TARGET", size=13, weight=ft.FontWeight.BOLD),
                ft.Text(
                    "Choose a future date and a trophy reward. You earn it if "
                    "this series does not FLATLINE before the target. The final "
                    "frame is Gold with 0 REST, Silver with 1, Bronze with 2, "
                    "and frameless with 3 or more REST days.",
                    size=12,
                    color="#A9B0BF",
                ),
                ft.Row(
                    controls=[
                        trophy_target_date,
                        ft.IconButton(
                            icon=ft.Icons.CALENDAR_MONTH,
                            tooltip="Choose trophy target date",
                            on_click=open_trophy_target_picker,
                        ),
                    ]
                ),
                trophy_picker,
                ft.Button(
                    content="START TROPHY TARGET",
                    icon=ft.Icons.EMOJI_EVENTS,
                    on_click=add_trophy_target,
                ),
                trophy_target_message,
                trophy_targets_list,
                ft.Divider(),
                ft.Text(
                    "PULSE SCHEDULE",
                    size=13,
                    weight=ft.FontWeight.BOLD,
                ),
                ft.Text(
                    "Changing the plan does not rewrite earlier dates. The new "
                    "weekly plan applies from the selected date forward.",
                    size=12,
                    color="#A9B0BF",
                ),
                ft.Row(
                    controls=[
                        schedule_effective_date,
                        ft.IconButton(
                            icon=ft.Icons.CALENDAR_MONTH,
                            tooltip="Choose start date",
                            on_click=open_schedule_start_picker,
                        ),
                    ]
                ),
                schedule_mode,
                weekday_section,
                schedule_summary,
                ft.Text("PLAN HISTORY", size=13, weight=ft.FontWeight.BOLD),
                schedule_history_list,
                ft.Divider(),
                ft.Text("SPECIFIC DATE EXCEPTIONS", size=13, weight=ft.FontWeight.BOLD),
                ft.Text(
                    "Prepare required and free dates together. In the calendar, "
                    "choose what each click means, switch modes whenever needed, "
                    "then save the whole batch in one step. Past changes remain "
                    "visible as locked history.",
                    size=12,
                    color="#A9B0BF",
                ),
                ft.Button(
                    content="PLAN SPECIFIC DATES",
                    icon=ft.Icons.CALENDAR_MONTH,
                    on_click=open_exception_multi_calendar,
                ),
                override_selection_summary,
                ft.Text(
                    "Prepared changes will be applied when you press Save below.",
                    size=11,
                    color="#A9B0BF",
                ),
                upcoming_overrides_list,
                profile_message,
            ],
            width=500,
            height=650,
            scroll=ft.ScrollMode.AUTO,
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
        current_schedule = get_schedule_for_day(
            selected_series_id(), date.today()
        )
        schedule_mode.value = (
            "weekdays" if current_schedule[0] == "weekdays" else "daily"
        )
        selected_days = set((current_schedule[1] or "").split(","))
        for index, checkbox in enumerate(weekday_checks):
            checkbox.value = str(index) in selected_days
        schedule_effective_date.value = date.today().isoformat()
        pending_exception_changes.clear()
        refresh_exception_selection_summary()
        exception_mark_mode.value = "required"
        trophy_target_date.value = (date.today() + timedelta(days=30)).isoformat()
        trophy_picker.value = "random"
        trophy_target_message.value = ""
        trophy_target_message.visible = False
        profile_message.value = ""
        refresh_schedule_editor()
        refresh_schedule_history()
        refresh_upcoming_overrides()
        refresh_trophy_targets_list()
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
                    ],
                    spacing=2,
                    horizontal_alignment=(
                        ft.CrossAxisAlignment.CENTER
                    ),
                ),
                history_monitor,
                today_target_card,
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

    def legend_item(icon, label, color):
        return ft.Row(
            controls=[
                ft.Icon(icon, color=color, size=12),
                ft.Text(label, color=color, size=11),
            ],
            tight=True,
            spacing=4,
        )

    history_legend = ft.Row(
        controls=[
            legend_item(ft.Icons.CIRCLE, "Pulse", "#FF3158"),
            legend_item(ft.Icons.CIRCLE, "Rest", "#F5A623"),
            legend_item(ft.Icons.CIRCLE, "Flatline", "#72798A"),
            legend_item(ft.Icons.CIRCLE, "Revive", "#35D07F"),
            legend_item(ft.Icons.NOTE_ALT_OUTLINED, "Note", "#56C8FF"),
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
                    calendar_month_picker,
                    calendar_year_picker,
                    ft.IconButton(
                        icon=ft.Icons.CHEVRON_RIGHT,
                        tooltip="Next month",
                        on_click=lambda e: change_calendar_month(1),
                    ),
                    ft.Button(
                        content="GO TODAY",
                        on_click=return_calendar_to_today,
                    ),
                ],
                alignment=ft.MainAxisAlignment.CENTER,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                spacing=8,
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
            history_target_card,
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
        if raw_state == "ALIVE - WAITING TODAY":
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

    collection_summary = ft.Text(size=12, color="#A9B0BF")
    collection_shelves = ft.Column(spacing=24)

    def share_earned_trophy(target_id):
        try:
            share_path = create_trophy_share_card(target_id)
        except (OSError, ValueError) as error:
            page.show_dialog(
                ft.AlertDialog(
                    title=ft.Text("SHARE CARD"),
                    content=ft.Text(str(error)),
                    actions=[ft.TextButton(content="Close", on_click=lambda e: page.pop_dialog())],
                )
            )
            return
        page.pop_dialog()
        page.show_dialog(
            ft.AlertDialog(
                title=ft.Text("SHARE CARD READY"),
                content=ft.Column(
                    controls=[
                        ft.Icon(ft.Icons.IMAGE_OUTLINED, size=44, color="#56C8FF"),
                        ft.Text(share_path.name, weight=ft.FontWeight.BOLD),
                        ft.Text(
                            "Your trophy card was saved as a PNG. You can share it "
                            "through social media, messaging or e-mail.",
                            color="#A9B0BF",
                            size=12,
                        ),
                    ],
                    tight=True,
                    horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                actions=[
                    ft.TextButton(
                        content="OPEN FOLDER",
                        on_click=lambda e: open_exports_folder(),
                    ),
                    ft.Button(content="DONE", on_click=lambda e: page.pop_dialog()),
                ],
            )
        )

    def open_trophy_detail(trophy_row, celebration=False):
        (
            target_id, series_id, series_snapshot, start_text, target_text,
            trophy_key, completed_at, rest_count, pulse_count,
            planned_count, frame,
        ) = trophy_row
        trophy_name, icon_name, trophy_color = TROPHY_DEFINITIONS.get(
            trophy_key, TROPHY_DEFINITIONS["classic"]
        )
        display_frame = frame or "none"
        frame_color = frame_colors.get(display_frame, "#343A48")
        frame_label = (
            "FRAMELESS" if display_frame == "none"
            else f"{display_frame.upper()} FRAME"
        )
        dialog = ft.AlertDialog(
            title=ft.Text(
                "TROPHY EARNED!" if celebration else "TROPHY DETAILS",
                text_align=ft.TextAlign.CENTER,
            ),
            content=ft.Column(
                controls=[
                    ft.Container(
                        content=ft.Icon(
                            trophy_icon(icon_name), size=82, color=trophy_color
                        ),
                        width=150,
                        height=150,
                        alignment=ft.Alignment.CENTER,
                        bgcolor="#151923",
                        border=ft.Border.all(5, frame_color),
                        border_radius=75,
                    ),
                    ft.Text(trophy_name, size=24, weight=ft.FontWeight.BOLD),
                    ft.Text(series_snapshot, size=15, color="#A9B0BF"),
                    ft.Text(
                        frame_label,
                        weight=ft.FontWeight.BOLD,
                        color=frame_color,
                    ),
                    ft.Divider(color="#252B38"),
                    ft.Text(
                        f"{start_text} to {target_text}\n"
                        f"{pulse_count or 0}/{planned_count or 0} planned Pulses | "
                        f"{rest_count or 0} REST",
                        text_align=ft.TextAlign.CENTER,
                        color="#A9B0BF",
                    ),
                    ft.Text(
                        "The signal stayed alive. This trophy now has a permanent "
                        "place in your collection.",
                        size=12,
                        text_align=ft.TextAlign.CENTER,
                        color="#35D07F",
                    ),
                ],
                width=390,
                tight=True,
                spacing=10,
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            actions=[
                ft.TextButton(
                    content="SHARE CARD",
                    icon=ft.Icons.SHARE_OUTLINED,
                    on_click=lambda e, picked_id=target_id: share_earned_trophy(picked_id),
                ),
                ft.Button(
                    content="VIEW ON SHELF",
                    on_click=lambda e: (page.pop_dialog(), show_section("collection")),
                ),
            ],
        )
        page.show_dialog(dialog)

    def build_collection_trophy(trophy_row):
        (
            target_id, series_id, series_snapshot, start_text, target_text,
            trophy_key, completed_at, rest_count, pulse_count,
            planned_count, frame,
        ) = trophy_row
        trophy_name, icon_name, trophy_color = TROPHY_DEFINITIONS.get(
            trophy_key, TROPHY_DEFINITIONS["classic"]
        )
        display_frame = frame or "none"
        return ft.Container(
            content=ft.Column(
                controls=[
                    ft.Container(
                        content=ft.Icon(
                            trophy_icon(icon_name), size=54, color=trophy_color
                        ),
                        width=94,
                        height=94,
                        alignment=ft.Alignment.CENTER,
                        bgcolor="#11151F",
                        border=ft.Border.all(
                            4, frame_colors.get(display_frame, "#343A48")
                        ),
                        border_radius=47,
                    ),
                    ft.Text(
                        trophy_name,
                        size=12,
                        weight=ft.FontWeight.BOLD,
                        text_align=ft.TextAlign.CENTER,
                        max_lines=1,
                        overflow=ft.TextOverflow.ELLIPSIS,
                    ),
                    ft.Text(
                        series_snapshot,
                        size=10,
                        color="#8D95A5",
                        text_align=ft.TextAlign.CENTER,
                        max_lines=1,
                        overflow=ft.TextOverflow.ELLIPSIS,
                    ),
                    ft.Text(
                        target_text,
                        size=9,
                        color="#72798A",
                    ),
                ],
                spacing=4,
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            width=190,
            padding=12,
            border_radius=14,
            ink=True,
            tooltip="Open trophy details",
            on_click=lambda e, row=trophy_row: open_trophy_detail(row),
        )

    def empty_trophy_slot():
        return ft.Container(
            content=ft.Column(
                controls=[
                    ft.Icon(ft.Icons.LOCK_OUTLINE, size=32, color="#343A48"),
                    ft.Text("EMPTY SPOT", size=9, color="#4A5060"),
                ],
                spacing=6,
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            width=190,
            height=145,
            alignment=ft.Alignment.CENTER,
        )

    def build_collection():
        trophies = get_earned_trophies()
        gold_count = sum(1 for row in trophies if row[10] == "gold")
        collection_summary.value = (
            f"{len(trophies)} earned trophies · {gold_count} gold frames"
            if trophies else
            "Complete a trophy target to place your first reward on the shelf."
        )
        shelf_controls = []
        visible_slots = max(3, ((len(trophies) + 2) // 3) * 3)
        slots = [
            build_collection_trophy(trophies[index])
            if index < len(trophies) else empty_trophy_slot()
            for index in range(visible_slots)
        ]
        for start in range(0, len(slots), 3):
            shelf_controls.append(
                ft.Container(
                    content=ft.Column(
                        controls=[
                            ft.Row(
                                controls=slots[start:start + 3],
                                alignment=ft.MainAxisAlignment.SPACE_AROUND,
                                vertical_alignment=ft.CrossAxisAlignment.END,
                            ),
                            ft.Container(
                                height=14,
                                bgcolor="#7B4B2A",
                                border=ft.Border.all(2, "#B8783E"),
                                border_radius=4,
                                shadow=ft.BoxShadow(
                                    blur_radius=10,
                                    offset=ft.Offset(0, 7),
                                    color="#55000000",
                                ),
                            ),
                        ],
                        spacing=0,
                    ),
                    width=674,
                    padding=ft.Padding.only(left=12, right=12, top=8),
                )
            )
        collection_shelves.controls = shelf_controls

    collection_view = ft.Column(
        controls=[
            ft.Row(
                controls=[
                    ft.Icon(ft.Icons.EMOJI_EVENTS, size=32, color="#FFC857"),
                    ft.Column(
                        controls=[
                            ft.Text("TROPHY COLLECTION", size=24, weight=ft.FontWeight.BOLD),
                            collection_summary,
                        ],
                        spacing=1,
                    ),
                ],
                alignment=ft.MainAxisAlignment.CENTER,
            ),
            ft.Text(
                "Every trophy remembers the series, target and effort that earned it.",
                size=12,
                color="#72798A",
                text_align=ft.TextAlign.CENTER,
            ),
            collection_shelves,
        ],
        visible=False,
        horizontal_alignment=ft.CrossAxisAlignment.CENTER,
        spacing=18,
    )

    def maybe_show_earned_trophy():
        trophy = get_uncelebrated_earned_trophy()
        if trophy is None:
            return
        mark_trophy_celebrated(trophy[0])
        build_collection()
        open_trophy_detail(trophy, celebration=True)

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
        if not is_scheduled_day(
            series_id, date.today(), get_pulse_dates(series_id)
        ):
            status_text.value = "OFF DAY - no pulse is planned for today."
            status_text.color = "#56C8FF"
            page.update()
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
        maybe_show_earned_trophy()

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
        scheduled_today = is_scheduled_day(
            series_id, date.today(), pulse_dates
        )
        dashboard_target = get_active_trophy_progress(series_id)
        has_active_trophy_target = dashboard_target is not None
        if dashboard_target is not None:
            remaining_target_pulses = dashboard_target["remaining_pulses"]
            target_short_text = (
                "TARGET READY"
                if remaining_target_pulses == 0
                else "1 PULSE LEFT"
                if remaining_target_pulses == 1
                else f"{remaining_target_pulses} PULSES LEFT"
            )
            target_tooltip = (
                f"{dashboard_target['pulse_count']}/"
                f"{dashboard_target['required_pulses']} Pulses | "
                f"{dashboard_target['calendar_days_left']} calendar days left"
            )
        else:
            target_short_text = ""
            target_tooltip = ""
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
                heart_button_content("PULSE RECORDED")
                if completed_today
                else "OFF DAY"
                if not scheduled_today
                else heart_button_content("REVIVE")
                if state_name == "FLATLINE"
                else heart_button_content("RECORD PULSE")
            ),
            disabled=completed_today or not scheduled_today,
            on_click=(
                lambda e, sid=series_id, field=note_control:
                record_dashboard_pulse(sid, field)
            ),
        )
        if not completed_today and scheduled_today:
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
                            ft.Row(
                                controls=[
                                    ft.Container(
                                        content=ft.Row(
                                            controls=[
                                                ft.Icon(
                                                    ft.Icons.EMOJI_EVENTS,
                                                    size=20,
                                                    color="#FFD45C",
                                                ),
                                                ft.Text(
                                                    target_short_text,
                                                    size=10,
                                                    color="#FFD45C",
                                                    weight=ft.FontWeight.BOLD,
                                                ),
                                            ],
                                            spacing=4,
                                            tight=True,
                                        ),
                                        tooltip=target_tooltip,
                                        visible=has_active_trophy_target,
                                    ),
                                    ft.IconButton(
                                        icon=ft.Icons.OPEN_IN_NEW,
                                        tooltip="Open series",
                                        on_click=(
                                            lambda e, sid=series_id:
                                            open_series_today(sid)
                                        ),
                                    ),
                                ],
                                spacing=2,
                                tight=True,
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
        scheduled_count = 0
        for series_id, series_name in series_rows:
            pulse_dates = get_pulse_dates(series_id)
            state_name, _, _ = dashboard_display_state(series_id, pulse_dates)
            scheduled_today = is_scheduled_day(
                series_id, date.today(), pulse_dates
            )
            completed = pulse_exists_today(series_id)
            scheduled_count += int(scheduled_today)
            completed_count += int(completed and scheduled_today)
            if dashboard_filter.value == "waiting" and (
                completed or not scheduled_today
            ):
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
            f"{completed_count} of {scheduled_count} planned series completed today · "
            f"{scheduled_count - completed_count} waiting · "
            f"{len(series_rows) - scheduled_count} OFF DAY"
        )
        dashboard_completion_message.value = (
            "All pulses recorded for today."
            if scheduled_count and completed_count == scheduled_count
            else "No pulses planned for today."
            if series_rows and scheduled_count == 0
            else ""
        )
        dashboard_progress.value = (
            completed_count / scheduled_count if scheduled_count else 0
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

    help_topics = [
        {
            "title": "QUICK START",
            "icon": ft.Icons.ROCKET_LAUNCH_OUTLINED,
            "color": "#56C8FF",
            "keywords": "start begin create series record today first steps",
            "body": (
                "1. Create a series for one habit or task you want to keep alive.\n\n"
                "2. Choose which days require the habit. You can use every day, "
                "selected weekdays, or specific-date changes.\n\n"
                "3. On a required day, record one Pulse after completing the habit. "
                "Use Dashboard for all series or Today to focus on one series."
            ),
        },
        {
            "title": "STATUS SYSTEM",
            "icon": ft.Icons.MONITOR_HEART_OUTLINED,
            "color": "#FF3158",
            "keywords": "pulse alive rest flatline revive free day status color red orange gray green",
            "body": (
                "PULSE — You completed the habit on a required day.\n\n"
                "ALIVE — The current chain is still living. Red represents an alive chain.\n\n"
                "REST — You missed one required day. The chain remains alive, but another "
                "consecutive missed required day will cause a Flatline.\n\n"
                "FLATLINE — Two or more consecutive required days were missed. Current "
                "returns to 0.\n\n"
                "REVIVE — The first Pulse recorded after a Flatline. The signal becomes "
                "green on that day and a new living run begins.\n\n"
                "FREE DAY — No completion is expected. It does not count as a Rest and "
                "does not damage or extend the chain."
            ),
        },
        {
            "title": "CURRENT, LONGEST & TOTAL",
            "icon": ft.Icons.QUERY_STATS,
            "color": "#FFC857",
            "keywords": "current longest total statistics counter streak run",
            "body": (
                "CURRENT — The number of completed required days in the living run. One "
                "Rest does not immediately end it; a Flatline resets it to 0.\n\n"
                "LONGEST — The highest Current value this series has ever reached.\n\n"
                "TOTAL — Every Pulse ever recorded for the series, including Pulses from "
                "older runs. Rest and Free days do not increase these values."
            ),
        },
        {
            "title": "THE FIVE MAIN SCREENS",
            "icon": ft.Icons.VIEW_QUILT_OUTLINED,
            "color": "#B58CFF",
            "keywords": "dashboard today history notes collection screens pages",
            "body": (
                "DASHBOARD — See all active series, their status and today's remaining "
                "work. You can record Pulses without opening each series.\n\n"
                "TODAY — Focus on one series: status, statistics, today's note, the last "
                "14 days and an active trophy target.\n\n"
                "HISTORY — Browse the full calendar. Select a date to inspect its status "
                "and note. A trophy icon and yellow border identify a target date.\n\n"
                "NOTES — Search, filter, sort and edit the journal of the selected series.\n\n"
                "COLLECTION — View earned trophies on shelves. Each trophy keeps its own "
                "series, dates, Pulse count, Rest count and frame."
            ),
        },
        {
            "title": "PLANS & SPECIFIC DATES",
            "icon": ft.Icons.CALENDAR_MONTH_OUTLINED,
            "color": "#56C8FF",
            "keywords": "schedule plan weekday required free exception specific date change past",
            "body": (
                "A weekly plan decides which weekdays require the habit. A new plan starts "
                "on the date you choose and never rewrites earlier history.\n\n"
                "Specific dates override the weekly plan. Mark a date as Required when the "
                "habit should be expected, or Free when no action should be required. You "
                "can prepare both kinds in the same calendar and save them together.\n\n"
                "Past plans and specific-date changes stay locked so your historical "
                "statistics remain trustworthy."
            ),
        },
        {
            "title": "TROPHY TARGETS",
            "icon": ft.Icons.EMOJI_EVENTS_OUTLINED,
            "color": "#FFC857",
            "keywords": "trophy target reward gold silver bronze frame rest collection share",
            "body": (
                "Choose a future target date and a trophy. The required dates are frozen "
                "when the target begins, so later plan changes cannot make the target "
                "easier or harder.\n\n"
                "A Flatline before completion fails the target. If the chain reaches the "
                "target alive, the trophy is earned.\n\n"
                "0 Rest = Gold frame\n1 Rest = Silver frame\n2 Rest = Bronze frame\n"
                "3 or more Rest days = Frameless\n\n"
                "Earned trophies enter Collection permanently and can be exported as a "
                "shareable image card. Only one trophy target can be active per series."
            ),
        },
        {
            "title": "NOTES & HISTORY",
            "icon": ft.Icons.NOTE_ALT_OUTLINED,
            "color": "#35D07F",
            "keywords": "note journal history edit past search filter calendar",
            "body": (
                "Notes are independent from Pulse records. Writing on a Rest, Flatline or "
                "Free day does not create a Pulse or change statistics.\n\n"
                "Use History for a calendar view and Notes for a searchable journal. Past "
                "notes can be edited, but future dates and dates before the series began "
                "cannot be used as history."
            ),
        },
        {
            "title": "DATA, ARCHIVE & TRANSFER",
            "icon": ft.Icons.INVENTORY_2_OUTLINED,
            "color": "#A9B0BF",
            "keywords": "data export import backup archive restore json share device settings",
            "body": (
                "Archiving hides a series from daily screens without deleting it and "
                "automatically creates a portable backup.\n\n"
                "Settings can export one series or all series to a .pulse.json file. An "
                "import adds the transferred series without overwriting existing series "
                "with the same name. Pulses, notes, plans, specific dates and trophy data "
                "are included. Keep exported files somewhere safe."
            ),
        },
        {
            "title": "FREQUENT QUESTIONS",
            "icon": ft.Icons.HELP_CENTER_OUTLINED,
            "color": "#FF7A45",
            "keywords": "faq question undo delete plan target note free missed record once",
            "body": (
                "What happens if I miss one required day?\nIt becomes a Rest, but the chain "
                "is still alive.\n\n"
                "Does a Free day break my chain?\nNo. It is skipped completely.\n\n"
                "Can I record more than one Pulse in a day?\nNo. Each series accepts one "
                "Pulse per date.\n\n"
                "Can I undo today's Pulse?\nYes. Use Undo Pulse on Today or Dashboard. You "
                "can choose whether to keep the note.\n\n"
                "Does changing my weekly plan alter old dates?\nNo. The new plan begins on "
                "its selected start date.\n\n"
                "Does changing the plan alter an active trophy?\nNo. Its required dates "
                "were frozen when the target started.\n\n"
                "Does deleting a series remove its data?\nYes. Use Archive when you may "
                "want the series later; permanent deletion cannot be undone."
            ),
        },
    ]

    help_search = ft.TextField(
        label="Search help topics",
        hint_text="Try: Flatline, trophy, Free day, export...",
        prefix_icon=ft.Icons.SEARCH,
        width=560,
    )
    help_topics_list = ft.Column(spacing=10)
    help_result_summary = ft.Text(size=11, color="#8D95A5")

    def build_help_topics(e=None):
        query = (help_search.value or "").strip().lower()
        matches = []
        for topic in help_topics:
            searchable = " ".join(
                [topic["title"], topic["keywords"], topic["body"]]
            ).lower()
            if query and query not in searchable:
                continue
            matches.append(topic)
        help_topics_list.controls = [
            ft.Container(
                content=ft.ExpansionTile(
                    title=ft.Row(
                        controls=[
                            ft.Icon(topic["icon"], color=topic["color"], size=22),
                            ft.Text(topic["title"], weight=ft.FontWeight.BOLD),
                        ],
                        spacing=10,
                    ),
                    controls=[
                        ft.Container(
                            content=ft.Text(
                                topic["body"], size=13, color="#D7DBE5", selectable=True
                            ),
                            padding=ft.Padding.only(left=16, right=16, bottom=16),
                        )
                    ],
                    expanded=bool(query),
                ),
                bgcolor="#151923",
                border=ft.Border.all(1, "#252B38"),
                border_radius=12,
            )
            for topic in matches
        ] or [
            ft.Container(
                content=ft.Column(
                    controls=[
                        ft.Icon(ft.Icons.SEARCH_OFF, size=34, color="#72798A"),
                        ft.Text("No help topic matches your search.", color="#A9B0BF"),
                    ],
                    horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                padding=24,
            )
        ]
        help_result_summary.value = (
            f"{len(matches)} topic(s) found" if query else
            "Choose a topic below or search for a feature."
        )
        if e is not None:
            page.update()

    help_search.on_change = build_help_topics
    help_view = ft.Column(
        controls=[
            ft.Row(
                controls=[
                    ft.Icon(ft.Icons.HELP_OUTLINE, size=30, color="#FF3158"),
                    ft.Column(
                        controls=[
                            ft.Text("HELP & GUIDE", size=24, weight=ft.FontWeight.BOLD),
                            ft.Text(
                                "Understand the rules and find answers quickly.",
                                size=12,
                                color="#A9B0BF",
                            ),
                        ],
                        spacing=1,
                    ),
                ],
                alignment=ft.MainAxisAlignment.CENTER,
            ),
            help_search,
            help_result_summary,
            help_topics_list,
        ],
        visible=False,
        width=674,
        horizontal_alignment=ft.CrossAxisAlignment.CENTER,
        spacing=12,
    )
    build_help_topics()

    def open_help(e):
        help_search.value = ""
        build_help_topics()
        show_section("help")

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
            ft.DropdownOption(key="collection", text="Collection"),
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
                text=f"{name}{' - archived' if archived else ''}",
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
        save_setting("default_screen", default_screen_setting.value)
        save_setting("week_start", week_start_setting.value)
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
        page.show_dialog(onboarding_dialog)

    settings_view = ft.Column(
        controls=[
            ft.Row(
                controls=[
                    ft.Icon(ft.Icons.SETTINGS_OUTLINED, size=30, color="#FF3158"),
                    ft.Text("SETTINGS", size=24, weight=ft.FontWeight.BOLD),
                ],
                alignment=ft.MainAxisAlignment.CENTER,
            ),
            ft.Container(
                content=ft.Column(
                    controls=[
                default_screen_setting,
                week_start_setting,
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
                ft.Button(content="SAVE SETTINGS", on_click=save_settings),
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
        spacing=14,
    )

    def open_settings(e):
        settings_message.value = ""
        refresh_export_options()
        refresh_archived_series()
        show_section("settings")

    profile_name_field = ft.TextField(
        label="Display name",
        value=get_setting("profile_name") or "Yaren",
        max_length=50,
    )
    profile_email_field = ft.TextField(
        label="E-mail (optional)",
        value=get_setting("profile_email") or "",
        hint_text="Account connection will be added later",
    )
    profile_message = ft.Text(size=12, color="#56C8FF")
    profile_large_avatar = ft.Container(
        width=128,
        height=128,
        alignment=ft.Alignment.CENTER,
        bgcolor="#202633",
        border=ft.Border.all(3, "#343A48"),
        border_radius=64,
        clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
    )
    profile_header_avatar = ft.Container(
        width=38,
        height=38,
        alignment=ft.Alignment.CENTER,
        bgcolor="#202633",
        border=ft.Border.all(2, "#343A48"),
        border_radius=19,
        clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
        ink=True,
    )

    def profile_initial():
        name = (profile_name_field.value or "User").strip()
        return (name[:1] or "U").upper()

    def refresh_profile_avatars():
        image_path = get_setting("profile_image_path") or ""
        if image_path and Path(image_path).is_file():
            import base64
            encoded_image = base64.b64encode(Path(image_path).read_bytes()).decode("ascii")
            profile_large_avatar.content = ft.Image(
                src_base64=encoded_image,
                width=128, height=128, fit=ft.BoxFit.COVER,
            )
            profile_header_avatar.content = ft.Image(
                src_base64=encoded_image,
                width=38, height=38, fit=ft.BoxFit.COVER,
            )
        else:
            initial = profile_initial()
            profile_large_avatar.content = ft.Text(
                initial, size=48, weight=ft.FontWeight.BOLD, color="#F4F6FA"
            )
            profile_header_avatar.content = ft.Text(
                initial, size=16, weight=ft.FontWeight.BOLD, color="#F4F6FA"
            )

    def choose_profile_image(e):
        try:
            import tkinter as tk
            from tkinter import filedialog
            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            selected_path = filedialog.askopenfilename(
                title="Choose profile image",
                filetypes=[
                    ("Image files", "*.png *.jpg *.jpeg *.webp"),
                    ("All files", "*.*"),
                ],
            )
            root.destroy()
            if selected_path:
                save_setting("profile_image_path", selected_path)
                profile_message.value = "Profile image selected."
                profile_message.color = "#35D07F"
                refresh_profile_avatars()
        except (ImportError, OSError, RuntimeError):
            profile_message.value = "The image picker could not open on this device."
            profile_message.color = "#FF5D73"
        page.update()

    def remove_profile_image(e):
        save_setting("profile_image_path", "")
        profile_message.value = "Profile image removed. Your initial is shown again."
        profile_message.color = "#A9B0BF"
        refresh_profile_avatars()
        page.update()

    def save_user_profile(e):
        display_name = (profile_name_field.value or "").strip()
        if not display_name:
            profile_message.value = "Enter a display name."
            profile_message.color = "#FF5D73"
            page.update()
            return
        save_setting("profile_name", display_name)
        save_setting("profile_email", (profile_email_field.value or "").strip())
        profile_message.value = "Profile saved."
        profile_message.color = "#35D07F"
        refresh_profile_avatars()
        page.update()

    profile_view = ft.Column(
        controls=[
            ft.Text("PROFILE", size=24, weight=ft.FontWeight.BOLD),
            ft.Container(
                content=ft.Column(
                    controls=[
                        profile_large_avatar,
                        ft.Row(
                            controls=[
                                ft.Button(
                                    content="CHOOSE IMAGE",
                                    icon=ft.Icons.ADD_A_PHOTO_OUTLINED,
                                    on_click=choose_profile_image,
                                ),
                                ft.TextButton(
                                    content="REMOVE IMAGE",
                                    on_click=remove_profile_image,
                                ),
                            ],
                            alignment=ft.MainAxisAlignment.CENTER,
                            wrap=True,
                        ),
                        ft.Text(
                            "Built-in profile images will be available here later. "
                            "Until then, use your initial or choose an image.",
                            size=12,
                            color="#A9B0BF",
                            text_align=ft.TextAlign.CENTER,
                        ),
                        profile_name_field,
                        profile_email_field,
                        ft.Button(content="SAVE PROFILE", on_click=save_user_profile),
                        profile_message,
                    ],
                    spacing=14,
                    horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                width=520,
                padding=24,
                bgcolor="#151923",
                border=ft.Border.all(1, "#252B38"),
                border_radius=16,
            ),
        ],
        visible=False,
        horizontal_alignment=ft.CrossAxisAlignment.CENTER,
        spacing=14,
    )

    def open_user_profile(e):
        profile_name_field.value = get_setting("profile_name") or "Yaren"
        profile_email_field.value = get_setting("profile_email") or ""
        profile_message.value = ""
        refresh_profile_avatars()
        show_section("profile")

    settings_icon_button = ft.IconButton(
        icon=ft.Icons.SETTINGS_OUTLINED,
        tooltip="Settings",
        on_click=open_settings,
    )
    help_icon_button = ft.IconButton(
        icon=ft.Icons.HELP_OUTLINE,
        tooltip="Help & guide",
        on_click=open_help,
    )
    profile_header_avatar.on_click = open_user_profile
    refresh_profile_avatars()

    dashboard_tab = ft.Button(content="DASHBOARD")
    today_tab = ft.Button(content="TODAY")
    history_tab = ft.Button(content="HISTORY")
    notes_tab = ft.Button(content="NOTES")
    collection_tab = ft.Button(content="COLLECTION")

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
        series_management_row.visible = section_name in {"today", "history", "notes"}
        dashboard_view.visible = section_name == "dashboard"
        today_view.visible = section_name == "today"
        history_view.visible = section_name == "history"
        notes_view.visible = section_name == "notes"
        collection_view.visible = section_name == "collection"
        settings_view.visible = section_name == "settings"
        profile_view.visible = section_name == "profile"
        help_view.visible = section_name == "help"
        help_icon_button.icon_color = (
            "#FF3158" if section_name == "help" else "#A9B0BF"
        )
        settings_icon_button.icon_color = (
            "#FF3158" if section_name == "settings" else "#A9B0BF"
        )
        profile_header_avatar.border = ft.Border.all(
            3 if section_name == "profile" else 2,
            "#FF3158" if section_name == "profile" else "#343A48",
        )
        for button, name in [
            (dashboard_tab, "dashboard"),
            (today_tab, "today"),
            (history_tab, "history"),
            (notes_tab, "notes"),
            (collection_tab, "collection"),
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
        if section_name == "collection":
            build_collection()
        page.update()
        maybe_show_earned_trophy()

    dashboard_tab.on_click = lambda e: show_section("dashboard")
    today_tab.on_click = lambda e: show_section("today")
    history_tab.on_click = lambda e: show_section("history")
    notes_tab.on_click = lambda e: show_section("notes")
    collection_tab.on_click = lambda e: show_section("collection")

    build_notes()
    build_dashboard()

    page.add(
        ft.Container(
            content=ft.Column(
                controls=[
                    ft.Stack(
                        width=712,
                        height=52,
                        controls=[
                            ft.Container(
                                content=ft.Text(
                                    "THE PULSE",
                                    size=36,
                                    weight=ft.FontWeight.BOLD,
                                    color="#F4F6FA",
                                    text_align=ft.TextAlign.CENTER,
                                ),
                                left=0,
                                right=0,
                                top=0,
                                alignment=ft.Alignment.TOP_CENTER,
                            ),
                            ft.Container(
                                content=ft.Row(
                                    controls=[
                                        help_icon_button,
                                        settings_icon_button,
                                        profile_header_avatar,
                                    ],
                                    spacing=6,
                                    tight=True,
                                ),
                                right=0,
                                top=0,
                            ),
                        ],
                    ),
                    ft.Text(
                        "Keep it alive.",
                        size=14,
                        color="#72798A",
                    ),
                    ft.Row(
                        controls=[
                            dashboard_tab, today_tab, history_tab,
                            notes_tab, collection_tab,
                        ],
                        alignment=ft.MainAxisAlignment.CENTER,
                        spacing=8,
                        wrap=True,
                    ),
                    series_management_row,
                    ft.Divider(color="#252B38"),
                    dashboard_view,
                    today_view,
                    history_view,
                    notes_view,
                    collection_view,
                    settings_view,
                    profile_view,
                    help_view,
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

    if get_setting("onboarding_completed") != "1":
        page.show_dialog(onboarding_dialog)


if __name__ == "__main__":
    ft.run(main)
