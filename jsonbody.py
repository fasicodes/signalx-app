"""Safe readers for what a visitor sends. A request body that is not a JSON object (a list, a number, text) or a
field that is not text must give a normal 400 / empty value, never a 500 crash."""
from flask import request


def json_body():
    """The JSON body as a dict; {} when it is missing, broken, or not an object."""
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def as_text(value):
    """value if it is a string, else '' (so .strip() / .upper() can never crash on a number, list or null)."""
    return value if isinstance(value, str) else ""


def iso_z(dt):
    """A database time (naive UTC) as ISO text with 'Z', so browsers read it as UTC and not as their own local time."""
    if dt is None or not hasattr(dt, "isoformat"):
        return dt
    return dt.isoformat() + ("" if getattr(dt, "tzinfo", None) else "Z")
