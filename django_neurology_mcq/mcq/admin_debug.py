"""
Utilities for recording and retrieving Admin Debug Console events.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, Optional

from django.utils import timezone

from .models import AdminDebugEvent

logger = logging.getLogger(__name__)


def record_admin_debug_event(
    event_type: str,
    message: str,
    *,
    severity: str = "info",
    source: str = "backend",
    user=None,
    request=None,
    payload: Optional[Dict[str, Any]] = None,
    session_key: str = "",
    occurred_at: Optional[str] = None,
) -> AdminDebugEvent:
    """
    Persist an admin debug event and echo it to the structured logger.
    """
    if request and not user:
        candidate = getattr(request, "user", None)
        if candidate and candidate.is_authenticated:
            user = candidate
    if request and not session_key:
        session_key = getattr(getattr(request, "session", None), "session_key", "") or ""

    if occurred_at:
        try:
            occurred_dt = timezone.datetime.fromisoformat(occurred_at)
            if not timezone.is_aware(occurred_dt):
                occurred_dt = timezone.make_aware(occurred_dt, timezone.get_current_timezone())
        except Exception:  # pragma: no cover - defensive
            occurred_dt = timezone.now()
    else:
        occurred_dt = timezone.now()

    event = AdminDebugEvent.objects.create(
        event_type=event_type[:64],
        message=message,
        severity=severity,
        source=source,
        user=user if user and getattr(user, "is_authenticated", False) else None,
        session_key=session_key[:64],
        payload=payload or {},
        occurred_at=occurred_dt,
    )

    logger_extra = {
        "source": source,
        "severity": severity,
        "event_type": event_type,
        "user": getattr(user, "username", None),
    }
    logger.log(
        logging.INFO if severity in {"info", "success"} else logging.WARNING,
        "[AdminDebug] %s",
        message,
        extra=logger_extra,
    )
    return event


def serialize_events(events: Iterable[AdminDebugEvent]) -> Iterable[Dict[str, Any]]:
    """Convert AdminDebugEvent queryset/iterable into serializable dicts."""
    for event in events:
        yield event.as_dict()
