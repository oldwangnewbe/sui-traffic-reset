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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
import sqlite3
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Sequence
from urllib.parse import urlparse
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


def client_to_json(row: sqlite3.Row) -> dict[str, object]:
    up = int(row["up"] or 0)
    down = int(row["down"] or 0)
    volume = int(row["volume"] or 0)
    return {
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
    }


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


class WebApp:
    def __init__(self, db_path: str, token: str):
        self.db_path = db_path
        self.token = token

    def connect(self) -> sqlite3.Connection:
        return connect(self.db_path)


def make_handler(app: WebApp):
    class Handler(BaseHTTPRequestHandler):
        server_version = "SUIResetWeb/1.0"

        def log_message(self, fmt: str, *args) -> None:
            print(f"{self.address_string()} - {fmt % args}")

        def send_json(self, status: int, payload: dict[str, object]) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def send_text(self, status: int, body: str, content_type: str = "text/plain; charset=utf-8") -> None:
            data = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def read_json(self) -> dict[str, object]:
            length = int(self.headers.get("Content-Length", "0") or "0")
            if length == 0:
                return {}
            return json.loads(self.rfile.read(length).decode("utf-8"))

        def authenticated(self) -> bool:
            if not app.token:
                return True
            return self.headers.get("X-Reset-Token") == app.token

        def require_auth(self) -> bool:
            if self.authenticated():
                return True
            self.send_json(401, {"success": False, "error": "invalid token"})
            return False

        def do_GET(self) -> None:
            path = urlparse(self.path).path
            if path in {"/", "/index.html"}:
                index_path = os.path.join(STATIC_DIR, "index.html")
                with open(index_path, "r", encoding="utf-8") as fp:
                    self.send_text(200, fp.read(), "text/html; charset=utf-8")
                return
            if path == "/api/health":
                self.send_json(200, {"success": True, "authRequired": bool(app.token)})
                return
            if not path.startswith("/api/"):
                self.send_json(404, {"success": False, "error": "not found"})
                return
            if not self.require_auth():
                return
            try:
                with app.connect() as conn:
                    if path == "/api/clients":
                        ensure_sui_schema(conn)
                        rows = selected_clients(conn, all_clients=True)
                        groups = sorted({str(row["group"]) for row in rows if row["group"]})
                        self.send_json(200, {
                            "success": True,
                            "clients": [client_to_json(row) for row in rows],
                            "groups": groups,
                        })
                        return
                    if path == "/api/rules":
                        rules = fetch_rules(conn)
                        self.send_json(200, {"success": True, "rules": [rule_to_json(rule) for rule in rules]})
                        return
                self.send_json(404, {"success": False, "error": "not found"})
            except Exception as exc:
                self.send_json(500, {"success": False, "error": str(exc)})

        def do_POST(self) -> None:
            path = urlparse(self.path).path
            if not path.startswith("/api/"):
                self.send_json(404, {"success": False, "error": "not found"})
                return
            if not self.require_auth():
                return
            try:
                data = self.read_json()
                with app.connect() as conn:
                    if path == "/api/reset":
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
                        output = capture_output(command_run_due, conn, SimpleNamespace(dry_run=False))
                        self.send_json(200, {"success": True, "output": output})
                        return
                self.send_json(404, {"success": False, "error": "not found"})
            except Exception as exc:
                self.send_json(500, {"success": False, "error": str(exc)})

        def do_DELETE(self) -> None:
            path = urlparse(self.path).path
            if not self.require_auth():
                return
            parts = path.strip("/").split("/")
            if len(parts) == 3 and parts[:2] == ["api", "rules"]:
                try:
                    rule_id = int(parts[2])
                    with app.connect() as conn:
                        ensure_rule_schema(conn)
                        cur = conn.execute("DELETE FROM sui_traffic_reset_rules WHERE id = ?", (rule_id,))
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
    token = os.environ.get("RESET_WEB_TOKEN", "")
    app = WebApp(args.db, token)
    stop_event = threading.Event()
    scheduler = threading.Thread(
        target=scheduler_loop,
        args=(args.db, args.interval, stop_event),
        daemon=True,
    )
    scheduler.start()

    server = ThreadingHTTPServer((args.host, args.port), make_handler(app))
    print(f"Web UI listening on http://{args.host}:{args.port}")
    if token:
        print("Web token authentication is enabled.")
    else:
        print("WARNING: RESET_WEB_TOKEN is empty; API authentication is disabled.")
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
