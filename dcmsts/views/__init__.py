from datetime import date, datetime

from flask import g, request


def register(app):
    from . import admin, assets, auth, dashboard, people, predictive, reports, schedules, workorders
    for mod in (auth, dashboard, assets, schedules, workorders, people, predictive, reports, admin):
        app.register_blueprint(mod.bp)

    @app.context_processor
    def _inject():
        return {"current_user": g.get("user"), "today": date.today()}


def field(name, max_len=200, required=False):
    value = (request.form.get(name) or "").strip()
    if required and not value:
        raise ValueError(f"{name.replace('_', ' ').capitalize()} is required.")
    if len(value) > max_len:
        raise ValueError(f"{name.replace('_', ' ').capitalize()} is too long (max {max_len}).")
    return value or None


def date_field(name, required=False):
    value = field(name, 10, required)
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ValueError(f"{name.replace('_', ' ').capitalize()} must be a valid date.")


def datetime_field(name, required=False):
    value = field(name, 19, required)
    if value is None:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        raise ValueError(f"{name.replace('_', ' ').capitalize()} must be a valid date and time.")


def choice_field(name, choices, required=True):
    value = field(name, 50, required)
    if value is not None and value not in choices:
        raise ValueError(f"Invalid {name.replace('_', ' ')}.")
    return value


def number_field(name, required=False, minimum=None, cast=float):
    value = field(name, 20, required)
    if value is None:
        return None
    try:
        n = cast(value)
    except ValueError:
        raise ValueError(f"{name.replace('_', ' ').capitalize()} must be a number.")
    if minimum is not None and n < minimum:
        raise ValueError(f"{name.replace('_', ' ').capitalize()} must be at least {minimum}.")
    return n


def safe_next(target):
    if target and target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return None
