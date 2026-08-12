import flet as ft
import sqlite3

from datetime import date
from pathlib import Path


DB_PATH = Path(__file__).with_name("pulse.db")


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


def get_series():
    with connect_database() as connection:
        return connection.execute(
            """
            SELECT id, name
            FROM series
            ORDER BY id
            """
        ).fetchall()


def create_series(name):
    with connect_database() as connection:
        cursor = connection.execute(
            """
            INSERT INTO series (name)
            VALUES (?)
            """,
            (name,),
        )

        return cursor.lastrowid


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


def get_today_note(series_id):
    today = date.today().isoformat()

    with connect_database() as connection:
        row = connection.execute(
            """
            SELECT note
            FROM pulse_entries
            WHERE series_id = ?
              AND pulse_date = ?
            """,
            (series_id, today),
        ).fetchone()

    if row:
        return row[0]

    return ""


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


def calculate_stats(pulse_dates):
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

        # 1 REST günü pulse'ı bozmaz.
        if gap <= 2:
            active_run += 1
        else:
            active_run = 1

        longest_pulse = max(longest_pulse, active_run)

    days_since_last_pulse = (
        date.today() - pulse_dates[-1]
    ).days

    # İki veya daha fazla ardışık REST -> FLATLINE
    if days_since_last_pulse <= 2:
        current_pulse = active_run
    else:
        current_pulse = 0

    return current_pulse, longest_pulse, total_pulse

def get_pulse_status(pulse_dates):
    if not pulse_dates:
        return "NO PULSE", "This series has not received a pulse yet."

    today = date.today()
    last_pulse_date = pulse_dates[-1]

    if last_pulse_date == today:
        return "ALIVE", "Today's pulse is alive."

    # Bugün henüz bitmediği için yalnızca tamamen kaçırılmış
    # günleri sayıyoruz.
    missed_days = max(
        0,
        (today - last_pulse_date).days - 1,
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
    page.horizontal_alignment = ft.CrossAxisAlignment.CENTER
    page.vertical_alignment = ft.MainAxisAlignment.CENTER

    current_text = ft.Text(size=18)
    longest_text = ft.Text(size=18)
    total_text = ft.Text(size=18)
    status_text = ft.Text("")

    developer_mode = ft.Switch(
        label="Developer Test Mode",
        value=False,
    )

    test_date_field = ft.TextField(
        label="Test date",
        hint_text="YYYY-MM-DD",
        width=220,
    )

    test_note_field = ft.TextField(
        label="Test note",
        hint_text="Optional note for this test pulse",
        width=400,
    )

    test_status_text = ft.Text("")

    add_test_pulse_button = ft.Button(
        content="ADD TEST PULSE",
    )

    developer_panel = ft.Column(
        controls=[
            ft.Text(
                "TEST TOOLS",
                weight=ft.FontWeight.BOLD,
            ),
            ft.Text(
                "Add a pulse to a past date to test REST, "
                "FLATLINE and REVIVE.",
                size=13,
            ),
            test_date_field,
            test_note_field,
            add_test_pulse_button,
            test_status_text,
        ],
        horizontal_alignment=ft.CrossAxisAlignment.CENTER,
        visible=False,
    )

    pulse_state_text = ft.Text(
    size=18,
    weight=ft.FontWeight.BOLD,
)

    pulse_state_detail = ft.Text(
        size=14,
    )

    series_title = ft.Text(
        size=22,
        weight=ft.FontWeight.BOLD,
    )

    note_field = ft.TextField(
        label="Today's Note",
        hint_text="What did you do today?",
        multiline=True,
        min_lines=3,
        max_lines=5,
        width=400,
    )

    record_button = ft.Button(
        content="♥ RECORD PULSE",
    )

    series_dropdown = ft.Dropdown(
        width=260,
        label="Series",
    )

    new_series_field = ft.TextField(
        label="Series name",
        hint_text="Reading",
    )

    new_series_error = ft.Text("")

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
            calculate_stats(pulse_dates)
        )

        pulse_state, pulse_state_message = (
            get_pulse_status(pulse_dates)
        )

        pulse_state_text.value = pulse_state
        pulse_state_detail.value = pulse_state_message

        current_text.value = (
            f"Current Pulse     {current_pulse}"
        )
        longest_text.value = (
            f"Longest Pulse     {longest_pulse}"
        )
        total_text.value = (
            f"Total Pulse       {total_pulse}"
        )

        if pulse_exists_today(series_id):
            note_field.value = get_today_note(series_id)

            record_button.disabled = True
            record_button.content = "♥ PULSE RECORDED"

        else:
            note_field.value = ""
            record_button.disabled = False

            if pulse_state == "FLATLINE":
                record_button.content = "♥ REVIVE"
            else:
                record_button.content = "♥ RECORD PULSE"

        status_text.value = ""

    def record_pulse(e):
        series_id = selected_series_id()
        today = date.today().isoformat()
        note = note_field.value or ""

        pulse_dates_before = get_pulse_dates(series_id)

        was_flatline = (
            get_pulse_status(pulse_dates_before)[0]
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

        except sqlite3.IntegrityError:
            status_text.value = (
                "Today's pulse has already been recorded."
            )
            return

        refresh_screen()

        if was_flatline:
            status_text.value = "♥ Pulse revived."
        else:
            status_text.value = "♥ Pulse recorded."

        page.update()

    def toggle_developer_mode(e):
        developer_panel.visible = developer_mode.value
        test_status_text.value = ""

        if developer_mode.value:
            test_date_field.value = date.today().isoformat()

        page.update()

    def add_test_pulse(e):
        raw_test_date = (test_date_field.value or "").strip()

        try:
            test_date = date.fromisoformat(raw_test_date)
        except ValueError:
            test_status_text.value = (
                "Enter the date in YYYY-MM-DD format."
            )
            page.update()
            return

        if test_date > date.today():
            test_status_text.value = (
                "A pulse cannot be recorded in the future."
            )
            page.update()
            return

        series_id = selected_series_id()
        note = test_note_field.value or ""

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
                        test_date.isoformat(),
                        note,
                    ),
                )

        except sqlite3.IntegrityError:
            test_status_text.value = (
                "This date already has a pulse."
            )
            page.update()
            return

        refresh_screen()
        test_note_field.value = ""
        test_status_text.value = (
            f"Test pulse added for {test_date.isoformat()}."
        )
        page.update()

    def change_series(e):
        refresh_screen()

    def save_new_series(e):
        name = (new_series_field.value or "").strip()

        if not name:
            new_series_error.value = (
                "Please enter a series name."
            )
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

        page.pop_dialog()

        refresh_screen()

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

    record_button.on_click = record_pulse
    series_dropdown.on_select = change_series
    developer_mode.on_change = toggle_developer_mode
    add_test_pulse_button.on_click = add_test_pulse

    all_series = refresh_series_options()

    if not all_series:
        default_id = create_series(
            "Daily Development"
        )
        refresh_series_options()
        series_dropdown.value = str(default_id)
    else:
        series_dropdown.value = str(all_series[0][0])

    refresh_screen()

    page.add(
        ft.Column(
            controls=[
                ft.Text(
                    "THE PULSE",
                    size=32,
                    weight=ft.FontWeight.BOLD,
                ),

                ft.Row(
                    controls=[
                        series_dropdown,
                        ft.Button(
                            content="+ NEW SERIES",
                            on_click=open_new_series_dialog,
                        ),
                    ],
                    alignment=ft.MainAxisAlignment.CENTER,
                ),

                series_title,

                pulse_state_text,
                pulse_state_detail,

                ft.Divider(),

                current_text,
                longest_text,
                total_text,

                ft.Divider(),

                note_field,

                record_button,

                status_text,

                ft.Divider(),

                developer_mode,
                developer_panel,
            ],
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            spacing=16,
        )
    )


if __name__ == "__main__":
    ft.run(main)