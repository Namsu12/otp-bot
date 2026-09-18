#!/usr/bin/env python3
import os
import re
import time
import hashlib
import threading
from datetime import datetime

import requests
import telebot
from telebot import types
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ==================== CONFIG ====================
BOT_TOKEN = os.getenv('BOT_TOKEN', '')
CHAT_ID = os.getenv('CHAT_ID', '')
USERNAME = os.getenv('USERNAME', '')
PASSWORD = os.getenv('PASSWORD', '')
SUPER_ADMIN = int(os.getenv('SUPER_ADMIN', '8993161626'))

TURSO_URL = os.getenv('TURSO_URL', '')
TURSO_TOKEN = os.getenv('TURSO_TOKEN', '')

BASE_URL = 'http://51.77.52.79/ints'
LOGIN_URL = f'{BASE_URL}/login'

if not BOT_TOKEN or not CHAT_ID:
    print("ERROR: BOT_TOKEN and CHAT_ID must be set!")
    exit(1)
if not TURSO_URL or not TURSO_TOKEN:
    print("ERROR: TURSO_URL and TURSO_TOKEN must be set!")
    exit(1)

bot = telebot.TeleBot(BOT_TOKEN)


# ==================== TURSO DATABASE (HTTP API) ====================
class DB:
    def __init__(self):
        self.url = TURSO_URL
        self.auth = TURSO_TOKEN
        self.lock = threading.Lock()
        self.http_url = self.url.replace('libsql://', 'https://')
        print(f"Turso HTTP: {self.http_url}")

    def _pipeline(self, requests_list):
        payload = {"requests": requests_list}
        headers = {
            "Authorization": f"Bearer {self.auth}",
            "Content-Type": "application/json",
        }
        return requests.post(
            f"{self.http_url}/v2/pipeline",
            json=payload,
            headers=headers,
            timeout=20,
        )

    def execute(self, sql, params=None):
        with self.lock:
            try:
                args = []
                if params:
                    for p in params:
                        if isinstance(p, int):
                            args.append({"type": "integer", "value": str(p)})
                        elif isinstance(p, float):
                            args.append({"type": "float", "value": p})
                        elif p is None:
                            args.append({"type": "null"})
                        else:
                            args.append({"type": "text", "value": str(p)})

                req = {"type": "execute", "stmt": {"sql": sql, "args": args}}
                close_req = {"type": "close"}

                r = self._pipeline([req, close_req])
                if r.status_code != 200:
                    print(f"DB HTTP {r.status_code}: {r.text[:200]}")
                    return None

                data = r.json()
                results = data.get('results', [])
                if not results:
                    return None

                first = results[0]
                if first.get('type') == 'error':
                    print(f"DB SQL error: {first.get('error')}")
                    return None

                if first.get('type') == 'execute':
                    resp = first.get('response', {})
                    result_data = resp.get('result', {})
                    rows_raw = result_data.get('rows', [])
                    rows = []
                    for row in rows_raw:
                        new_row = []
                        for cell in row:
                            if cell is None:
                                new_row.append(None)
                            elif isinstance(cell, dict):
                                new_row.append(cell.get('value'))
                            else:
                                new_row.append(cell)
                        rows.append(new_row)
                    return rows
                return None

            except Exception as e:
                print(f"DB error: {e}")
                return None

    def query(self, sql, params=None):
        r = self.execute(sql, params)
        return r if r else []

    def query_one(self, sql, params=None):
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def init_tables(self):
        tables = [
            """CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                first_seen TEXT,
                numbers_per_user INTEGER DEFAULT 3,
                country_code_on INTEGER DEFAULT 1
            )""",
            """CREATE TABLE IF NOT EXISTS admins (
                user_id INTEGER PRIMARY KEY,
                added_by INTEGER,
                added_at TEXT
            )""",
            """CREATE TABLE IF NOT EXISTS services (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE,
                created_at TEXT
            )""",
            """CREATE TABLE IF NOT EXISTS countries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                service_id INTEGER,
                name TEXT,
                code TEXT,
                created_at TEXT
            )""",
            """CREATE TABLE IF NOT EXISTS numbers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                country_id INTEGER,
                phone TEXT,
                assigned_to INTEGER DEFAULT 0,
                assigned_at TEXT,
                created_at TEXT
            )""",
            """CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )""",
            """CREATE TABLE IF NOT EXISTS withdrawals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                amount REAL,
                status TEXT DEFAULT 'pending',
                requested_at TEXT,
                processed_at TEXT
            )""",
        ]
        for sql in tables:
            self.execute(sql)

        defaults = {
            'otp_link': 'https://t.me/alohaotp',
            'numbers_per_user': '3',
            'country_code_default': '1',
        }
        for k, v in defaults.items():
            existing = self.query_one("SELECT value FROM settings WHERE key=?", [k])
            if not existing:
                self.execute("INSERT INTO settings (key, value) VALUES (?, ?)", [k, v])

        print("Database initialized")


db = DB()


def get_setting(key, default=''):
    row = db.query_one("SELECT value FROM settings WHERE key=?", [key])
    return row[0] if row else default


def set_setting(key, value):
    existing = db.query_one("SELECT value FROM settings WHERE key=?", [key])
    if existing:
        db.execute("UPDATE settings SET value=? WHERE key=?", [str(value), key])
    else:
        db.execute("INSERT INTO settings (key, value) VALUES (?, ?)", [key, str(value)])


def is_admin(user_id):
    if user_id == SUPER_ADMIN:
        return True
    row = db.query_one("SELECT user_id FROM admins WHERE user_id=?", [user_id])
    return row is not None


def get_all_admins():
    ids = [SUPER_ADMIN]
    for row in db.query("SELECT user_id FROM admins"):
        ids.append(row[0])
    return ids


def upsert_user(user_id, username, first_name):
    existing = db.query_one("SELECT user_id FROM users WHERE user_id=?", [user_id])
    if not existing:
        db.execute(
            "INSERT INTO users (user_id, username, first_name, first_seen, numbers_per_user, country_code_on) VALUES (?, ?, ?, ?, ?, ?)",
            [user_id, username or '', first_name or '', datetime.now().isoformat(),
             int(get_setting('numbers_per_user', '3')), 1]
        )
        return True
    return False


def get_user(user_id):
    row = db.query_one(
        "SELECT user_id, username, first_name, first_seen, numbers_per_user, country_code_on FROM users WHERE user_id=?",
        [user_id]
    )
    if not row:
        return None
    return {
        'user_id': row[0], 'username': row[1], 'first_name': row[2],
        'first_seen': row[3], 'numbers_per_user': int(row[4]) if row[4] else 3,
        'country_code_on': int(row[5]) if row[5] is not None else 1
    }
