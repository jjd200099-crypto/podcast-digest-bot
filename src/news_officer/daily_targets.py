"""Pure daily-recipient validation shared by delivery and document tasks."""

import re

_PREFIX = {"chat_id": "oc_", "open_id": "ou_"}
_ID_BODY = r"[A-Za-z0-9_-]{1,125}"


def valid_target_id(kind, target) -> bool:
    prefix = _PREFIX.get(kind)
    return bool(prefix and isinstance(target, str) and re.fullmatch(prefix + _ID_BODY, target))


def valid_allowlist(chat_ids) -> tuple[str, ...]:
    """Well-formed group chat IDs from an allowlist, de-duplicated in order."""
    return tuple(dict.fromkeys(c for c in chat_ids if valid_target_id("chat_id", c)))


def filter_daily_targets(targets, allowlist=(), *, require=False, on_invalid=None):
    """Keep well-formed targets; when restricted, only allowlisted chat IDs.

    Restricted means an allowlist is configured, or `require` is on (in which
    case an empty allowlist matches nothing: fail closed, never fall back).
    """
    restricted = bool(allowlist) or require
    valid = []
    for kind, target in targets:
        if not valid_target_id(kind, target):
            if on_invalid:
                on_invalid(kind)
            continue
        if restricted and (kind != "chat_id" or target not in allowlist):
            continue
        valid.append((kind, target))
    return valid


def allowlist_warnings(chat_ids, require=False, watch_chat_id="") -> list[str]:
    """Non-fatal configuration warnings for operations/watchdog status."""
    valid = valid_allowlist(chat_ids)
    warnings = []
    if require and not valid:
        warnings.append("daily_allowlist_required_but_missing: NEWS_OFFICER_DAILY_REQUIRE_CHAT_IDS is on "
                        "but NEWS_OFFICER_DAILY_CHAT_IDS is empty or has no valid oc_ ids; the daily will not send")
    elif chat_ids and not valid:
        warnings.append("daily_allowlist_invalid: NEWS_OFFICER_DAILY_CHAT_IDS has no valid oc_ ids; "
                        "the daily will not send")
    elif not chat_ids:
        warnings.append("daily_allowlist_unset: NEWS_OFFICER_DAILY_CHAT_IDS is empty; the daily falls back "
                        "to every active subscription (set it, then enable NEWS_OFFICER_DAILY_REQUIRE_CHAT_IDS)")
    if valid and watch_chat_id and watch_chat_id not in valid:
        warnings.append("watch_chat_not_in_daily_allowlist: NEWS_OFFICER_WATCH_CHAT_ID is not one of "
                        "NEWS_OFFICER_DAILY_CHAT_IDS; the watchdog checks a chat the daily does not target")
    return warnings
