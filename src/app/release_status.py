"""Choose a tracking default only when catalogue data establishes a future release."""

from datetime import date, datetime

from django.utils import timezone

UPCOMING_STATUSES = {
    "upcoming",
    "unreleased",
    "not yet aired",
    "not yet published",
    "planned",
    "in production",
    "post production",
    "rumored",
}


def is_unreleased(metadata, today=None):
    today = today or timezone.localdate()
    details = metadata.get("details", {})
    status = str(details.get("status", "")).replace("_", " ").strip().lower()
    if status in UPCOMING_STATUSES:
        return True
    for name in (
        "release_date",
        "first_air_date",
        "publish_date",
        "start_date",
        "year",
    ):
        value = details.get(name)
        if value is None:
            continue
        if isinstance(value, datetime):
            return value.date() > today
        if isinstance(value, date):
            return value > today
        parts = str(value).split("-")
        try:
            if len(parts) == 1 and len(parts[0]) == 4:
                return int(parts[0]) > today.year
            if len(parts) == 2 and len(parts[0]) == 4:
                return date(int(parts[0]), int(parts[1]), 1) > today
            if len(parts) == 3:
                return date.fromisoformat(str(value)) > today
        except ValueError:
            continue
    return False
