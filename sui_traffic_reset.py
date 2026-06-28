#!/usr/bin/env python3
"""
External traffic reset tool for s-ui.

This script does not modify s-ui source code. It updates the s-ui SQLite
database directly, and can be run manually or by cron.
"""

from __future__ import annotations

import argparse
import calendar
import contextlib
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import base64
import hashlib
import hmac
import io
import json
import os
import secrets
import sqlite3
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Sequence
from urllib import error as urllib_error
from urllib import request as urllib_request
from urllib.parse import unquote, urlparse
from zoneinfo import ZoneInfo


DEFAULT_DB = "/usr/local/s-ui/db/s-ui.db"
ACTOR = "sui-traffic-reset"
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


class ToolError(Exception):
    pass


@dataclass
class Rule:
    id: int
    target_type: str
    target_value: str
    cycle: str
    interval_days: int
    day_of_month: int
    hour: int
    minute: int
    timezone: str
    enable_after_reset: bool
    next_reset: int


@dataclass
class AuthUser:
    username: str
    role: str
    client_names: list[str]


@dataclass
class SessionInfo:
    user: AuthUser
    expires: int
    csrf_token: str


def connect(db_path: str) -> sqlite3.Connection:
    if not os.path.exists(db_path):
        raise ToolError(f"database not found: {db_path}")
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 10000")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def columns(conn: sqlite3.Connection, table: str) -> set[str]:
    if not table_exists(conn, table):
        return set()
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def ensure_sui_schema(conn: sqlite3.Connection) -> None:
    cols = columns(conn, "clients")
    required = {"id", "name", "up", "down"}
    missing = required - cols
    if missing:
        raise ToolError("clients table is missing columns: " + ", ".join(sorted(missing)))


def ensure_rule_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sui_traffic_reset_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            target_type TEXT NOT NULL,
            target_value TEXT NOT NULL DEFAULT '',
            cycle TEXT NOT NULL DEFAULT 'monthly',
            interval_days INTEGER NOT NULL DEFAULT 30,
            day_of_month INTEGER NOT NULL DEFAULT 1,
            hour INTEGER NOT NULL DEFAULT 0,
            minute INTEGER NOT NULL DEFAULT 0,
            timezone TEXT NOT NULL DEFAULT 'local',
            enable_after_reset INTEGER NOT NULL DEFAULT 1,
            enabled INTEGER NOT NULL DEFAULT 1,
            next_reset INTEGER NOT NULL,
            last_reset INTEGER NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            UNIQUE(target_type, target_value)
        )
        """
    )
    conn.commit()


def ensure_auth_schema(conn: sqlite3.Connection, admin_user: str, admin_password: str) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sui_traffic_reset_accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'user',
            client_names TEXT NOT NULL DEFAULT '[]',
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        )
        """
    )
    now_ts = int(time.time())
    existing = conn.execute(
        "SELECT id FROM sui_traffic_reset_accounts WHERE username = ?",
        (admin_user,),
    ).fetchone()
    password_hash = hash_password(admin_password)
    if existing:
        conn.execute(
            """
            UPDATE sui_traffic_reset_accounts
            SET password_hash = ?, role = 'admin', client_names = '[]', updated_at = ?
            WHERE username = ?
            """,
            (password_hash, now_ts, admin_user),
        )
    else:
        conn.execute(
            """
            INSERT INTO sui_traffic_reset_accounts (
                username, password_hash, role, client_names, created_at, updated_at
            )
            VALUES (?, ?, 'admin', '[]', ?, ?)
            """,
            (admin_user, password_hash, now_ts, now_ts),
        )
    conn.commit()


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    iterations = 210_000
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return "pbkdf2_sha256${}${}${}".format(
        iterations,
        base64.urlsafe_b64encode(salt).decode("ascii"),
        base64.urlsafe_b64encode(digest).decode("ascii"),
    )


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations_raw, salt_raw, digest_raw = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        salt = base64.urlsafe_b64decode(salt_raw.encode("ascii"))
        expected = base64.urlsafe_b64decode(digest_raw.encode("ascii"))
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iterations_raw))
        return hmac.compare_digest(actual, expected)
    except Exception:
        return False


def parse_client_names(value: str | None) -> list[str]:
    if not value:
        return []
    try:
        raw = json.loads(value)
    except json.JSONDecodeError:
        raw = [part.strip() for part in value.split(",")]
    if not isinstance(raw, list):
        return []
    names = []
    for item in raw:
        name = str(item).strip()
        if name and name not in names:
            names.append(name)
    return names


def account_to_json(row: sqlite3.Row) -> dict[str, object]:
    return {
        "username": row["username"],
        "role": row["role"],
        "clientNames": parse_client_names(row["client_names"]),
    }


def get_account(conn: sqlite3.Connection, username: str) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT username, password_hash, role, client_names
        FROM sui_traffic_reset_accounts
        WHERE username = ?
        """,
        (username,),
    ).fetchone()


def authenticate_account(conn: sqlite3.Connection, username: str, password: str) -> AuthUser | None:
    row = get_account(conn, username)
    if not row or not verify_password(password, row["password_hash"]):
        return None
    return AuthUser(
        username=row["username"],
        role=row["role"],
        client_names=parse_client_names(row["client_names"]),
    )


def fetch_accounts(conn: sqlite3.Connection) -> list[dict[str, object]]:
    rows = conn.execute(
        """
        SELECT username, role, client_names
        FROM sui_traffic_reset_accounts
        ORDER BY role, username
        """
    ).fetchall()
    return [account_to_json(row) for row in rows]


def upsert_account(
    conn: sqlite3.Connection,
    *,
    username: str,
    password: str,
    role: str,
    client_names: Sequence[str],
) -> None:
    username = username.strip()
    if not username:
        raise ToolError("username is required")
    if role != "user":
        raise ToolError("only normal user accounts can be managed here")
    clean_names = parse_client_names(json.dumps(list(client_names)))
    now_ts = int(time.time())
    existing = get_account(conn, username)
    if existing:
        if existing["role"] == "admin":
            raise ToolError("admin account is managed by environment variables")
        updates = ["role = ?", "client_names = ?", "updated_at = ?"]
        params: list[object] = [role, json.dumps(clean_names), now_ts]
        if password:
            updates.insert(0, "password_hash = ?")
            params.insert(0, hash_password(password))
        params.append(username)
        conn.execute(
            f"UPDATE sui_traffic_reset_accounts SET {', '.join(updates)} WHERE username = ?",
            params,
        )
    else:
        if not password:
            raise ToolError("password is required for new account")
        conn.execute(
            """
            INSERT INTO sui_traffic_reset_accounts (
                username, password_hash, role, client_names, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (username, hash_password(password), role, json.dumps(clean_names), now_ts, now_ts),
        )


def parse_clock(value: str) -> tuple[int, int]:
    try:
        hour_raw, minute_raw = value.split(":", 1)
        hour = int(hour_raw)
        minute = int(minute_raw)
    except ValueError as exc:
        raise ToolError("--time must use HH:MM, for example 00:00") from exc
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ToolError("--time must be between 00:00 and 23:59")
    return hour, minute


def load_tz(name: str):
    if name == "local":
        return datetime.now().astimezone().tzinfo
    try:
        return ZoneInfo(name)
    except Exception as exc:
        raise ToolError(f"invalid timezone: {name}") from exc


def monthly_candidate(year: int, month: int, day: int, hour: int, minute: int, tz) -> datetime:
    max_day = calendar.monthrange(year, month)[1]
    return datetime(year, month, min(day, max_day), hour, minute, tzinfo=tz)


def next_month(year: int, month: int) -> tuple[int, int]:
    if month == 12:
        return year + 1, 1
    return year, month + 1


def next_reset_ts(
    *,
    cycle: str,
    interval_days: int,
    day_of_month: int,
    hour: int,
    minute: int,
    timezone: str,
    after_ts: int | None = None,
) -> int:
    tz = load_tz(timezone)
    now = datetime.fromtimestamp(after_ts or int(time.time()), tz)

    if cycle == "monthly":
        if not (1 <= day_of_month <= 31):
            raise ToolError("--day must be between 1 and 31")
        candidate = monthly_candidate(now.year, now.month, day_of_month, hour, minute, tz)
        if candidate <= now:
            year, month = next_month(now.year, now.month)
            candidate = monthly_candidate(year, month, day_of_month, hour, minute, tz)
        return int(candidate.timestamp())

    if cycle == "days":
        if interval_days <= 0:
            raise ToolError("--interval-days must be greater than 0")
        candidate = datetime(now.year, now.month, now.day, hour, minute, tzinfo=tz)
        while candidate <= now:
            candidate += timedelta(days=interval_days)
        return int(candidate.timestamp())

    raise ToolError("cycle must be monthly or days")


def format_size(value: int) -> str:
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    size = float(value or 0)
    for unit in units:
        if abs(size) < 1024 or unit == units[-1]:
            return f"{size:.2f} {unit}"
        size /= 1024
    return f"{size:.2f} TiB"


def selected_clients(
    conn: sqlite3.Connection,
    *,
    all_clients: bool = False,
    users: Sequence[str] = (),
    group: str | None = None,
) -> list[sqlite3.Row]:
    clauses: list[str] = []
    params: list[str] = []

    if users:
        placeholders = ",".join("?" for _ in users)
        clauses.append(f"name IN ({placeholders})")
        params.extend(users)
    if group is not None:
        clauses.append('"group" = ?')
        params.append(group)
    if not all_clients and not clauses:
        raise ToolError("choose --all, --user, or --group")

    where_sql = " OR ".join(f"({clause})" for clause in clauses) if clauses else "1 = 1"
    return list(
        conn.execute(
            f"""
            SELECT id, name, up, down,
                   COALESCE(volume, 0) AS volume,
                   COALESCE(expiry, 0) AS expiry,
                   COALESCE(enable, 1) AS enable,
                   COALESCE("group", '') AS "group"
            FROM clients
            WHERE {where_sql}
            ORDER BY id
            """,
            params,
        )
    )


def clients_for_target(conn: sqlite3.Connection, target_type: str, target_value: str = "") -> list[sqlite3.Row]:
    if target_type == "all":
        return selected_clients(conn, all_clients=True)
    if target_type == "client":
        if not target_value:
            raise ToolError("client target needs a name")
        return selected_clients(conn, users=[target_value])
    if target_type == "group":
        return selected_clients(conn, group=target_value)
    raise ToolError("targetType must be all, client, or group")


def log_change(conn: sqlite3.Connection, client_name: str, action: str, now_ts: int) -> None:
    if not table_exists(conn, "changes"):
        return
    cols = columns(conn, "changes")
    required = {"date_time", "actor", "key", "action", "obj"}
    if not required.issubset(cols):
        return
    conn.execute(
        'INSERT INTO changes (date_time, actor, "key", action, obj) VALUES (?, ?, ?, ?, ?)',
        (now_ts, ACTOR, "clients", action, json.dumps(client_name)),
    )


def reset_clients(
    conn: sqlite3.Connection,
    clients: Sequence[sqlite3.Row],
    *,
    enable_after_reset: bool,
    dry_run: bool = False,
) -> int:
    if not clients:
        return 0

    client_cols = columns(conn, "clients")
    now_ts = int(time.time())
    changed = 0

    for client in clients:
        used = int(client["up"] or 0) + int(client["down"] or 0)
        print(f"{client['name']}: reset {format_size(used)}")
        changed += 1

    if dry_run:
        return changed

    ids = [str(client["id"]) for client in clients]
    placeholders = ",".join("?" for _ in ids)
    updates = ["up = 0", "down = 0"]
    if "total_up" in client_cols:
        updates.insert(0, "total_up = COALESCE(total_up, 0) + COALESCE(up, 0)")
    if "total_down" in client_cols:
        updates.insert(1, "total_down = COALESCE(total_down, 0) + COALESCE(down, 0)")
    if enable_after_reset and "enable" in client_cols:
        if "expiry" in client_cols:
            updates.append("enable = CASE WHEN COALESCE(expiry, 0) = 0 OR expiry > ? THEN 1 ELSE enable END")
            params: list[object] = [now_ts, *ids]
        else:
            updates.append("enable = 1")
            params = [*ids]
    else:
        params = [*ids]

    conn.execute(f"UPDATE clients SET {', '.join(updates)} WHERE id IN ({placeholders})", params)
    for client in clients:
        log_change(conn, str(client["name"]), "reset", now_ts)
    return changed


def set_clients_expiry(
    conn: sqlite3.Connection,
    clients: Sequence[sqlite3.Row],
    *,
    expiry: int,
    enable_after_update: bool,
    dry_run: bool = False,
) -> int:
    if expiry < 0:
        raise ToolError("expiry must be 0 or a Unix timestamp")
    if not clients:
        return 0

    client_cols = columns(conn, "clients")
    if "expiry" not in client_cols:
        raise ToolError("clients table does not have expiry column")

    now_ts = int(time.time())
    expiry_label = "unlimited" if expiry == 0 else datetime.fromtimestamp(expiry).strftime("%Y-%m-%d %H:%M:%S")
    for client in clients:
        print(f"{client['name']}: expiry -> {expiry_label}")

    if dry_run:
        return len(clients)

    ids = [str(client["id"]) for client in clients]
    placeholders = ",".join("?" for _ in ids)
    updates = ["expiry = ?"]
    params: list[object] = [expiry]

    if enable_after_update and "enable" in client_cols:
        updates.append("enable = CASE WHEN ? = 0 OR ? > ? THEN 1 ELSE enable END")
        params.extend([expiry, expiry, now_ts])

    params.extend(ids)
    conn.execute(f"UPDATE clients SET {', '.join(updates)} WHERE id IN ({placeholders})", params)
    for client in clients:
        log_change(conn, str(client["name"]), "set-expiry", now_ts)
    return len(clients)


def print_clients(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    ensure_sui_schema(conn)
    rows = selected_clients(conn, all_clients=args.all, users=args.user, group=args.group)
    if not rows:
        print("No clients matched.")
        return
    for row in rows:
        used = int(row["up"] or 0) + int(row["down"] or 0)
        volume = int(row["volume"] or 0)
        limit = "unlimited" if volume == 0 else format_size(volume)
        print(f"{row['id']:>4}  {row['name']:<24} used={format_size(used):>12} limit={limit}")


def command_reset(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    ensure_sui_schema(conn)
    rows = selected_clients(conn, all_clients=args.all, users=args.user, group=args.group)
    count = reset_clients(
        conn,
        rows,
        enable_after_reset=args.enable_after_reset,
        dry_run=args.dry_run,
    )
    if not args.dry_run:
        conn.commit()
    print(f"{'Would reset' if args.dry_run else 'Reset'} {count} client(s).")


def target_args(args: argparse.Namespace) -> list[tuple[str, str]]:
    targets: list[tuple[str, str]] = []
    if args.all:
        targets.append(("all", ""))
    for user in args.user:
        targets.append(("client", user))
    if args.group is not None:
        targets.append(("group", args.group))
    if not targets:
        raise ToolError("choose --all, --user, or --group")
    return targets


def command_rule_add(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    ensure_sui_schema(conn)
    ensure_rule_schema(conn)
    hour, minute = parse_clock(args.time)

    for target_type, target_value in target_args(args):
        next_reset = upsert_rule(
            conn,
            target_type=target_type,
            target_value=target_value,
            cycle=args.cycle,
            interval_days=args.interval_days,
            day_of_month=args.day,
            hour=hour,
            minute=minute,
            timezone=args.timezone,
            enable_after_reset=args.enable_after_reset,
        )
        target_label = "*" if target_type == "all" else f"{target_type}:{target_value}"
        print(f"Saved rule for {target_label}; next reset at {datetime.fromtimestamp(next_reset)}")
    conn.commit()


def upsert_rule(
    conn: sqlite3.Connection,
    *,
    target_type: str,
    target_value: str,
    cycle: str,
    interval_days: int,
    day_of_month: int,
    hour: int,
    minute: int,
    timezone: str,
    enable_after_reset: bool,
) -> int:
    ensure_sui_schema(conn)
    ensure_rule_schema(conn)
    if target_type not in {"all", "client", "group"}:
        raise ToolError("targetType must be all, client, or group")
    if target_type == "all":
        target_value = ""
    elif not target_value:
        raise ToolError(f"{target_type} target needs a value")

    next_reset = next_reset_ts(
        cycle=cycle,
        interval_days=interval_days,
        day_of_month=day_of_month,
        hour=hour,
        minute=minute,
        timezone=timezone,
    )
    now_ts = int(time.time())
    conn.execute(
        """
        INSERT INTO sui_traffic_reset_rules (
            target_type, target_value, cycle, interval_days, day_of_month,
            hour, minute, timezone, enable_after_reset, enabled, next_reset,
            created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
        ON CONFLICT(target_type, target_value) DO UPDATE SET
            cycle = excluded.cycle,
            interval_days = excluded.interval_days,
            day_of_month = excluded.day_of_month,
            hour = excluded.hour,
            minute = excluded.minute,
            timezone = excluded.timezone,
            enable_after_reset = excluded.enable_after_reset,
            enabled = 1,
            next_reset = excluded.next_reset,
            updated_at = excluded.updated_at
        """,
        (
            target_type,
            target_value,
            cycle,
            interval_days,
            day_of_month,
            hour,
            minute,
            timezone,
            1 if enable_after_reset else 0,
            next_reset,
            now_ts,
            now_ts,
        ),
    )
    return next_reset


def fetch_rules(conn: sqlite3.Connection, due_only: bool = False) -> list[Rule]:
    ensure_rule_schema(conn)
    params: list[object] = []
    where = "enabled = 1"
    if due_only:
        where += " AND next_reset <= ?"
        params.append(int(time.time()))
    rows = conn.execute(
        f"""
        SELECT id, target_type, target_value, cycle, interval_days, day_of_month,
               hour, minute, timezone, enable_after_reset, next_reset
        FROM sui_traffic_reset_rules
        WHERE {where}
        ORDER BY next_reset, id
        """,
        params,
    ).fetchall()
    return [
        Rule(
            id=row["id"],
            target_type=row["target_type"],
            target_value=row["target_value"],
            cycle=row["cycle"],
            interval_days=row["interval_days"],
            day_of_month=row["day_of_month"],
            hour=row["hour"],
            minute=row["minute"],
            timezone=row["timezone"],
            enable_after_reset=bool(row["enable_after_reset"]),
            next_reset=row["next_reset"],
        )
        for row in rows
    ]


def clients_for_rule(conn: sqlite3.Connection, rule: Rule) -> list[sqlite3.Row]:
    if rule.target_type == "all":
        return selected_clients(conn, all_clients=True)
    if rule.target_type == "client":
        return selected_clients(conn, users=[rule.target_value])
    if rule.target_type == "group":
        return selected_clients(conn, group=rule.target_value)
    raise ToolError(f"unknown target type in rule {rule.id}: {rule.target_type}")


def command_rule_list(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    rules = fetch_rules(conn)
    if not rules:
        print("No rules configured.")
        return
    for rule in rules:
        target = "*" if rule.target_type == "all" else f"{rule.target_type}:{rule.target_value}"
        when = datetime.fromtimestamp(rule.next_reset)
        if rule.cycle == "monthly":
            schedule = f"monthly day {rule.day_of_month:02d} {rule.hour:02d}:{rule.minute:02d}"
        else:
            schedule = f"every {rule.interval_days} day(s) at {rule.hour:02d}:{rule.minute:02d}"
        print(f"{rule.id:>3}  {target:<28} {schedule:<28} next={when}")


def command_rule_remove(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    ensure_rule_schema(conn)
    cur = conn.execute("DELETE FROM sui_traffic_reset_rules WHERE id = ?", (args.id,))
    conn.commit()
    print(f"Removed {cur.rowcount} rule(s).")


def command_run_due(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    ensure_sui_schema(conn)
    rules = fetch_rules(conn, due_only=True)
    if not rules:
        print("No due rules.")
        return

    now_ts = int(time.time())
    total = 0
    for rule in rules:
        rows = clients_for_rule(conn, rule)
        print(f"Rule {rule.id}: {len(rows)} matched client(s)")
        total += reset_clients(
            conn,
            rows,
            enable_after_reset=rule.enable_after_reset,
            dry_run=args.dry_run,
        )
        if not args.dry_run:
            next_reset = next_reset_ts(
                cycle=rule.cycle,
                interval_days=rule.interval_days,
                day_of_month=rule.day_of_month,
                hour=rule.hour,
                minute=rule.minute,
                timezone=rule.timezone,
                after_ts=now_ts,
            )
            conn.execute(
                """
                UPDATE sui_traffic_reset_rules
                SET last_reset = ?, next_reset = ?, updated_at = ?
                WHERE id = ?
                """,
                (now_ts, next_reset, now_ts, rule.id),
            )

    if not args.dry_run:
        conn.commit()
    print(f"{'Would reset' if args.dry_run else 'Reset'} {total} client(s) from due rule(s).")


def client_next_rule(row: sqlite3.Row, rules: Sequence[Rule]) -> Rule | None:
    matched = []
    for rule in rules:
        if rule.target_type == "all":
            matched.append(rule)
        elif rule.target_type == "client" and rule.target_value == row["name"]:
            matched.append(rule)
        elif rule.target_type == "group" and rule.target_value == row["group"]:
            matched.append(rule)
    if not matched:
        return None
    return min(matched, key=lambda rule: rule.next_reset)


def client_to_json(
    row: sqlite3.Row,
    rules: Sequence[Rule] = (),
    online_ips_by_user: dict[str, list[str]] | None = None,
) -> dict[str, object]:
    up = int(row["up"] or 0)
    down = int(row["down"] or 0)
    volume = int(row["volume"] or 0)
    next_rule = client_next_rule(row, rules)
    data = {
        "id": row["id"],
        "name": row["name"],
        "group": row["group"],
        "up": up,
        "down": down,
        "used": up + down,
        "volume": volume,
        "expiry": int(row["expiry"] or 0),
        "enable": bool(row["enable"]),
        "usedText": format_size(up + down),
        "volumeText": "unlimited" if volume == 0 else format_size(volume),
        "nextReset": next_rule.next_reset if next_rule else 0,
        "nextResetText": datetime.fromtimestamp(next_rule.next_reset).strftime("%Y-%m-%d %H:%M:%S") if next_rule else "",
    }
    if online_ips_by_user is not None:
        online_ips = online_ips_by_user.get(str(row["name"]), [])
        data.update({
            "online": bool(online_ips),
            "onlineIps": online_ips,
            "onlineIpCount": len(online_ips),
        })
    return data


def rule_to_json(rule: Rule) -> dict[str, object]:
    target = "*" if rule.target_type == "all" else rule.target_value
    return {
        "id": rule.id,
        "targetType": rule.target_type,
        "targetValue": rule.target_value,
        "target": target,
        "cycle": rule.cycle,
        "intervalDays": rule.interval_days,
        "dayOfMonth": rule.day_of_month,
        "hour": rule.hour,
        "minute": rule.minute,
        "time": f"{rule.hour:02d}:{rule.minute:02d}",
        "timezone": rule.timezone,
        "enableAfterReset": rule.enable_after_reset,
        "nextReset": rule.next_reset,
        "nextResetText": datetime.fromtimestamp(rule.next_reset).strftime("%Y-%m-%d %H:%M:%S"),
    }


def capture_output(func, *args, **kwargs) -> str:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        func(*args, **kwargs)
    return buf.getvalue().strip()


def endpoint_url(base_url: str, action: str) -> str:
    base = base_url.strip().rstrip("/")
    if not base:
        return ""
    parsed = urlparse(base)
    path = parsed.path.rstrip("/")
    if path.endswith("/api") or path.endswith("/apiv2"):
        return f"{base}/{action}"
    return f"{base}/apiv2/{action}"


def collect_ip_strings(value: object) -> list[str]:
    result: list[str] = []
    if isinstance(value, str):
        text = value.strip()
        if text:
            result.append(text)
    elif isinstance(value, list):
        for item in value:
            result.extend(collect_ip_strings(item))
    elif isinstance(value, dict):
        for key, item in value.items():
            if isinstance(item, (bool, int, float)):
                text = str(key).strip()
                if text:
                    result.append(text)
            result.extend(collect_ip_strings(item))
    return result


def normalize_online_ips(payload: object) -> dict[str, list[str]]:
    if isinstance(payload, dict) and "obj" in payload:
        payload = payload.get("obj")
    if not isinstance(payload, dict):
        return {}
    online: dict[str, list[str]] = {}
    for user, value in payload.items():
        name = str(user).strip()
        if not name:
            continue
        ips = sorted(set(collect_ip_strings(value)))
        online[name] = ips
    return online


class SuiOnlineClient:
    def __init__(self) -> None:
        self.base_url = os.environ.get("SUI_PANEL_URL", "").strip()
        self.token = os.environ.get("SUI_API_TOKEN", "").strip()
        self.timeout = max(1.0, float(os.environ.get("SUI_API_TIMEOUT", "3")))
        self.cache_ttl = max(1, int(os.environ.get("SUI_ONLINE_CACHE_TTL", "5")))
        self.cache_at = 0
        self.cache: dict[str, list[str]] = {}
        self.cache_error = ""
        self.lock = threading.Lock()

    def enabled(self) -> bool:
        return bool(self.base_url and self.token)

    def get_online_ips(self) -> tuple[dict[str, list[str]] | None, str]:
        if not self.enabled():
            return None, ""
        now = int(time.time())
        with self.lock:
            if self.cache_at and now - self.cache_at < self.cache_ttl:
                return self.cache, self.cache_error
        try:
            data = self.fetch_online_ips()
            error = ""
        except Exception as exc:
            data = {}
            error = str(exc)
        with self.lock:
            self.cache = data
            self.cache_error = error
            self.cache_at = now
        return data, error

    def fetch_online_ips(self) -> dict[str, list[str]]:
        url = endpoint_url(self.base_url, "onlineIps")
        req = urllib_request.Request(
            url,
            headers={
                "Accept": "application/json",
                "Token": self.token,
                "X-Requested-With": "XMLHttpRequest",
            },
        )
        try:
            with urllib_request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read(512 * 1024)
        except urllib_error.URLError as exc:
            raise ToolError(f"s-ui online API unavailable: {exc}") from exc
        payload = json.loads(raw.decode("utf-8"))
        if isinstance(payload, dict) and payload.get("success") is False:
            raise ToolError(str(payload.get("msg") or "s-ui online API returned failed"))
        return normalize_online_ips(payload)


class WebApp:
    def __init__(self, db_path: str, admin_user: str, admin_password: str):
        self.db_path = db_path
        self.admin_user = admin_user
        self.admin_password = admin_password
        self.sessions: dict[str, SessionInfo] = {}
        self.session_lock = threading.Lock()
        self.session_ttl = max(300, int(os.environ.get("RESET_SESSION_TTL", str(7 * 24 * 3600))))
        self.secure_cookie = os.environ.get("RESET_COOKIE_SECURE", "").lower() in {"1", "true", "yes", "on"}
        self.login_attempts: dict[str, list[int]] = {}
        self.login_lock = threading.Lock()
        self.login_window = max(60, int(os.environ.get("RESET_LOGIN_WINDOW", "600")))
        self.max_login_attempts = max(1, int(os.environ.get("RESET_LOGIN_MAX_ATTEMPTS", "8")))
        self.sui_online = SuiOnlineClient()

    def connect(self) -> sqlite3.Connection:
        return connect(self.db_path)

    def create_session(self, user: AuthUser) -> tuple[str, SessionInfo]:
        token = secrets.token_urlsafe(32)
        expires = int(time.time()) + self.session_ttl
        session = SessionInfo(user=user, expires=expires, csrf_token=secrets.token_urlsafe(32))
        with self.session_lock:
            self.sessions[token] = session
        return token, session

    def get_session(self, token: str) -> SessionInfo | None:
        if not token:
            return None
        with self.session_lock:
            session = self.sessions.get(token)
            if not session:
                return None
            if session.expires < int(time.time()):
                self.sessions.pop(token, None)
                return None
            return session

    def delete_session(self, token: str) -> None:
        with self.session_lock:
            self.sessions.pop(token, None)

    def login_limited(self, client_key: str) -> bool:
        now = int(time.time())
        cutoff = now - self.login_window
        with self.login_lock:
            attempts = [ts for ts in self.login_attempts.get(client_key, []) if ts >= cutoff]
            self.login_attempts[client_key] = attempts
            return len(attempts) >= self.max_login_attempts

    def record_failed_login(self, client_key: str) -> None:
        now = int(time.time())
        cutoff = now - self.login_window
        with self.login_lock:
            attempts = [ts for ts in self.login_attempts.get(client_key, []) if ts >= cutoff]
            attempts.append(now)
            self.login_attempts[client_key] = attempts

    def clear_failed_logins(self, client_key: str) -> None:
        with self.login_lock:
            self.login_attempts.pop(client_key, None)


def user_visible_clients(conn: sqlite3.Connection, user: AuthUser) -> list[sqlite3.Row]:
    if user.role == "admin":
        return selected_clients(conn, all_clients=True)
    if not user.client_names:
        return []
    return selected_clients(conn, users=user.client_names)


def user_visible_rules(conn: sqlite3.Connection, user: AuthUser, clients: Sequence[sqlite3.Row]) -> list[Rule]:
    rules = fetch_rules(conn)
    if user.role == "admin":
        return rules
    client_names = {str(client["name"]) for client in clients}
    groups = {str(client["group"]) for client in clients if client["group"]}
    return [
        rule for rule in rules
        if rule.target_type == "all"
        or (rule.target_type == "client" and rule.target_value in client_names)
        or (rule.target_type == "group" and rule.target_value in groups)
    ]


def require_admin(user: AuthUser) -> None:
    if user.role != "admin":
        raise ToolError("admin permission required")


def make_handler(app: WebApp):
    class Handler(BaseHTTPRequestHandler):
        server_version = "SUIResetWeb/1.0"

        def log_message(self, fmt: str, *args) -> None:
            print(f"{self.address_string()} - {fmt % args}")

        def send_security_headers(self) -> None:
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cache-Control", "no-store")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; "
                "script-src 'self' 'unsafe-inline'; "
                "style-src 'self' 'unsafe-inline'; "
                "img-src 'self' data:; "
                "connect-src 'self'; "
                "base-uri 'none'; "
                "frame-ancestors 'none'; "
                "form-action 'self'",
            )

        def send_json(
            self,
            status: int,
            payload: dict[str, object],
            extra_headers: dict[str, str] | None = None,
        ) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_security_headers()
            for key, value in (extra_headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def send_text(self, status: int, body: str, content_type: str = "text/plain; charset=utf-8") -> None:
            data = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_security_headers()
            self.end_headers()
            self.wfile.write(data)

        def read_json(self) -> dict[str, object]:
            length = int(self.headers.get("Content-Length", "0") or "0")
            if length == 0:
                return {}
            if length > 64 * 1024:
                raise ToolError("request body too large")
            return json.loads(self.rfile.read(length).decode("utf-8"))

        def session_token(self) -> str:
            raw = self.headers.get("Cookie", "")
            if not raw:
                return ""
            jar = cookies.SimpleCookie()
            jar.load(raw)
            morsel = jar.get("sui_reset_session")
            return morsel.value if morsel else ""

        def current_session(self) -> SessionInfo | None:
            return app.get_session(self.session_token())

        def require_auth(self) -> SessionInfo | None:
            session = self.current_session()
            if session:
                return session
            self.send_json(401, {"success": False, "error": "请先登录"})
            return None

        def require_csrf(self, session: SessionInfo) -> bool:
            token = self.headers.get("X-CSRF-Token", "")
            if token and hmac.compare_digest(token, session.csrf_token):
                return True
            self.send_json(403, {"success": False, "error": "安全校验失败，请刷新后重试"})
            return False

        def session_cookie(self, token: str, max_age: int | None = None) -> str:
            cookie = cookies.SimpleCookie()
            cookie["sui_reset_session"] = token
            cookie["sui_reset_session"]["path"] = "/"
            cookie["sui_reset_session"]["httponly"] = True
            cookie["sui_reset_session"]["samesite"] = "Strict"
            if app.secure_cookie:
                cookie["sui_reset_session"]["secure"] = True
            if max_age is not None:
                cookie["sui_reset_session"]["max-age"] = str(max_age)
            return cookie.output(header="").strip()

        def do_GET(self) -> None:
            path = urlparse(self.path).path
            if path in {"/", "/index.html"}:
                index_path = os.path.join(STATIC_DIR, "index.html")
                with open(index_path, "r", encoding="utf-8") as fp:
                    self.send_text(200, fp.read(), "text/html; charset=utf-8")
                return
            if path == "/api/health":
                self.send_json(200, {"success": True, "authRequired": True})
                return
            if not path.startswith("/api/"):
                self.send_json(404, {"success": False, "error": "not found"})
                return
            session = self.require_auth()
            if not session:
                return
            user = session.user
            try:
                with app.connect() as conn:
                    if path == "/api/me":
                        self.send_json(200, {
                            "success": True,
                            "user": {
                                "username": user.username,
                                "role": user.role,
                                "clientNames": user.client_names,
                            },
                            "csrfToken": session.csrf_token,
                        })
                        return
                    if path == "/api/clients":
                        ensure_sui_schema(conn)
                        rows = user_visible_clients(conn, user)
                        rules = user_visible_rules(conn, user, rows)
                        groups = sorted({str(row["group"]) for row in rows if row["group"]})
                        online_ips = None
                        online_error = ""
                        if user.role == "admin":
                            online_ips, online_error = app.sui_online.get_online_ips()
                        self.send_json(200, {
                            "success": True,
                            "clients": [client_to_json(row, rules, online_ips) for row in rows],
                            "groups": groups,
                            "onlineConfigured": app.sui_online.enabled(),
                            "onlineError": online_error,
                        })
                        return
                    if path == "/api/rules":
                        rows = user_visible_clients(conn, user)
                        rules = user_visible_rules(conn, user, rows)
                        self.send_json(200, {"success": True, "rules": [rule_to_json(rule) for rule in rules]})
                        return
                    if path == "/api/accounts":
                        require_admin(user)
                        self.send_json(200, {"success": True, "accounts": fetch_accounts(conn)})
                        return
                self.send_json(404, {"success": False, "error": "not found"})
            except Exception as exc:
                self.send_json(500, {"success": False, "error": str(exc)})

        def do_POST(self) -> None:
            path = urlparse(self.path).path
            if not path.startswith("/api/"):
                self.send_json(404, {"success": False, "error": "not found"})
                return
            try:
                data = self.read_json()
            except Exception as exc:
                self.send_json(400, {"success": False, "error": str(exc)})
                return
            if path == "/api/login":
                try:
                    client_key = self.client_address[0] if self.client_address else "unknown"
                    if app.login_limited(client_key):
                        self.send_json(429, {"success": False, "error": "登录失败次数过多，请稍后再试"})
                        return
                    username = str(data.get("username", "")).strip()
                    password = str(data.get("password", ""))
                    with app.connect() as conn:
                        user = authenticate_account(conn, username, password)
                    if not user:
                        app.record_failed_login(client_key)
                        self.send_json(401, {"success": False, "error": "用户名或密码错误"})
                        return
                    app.clear_failed_logins(client_key)
                    token, session = app.create_session(user)
                    self.send_json(
                        200,
                        {
                            "success": True,
                            "user": {
                                "username": user.username,
                                "role": user.role,
                                "clientNames": user.client_names,
                            },
                            "csrfToken": session.csrf_token,
                        },
                        {"Set-Cookie": self.session_cookie(token, app.session_ttl)},
                    )
                    return
                except Exception as exc:
                    self.send_json(500, {"success": False, "error": str(exc)})
                    return
            session = self.require_auth()
            if not session:
                return
            if not self.require_csrf(session):
                return
            user = session.user
            try:
                with app.connect() as conn:
                    if path == "/api/logout":
                        app.delete_session(self.session_token())
                        self.send_json(
                            200,
                            {"success": True},
                            {"Set-Cookie": self.session_cookie("", 0)},
                        )
                        return
                    if path == "/api/reset":
                        require_admin(user)
                        target_type = str(data.get("targetType", ""))
                        target_value = str(data.get("targetValue", ""))
                        rows = clients_for_target(conn, target_type, target_value)
                        output = capture_output(
                            reset_clients,
                            conn,
                            rows,
                            enable_after_reset=bool(data.get("enableAfterReset", True)),
                            dry_run=False,
                        )
                        conn.commit()
                        self.send_json(200, {"success": True, "count": len(rows), "output": output})
                        return
                    if path == "/api/expiry":
                        require_admin(user)
                        target_type = str(data.get("targetType", ""))
                        target_value = str(data.get("targetValue", ""))
                        rows = clients_for_target(conn, target_type, target_value)
                        expiry = int(data.get("expiry", 0) or 0)
                        output = capture_output(
                            set_clients_expiry,
                            conn,
                            rows,
                            expiry=expiry,
                            enable_after_update=bool(data.get("enableAfterUpdate", True)),
                            dry_run=False,
                        )
                        conn.commit()
                        self.send_json(200, {"success": True, "count": len(rows), "output": output})
                        return
                    if path == "/api/rules":
                        require_admin(user)
                        hour, minute = parse_clock(str(data.get("time", "00:00")))
                        next_reset = upsert_rule(
                            conn,
                            target_type=str(data.get("targetType", "")),
                            target_value=str(data.get("targetValue", "")),
                            cycle=str(data.get("cycle", "monthly")),
                            interval_days=int(data.get("intervalDays", 30) or 30),
                            day_of_month=int(data.get("dayOfMonth", 1) or 1),
                            hour=hour,
                            minute=minute,
                            timezone=str(data.get("timezone", "local")),
                            enable_after_reset=bool(data.get("enableAfterReset", True)),
                        )
                        conn.commit()
                        self.send_json(200, {
                            "success": True,
                            "nextReset": next_reset,
                            "nextResetText": datetime.fromtimestamp(next_reset).strftime("%Y-%m-%d %H:%M:%S"),
                        })
                        return
                    if path == "/api/run-due":
                        require_admin(user)
                        output = capture_output(command_run_due, conn, SimpleNamespace(dry_run=False))
                        self.send_json(200, {"success": True, "output": output})
                        return
                    if path == "/api/accounts":
                        require_admin(user)
                        upsert_account(
                            conn,
                            username=str(data.get("username", "")),
                            password=str(data.get("password", "")),
                            role=str(data.get("role", "user")),
                            client_names=data.get("clientNames", []) if isinstance(data.get("clientNames", []), list) else [],
                        )
                        conn.commit()
                        self.send_json(200, {"success": True})
                        return
                self.send_json(404, {"success": False, "error": "not found"})
            except Exception as exc:
                self.send_json(500, {"success": False, "error": str(exc)})

        def do_DELETE(self) -> None:
            path = urlparse(self.path).path
            session = self.require_auth()
            if not session:
                return
            if not self.require_csrf(session):
                return
            user = session.user
            parts = path.strip("/").split("/")
            if len(parts) == 3 and parts[:2] == ["api", "rules"]:
                try:
                    require_admin(user)
                    rule_id = int(parts[2])
                    with app.connect() as conn:
                        ensure_rule_schema(conn)
                        cur = conn.execute("DELETE FROM sui_traffic_reset_rules WHERE id = ?", (rule_id,))
                        conn.commit()
                    self.send_json(200, {"success": True, "removed": cur.rowcount})
                except Exception as exc:
                    self.send_json(500, {"success": False, "error": str(exc)})
                return
            if len(parts) == 3 and parts[:2] == ["api", "accounts"]:
                try:
                    require_admin(user)
                    username = unquote(parts[2])
                    if username == user.username:
                        raise ToolError("cannot delete current account")
                    with app.connect() as conn:
                        account = get_account(conn, username)
                        if account and account["role"] == "admin":
                            raise ToolError("admin account is managed by environment variables")
                        cur = conn.execute("DELETE FROM sui_traffic_reset_accounts WHERE username = ?", (username,))
                        conn.commit()
                    self.send_json(200, {"success": True, "removed": cur.rowcount})
                except Exception as exc:
                    self.send_json(500, {"success": False, "error": str(exc)})
                return
            self.send_json(404, {"success": False, "error": "not found"})

    return Handler


def scheduler_loop(db_path: str, interval: int, stop_event: threading.Event) -> None:
    while not stop_event.is_set():
        try:
            with connect(db_path) as conn:
                output = capture_output(command_run_due, conn, SimpleNamespace(dry_run=False))
                if output:
                    print(output)
        except Exception as exc:
            print(f"scheduler error: {exc}", file=sys.stderr)
        stop_event.wait(interval)


def command_serve(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    ensure_sui_schema(conn)
    ensure_rule_schema(conn)
    admin_user = os.environ.get("RESET_ADMIN_USER", "admin")
    admin_password = os.environ.get("RESET_ADMIN_PASSWORD", "admin")
    ensure_auth_schema(conn, admin_user, admin_password)
    if admin_password == "admin":
        print("WARNING: RESET_ADMIN_PASSWORD is using the default value; change it before exposing the UI.")
    app = WebApp(args.db, admin_user, admin_password)
    stop_event = threading.Event()
    scheduler = threading.Thread(
        target=scheduler_loop,
        args=(args.db, args.interval, stop_event),
        daemon=True,
    )
    scheduler.start()

    server = ThreadingHTTPServer((args.host, args.port), make_handler(app))
    print(f"Web UI listening on http://{args.host}:{args.port}")
    print(f"Login authentication is enabled. Admin user: {admin_user}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        server.server_close()


def add_target_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--all", action="store_true", help="target all clients")
    parser.add_argument("--user", action="append", default=[], help="target one client name; can be repeated")
    parser.add_argument("--group", help="target one s-ui client group")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="External s-ui traffic reset tool")
    parser.add_argument("--db", default=DEFAULT_DB, help=f"s-ui database path, default: {DEFAULT_DB}")
    sub = parser.add_subparsers(dest="command", required=True)

    list_parser = sub.add_parser("list", help="list clients and current traffic")
    add_target_options(list_parser)
    list_parser.set_defaults(func=print_clients)

    reset_parser = sub.add_parser("reset", help="reset selected clients now")
    add_target_options(reset_parser)
    reset_parser.add_argument("--enable-after-reset", action="store_true", help="enable selected clients again if not expired")
    reset_parser.add_argument("--dry-run", action="store_true", help="show what would happen without writing")
    reset_parser.set_defaults(func=command_reset)

    rule_add = sub.add_parser("rule-add", help="add or update an automatic reset rule")
    add_target_options(rule_add)
    rule_add.add_argument("--cycle", choices=["monthly", "days"], default="monthly")
    rule_add.add_argument("--day", type=int, default=1, help="monthly day, 1-31; larger than month length uses last day")
    rule_add.add_argument("--interval-days", type=int, default=30, help="used when --cycle days")
    rule_add.add_argument("--time", default="00:00", help="reset time in HH:MM")
    rule_add.add_argument("--timezone", default="local", help="IANA timezone, for example Asia/Shanghai")
    rule_add.add_argument("--enable-after-reset", action="store_true", default=True)
    rule_add.set_defaults(func=command_rule_add)

    rule_list = sub.add_parser("rule-list", help="list automatic reset rules")
    rule_list.set_defaults(func=command_rule_list)

    rule_remove = sub.add_parser("rule-remove", help="remove an automatic reset rule by id")
    rule_remove.add_argument("id", type=int)
    rule_remove.set_defaults(func=command_rule_remove)

    run_due = sub.add_parser("run-due", help="run all due automatic reset rules")
    run_due.add_argument("--dry-run", action="store_true", help="show what would happen without writing")
    run_due.set_defaults(func=command_run_due)

    serve = sub.add_parser("serve", help="start the web UI and due-rule scheduler")
    serve.add_argument("--host", default=os.environ.get("RESET_WEB_HOST", "0.0.0.0"))
    serve.add_argument("--port", type=int, default=int(os.environ.get("RESET_WEB_PORT", "8080")))
    serve.add_argument("--interval", type=int, default=int(os.environ.get("CHECK_INTERVAL", "60")))
    serve.set_defaults(func=command_serve)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        with connect(args.db) as conn:
            args.func(conn, args)
    except ToolError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except sqlite3.Error as exc:
        print(f"sqlite error: {exc}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
