"""discord.utils -- the two helpers that are worth having."""

from __future__ import annotations

import datetime


def get(iterable, **attrs):
    """First element whose attributes all match, or None."""
    for item in iterable:
        if all(getattr(item, key, None) == value for key, value in attrs.items()):
            return item
    return None


def find(predicate, iterable):
    for item in iterable:
        if predicate(item):
            return item
    return None


def utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)
