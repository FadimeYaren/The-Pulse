import flet as ft
import sqlite3

from datetime import date
from pathlib import Path


DB_PATH = Path(__file__).with_name("pulse.db")


def initialize_database():
    with sqlite3.connect(DB_PATH) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS pulse_entries (
                pulse_date TEXT PRIMARY KEY,
                note TEXT NOT NULL DEFAULT ''
            )
            """
        )


def get_pulse_dates():
    with sqlite3.connect(DB_PATH) as connection:
        rows = connection.execute(
            """
            SELECT pulse_date
            FROM pulse_entries
            ORDER BY pulse_date
            """
        ).fetchall()

    return [date.fromisoformat(row[0]) for row in rows]


def get_today_note():
    today = date.today().isoformat()

    with sqlite3.connect(DB_PATH) as connection:
        row = connection.execute(
            """
            SELECT note
            FROM pulse_entries
            WHERE pulse_date = ?
            """,
            (today,),
        ).fetchone()

    if row:
        return row[0]

    return ""


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

        # En fazla 1 REST günü varsa pulse yaşamaya devam eder.
        if gap <= 2:
            active_run += 1
        else:
            active_run = 1

        longest_pulse = max(longest_pulse, active_run)

    days_since_last_pulse = (
        date.today() - pulse_dates[-1]
    ).days

    # 0 veya 1 REST günü varsa CURRENT hala canlıdır.
    if days_since_last_pulse <= 2:
        current_pulse = active_run
    else:
        current_pulse = 0

    return current_pulse, longest_pulse, total_pulse


def main(page: ft.Page):
    initialize_database()

    page.title = "The Pulse"
    page.horizontal_alignment = ft.CrossAxisAlignment.CENTER
    page.vertical_alignment = ft.MainAxisAlignment.CENTER

    current_text = ft.Text(size=18)
    longest_text = ft.Text(size=18)
    total_text = ft.Text(size=18)
    status_text = ft.Text("")

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

    def refresh_stats():
        pulse_dates = get_pulse_dates()

        current_pulse, longest_pulse, total_pulse = (
            calculate_stats(pulse_dates)
        )

        current_text.value = f"Current Pulse     {current_pulse}"
        longest_text.value = f"Longest Pulse     {longest_pulse}"
        total_text.value = f"Total Pulse       {total_pulse}"

    def pulse_exists_today():
        return date.today() in get_pulse_dates()

    def record_pulse(e):
        today = date.today().isoformat()
        note = note_field.value or ""

        try:
            with sqlite3.connect(DB_PATH) as connection:
                connection.execute(
                    """
                    INSERT INTO pulse_entries (
                        pulse_date,
                        note
                    )
                    VALUES (?, ?)
                    """,
                    (today, note),
                )

        except sqlite3.IntegrityError:
            status_text.value = (
                "Today's pulse has already been recorded."
            )
            return

        refresh_stats()

        record_button.disabled = True
        status_text.value = "♥ Pulse recorded."

    record_button.on_click = record_pulse

    refresh_stats()

    if pulse_exists_today():
        record_button.disabled = True
        note_field.value = get_today_note()
        status_text.value = "♥ Today's pulse is alive."

    page.add(
        ft.Column(
            controls=[
                ft.Text(
                    "THE PULSE",
                    size=32,
                    weight=ft.FontWeight.BOLD,
                ),
                ft.Text(
                    "Daily Development",
                    size=22,
                ),
                ft.Divider(),
                current_text,
                longest_text,
                total_text,
                ft.Divider(),
                note_field,
                record_button,
                status_text,
            ],
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            spacing=16,
        )
    )


if __name__ == "__main__":
    ft.run(main)