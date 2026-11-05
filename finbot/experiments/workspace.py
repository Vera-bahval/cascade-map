"""Persistent browser access to guest experiments, independent of CTF sessions."""

import json

from finbot.config import settings

COOKIE = "finbot_experiment_workspace"
LIFETIME = 365 * 86400


def registered_owner(owner):
    from finbot.core.data.database import SessionLocal
    from finbot.core.data.models import User

    namespace, user_id = json.loads(owner)
    with SessionLocal() as db:
        return (
            db.query(User.user_id)
            .filter(User.user_id == user_id, User.namespace == namespace)
            .first()
            is not None
        )


def workspace_owner(request, response, session, store):
    current = json.dumps([session.namespace, session.user_id], separators=(",", ":"))
    if not session.is_temporary:
        store.forget_workspace(current)
        response.delete_cookie(COOKIE, path="/")
        return current

    token = request.cookies.get(COOKIE)
    previous = store.workspace(token)
    if previous and registered_owner(previous):
        store.forget_workspace(previous)
        previous = None
    selected = previous or current
    # A guest token must never become a second login method for a bound account.
    if not previous and registered_owner(current):
        response.delete_cookie(COOKIE, path="/")
        return current
    token = store.remember_workspace(selected, LIFETIME, token if previous else None)
    response.set_cookie(
        COOKIE,
        token,
        max_age=LIFETIME,
        httponly=True,
        secure=settings.SESSION_COOKIE_SECURE,
        samesite="lax",
        path="/",
    )
    return selected
