from __future__ import annotations

from datetime import datetime
from typing import Any, Dict

_FORMATS = [
    '%y.%m.%d(%a) %H:%M',
    '%y.%m.%d %H:%M',
    '%Y-%m-%d %H:%M:%S',
    '%Y-%m-%d %H:%M',
]


def parse_start_date(value: str | None) -> str | None:
    if not value:
        return None
    text = str(value).strip()
    for fmt in _FORMATS:
        try:
            return datetime.strptime(text, fmt).strftime('%Y-%m-%d %H:%M:%S')
        except Exception:
            pass
    try:
        return datetime.fromisoformat(text).strftime('%Y-%m-%d %H:%M:%S')
    except Exception:
        return None


def format_datetime_fields(data: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(data)
    for key in ('start_date', 'created_at', 'updated_at', 'joined_at'):
        value = out.get(key)
        if hasattr(value, 'strftime'):
            out[key] = value.strftime('%Y-%m-%d %H:%M:%S')
    return out
