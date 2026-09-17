#!/usr/bin/env python3
import os
import re
import time
import json
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
    print("❌ ERROR: BOT_TOKEN and CHAT_ID must be set!")
    exit(1)
if not TURSO_URL or not TURSO_TOKEN:
    print("❌ ERROR: TURSO_URL and TURSO_TOKEN must be set!")
    exit(1)

bot = telebot.TeleBot(BOT_TOKEN)

# ==================== TURSO DATABASE ====================
import libsql_experimental as libsql

class DB:
    def __init__(self):
        self.url = TURSO_URL
        self.auth = TURSO_TOKEN
        self.lock = threading.Lock()
        print(f"🔗 Connecting to: {self.url}")

    def execute(self, sql, params=None):
        with self.lock:
            try:
                conn = libsql.connect(database=self.url, auth_token=self.auth)
                cur = conn.execute(sql, params or [])
                rows = cur.fetchall()
                conn.commit()
                conn.close()
                return rows
            except Exception as e:
                print(f"⚠️ DB error: {e}")
                return None

    def query(self, sql, params=None):
        r = self.execute(sql, params)
        return [list(row) for row in r] if r else []

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

        print("✅ Database initialized")


db = DB()

# Settings helpers
def get_setting(key, default=''):
    row = db.query_one("SELECT value FROM settings WHERE key=?", [key])
    return row[0] if row else default

def set_setting(key, value):
    existing = db.query_one("SELECT value FROM settings WHERE key=?", [key])
    if existing:
        db.execute("UPDATE settings SET value=? WHERE key=?", [str(value), key])
    else:
        db.execute("INSERT INTO settings (key, value) VALUES (?, ?)", [key, str(value)])

# Admin check
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

# User helpers
def upsert_user(user_id, username, first_name):
    existing = db.query_one("SELECT user_id FROM users WHERE user_id=?", [user_id])
    if not existing:
        db.execute(
            "INSERT INTO users (user_id, username, first_name, first_seen, numbers_per_user, country_code_on) VALUES (?, ?, ?, ?, ?, ?)",
            [user_id, username or '', first_name or '', datetime.now().isoformat(), int(get_setting('numbers_per_user', '3')), 1]
        )
        return True
    return False

def get_user(user_id):
    row = db.query_one("SELECT user_id, username, first_name, first_seen, numbers_per_user, country_code_on FROM users WHERE user_id=?", [user_id])
    if not row:
        return None
    return {
        'user_id': row[0], 'username': row[1], 'first_name': row[2],
        'first_seen': row[3], 'numbers_per_user': row[4], 'country_code_on': row[5]
    }

# ==================== DASHBOARD SCRAPER ====================
class Dashboard:
    def __init__(self):
        self.session = requests.Session()
        retry = Retry(total=3, backoff_factor=1, status_forcelist=[500, 502, 503, 504])
        self.session.mount('http://', HTTPAdapter(max_retries=retry))
        self.session.mount('https://', HTTPAdapter(max_retries=retry))
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Linux; Android 12; Mobile) AppleWebKit/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
            'Connection': 'keep-alive',
        })
        self.is_logged_in = False
        self.processed_sms = set()
        self.sms_history = []
        self.last_check = None
        self.running = True

    def solve_captcha(self, text):
        m = re.search(r'(\d+)\s*([+\-*/])\s*(\d+)', text)
        if not m:
            return None
        a, op, b = int(m.group(1)), m.group(2), int(m.group(3))
        if op == '+': r = a + b
        elif op == '-': r = a - b
        elif op == '*': r = a * b
        elif op == '/': r = a // b if b else 0
        else: return None
        return str(r)

    def login(self):
        try:
            self.session.cookies.clear()
            page = self.session.get(LOGIN_URL, timeout=15)
            soup = BeautifulSoup(page.text, 'html.parser')
            form = soup.find('form')
            if not form:
                return False

            action = form.get('action', '')
            if action.startswith('http'):
                submit_url = action
            elif action:
                submit_url = f'{BASE_URL}/{action.lstrip("/")}'
            else:
                submit_url = LOGIN_URL

            data = {}
            captcha_field = None

            for inp in form.find_all('input'):
                name = inp.get('name') or inp.get('id')
                itype = inp.get('type', 'text')
                value = inp.get('value', '')
                if not name:
                    continue
                if itype == 'hidden':
                    data[name] = value
                elif 'user' in name.lower() or 'login' in name.lower():
                    data[name] = USERNAME
                elif 'pass' in name.lower():
                    data[name] = PASSWORD
                elif any(k in name.lower() for k in ['capt', 'verif', 'answer', 'result', 'math']):
                    captcha_field = name

            page_text = soup.get_text()
            if captcha_field:
                captcha_input = form.find('input', {'name': captcha_field})
                parent = captcha_input.find_parent() if captcha_input else None
                q_text = parent.get_text(strip=True) if parent else page_text
                ans = self.solve_captcha(q_text) or self.solve_captcha(page_text)
                if ans:
                    data[captcha_field] = ans

            data.setdefault('submit', 'Login')

            self.session.headers.update({'Referer': LOGIN_URL, 'Origin': BASE_URL})
            resp = self.session.post(submit_url, data=data, timeout=15, allow_redirects=True)

            self.is_logged_in = 'login' not in resp.url.lower()
            return self.is_logged_in

        except Exception as e:
            print(f'❌ Login error: {e}')
            return False

    def get_page(self, path):
        if not self.is_logged_in:
            if not self.login():
                return None
        try:
            url = f'{BASE_URL}/agent/{path}'
            resp = self.session.get(url, timeout=20)
            if resp.status_code == 200 and 'login' not in resp.url.lower():
                return resp.text
            elif 'login' in resp.url.lower():
                self.is_logged_in = False
        except Exception as e:
            print(f'❌ Page error ({path}): {e}')
        return None

    def fetch_sms(self):
        if not self.is_logged_in:
            if not self.login():
                return []
        try:
            time.sleep(2)
            resp = self.session.get(f'{BASE_URL}/agent/SMSCDRReports', timeout=20)
            if resp.status_code == 200:
                self.last_check = datetime.now()
                return self.extract_sms(resp.text)
            elif resp.status_code in [401, 403]:
                self.is_logged_in = False
        except Exception as e:
            print(f'⚠️ Fetch error: {e}')
        return []

    def extract_sms(self, html):
        """Extract SMS rows from the CDR table"""
        soup = BeautifulSoup(html, 'html.parser')
        results = []
        tables = soup.find_all('table')
        if not tables:
            return results

        table = tables[0]
        rows = table.find_all('tr')

        for row in rows:
            cells = row.find_all('td')
            if len(cells) < 6:
                continue

            cell_texts = [c.get_text(strip=True) for c in cells]
            # Expected: Date, Range, Number, CLI, Client, SMS, Currency, My Payout, Client Payout
            if len(cell_texts) < 6:
                continue

            date_val = cell_texts[0] if len(cell_texts) > 0 else ''
            range_val = cell_texts[1] if len(cell_texts) > 1 else ''
            number = cell_texts[2] if len(cell_texts) > 2 else ''
            cli = cell_texts[3] if len(cell_texts) > 3 else ''
            client = cell_texts[4] if len(cell_texts) > 4 else ''
            sms_text = cell_texts[5] if len(cell_texts) > 5 else ''

            # Skip header/summary rows
            if not sms_text or len(sms_text) < 5:
                continue
            if 'total sms' in sms_text.lower():
                continue

            # Extract OTP
            otp = self.find_otp(sms_text)
            if not otp:
                # Try to extract from any digit group
                digits = re.findall(r'\b\d{4,6}\b', sms_text)
                if digits:
                    otp = digits[0]

            if not otp:
                continue

            # Clean number
            clean_number = re.sub(r'[^\d]', '', number)
            if not clean_number:
                continue

            # Hash to dedupe
            row_hash = hashlib.md5(
                f"{date_val}|{number}|{sms_text}".encode()
            ).hexdigest()

            if row_hash in self.processed_sms:
                continue

            results.append({
                'date': date_val,
                'range': range_val,
                'number': clean_number,
                'raw_number': number,
                'cli': cli,
                'client': client,
                'sms': sms_text,
                'otp': otp,
                'hash': row_hash,
            })

        return results

    def find_otp(self, text):
        patterns = [
            r'(?:OTP|code|verification|pin)[:\s\-]*(\d{4,8})',
            r'(?:is|:)\s*(\d{4,8})\b',
            r'\b(\d{6})\b',
            r'\b(\d{5})\b',
            r'\b(\d{4})\b',
        ]
        for p in patterns:
            m = re.search(p, text, re.IGNORECASE)
            if m:
                otp = m.group(1) if m.lastindex else m.group(0)
                if otp.isdigit() and 4 <= len(otp) <= 8:
                    return otp
        return None

    def get_balance(self):
        html = self.get_page('Statements?ecuid=Qg==')
        if not html:
            return None
        soup = BeautifulSoup(html, 'html.parser')
        text = soup.get_text()
        patterns = [
            r'(?:balance|available|credit)[:\s]*[\$\€\£]?\s*([\d,]+\.?\d*)',
            r'[\$\€\£]\s*([\d,]+\.?\d*)',
        ]
        for p in patterns:
            m = re.search(p, text, re.IGNORECASE)
            if m:
                return m.group(0)
        return None


dashboard = Dashboard()

# ==================== MENU BUILDERS ====================
def main_menu(user_id):
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(types.InlineKeyboardButton("📱 Get Number", callback_data="get_number"))
    markup.add(
        types.InlineKeyboardButton("💸 Withdraw", callback_data="withdraw"),
        types.InlineKeyboardButton("💵 Balance", callback_data="balance")
    )
    markup.add(
        types.InlineKeyboardButton("🌍 Available Country", callback_data="country_list"),
        types.InlineKeyboardButton("📊 Status", callback_data="status")
    )
    markup.add(types.InlineKeyboardButton("❓ Help", callback_data="help"))
    if is_admin(user_id):
        markup.add(types.InlineKeyboardButton("🛠️ Admin Panel", callback_data="admin_panel"))
    return markup


def back_to_main():
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🔙 Main Menu", callback_data="back_main"))
    return markup


def services_menu():
    services = db.query("SELECT id, name FROM services ORDER BY name")
    if not services:
        return None, "❌ *No services available yet.*\n\nAdmin needs to add services first."
    markup = types.InlineKeyboardMarkup(row_width=2)
    buttons = [types.InlineKeyboardButton(f"⚙️ {s[1]}", callback_data=f"svc_{s[0]}") for s in services]
    markup.add(*buttons)
    markup.add(types.InlineKeyboardButton("🔙 Main Menu", callback_data="back_main"))
    return markup, "⚙️ *Select a Service:*"


def countries_menu(service_id):
    service = db.query_one("SELECT name FROM services WHERE id=?", [service_id])
    if not service:
        return None, "❌ Service not found."
    countries = db.query("SELECT id, name, code FROM countries WHERE service_id=? ORDER BY name", [service_id])
    if not countries:
        return None, f"❌ *No countries for {service[0]} yet.*"
    markup = types.InlineKeyboardMarkup(row_width=2)
    buttons = [types.InlineKeyboardButton(f"🌍 {c[1]} ({c[2]})", callback_data=f"ctry_{c[0]}") for c in countries]
    markup.add(*buttons)
    markup.add(types.InlineKeyboardButton("🔙 Services", callback_data="get_number"))
    return markup, f"🌍 *{service[0]}* — Select a Country:"


def numbers_screen(user_id, service_id, country_id):
    """Build the assigned numbers screen."""
    service = db.query_one("SELECT name FROM services WHERE id=?", [service_id])
    country = db.query_one("SELECT name, code FROM countries WHERE id=?", [country_id])
    user = get_user(user_id)
    if not service or not country or not user:
        return None, "❌ Error loading."

    # Check existing assignments for this user in this country
    assigned = db.query(
        "SELECT id, phone FROM numbers WHERE assigned_to=? AND country_id=? ORDER BY id",
        [user_id, country_id]
    )

    n_per_user = user['numbers_per_user']

    # If fewer assigned than required, assign more
    if len(assigned) < n_per_user:
        needed = n_per_user - len(assigned)
        available = db.query(
            "SELECT id, phone FROM numbers WHERE country_id=? AND assigned_to=0 LIMIT ?",
            [country_id, needed]
        )
        now = datetime.now().isoformat()
        for num in available:
            db.execute(
                "UPDATE numbers SET assigned_to=?, assigned_at=? WHERE id=?",
                [user_id, now, num[0]]
            )
        # Re-fetch
        assigned = db.query(
            "SELECT id, phone FROM numbers WHERE assigned_to=? AND country_id=? ORDER BY id",
            [user_id, country_id]
        )

    # Stock count
    stock = db.query_one(
        "SELECT COUNT(*) FROM numbers WHERE country_id=? AND assigned_to=0",
        [country_id]
    )
    stock_count = stock[0] if stock else 0

    # Build display
    code_on = user['country_code_on']
    code = country[1]

    if assigned:
        display_nums = []
        for n in assigned:
            raw = n[1]
            if code_on:
                display_nums.append(f"{code}{raw}" if not raw.startswith(code) else raw)
            else:
                display_nums.append(raw)
        assigned_text = "\n".join([f"• 📱 `{d}`" for d in display_nums])
    else:
        assigned_text = "_No numbers available right now._"

    title = f"🌍 *{country[0]} ({service[0]}) — {len(assigned)} Numbers Assigned:*"
    stock_line = f"📦 *Stock Left:* {stock_count}"
    status_line = "⏳ _Waiting for OTP..._"

    text = f"{title}\n\n*Country:* {country[0]} — {code}\n\n{assigned_text}\n\n{stock_line}\n{status_line}"

    # Build buttons
    markup = types.InlineKeyboardMarkup(row_width=1)

    # Number buttons (auto-copy as code blocks)
    for n in assigned:
        raw = n[1]
        if code_on:
            num_display = f"{code}{raw}" if not raw.startswith(code) else raw
        else:
            num_display = raw
        markup.add(types.InlineKeyboardButton(
            f"📋 {num_display}",
            callback_data=f"copy_{num_display}"
        ))

    # Change numbers
    markup.add(types.InlineKeyboardButton("🔄 Change Numbers", callback_data=f"chgnum_{service_id}_{country_id}"))

    # Change country / service (side by side)
    markup.row(
        types.InlineKeyboardButton("🌍 Change Country", callback_data=f"svc_{service_id}"),
        types.InlineKeyboardButton("⚙️ Change Service", callback_data="get_number")
    )

    # Country code toggle
    toggle_label = "🟢 Country Code: ON" if code_on else "🔴 Country Code: OFF"
    markup.add(types.InlineKeyboardButton(toggle_label, callback_data=f"togglecc_{service_id}_{country_id}"))

    # View OTP
    otp_link = get_setting('otp_link', 'https://t.me/alohaotp')
    markup.add(types.InlineKeyboardButton("📬 View OTP", url=otp_link))

    # Back
    markup.add(types.InlineKeyboardButton("🔙 Main Menu", callback_data="back_main"))

    return markup, text


# ==================== /start COMMAND ====================
@bot.message_handler(commands=['start'])
def cmd_start(message):
    user_id = message.from_user.id
    upsert_user(user_id, message.from_user.username, message.from_user.first_name)

    text = f"""
👋 *Welcome to NBHC OTP Bot*

Get virtual numbers, receive OTPs, and manage your account — all from here.

*Hello {message.from_user.first_name}!*

Select an option below 👇
"""
    bot.send_message(message.chat.id, text, parse_mode='Markdown', reply_markup=main_menu(user_id))


# ==================== CALLBACK HANDLER ====================
@bot.callback_query_handler(func=lambda call: True)
def handle_callback(call):
    user_id = call.from_user.id
    upsert_user(user_id, call.from_user.username, call.from_user.first_name)
    data = call.data

    try:
        # ========== BACK TO MAIN ==========
        if data == "back_main":
            bot.answer_callback_query(call.id)
            try:
                bot.delete_message(call.message.chat.id, call.message.message_id)
            except:
                pass
            cmd_start(call.message)
            return

        # ========== GET NUMBER ==========
        if data == "get_number":
            bot.answer_callback_query(call.id)
            markup, text = services_menu()
            if not markup:
                bot.edit_message_text(text, call.message.chat.id, call.message.message_id,
                                      parse_mode='Markdown', reply_markup=back_to_main())
            else:
                bot.edit_message_text(text, call.message.chat.id, call.message.message_id,
                                      parse_mode='Markdown', reply_markup=markup)
            return

        if data.startswith("svc_"):
            service_id = int(data.split("_")[1])
            bot.answer_callback_query(call.id)
            markup, text = countries_menu(service_id)
            if not markup:
                bot.edit_message_text(text, call.message.chat.id, call.message.message_id,
                                      parse_mode='Markdown', reply_markup=back_to_main())
            else:
                bot.edit_message_text(text, call.message.chat.id, call.message.message_id,
                                      parse_mode='Markdown', reply_markup=markup)
            return

        if data.startswith("ctry_"):
            country_id = int(data.split("_")[1])
            country = db.query_one("SELECT service_id FROM countries WHERE id=?", [country_id])
            if not country:
                bot.answer_callback_query(call.id, "❌ Country not found")
                return
            service_id = country[0]
            bot.answer_callback_query(call.id, "🌍 Loading numbers...")
            markup, text = numbers_screen(user_id, service_id, country_id)
            if markup:
                try:
                    bot.edit_message_text(text, call.message.chat.id, call.message.message_id,
                                          parse_mode='Markdown', reply_markup=markup)
                except:
                    bot.send_message(call.message.chat.id, text, parse_mode='Markdown', reply_markup=markup)
            return

        # ========== CHANGE NUMBERS ==========
        if data.startswith("chgnum_"):
            _, svc_id, ctry_id = data.split("_")
            svc_id, ctry_id = int(svc_id), int(ctry_id)
            # Release old numbers
            db.execute("UPDATE numbers SET assigned_to=0, assigned_at=NULL WHERE assigned_to=? AND country_id=?",
                       [user_id, ctry_id])
            bot.answer_callback_query(call.id, "🔄 Getting new numbers...")
            markup, text = numbers_screen(user_id, svc_id, ctry_id)
            if markup:
                try:
                    bot.edit_message_text(text, call.message.chat.id, call.message.message_id,
                                          parse_mode='Markdown', reply_markup=markup)
                except:
                    pass
            return

        # ========== COUNTRY CODE TOGGLE ==========
        if data.startswith("togglecc_"):
            _, svc_id, ctry_id = data.split("_")
            svc_id, ctry_id = int(svc_id), int(ctry_id)
            user = get_user(user_id)
            new_val = 0 if user['country_code_on'] else 1
            db.execute("UPDATE users SET country_code_on=? WHERE user_id=?", [new_val, user_id])
            bot.answer_callback_query(call.id, "✅ Toggled!")
            markup, text = numbers_screen(user_id, svc_id, ctry_id)
            if markup:
                try:
                    bot.edit_message_text(text, call.message.chat.id, call.message.message_id,
                                          parse_mode='Markdown', reply_markup=markup)
                except:
                    pass
            return

        # ========== COPY NUMBER ==========
        if data.startswith("copy_"):
            num = data.replace("copy_", "", 1)
            bot.answer_callback_query(call.id, "📋 Tap the message below to copy", show_alert=False)
            bot.send_message(call.message.chat.id, f"📋 Copy this number:\n\n`{num}`",
                             parse_mode='Markdown')
            return

        # ========== OTHER MAIN MENU BUTTONS ==========
        if data == "withdraw":
            bot.answer_callback_query(call.id, "💸 Withdraw coming soon")
            bot.send_message(call.message.chat.id,
                             "💸 *Withdraw*\n\n_Coming soon._",
                             parse_mode='Markdown', reply_markup=back_to_main())
            return

        if data == "balance":
            bot.answer_callback_query(call.id, "💵 Loading...")
            bal = dashboard.get_balance()
            text = f"💵 *Your Balance*\n\n💰 `{bal}`" if bal else "💵 *Your Balance*\n\n💰 `$0.00`"
            bot.send_message(call.message.chat.id, text, parse_mode='Markdown', reply_markup=back_to_main())
            return

        if data == "country_list":
            bot.answer_callback_query(call.id)
            rows = db.query("""
                SELECT c.name, c.code, COUNT(n.id)
                FROM countries c
                LEFT JOIN numbers n ON n.country_id = c.id AND n.assigned_to = 0
                GROUP BY c.id
                ORDER BY c.name
            """)
            if not rows:
                bot.send_message(call.message.chat.id, "🌍 *No countries added yet.*",
                                 parse_mode='Markdown', reply_markup=back_to_main())
                return
            text = "🌍 *Available Countries*\n\n"
            for name, code, count in rows:
                text += f"• *{name}* ({code}) — `{count}` in stock\n"
            bot.send_message(call.message.chat.id, text, parse_mode='Markdown', reply_markup=back_to_main())
            return

        if data == "status":
            bot.answer_callback_query(call.id, "📊 Loading...")
            text = f"""
📊 *Bot Status*

🔐 Login: {'✅ Yes' if dashboard.is_logged_in else '❌ No'}
📬 OTPs Forwarded: {len(dashboard.sms_history)}
🕐 Last Check: {dashboard.last_check.strftime('%H:%M:%S') if dashboard.last_check else 'Never'}
🔄 Monitoring: Active
☁️ Host: Koyeb Cloud
"""
            bot.send_message(call.message.chat.id, text, parse_mode='Markdown', reply_markup=back_to_main())
            return

        if data == "help":
            bot.answer_callback_query(call.id)
            text = """
❓ *Help*

• *Get Number* — Pick a service and country to get numbers
• *Balance* — Check your earnings
• *Withdraw* — Request payout (soon)
• *Available Country* — See all countries and stock
• *Status* — Bot health check

📢 *OTP Group:* Tap "View OTP" in the number screen.

Need help? Contact admin.
"""
            bot.send_message(call.message.chat.id, text, parse_mode='Markdown', reply_markup=back_to_main())
            return

        # ========== ADMIN PANEL ==========
        if data.startswith("admin_"):
            handle_admin(call)
            return

        if data == "admin_panel":
            if not is_admin(user_id):
                bot.answer_callback_query(call.id, "⛔ Admins only")
                return
            bot.answer_callback_query(call.id)
            markup = types.InlineKeyboardMarkup(row_width=2)
            markup.add(
                types.InlineKeyboardButton("⚙️ Add Service", callback_data="admin_add_service"),
                types.InlineKeyboardButton("🌍 Add Country", callback_data="admin_add_country")
            )
            markup.add(
                types.InlineKeyboardButton("📥 Add Numbers", callback_data="admin_add_numbers"),
                types.InlineKeyboardButton("📋 List Numbers", callback_data="admin_list_numbers")
            )
            markup.add(
                types.InlineKeyboardButton("🔗 Set OTP Link", callback_data="admin_set_otp_link"),
                types.InlineKeyboardButton("⚙️ Numbers/User", callback_data="admin_set_npu")
            )
            markup.add(
                types.InlineKeyboardButton("📢 Broadcast", callback_data="admin_broadcast"),
                types.InlineKeyboardButton("👥 Users", callback_data="admin_users")
            )
            markup.add(
                types.InlineKeyboardButton("👑 Admins", callback_data="admin_admins"),
                types.InlineKeyboardButton("📊 Stats", callback_data="admin_stats")
            )
            markup.add(types.InlineKeyboardButton("🔙 Main Menu", callback_data="back_main"))
            bot.edit_message_text("🛠️ *Admin Panel*\n\nPick an option:",
                                  call.message.chat.id, call.message.message_id,
                                  parse_mode='Markdown', reply_markup=markup)
            return

    except Exception as e:
        print(f"Callback error: {e}")
        bot.answer_callback_query(call.id, "⚠️ Error, try again")

# ==================== ADMIN PANEL ====================
def handle_admin(call):
    user_id = call.from_user.id
    if not is_admin(user_id):
        bot.answer_callback_query(call.id, "⛔ Admins only")
        return

    data = call.data

    # ---- BACK TO ADMIN ----
    if data == "admin_back":
        bot.answer_callback_query(call.id)
        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.add(
            types.InlineKeyboardButton("⚙️ Add Service", callback_data="admin_add_service"),
            types.InlineKeyboardButton("🌍 Add Country", callback_data="admin_add_country")
        )
        markup.add(
            types.InlineKeyboardButton("📥 Add Numbers", callback_data="admin_add_numbers"),
            types.InlineKeyboardButton("📋 List Numbers", callback_data="admin_list_numbers")
        )
        markup.add(
            types.InlineKeyboardButton("🔗 Set OTP Link", callback_data="admin_set_otp_link"),
            types.InlineKeyboardButton("⚙️ Numbers/User", callback_data="admin_set_npu")
        )
        markup.add(
            types.InlineKeyboardButton("📢 Broadcast", callback_data="admin_broadcast"),
            types.InlineKeyboardButton("👥 Users", callback_data="admin_users")
        )
        markup.add(
            types.InlineKeyboardButton("👑 Admins", callback_data="admin_admins"),
            types.InlineKeyboardButton("📊 Stats", callback_data="admin_stats")
        )
        markup.add(types.InlineKeyboardButton("🔙 Main Menu", callback_data="back_main"))
        bot.edit_message_text("🛠️ *Admin Panel*\n\nPick an option:",
                              call.message.chat.id, call.message.message_id,
                              parse_mode='Markdown', reply_markup=markup)
        return

    # ---- ADD SERVICE ----
    if data == "admin_add_service":
        bot.answer_callback_query(call.id)
        msg = bot.send_message(call.message.chat.id,
                               "⚙️ *Add Service*\n\nSend the service name.\nExample: `WhatsApp`",
                               parse_mode='Markdown',
                               reply_markup=types.ForceReply(selective=True))
        bot.register_next_step_handler(msg, admin_save_service)
        return

    # ---- ADD COUNTRY ----
    if data == "admin_add_country":
        bot.answer_callback_query(call.id)
        services = db.query("SELECT id, name FROM services ORDER BY name")
        if not services:
            bot.send_message(call.message.chat.id, "❌ Add a service first.")
            return
        markup = types.InlineKeyboardMarkup(row_width=2)
        for s in services:
            markup.add(types.InlineKeyboardButton(f"⚙️ {s[1]}", callback_data=f"adminc_{s[0]}"))
        markup.add(types.InlineKeyboardButton("🔙 Back", callback_data="admin_back"))
        bot.edit_message_text("🌍 *Add Country* — pick a service first:",
                              call.message.chat.id, call.message.message_id,
                              parse_mode='Markdown', reply_markup=markup)
        return

    if data.startswith("adminc_"):
        svc_id = int(data.split("_")[1])
        bot.answer_callback_query(call.id)
        msg = bot.send_message(call.message.chat.id,
                               f"🌍 *Add Country to service ID {svc_id}*\n\n"
                               f"Send in format:\n`CountryName | Code`\n\nExample:\n`Nigeria | +234`",
                               parse_mode='Markdown',
                               reply_markup=types.ForceReply(selective=True))
        bot.register_next_step_handler(msg, admin_save_country, svc_id)
        return

    # ---- ADD NUMBERS ----
    if data == "admin_add_numbers":
        bot.answer_callback_query(call.id)
        services = db.query("SELECT id, name FROM services ORDER BY name")
        if not services:
            bot.send_message(call.message.chat.id, "❌ Add a service first.")
            return
        markup = types.InlineKeyboardMarkup(row_width=2)
        for s in services:
            markup.add(types.InlineKeyboardButton(f"⚙️ {s[1]}", callback_data=f"adminan_{s[0]}"))
        markup.add(types.InlineKeyboardButton("🔙 Back", callback_data="admin_back"))
        bot.edit_message_text("📥 *Add Numbers* — pick a service:",
                              call.message.chat.id, call.message.message_id,
                              parse_mode='Markdown', reply_markup=markup)
        return

    if data.startswith("adminan_"):
        svc_id = int(data.split("_")[1])
        bot.answer_callback_query(call.id)
        countries = db.query("SELECT id, name, code FROM countries WHERE service_id=? ORDER BY name", [svc_id])
        if not countries:
            bot.send_message(call.message.chat.id, "❌ No countries for that service.")
            return
        markup = types.InlineKeyboardMarkup(row_width=2)
        for c in countries:
            markup.add(types.InlineKeyboardButton(f"🌍 {c[1]} ({c[2]})", callback_data=f"adminanc_{c[0]}"))
        markup.add(types.InlineKeyboardButton("🔙 Back", callback_data="admin_back"))
        bot.edit_message_text("📥 *Add Numbers* — pick a country:",
                              call.message.chat.id, call.message.message_id,
                              parse_mode='Markdown', reply_markup=markup)
        return

    if data.startswith("adminanc_"):
        ctry_id = int(data.split("_")[1])
        bot.answer_callback_query(call.id)
        msg = bot.send_message(call.message.chat.id,
                               "📥 *Paste numbers*\n\n"
                               "Send numbers separated by commas, spaces, or new lines.\n"
                               "Example:\n`8097716173, 8097716408, 8097717000`",
                               parse_mode='Markdown',
                               reply_markup=types.ForceReply(selective=True))
        bot.register_next_step_handler(msg, admin_save_numbers, ctry_id)
        return

    # ---- LIST NUMBERS ----
    if data == "admin_list_numbers":
        bot.answer_callback_query(call.id)
        services = db.query("SELECT id, name FROM services ORDER BY name")
        if not services:
            bot.send_message(call.message.chat.id, "❌ No services yet.")
            return
        markup = types.InlineKeyboardMarkup(row_width=2)
        for s in services:
            markup.add(types.InlineKeyboardButton(f"⚙️ {s[1]}", callback_data=f"adminln_{s[0]}"))
        markup.add(types.InlineKeyboardButton("🔙 Back", callback_data="admin_back"))
        bot.edit_message_text("📋 *List Numbers* — pick a service:",
                              call.message.chat.id, call.message.message_id,
                              parse_mode='Markdown', reply_markup=markup)
        return

    if data.startswith("adminln_"):
        svc_id = int(data.split("_")[1])
        bot.answer_callback_query(call.id)
        rows = db.query("""
            SELECT c.name, c.code,
                (SELECT COUNT(*) FROM numbers WHERE country_id=c.id AND assigned_to=0) as avail,
                (SELECT COUNT(*) FROM numbers WHERE country_id=c.id AND assigned_to!=0) as assigned
            FROM countries c
            WHERE c.service_id=?
            ORDER BY c.name
        """, [svc_id])
        if not rows:
            bot.send_message(call.message.chat.id, "❌ No countries.")
            return
        text = "📋 *Numbers Overview*\n\n"
        for name, code, avail, assigned in rows:
            text += f"🌍 *{name}* ({code})\n   📦 Available: `{avail}` | 🔒 Assigned: `{assigned}`\n\n"
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 Back", callback_data="admin_back"))
        try:
            bot.edit_message_text(text[:4000], call.message.chat.id, call.message.message_id,
                                  parse_mode='Markdown', reply_markup=markup)
        except:
            bot.send_message(call.message.chat.id, text[:4000], parse_mode='Markdown', reply_markup=markup)
        return

    # ---- SET OTP LINK ----
    if data == "admin_set_otp_link":
        bot.answer_callback_query(call.id)
        current = get_setting('otp_link', 'https://t.me/alohaotp')
        msg = bot.send_message(call.message.chat.id,
                               f"🔗 *Set OTP Group Link*\n\n"
                               f"Current: `{current}`\n\n"
                               f"Send the new link (must start with `https://t.me/`)",
                               parse_mode='Markdown',
                               reply_markup=types.ForceReply(selective=True))
        bot.register_next_step_handler(msg, admin_save_otp_link)
        return

    # ---- SET NUMBERS PER USER ----
    if data == "admin_set_npu":
        bot.answer_callback_query(call.id)
        current = get_setting('numbers_per_user', '3')
        msg = bot.send_message(call.message.chat.id,
                               f"⚙️ *Numbers Per User*\n\n"
                               f"Current: `{current}`\n\n"
                               f"Send a number (1-10) to change the default for new users.",
                               parse_mode='Markdown',
                               reply_markup=types.ForceReply(selective=True))
        bot.register_next_step_handler(msg, admin_save_npu)
        return

    # ---- BROADCAST ----
    if data == "admin_broadcast":
        bot.answer_callback_query(call.id)
        msg = bot.send_message(call.message.chat.id,
                               "📢 *Broadcast*\n\nSend the message to send to all users:",
                               parse_mode='Markdown',
                               reply_markup=types.ForceReply(selective=True))
        bot.register_next_step_handler(msg, admin_send_broadcast)
        return

    # ---- USERS ----
    if data == "admin_users":
        bot.answer_callback_query(call.id)
        users = db.query("SELECT user_id, username, first_name, first_seen FROM users ORDER BY first_seen DESC LIMIT 30")
        if not users:
            bot.send_message(call.message.chat.id, "👥 No users yet.")
            return
        total = db.query_one("SELECT COUNT(*) FROM users")
        text = f"👥 *Users* ({total[0] if total else 0} total)\n\n"
        for u in users:
            uname = f"@{u[1]}" if u[1] else (u[2] or "Unknown")
            text += f"• `{u[0]}` — {uname}\n"
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 Back", callback_data="admin_back"))
        try:
            bot.edit_message_text(text[:4000], call.message.chat.id, call.message.message_id,
                                  parse_mode='Markdown', reply_markup=markup)
        except:
            bot.send_message(call.message.chat.id, text[:4000], parse_mode='Markdown', reply_markup=markup)
        return

    # ---- ADMINS ----
    if data == "admin_admins":
        bot.answer_callback_query(call.id)
        admins = get_all_admins()
        text = "👑 *Admins*\n\n"
        for a in admins:
            tag = " (super)" if a == SUPER_ADMIN else ""
            text += f"• `{a}`{tag}\n"
        markup = types.InlineKeyboardMarkup(row_width=1)
        markup.add(types.InlineKeyboardButton("➕ Add Admin", callback_data="admin_add_admin"))
        if user_id == SUPER_ADMIN:
            markup.add(types.InlineKeyboardButton("➖ Remove Admin", callback_data="admin_rem_admin"))
        markup.add(types.InlineKeyboardButton("🔙 Back", callback_data="admin_back"))
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id,
                              parse_mode='Markdown', reply_markup=markup)
        return

    if data == "admin_add_admin":
        if not is_admin(user_id):
            bot.answer_callback_query(call.id, "⛔")
            return
        bot.answer_callback_query(call.id)
        msg = bot.send_message(call.message.chat.id,
                               "👑 Send the Telegram *user ID* to add as admin:",
                               parse_mode='Markdown',
                               reply_markup=types.ForceReply(selective=True))
        bot.register_next_step_handler(msg, admin_save_admin)
        return

    if data == "admin_rem_admin":
        if user_id != SUPER_ADMIN:
            bot.answer_callback_query(call.id, "⛔ Super admin only")
            return
        bot.answer_callback_query(call.id)
        rows = db.query("SELECT user_id FROM admins")
        if not rows:
            bot.send_message(call.message.chat.id, "No additional admins.")
            return
        markup = types.InlineKeyboardMarkup(row_width=1)
        for r in rows:
            markup.add(types.InlineKeyboardButton(f"❌ {r[0]}", callback_data=f"adminrem_{r[0]}"))
        markup.add(types.InlineKeyboardButton("🔙 Back", callback_data="admin_back"))
        bot.edit_message_text("➖ Pick admin to remove:",
                              call.message.chat.id, call.message.message_id,
                              parse_mode='Markdown', reply_markup=markup)
        return

    if data.startswith("adminrem_"):
        rid = int(data.split("_")[1])
        if user_id != SUPER_ADMIN:
            bot.answer_callback_query(call.id, "⛔")
            return
        db.execute("DELETE FROM admins WHERE user_id=?", [rid])
        bot.answer_callback_query(call.id, f"✅ Removed {rid}")
        handle_admin(call)
        return

    # ---- STATS ----
    if data == "admin_stats":
        bot.answer_callback_query(call.id)
        total_users = db.query_one("SELECT COUNT(*) FROM users")
        total_services = db.query_one("SELECT COUNT(*) FROM services")
        total_countries = db.query_one("SELECT COUNT(*) FROM countries")
        total_numbers = db.query_one("SELECT COUNT(*) FROM numbers")
        available = db.query_one("SELECT COUNT(*) FROM numbers WHERE assigned_to=0")
        assigned = db.query_one("SELECT COUNT(*) FROM numbers WHERE assigned_to!=0")
        text = f"""
📊 *Bot Stats*

👥 Users: `{total_users[0] if total_users else 0}`
⚙️ Services: `{total_services[0] if total_services else 0}`
🌍 Countries: `{total_countries[0] if total_countries else 0}`
📞 Total Numbers: `{total_numbers[0] if total_numbers else 0}`
📦 Available: `{available[0] if available else 0}`
🔒 Assigned: `{assigned[0] if assigned else 0}`
"""
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 Back", callback_data="admin_back"))
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id,
                              parse_mode='Markdown', reply_markup=markup)
        return


# ==================== ADMIN SAVE HANDLERS ====================
def admin_save_service(message):
    if not is_admin(message.from_user.id):
        return
    name = message.text.strip()
    if not name or len(name) > 40:
        bot.send_message(message.chat.id, "❌ Invalid name.")
        return
    existing = db.query_one("SELECT id FROM services WHERE name=?", [name])
    if existing:
        bot.send_message(message.chat.id, f"❌ Service `{name}` already exists.")
        return
    db.execute("INSERT INTO services (name, created_at) VALUES (?, ?)",
               [name, datetime.now().isoformat()])
    bot.send_message(message.chat.id, f"✅ Service `{name}` added!")


def admin_save_country(message, service_id):
    if not is_admin(message.from_user.id):
        return
    text = message.text.strip()
    if '|' not in text:
        bot.send_message(message.chat.id, "❌ Format: `CountryName | Code`")
        return
    parts = [p.strip() for p in text.split('|', 1)]
    name, code = parts[0], parts[1]
    if not name or not code:
        bot.send_message(message.chat.id, "❌ Both fields required.")
        return
    db.execute("INSERT INTO countries (service_id, name, code, created_at) VALUES (?, ?, ?, ?)",
               [service_id, name, code, datetime.now().isoformat()])
    bot.send_message(message.chat.id, f"✅ Country *{name}* ({code}) added!")


def admin_save_numbers(message, country_id):
    if not is_admin(message.from_user.id):
        return
    raw = message.text.strip()
    # Split by newline, comma, or space
    parts = re.split(r'[\n,\s]+', raw)
    numbers = []
    for p in parts:
        clean = re.sub(r'[^\d]', '', p)
        if clean and len(clean) >= 6:
            numbers.append(clean)
    if not numbers:
        bot.send_message(message.chat.id, "❌ No valid numbers found.")
        return
    now = datetime.now().isoformat()
    added = 0
    for num in numbers:
        existing = db.query_one("SELECT id FROM numbers WHERE phone=? AND country_id=?", [num, country_id])
        if existing:
            continue
        db.execute("INSERT INTO numbers (country_id, phone, assigned_to, created_at) VALUES (?, ?, 0, ?)",
                   [country_id, num, now])
        added += 1
    bot.send_message(message.chat.id, f"✅ Added `{added}` new numbers (skipped {len(numbers) - added} duplicates).")


def admin_save_otp_link(message):
    if not is_admin(message.from_user.id):
        return
    link = message.text.strip()
    if not link.startswith(('https://t.me/', 'http://t.me/')):
        bot.send_message(message.chat.id, "❌ Must start with `https://t.me/`")
        return
    set_setting('otp_link', link)
    bot.send_message(message.chat.id, f"✅ OTP link updated to:\n`{link}`")


def admin_save_npu(message):
    if not is_admin(message.from_user.id):
        return
    try:
        n = int(message.text.strip())
        if 1 <= n <= 10:
            set_setting('numbers_per_user', str(n))
            bot.send_message(message.chat.id, f"✅ Default numbers per user set to `{n}` (for new users).")
        else:
            bot.send_message(message.chat.id, "❌ Must be 1-10.")
    except:
        bot.send_message(message.chat.id, "❌ Send a number.")


def admin_send_broadcast(message):
    if not is_admin(message.from_user.id):
        return
    text = message.text.strip()
    users = db.query("SELECT user_id FROM users")
    sent = 0
    failed = 0
    for u in users:
        try:
            bot.send_message(u[0], f"📢 *Announcement*\n\n{text}", parse_mode='Markdown')
            sent += 1
            time.sleep(0.05)
        except:
            failed += 1
    bot.send_message(message.chat.id, f"✅ Broadcast sent to `{sent}` users. Failed: `{failed}`")


def admin_save_admin(message):
    if not is_admin(message.from_user.id):
        return
    try:
        uid = int(message.text.strip())
        db.execute("INSERT OR IGNORE INTO admins (user_id, added_by, added_at) VALUES (?, ?, ?)",
                   [uid, message.from_user.id, datetime.now().isoformat()])
        bot.send_message(message.chat.id, f"✅ `{uid}` added as admin.")
    except:
        bot.send_message(message.chat.id, "❌ Invalid ID.")


# ==================== OTP MONITOR + ROUTING ====================
def find_user_for_number(phone_clean):
    """Find user assigned to this number"""
    row = db.query_one("SELECT assigned_to FROM numbers WHERE phone=?", [phone_clean])
    if row and row[0] and row[0] != 0:
        return row[0]
    return None


def format_number_for_user(phone, user_id):
    user = get_user(user_id)
    row = db.query_one("SELECT c.code FROM numbers n JOIN countries c ON n.country_id=c.id WHERE n.phone=?", [phone])
    code = row[0] if row else ''
    if user and user['country_code_on'] and code:
        return f"{code}{phone}"
    return phone


def send_otp_to_users(sms):
    """Send OTP to assigned user + group"""
    phone = sms['number']
    otp = sms['otp']
    cli = sms.get('cli', 'Unknown')
    text_body = sms['sms']

    user_id = find_user_for_number(phone)
    display_num = format_number_for_user(phone, user_id) if user_id else phone

    user_msg = f"""
🔐 *NEW OTP RECEIVED!*

📱 *Number:* `{display_num}`
📨 *From:* {cli}
🔑 *OTP:* `{otp}`

💬 *Message:*
{text_body[:300]}

🕐 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
"""

    # To assigned user
    if user_id:
        try:
            bot.send_message(user_id, user_msg, parse_mode='Markdown')
            print(f"✅ OTP sent to user {user_id}: {otp}")
        except Exception as e:
            print(f"❌ Failed to send to {user_id}: {e}")

    # To group
    group_msg = f"""
🔐 *OTP — {display_num}*

{user_msg}
👤 *Assigned to:* `{user_id if user_id else 'Unassigned'}`
"""
    try:
        bot.send_message(CHAT_ID, group_msg, parse_mode='Markdown')
    except Exception as e:
        print(f"❌ Group send failed: {e}")


def monitor():
    print("🔄 Monitor started")
    while dashboard.running:
        try:
            if not dashboard.is_logged_in:
                dashboard.login()
                time.sleep(15)
                continue
            messages = dashboard.fetch_sms()
            for msg in messages:
                if msg['hash'] not in dashboard.processed_sms:
                    dashboard.processed_sms.add(msg['hash'])
                    dashboard.sms_history.append({
                        'otp': msg['otp'],
                        'number': msg['number'],
                        'time': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                    })
                    send_otp_to_users(msg)
        except Exception as e:
            print(f'❌ Monitor error: {e}')
        time.sleep(10)


# ==================== STARTUP ====================
if __name__ == '__main__':
    print('=' * 50)
    print('🤖 NBHC OTP Bot v2.0')
    print('=' * 50)

    db.init_tables()

    print('\n🔐 Initial login...')
    dashboard.login()

    threading.Thread(target=monitor, daemon=True).start()
    print('✅ Monitor started')
    print('📱 Telegram bot ready')

    while True:
        try:
            bot.polling(none_stop=True, timeout=60)
        except Exception as e:
            print(f'⚠️ Polling error: {e}')
            time.sleep(5)
