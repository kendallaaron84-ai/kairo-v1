import json
from datetime import UTC, date, datetime
from email.utils import parsedate_to_datetime


def json_object(content: bytes) -> dict:
    try:
        value = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("primary-source payload is not valid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("primary-source JSON root must be an object")
    return value


def aware_datetime(value: str | datetime | date) -> datetime:
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, date):
        result = datetime.combine(value, datetime.min.time(), tzinfo=UTC)
    else:
        normalized = value.strip().replace("Z", "+00:00")
        try:
            result = datetime.fromisoformat(normalized)
        except ValueError:
            result = parsedate_to_datetime(value)
    if result.tzinfo is None or result.utcoffset() is None:
        result = result.replace(tzinfo=UTC)
    return result


def require_list(value: object, field_name: str) -> list[dict]:
    if not isinstance(value, list) or not all(isinstance(row, dict) for row in value):
        raise ValueError(f"{field_name} must be a list of objects")
    return value
