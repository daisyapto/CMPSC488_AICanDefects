"""
User accounts: one account per person, so the audit trail names who did what.

    operator  - PIN, 4-6 digits (quick to type on the station touchscreen)
    engineer  - password, at least 8 characters with a letter and a digit

Accounts live in data/accounts.json. PINs and passwords are stored only as
salted PBKDF2-SHA256 hashes. Accounts are never deleted, only turned off, so old
audit entries always point to a real person. Each account is locked for a few
minutes after too many wrong attempts.

The first engineer account is created on first start (see web.py /api/setup);
there is no default PIN or password.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from datetime import datetime

from .config import DATA_DIR

ACCOUNTS_FILE = DATA_DIR / "accounts.json"
ROLES = ("operator", "engineer")
MAX_FAILS = 5                 # wrong attempts before the account is locked
LOCK_S = 5 * 60               # how long it stays locked
IDLE_LOGOUT_S = {"operator": 30 * 60, "engineer": 15 * 60}
_ITER = 200_000
_USERNAME = re.compile(r"^[a-z0-9][a-z0-9._-]{1,23}$")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _hash(secret: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", secret.encode(), bytes.fromhex(salt), _ITER).hex()


def check_secret(role: str, secret: str) -> None:
    """Raise ValueError if the PIN / password does not meet the rule for the role."""
    if role == "operator":
        if not (secret.isdigit() and 4 <= len(secret) <= 6):
            raise ValueError("An operator PIN must be 4-6 digits")
        if len(set(secret)) == 1 or secret in "0123456789" or secret in "9876543210":
            raise ValueError("Choose a PIN that is not all the same digit or a simple sequence")
    elif role == "engineer":
        if len(secret) < 8 or not re.search(r"[A-Za-z]", secret) or not re.search(r"\d", secret):
            raise ValueError("An engineer password needs at least 8 characters, with a letter and a digit")
        if len(secret) > 128:
            raise ValueError("Password is too long")
    else:
        raise ValueError("Unknown role")


class Accounts:
    def __init__(self, path=ACCOUNTS_FILE):
        self.path = path
        self.lock = threading.RLock()
        self.users: dict[str, dict] = {}
        self.sessions: dict[str, dict] = {}       # token -> {user, last}
        self._load()

    # ------------------------------------------------------------ storage
    def _load(self):
        try:
            data = json.loads(self.path.read_text())
            self.users = {u["username"]: u for u in data.get("users", [])}
        except (OSError, ValueError, KeyError, TypeError):
            self.users = {}

    def _save(self):
        DATA_DIR.mkdir(exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"users": list(self.users.values())}, indent=1))
        try:
            os.chmod(tmp, 0o600)                  # only the station user can read the hashes
        except OSError:
            pass
        os.replace(tmp, self.path)

    # ------------------------------------------------------------ queries
    @property
    def setup_needed(self) -> bool:
        """True until the first engineer account exists."""
        return not any(u["role"] == "engineer" and u["active"] for u in self.users.values())

    @staticmethod
    def public(u: dict) -> dict:
        return {k: u.get(k) for k in ("username", "name", "role", "active", "created", "last_login")} | {
            "locked": u.get("locked_until", 0) > time.time()}

    def list(self) -> list[dict]:
        with self.lock:
            return sorted((self.public(u) for u in self.users.values()),
                          key=lambda u: (not u["active"], u["role"] != "engineer", u["name"].lower()))

    def login_choices(self) -> list[dict]:
        """Active accounts for the switch-user picker: names and roles only."""
        return [{"username": u["username"], "name": u["name"], "role": u["role"]}
                for u in self.list() if u["active"]]

    # ------------------------------------------------------------ changes
    def add(self, username: str, name: str, role: str, secret: str) -> dict:
        username, name = username.strip().lower(), name.strip()
        if not _USERNAME.match(username):
            raise ValueError("Username: 2-24 characters, lowercase letters, digits, . _ -")
        if not 1 <= len(name) <= 40:
            raise ValueError("Enter the person's name")
        if role not in ROLES:
            raise ValueError("Role must be operator or engineer")
        check_secret(role, secret)
        with self.lock:
            if username in self.users:
                raise ValueError(f"Username {username} is already taken")
            salt = secrets.token_hex(16)
            u = {"username": username, "name": name, "role": role, "active": True,
                 "salt": salt, "hash": _hash(secret, salt), "created": _now(),
                 "last_login": None, "fails": 0, "locked_until": 0}
            self.users[username] = u
            self._save()
            return self.public(u)

    def update(self, username: str, *, by: str, name=None, role=None, active=None, secret=None) -> list[str]:
        """Change an account. Returns a list of what changed (for the audit trail)."""
        with self.lock:
            u = self.users.get(username)
            if not u:
                raise ValueError("No such account")
            changes = []
            new_role = role if role is not None else u["role"]
            new_active = bool(active) if active is not None else u["active"]
            if username == by and (new_role != u["role"] or not new_active):
                raise ValueError("You cannot change your own role or turn off your own account")
            if u["role"] == "engineer" and u["active"] and (new_role != "engineer" or not new_active):
                others = [x for x in self.users.values()
                          if x["role"] == "engineer" and x["active"] and x["username"] != username]
                if not others:
                    raise ValueError("At least one active engineer account is needed")
            if role is not None and role != u["role"]:
                if role not in ROLES:
                    raise ValueError("Role must be operator or engineer")
                if secret is None:
                    raise ValueError(f"Set a new {'PIN' if role == 'operator' else 'password'} when changing the role")
                changes.append(f"role {u['role']} -> {role}")
                u["role"] = role
            if name is not None and name.strip() != u["name"]:
                if not 1 <= len(name.strip()) <= 40:
                    raise ValueError("Enter the person's name")
                changes.append(f"name {u['name']} -> {name.strip()}")
                u["name"] = name.strip()
            if active is not None and bool(active) != u["active"]:
                u["active"] = bool(active)
                changes.append("account turned on" if u["active"] else "account turned off")
                if not u["active"]:
                    self._end_sessions(username)
            if secret is not None:
                check_secret(u["role"], secret)
                u["salt"] = secrets.token_hex(16)
                u["hash"] = _hash(secret, u["salt"])
                u["fails"], u["locked_until"] = 0, 0
                changes.append("PIN reset" if u["role"] == "operator" else "password reset")
            if changes:
                self._save()
            return changes

    def change_own(self, username: str, current: str, new: str) -> str:
        with self.lock:
            u = self.users.get(username)
            if not u or not hmac.compare_digest(_hash(current, u["salt"]), u["hash"]):
                raise ValueError("Current PIN / password is wrong")
            check_secret(u["role"], new)
            u["salt"] = secrets.token_hex(16)
            u["hash"] = _hash(new, u["salt"])
            self._save()
            return "PIN changed" if u["role"] == "operator" else "password changed"

    # ------------------------------------------------------------ sessions
    def login(self, username: str, secret: str) -> tuple[str, dict, list[str]]:
        """Returns (token, account, audit events). Raises ValueError with a message for the screen;
        the exception's .events lists what to record (failed login, account locked)."""
        username = username.strip().lower()
        with self.lock:
            u = self.users.get(username)
            if not u or not u["active"]:
                err = ValueError("Unknown or turned-off account")
                err.events = ["login failed: unknown or turned-off account"]
                raise err
            wait = u.get("locked_until", 0) - time.time()
            if wait > 0:
                err = ValueError(f"Account locked after too many wrong attempts - try again in {wait / 60:.0f} min")
                err.events = ["login refused: account locked"]
                raise err
            if not hmac.compare_digest(_hash(secret, u["salt"]), u["hash"]):
                u["fails"] = u.get("fails", 0) + 1
                events = [f"login failed ({u['fails']} of {MAX_FAILS})"]
                if u["fails"] >= MAX_FAILS:
                    u["fails"], u["locked_until"] = 0, time.time() + LOCK_S
                    events.append(f"account locked for {LOCK_S // 60} min")
                self._save()
                err = ValueError("Wrong PIN" if u["role"] == "operator" else "Wrong password")
                err.events = events
                raise err
            u["fails"], u["locked_until"], u["last_login"] = 0, 0, _now()
            self._save()
            token = secrets.token_urlsafe(32)
            self.sessions[token] = {"user": username, "last": time.monotonic()}
            return token, self.public(u), ["login"]

    def session(self, token, touch=True):
        """The account behind a session cookie, or None (expired, turned off, unknown)."""
        if not token:
            return None
        with self.lock:
            s = self.sessions.get(token)
            if not s:
                return None
            u = self.users.get(s["user"])
            if not u or not u["active"]:
                self.sessions.pop(token, None)
                return None
            idle = time.monotonic() - s["last"]
            if idle > IDLE_LOGOUT_S[u["role"]]:
                self.sessions.pop(token, None)
                return None
            if touch:
                s["last"] = time.monotonic()
            return {"username": u["username"], "name": u["name"], "role": u["role"],
                    "idle_left": int(IDLE_LOGOUT_S[u["role"]] - (0 if touch else idle))}

    def logout(self, token):
        with self.lock:
            self.sessions.pop(token or "", None)

    def _end_sessions(self, username):
        for t in [t for t, s in self.sessions.items() if s["user"] == username]:
            self.sessions.pop(t, None)
