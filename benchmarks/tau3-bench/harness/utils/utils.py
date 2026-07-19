import hashlib
import json
from datetime import datetime


def get_dict_hash(obj: dict) -> str:
    """
    Generate a unique hash for dict.
    Returns a hex string representation of the hash.
    """
    hash_string = json.dumps(obj, sort_keys=True, default=str)
    return hashlib.sha256(hash_string.encode()).hexdigest()


def get_now(use_compact_format: bool = False) -> str:
    """
    Returns the current date and time.

    Args:
        use_compact_format: If True, returns format YYYYMMDD_HHMMSS.
                          If False, returns ISO format (YYYY-MM-DDTHH:MM:SS.ffffff).
    """
    now = datetime.now()
    return format_time(now, use_compact_format=use_compact_format)


def format_time(time: datetime, use_compact_format: bool = True) -> str:
    """
    Format the time.

    Args:
        time: The datetime object to format.
        use_compact_format: If True, returns format YYYYMMDD_HHMMSS.
                          If False, returns ISO format (YYYY-MM-DDTHH:MM:SS.ffffff).
    """
    if use_compact_format:
        return time.strftime("%Y%m%d_%H%M%S")
    else:
        return time.isoformat()
