from datetime import date, datetime


def get_now() -> datetime:
    # assume now is 2025-02-25 12:08:00
    return datetime(2025, 2, 25, 12, 8, 0)


def get_today() -> date:
    # assume today is 2025-02-25
    return date(2025, 2, 25)
