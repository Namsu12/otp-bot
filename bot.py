#!/usr/bin/env python3
import os
import re
import time
import json
import hashlib
import threading
import traceback
from datetime import datetime

import requests
import telebot
from telebot import types
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

import libsql_client

# ==================== CONFIG ====================
BOT_TOKEN = os.getenv('BOT_TOKEN', '')
CHAT_ID = os.getenv('CHAT_ID', '')
SUPER_ADMIN = int(os.getenv('SUPER_ADMIN', '8993161626'))

TURSO_URL = os.getenv('TURSO_URL', '')
TURSO_TOKEN = os.getenv('TURSO_TOKEN', '')

if not BOT_TOKEN or not CHAT_ID:
    print("ERROR: BOT_TOKEN and CHAT_ID must be set!")
    exit(1)
if not TURSO_URL or not TURSO_TOKEN:
    print("ERROR: TURSO_URL and TURSO_TOKEN must be set!")
    exit(1)

bot = telebot.TeleBot(BOT_TOKEN)


# ==================== TURSO DATABASE ====================
class DB:



    
    def __init__(self):
        self.url = TURSO_URL
        self.auth = TURSO_TOKEN
        self.lock = threading.Lock()
        print(f"Turso: {self.url}")

    def execute(self, sql, params=None):
        with self.lock:
            try:
                import asyncio
                import libsql_client

                http_url = self.url.replace('wss://', 'https://').replace('libsql://', 'https://')

                async def run():
                    client = libsql_client.create_client(url=http_url, auth_token=self.auth)
                    try:
                        if params:
                            result = await client.execute(sql, params)
                        else:
                            result = await client.execute(sql)
                    finally:
                        try:
                            await client.close()
                        except:
                            pass
                    if hasattr(result, 'rows'):
                        return [list(r) for r in result.rows]
                    return []
                return asyncio.run(run())
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
            "CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT, first_seen TEXT, otp_count INTEGER DEFAULT 0, verified INTEGER DEFAULT 0)",
            "CREATE TABLE IF NOT EXISTS admins (user_id INTEGER PRIMARY KEY, added_by INTEGER, added_at TEXT)",
            "CREATE TABLE IF NOT EXISTS panels (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, base_url TEXT, username TEXT, password TEXT, active INTEGER DEFAULT 1, last_check TEXT, created_at TEXT)",
            "CREATE TABLE IF NOT EXISTS services (id INTEGER PRIMARY KEY AUTOINCREMENT, panel_id INTEGER, name TEXT, created_at TEXT)",
            "CREATE TABLE IF NOT EXISTS countries (id INTEGER PRIMARY KEY AUTOINCREMENT, service_id INTEGER, name TEXT, code TEXT, numbers_per_user INTEGER DEFAULT 3, created_at TEXT)",
            "CREATE TABLE IF NOT EXISTS numbers (id INTEGER PRIMARY KEY AUTOINCREMENT, country_id INTEGER, phone TEXT, assigned_to INTEGER DEFAULT 0, assigned_at TEXT, created_at TEXT)",
            "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)",
            "CREATE TABLE IF NOT EXISTS force_join (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id TEXT, chat_title TEXT, invite_link TEXT, added_at TEXT)",
            "CREATE TABLE IF NOT EXISTS withdrawals (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, amount REAL, method TEXT, details TEXT, status TEXT DEFAULT 'pending', requested_at TEXT, processed_at TEXT)",
            "CREATE TABLE IF NOT EXISTS logs (id INTEGER PRIMARY KEY AUTOINCREMENT, event TEXT, details TEXT, timestamp TEXT)",
        ]
        for sql in tables:
            self.execute(sql)

        defaults = {
            'otp_link': 'https://t.me/alohaotp',
            'support_contact': '@your_username',
            'numbers_per_user': '3',
            'country_code_default': '1',
            'force_join_enabled': '0',
            'bot_name': 'NBHC OTP Bot',
        }
        for k, v in defaults.items():
            self.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", [k, v])

        check = self.query("SELECT value FROM settings WHERE key=?", ['otp_link'])
        print(f"DB write test: {check}")
        print("Database initialized")


db = DB()

# ==================== HELPERS ====================
def get_setting(key, default=''):
    row = db.query_one("SELECT value FROM settings WHERE key=?", [key])
    return row[0] if row else default


def set_setting(key, value):
    db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", [key, str(value)])


def is_admin(user_id):
    if user_id == SUPER_ADMIN:
        return True
    row = db.query_one("SELECT user_id FROM admins WHERE user_id=?", [user_id])
    return row is not None


def get_all_admins():
    ids = [SUPER_ADMIN]
    for row in db.query("SELECT user_id FROM admins"):
        ids.append(int(row[0]))
    return ids


def upsert_user(user_id, username, first_name):
    try:
        db.execute(
            "INSERT OR IGNORE INTO users (user_id, username, first_name, first_seen, otp_count, verified) VALUES (?, ?, ?, ?, 0, 0)",
            [user_id, username or '', first_name or '', datetime.now().isoformat()]
        )
    except Exception as e:
        print(f"upsert_user error: {e}")


def get_user(user_id):
    row = db.query_one("SELECT user_id, username, first_name, first_seen, otp_count, verified FROM users WHERE user_id=?", [user_id])
    if not row:
        return None
    return {
        'user_id': int(row[0]), 'username': row[1], 'first_name': row[2],
        'first_seen': row[3], 'otp_count': int(row[4]) if row[4] else 0,
        'verified': int(row[5]) if row[5] else 0
    }


def log_event(event, details=''):
    try:
        db.execute(
            "INSERT INTO logs (event, details, timestamp) VALUES (?, ?, ?)",
            [event, details[:500], datetime.now().isoformat()]
        )
    except:
        pass


def get_active_panels():
    return db.query("SELECT id, name, base_url, username, password FROM panels WHERE active=1")
# ==================== DASHBOARD SCRAPER ====================
class Dashboard:
    def __init__(self, panel_id, name, base_url, username, password):
        self.panel_id = panel_id
        self.name = name
        self.base_url = base_url.rstrip('/')
        self.username = username
        self.password = password
        self.login_url = f'{self.base_url}/login'
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
        self.last_check = None

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
            page = self.session.get(self.login_url, timeout=15)
            soup = BeautifulSoup(page.text, 'html.parser')
            form = soup.find('form')
            if not form:
                return False

            action = form.get('action', '')
            if action.startswith('http'):
                submit_url = action
            elif action:
                submit_url = f'{self.base_url}/{action.lstrip("/")}'
            else:
                submit_url = self.login_url

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
                    data[name] = self.username
                elif 'pass' in name.lower():
                    data[name] = self.password
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

            self.session.headers.update({'Referer': self.login_url, 'Origin': self.base_url})
            resp = self.session.post(submit_url, data=data, timeout=15, allow_redirects=True)

            self.is_logged_in = 'login' not in resp.url.lower()
            return self.is_logged_in

        except Exception as e:
            print(f'[{self.name}] Login error: {e}')
            return False

    def fetch_sms(self):
        if not self.is_logged_in:
            if not self.login():
                return []
        try:
            time.sleep(2)
            resp = self.session.get(f'{self.base_url}/agent/SMSCDRReports', timeout=20)
            if resp.status_code == 200:
                self.last_check = datetime.now()
                return self.extract_sms(resp.text)
            elif resp.status_code in [401, 403]:
                self.is_logged_in = False
        except Exception as e:
            print(f'[{self.name}] Fetch error: {e}')
        return []

    def extract_sms(self, html):
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
            if len(cell_texts) < 6:
                continue

            date_val = cell_texts[0] if len(cell_texts) > 0 else ''
            number = cell_texts[2] if len(cell_texts) > 2 else ''
            cli = cell_texts[3] if len(cell_texts) > 3 else ''
            sms_text = cell_texts[5] if len(cell_texts) > 5 else ''

            if not sms_text or len(sms_text) < 5:
                continue
            if 'total sms' in sms_text.lower():
                continue

            otp = self.find_otp(sms_text)
            if not otp:
                digits = re.findall(r'\b\d{4,8}\b', sms_text)
                if digits:
                    otp = digits[0]

            if not otp:
                continue

            clean_number = re.sub(r'[^\d]', '', number)
            if not clean_number:
                continue

            row_hash = hashlib.md5(f"{self.panel_id}|{date_val}|{number}|{sms_text}".encode()).hexdigest()
            if row_hash in self.processed_sms:
                continue

            results.append({
                'panel_id': self.panel_id,
                'panel_name': self.name,
                'date': date_val,
                'number': clean_number,
                'raw_number': number,
                'cli': cli,
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
        try:
            resp = self.session.get(f'{self.base_url}/agent/Statements?ecuid=Qg==', timeout=20)
            if resp.status_code != 200:
                return None
            soup = BeautifulSoup(resp.text, 'html.parser')
            text = soup.get_text()
            patterns = [
                r'(?:balance|available|credit)[:\s]*[\$\€\£]?\s*([\d,]+\.?\d*)',
                r'[\$\€\£]\s*([\d,]+\.?\d*)',
            ]
            for p in patterns:
                m = re.search(p, text, re.IGNORECASE)
                if m:
                    return m.group(0)
        except:
            pass
        return None


# ==================== PANEL MANAGER ====================
active_dashboards = {}
dashboards_lock = threading.Lock()


def load_all_dashboards():
    with dashboards_lock:
        panels = get_active_panels()
        new_map = {}
        for p in panels:
            panel_id, name, base_url, username, password = p
            pid = int(panel_id)
            if pid in active_dashboards:
                new_map[pid] = active_dashboards[pid]
            else:
                new_map[pid] = Dashboard(pid, name, base_url, username, password)
                print(f"Loaded panel: {name}")
        active_dashboards.clear()
        active_dashboards.update(new_map)


def get_all_panels():
    load_all_dashboards()
    return list(active_dashboards.values())


# ==================== OTP ROUTING ====================
def find_user_for_number(phone_clean):
    row = db.query_one("SELECT assigned_to FROM numbers WHERE phone=?", [phone_clean])
    if row and row[0] and int(row[0]) != 0:
        return int(row[0])
    return None


def format_number_for_user(phone, user_id):
    row = db.query_one(
        "SELECT c.code FROM numbers n JOIN countries c ON n.country_id=c.id WHERE n.phone=?",
        [phone]
    )
    code = row[0] if row else ''
    if code:
        return f"{code}{phone}"
    return phone


def send_otp_to_users(sms):
    phone = sms['number']
    otp = sms['otp']
    cli = sms.get('cli', 'Unknown')
    text_body = sms['sms']
    panel_name = sms.get('panel_name', 'Panel')

    user_id = find_user_for_number(phone)
    display_num = format_number_for_user(phone, user_id) if user_id else phone

    user_msg = f"🔐 *NEW OTP RECEIVED!*\n\n📱 *Number:* `{display_num}`\n📨 *From:* {cli}\n🔑 *OTP:* `{otp}`\n\n💬 *Message:*\n{text_body[:300]}\n\n🕐 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n🎛️ _Panel: {panel_name}_"

    if user_id:
        try:
            bot.send_message(user_id, user_msg, parse_mode='Markdown')
            db.execute("UPDATE users SET otp_count = otp_count + 1 WHERE user_id=?", [user_id])
            print(f"OTP to user {user_id}: {otp}")
        except Exception as e:
            print(f"Failed to send to {user_id}: {e}")

    group_msg = f"🔐 *OTP - {display_num}*\n\n{user_msg}\n\n👤 *Assigned to:* `{user_id if user_id else 'Unassigned'}`"
    try:
        bot.send_message(CHAT_ID, group_msg, parse_mode='Markdown')
    except Exception as e:
        print(f"Group send failed: {e}")

    log_event('OTP', f"{otp} for {display_num} -> user {user_id}")


def monitor_panel(dashboard):
    print(f"Monitor started for [{dashboard.name}]")
    while True:
        try:
            if not dashboard.is_logged_in:
                dashboard.login()
                time.sleep(15)
                continue

            messages = dashboard.fetch_sms()
            for msg in messages:
                if msg['hash'] not in dashboard.processed_sms:
                    dashboard.processed_sms.add(msg['hash'])
                    send_otp_to_users(msg)
        except Exception as e:
            print(f'[{dashboard.name}] Monitor error: {e}')
        time.sleep(10)


def start_all_monitors():
    panels = get_all_panels()
    if not panels:
        print("No active panels to monitor")
        return
    for d in panels:
        threading.Thread(target=monitor_panel, args=(d,), daemon=True).start()
    print(f"Started {len(panels)} monitor thread(s)")
# ==================== FORCE JOIN ====================
def get_force_join_channels():
    return db.query("SELECT id, chat_id, chat_title, invite_link FROM force_join ORDER BY id")


def check_user_joined(user_id):
    if get_setting('force_join_enabled', '0') != '1':
        return True
    channels = get_force_join_channels()
    if not channels:
        return True
    for ch in channels:
        chat_id = ch[1]
        try:
            member = bot.get_chat_member(chat_id, user_id)
            if member.status in ['left', 'kicked']:
                return False
        except Exception as e:
            print(f"Force-join check error: {e}")
            continue
    return True


def force_join_keyboard():
    channels = get_force_join_channels()
    markup = types.InlineKeyboardMarkup(row_width=1)
    for ch in channels:
        title = ch[2] or 'Channel'
        link = ch[3]
        if link:
            markup.add(types.InlineKeyboardButton(f"📢 {title}", url=link, style='primary'))
    markup.add(types.InlineKeyboardButton("✅ I Have Joined - Verify", callback_data="verify_join", style='success'))
    return markup


def send_force_join_message(chat_id, first_name=''):
    channels = get_force_join_channels()
    if not channels:
        return False
    text = f"🔒 *Access Restricted*\n\nHello {first_name}! You must join these first:\n\n"
    for i, ch in enumerate(channels, 1):
        text += f"{i}. *{ch[2] or 'Channel'}*\n"
    text += "\nOnce joined, tap the button below 👇"
    try:
        bot.send_message(chat_id, text, parse_mode='Markdown', reply_markup=force_join_keyboard())
        return True
    except:
        return False


# ==================== KEYBOARDS ====================
def reply_keyboard(user_id):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.add(
        types.KeyboardButton("📱 Get Number"),
        types.KeyboardButton("💸 Withdrawal")
    )
    markup.add(
        types.KeyboardButton("💰 Balance"),
        types.KeyboardButton("💬 Support")
    )
    if is_admin(user_id):
        markup.add(types.KeyboardButton("🛠️ Admin Panel"))
    return markup


def back_to_main_btn():
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🔙 Main Menu", callback_data="back_main", style='danger'))
    return markup


def services_menu():
    services = db.query("SELECT id, name FROM services ORDER BY name")
    if not services:
        return None, "❌ *No services available yet.*"
    markup = types.InlineKeyboardMarkup(row_width=2)
    buttons = [types.InlineKeyboardButton(f"⚙️ {s[1]}", callback_data=f"svc_{s[0]}", style='primary') for s in services]
    markup.add(*buttons)
    markup.add(types.InlineKeyboardButton("🔙 Main Menu", callback_data="back_main", style='danger'))
    return markup, "⚙️ *Select a Service:*"


def countries_menu(service_id):
    service = db.query_one("SELECT name FROM services WHERE id=?", [service_id])
    if not service:
        return None, "❌ Service not found."
    countries = db.query("SELECT id, name, code FROM countries WHERE service_id=? ORDER BY name", [service_id])
    if not countries:
        return None, f"❌ *No countries for {service[0]} yet.*"
    markup = types.InlineKeyboardMarkup(row_width=2)
    buttons = [types.InlineKeyboardButton(f"🌍 {c[1]} ({c[2]})", callback_data=f"ctry_{c[0]}", style='success') for c in countries]
    markup.add(*buttons)
    markup.add(types.InlineKeyboardButton("🔙 Services", callback_data="get_number", style='primary'))
    return markup, f"🌍 *{service[0]}* - Select a Country:"


def numbers_screen(user_id, service_id, country_id):
    service = db.query_one("SELECT name FROM services WHERE id=?", [service_id])
    country = db.query_one("SELECT name, code, numbers_per_user FROM countries WHERE id=?", [country_id])
    user = get_user(user_id)
    if not service or not country:
        return None, "❌ Error loading."

    n_per_user = int(country[2]) if country[2] else 3
    code = country[1]
    code_on = user['verified'] if user else 1

    assigned = db.query(
        "SELECT id, phone FROM numbers WHERE assigned_to=? AND country_id=? ORDER BY id",
        [user_id, country_id]
    )

    if len(assigned) < n_per_user:
        needed = n_per_user - len(assigned)
        available = db.query(
            "SELECT id, phone FROM numbers WHERE country_id=? AND assigned_to=0 LIMIT ?",
            [country_id, needed]
        )
        now = datetime.now().isoformat()
        for num in available:
            db.execute("UPDATE numbers SET assigned_to=?, assigned_at=? WHERE id=?",
                       [user_id, now, num[0]])
        assigned = db.query(
            "SELECT id, phone FROM numbers WHERE assigned_to=? AND country_id=? ORDER BY id",
            [user_id, country_id]
        )

    stock = db.query_one("SELECT COUNT(*) FROM numbers WHERE country_id=? AND assigned_to=0", [country_id])
    stock_count = int(stock[0]) if stock else 0

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

    title = f"🌍 *{country[0]} ({service[0]}) - {len(assigned)} Numbers Assigned:*"
    text = f"{title}\n\n*Country:* {country[0]} - {code}\n\n{assigned_text}\n\n📦 *Stock Left:* {stock_count}\n⏳ _Waiting for OTP..._"

    markup = types.InlineKeyboardMarkup(row_width=1)

    for n in assigned:
        raw = n[1]
        if code_on:
            num_display = f"{code}{raw}" if not raw.startswith(code) else raw
        else:
            num_display = raw
        markup.add(types.InlineKeyboardButton(
            f"📋 {num_display}",
            callback_data=f"copy_{num_display}",
            style='success'
        ))

    markup.add(types.InlineKeyboardButton("🔄 Change Numbers", callback_data=f"chgnum_{service_id}_{country_id}", style='primary'))
    markup.row(
        types.InlineKeyboardButton("🌍 Change Country", callback_data=f"svc_{service_id}", style='success'),
        types.InlineKeyboardButton("⚙️ Change Service", callback_data="get_number", style='primary')
    )

    toggle_label = "🟢 Country Code: ON" if code_on else "🔴 Country Code: OFF"
    toggle_style = 'success' if code_on else 'danger'
    markup.add(types.InlineKeyboardButton(toggle_label, callback_data=f"togglecc_{service_id}_{country_id}", style=toggle_style))

    otp_link = get_setting('otp_link', 'https://t.me/alohaotp')
    markup.add(types.InlineKeyboardButton("📬 View OTP", url=otp_link, style='primary'))

    markup.add(types.InlineKeyboardButton("🔙 Main Menu", callback_data="back_main", style='danger'))

    return markup, text


# ==================== /start ====================
@bot.message_handler(commands=['start'])
def cmd_start(message):
    user_id = message.from_user.id
    first_name = message.from_user.first_name or 'Friend'
    upsert_user(user_id, message.from_user.username, first_name)

    if not check_user_joined(user_id):
        send_force_join_message(message.chat.id, first_name)
        return

    text = f"👋 *Welcome to {get_setting('bot_name', 'NBHC OTP Bot')}*\n\nGet virtual numbers, receive OTPs, and manage your account - all from here.\n\n*Hello {first_name}!*\n\nUse the buttons below 👇"
    bot.send_message(message.chat.id, text, parse_mode='Markdown', reply_markup=reply_keyboard(user_id))


# ==================== REPLY KEYBOARD HANDLERS ====================
@bot.message_handler(func=lambda m: m.text == "📱 Get Number")
def kb_get_number(message):
    user_id = message.from_user.id
    upsert_user(user_id, message.from_user.username, message.from_user.first_name)
    if not check_user_joined(user_id):
        send_force_join_message(message.chat.id, message.from_user.first_name or '')
        return
    markup, text = services_menu()
    if not markup:
        bot.send_message(message.chat.id, text, parse_mode='Markdown', reply_markup=back_to_main_btn())
    else:
        bot.send_message(message.chat.id, text, parse_mode='Markdown', reply_markup=markup)


@bot.message_handler(func=lambda m: m.text == "💸 Withdrawal")
def kb_withdraw(message):
    user_id = message.from_user.id
    if not check_user_joined(user_id):
        send_force_join_message(message.chat.id, message.from_user.first_name or '')
        return
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🔙 Main Menu", callback_data="back_main", style='danger'))
    bot.send_message(message.chat.id, "💸 *Withdrawal*\n\n_Coming soon - admin will process soon._",
                     parse_mode='Markdown', reply_markup=markup)


@bot.message_handler(func=lambda m: m.text == "💰 Balance")
def kb_balance(message):
    user_id = message.from_user.id
    if not check_user_joined(user_id):
        send_force_join_message(message.chat.id, message.from_user.first_name or '')
        return
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🔙 Main Menu", callback_data="back_main", style='danger'))
    bot.send_message(message.chat.id, "💰 *Your Balance*\n\n💰 `$0.00`\n\n_Earnings will appear here._",
                     parse_mode='Markdown', reply_markup=markup)


@bot.message_handler(func=lambda m: m.text == "💬 Support")
def kb_support(message):
    user_id = message.from_user.id
    if not check_user_joined(user_id):
        send_force_join_message(message.chat.id, message.from_user.first_name or '')
        return
    contact = get_setting('support_contact', '@your_username')
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🔙 Main Menu", callback_data="back_main", style='danger'))
    bot.send_message(message.chat.id, f"💬 *Support*\n\nContact: {contact}",
                     parse_mode='Markdown', reply_markup=markup)


@bot.message_handler(func=lambda m: m.text == "🛠️ Admin Panel")
def kb_admin(message):
    user_id = message.from_user.id
    if not is_admin(user_id):
        bot.send_message(message.chat.id, "⛔ Admins only.")
        return
    send_admin_panel(message.chat.id)


# ==================== ADMIN PANEL VIEW ====================
def safe_answer(call, text='', show_alert=False):
    try:
        bot.answer_callback_query(call.id, text=text, show_alert=show_alert)
    except:
        pass


def send_admin_panel(chat_id):
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("🎛️ Panels", callback_data="adm_panels", style='primary'),
        types.InlineKeyboardButton("➕ Upload Numbers", callback_data="adm_upload", style='success')
    )
    markup.add(
        types.InlineKeyboardButton("🗑️ Delete Numbers", callback_data="adm_delete", style='danger'),
        types.InlineKeyboardButton("📊 Stock Overview", callback_data="adm_stock", style='primary')
    )
    markup.add(
        types.InlineKeyboardButton("📢 OTP Groups", callback_data="adm_otp_groups", style='primary'),
        types.InlineKeyboardButton("👥 Users", callback_data="adm_users", style='success')
    )
    markup.add(
        types.InlineKeyboardButton("📊 Stats", callback_data="adm_stats", style='success'),
        types.InlineKeyboardButton("📢 Broadcast", callback_data="adm_broadcast", style='success')
    )
    markup.add(
        types.InlineKeyboardButton("📣 Announcement", callback_data="adm_announce", style='success'),
        types.InlineKeyboardButton("💰 Balance", callback_data="adm_balance", style='primary')
    )
    markup.add(
        types.InlineKeyboardButton("💳 Withdrawals", callback_data="adm_withdrawals", style='primary'),
        types.InlineKeyboardButton("💬 Support", callback_data="adm_support", style='primary')
    )
    markup.add(
        types.InlineKeyboardButton("👑 Admins", callback_data="adm_admins", style='success'),
        types.InlineKeyboardButton("⚙️ Settings", callback_data="adm_settings", style='primary')
    )
    markup.add(
        types.InlineKeyboardButton("📄 Logs", callback_data="adm_logs", style='primary'),
        types.InlineKeyboardButton("💾 Backup", callback_data="adm_backup", style='primary')
    )
    markup.add(types.InlineKeyboardButton("📥 Restore", callback_data="adm_restore", style='primary'))
    markup.add(types.InlineKeyboardButton("🔙 Back", callback_data="adm_back", style='danger'))
    bot.send_message(chat_id, "🛠️ *Admin Panel*\n\nPick an option:",
                     parse_mode='Markdown', reply_markup=markup)
# ==================== CALLBACK HANDLER ====================
@bot.callback_query_handler(func=lambda call: True)
def handle_callback(call):
    user_id = call.from_user.id
    data = call.data
    upsert_user(user_id, call.from_user.username, call.from_user.first_name)

    try:
        if data == "verify_join":
            safe_answer(call)
            if check_user_joined(user_id):
                try:
                    bot.delete_message(call.message.chat.id, call.message.message_id)
                except:
                    pass
                bot.send_message(call.message.chat.id, "✅ *Verified!*", parse_mode='Markdown')
                cmd_start(call.message)
            else:
                safe_answer(call, "❌ Join all channels first!", True)
            return

        if data == "back_main":
            safe_answer(call)
            try:
                bot.delete_message(call.message.chat.id, call.message.message_id)
            except:
                pass
            cmd_start(call.message)
            return

        if data == "get_number":
            safe_answer(call)
            markup, text = services_menu()
            try:
                if not markup:
                    bot.edit_message_text(text, call.message.chat.id, call.message.message_id,
                                          parse_mode='Markdown', reply_markup=back_to_main_btn())
                else:
                    bot.edit_message_text(text, call.message.chat.id, call.message.message_id,
                                          parse_mode='Markdown', reply_markup=markup)
            except:
                pass
            return

        if data.startswith("svc_"):
            service_id = int(data.split("_")[1])
            safe_answer(call)
            markup, text = countries_menu(service_id)
            try:
                if not markup:
                    bot.edit_message_text(text, call.message.chat.id, call.message.message_id,
                                          parse_mode='Markdown', reply_markup=back_to_main_btn())
                else:
                    bot.edit_message_text(text, call.message.chat.id, call.message.message_id,
                                          parse_mode='Markdown', reply_markup=markup)
            except:
                pass
            return

        if data.startswith("ctry_"):
            country_id = int(data.split("_")[1])
            country = db.query_one("SELECT service_id FROM countries WHERE id=?", [country_id])
            if not country:
                safe_answer(call, "❌ Country not found")
                return
            service_id = int(country[0])
            safe_answer(call, "Loading...")
            markup, text = numbers_screen(user_id, service_id, country_id)
            try:
                if markup:
                    bot.edit_message_text(text, call.message.chat.id, call.message.message_id,
                                          parse_mode='Markdown', reply_markup=markup)
            except:
                if markup:
                    bot.send_message(call.message.chat.id, text, parse_mode='Markdown', reply_markup=markup)
            return

        if data.startswith("chgnum_"):
            parts = data.split("_")
            svc_id, ctry_id = int(parts[1]), int(parts[2])
            db.execute("UPDATE numbers SET assigned_to=0, assigned_at=NULL WHERE assigned_to=? AND country_id=?",
                       [user_id, ctry_id])
            safe_answer(call, "Getting new numbers...")
            markup, text = numbers_screen(user_id, svc_id, ctry_id)
            try:
                if markup:
                    bot.edit_message_text(text, call.message.chat.id, call.message.message_id,
                                          parse_mode='Markdown', reply_markup=markup)
            except:
                pass
            return

        if data.startswith("togglecc_"):
            parts = data.split("_")
            svc_id, ctry_id = int(parts[1]), int(parts[2])
            user = get_user(user_id)
            new_val = 0 if user['verified'] else 1
            db.execute("UPDATE users SET verified=? WHERE user_id=?", [new_val, user_id])
            safe_answer(call, "Toggled!")
            markup, text = numbers_screen(user_id, svc_id, ctry_id)
            try:
                if markup:
                    bot.edit_message_text(text, call.message.chat.id, call.message.message_id,
                                          parse_mode='Markdown', reply_markup=markup)
            except:
                pass
            return


if data.startswith("copy_"):
        num = data.replace("copy_", "", 1)
    safe_answer(call, "📋 Tap to copy!")
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except:
        pass
    bot.send_message(call.message.chat.id, f"`{num}`", parse_mode='Markdown')
    return





        if data == "admin_panel":
            if not is_admin(user_id):
                safe_answer(call, "⛔ Admins only")
                return
            safe_answer(call)
            send_admin_panel(call.message.chat.id)
            return

        if data.startswith("adm_"):
            handle_admin_callback(call)
            return

    except Exception as e:
        print(f"Callback error: {e}")
        traceback.print_exc()
        safe_answer(call, "⚠️ Error")


# ==================== ADMIN CALLBACKS ====================
def handle_admin_callback(call):
    user_id = call.from_user.id
    if not is_admin(user_id):
        safe_answer(call, "⛔ Admins only")
        return

    data = call.data
    chat_id = call.message.chat.id
    msg_id = call.message.message_id

    if data == "adm_back":
        safe_answer(call)
        try:
            bot.delete_message(chat_id, msg_id)
        except:
            pass
        send_admin_panel(chat_id)
        return

    # ---- PANELS ----
    if data == "adm_panels":
        safe_answer(call)
        panels = db.query("SELECT id, name, base_url, active FROM panels ORDER BY id")
        text = "🎛️ *Panels*\n\n"
        if not panels:
            text += "_No panels added yet._"
        else:
            for p in panels:
                status = "🟢" if p[3] and int(p[3]) == 1 else "🔴"
                text += f"{status} *{p[1]}*\n   URL: `{p[2]}`\n\n"
        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.add(
            types.InlineKeyboardButton("➕ Add Panel", callback_data="adm_add_panel", style='success'),
            types.InlineKeyboardButton("✏️ Edit Panel", callback_data="adm_edit_panel", style='primary')
        )
        markup.add(
            types.InlineKeyboardButton("🗑️ Delete Panel", callback_data="adm_del_panel", style='danger'),
            types.InlineKeyboardButton("🔄 Toggle Active", callback_data="adm_toggle_panel", style='primary')
        )
        markup.add(types.InlineKeyboardButton("🔙 Back", callback_data="adm_back", style='danger'))
        try:
            bot.edit_message_text(text[:4000], chat_id, msg_id, parse_mode='Markdown', reply_markup=markup)
        except:
            bot.send_message(chat_id, text[:4000], parse_mode='Markdown', reply_markup=markup)
        return

    if data == "adm_add_panel":
        safe_answer(call)
        msg = bot.send_message(chat_id,
                               "🎛️ *Add Panel*\n\nSend in this format (4 lines):\n\n```\nPanel Name\nhttp://panel-url.com/ints\nusername\npassword\n```",
                               parse_mode='Markdown',
                               reply_markup=types.ForceReply(selective=True))
        bot.register_next_step_handler(msg, admin_save_panel)
        return

    if data == "adm_edit_panel":
        safe_answer(call)
        panels = db.query("SELECT id, name FROM panels ORDER BY id")
        if not panels:
            bot.send_message(chat_id, "❌ No panels to edit.")
            return
        markup = types.InlineKeyboardMarkup(row_width=1)
        for p in panels:
            markup.add(types.InlineKeyboardButton(f"✏️ {p[1]}", callback_data=f"adm_editp_{p[0]}", style='primary'))
        markup.add(types.InlineKeyboardButton("🔙 Back", callback_data="adm_panels", style='danger'))
        try:
            bot.edit_message_text("✏️ Pick a panel to edit:", chat_id, msg_id,
                                  parse_mode='Markdown', reply_markup=markup)
        except:
            pass
        return

    if data.startswith("adm_editp_"):
        pid = int(data.split("_")[2])
        safe_answer(call)
        panel = db.query_one("SELECT name, base_url, username, password FROM panels WHERE id=?", [pid])
        if not panel:
            bot.send_message(chat_id, "❌ Panel not found")
            return
        msg = bot.send_message(chat_id,
                               f"✏️ *Edit Panel #{pid}*\n\nCurrent:\nName: `{panel[0]}`\nURL: `{panel[1]}`\nUser: `{panel[2]}`\n\nSend new values (4 lines)",
                               parse_mode='Markdown',
                               reply_markup=types.ForceReply(selective=True))
        bot.register_next_step_handler(msg, admin_update_panel, pid)
        return

    if data == "adm_del_panel":
        safe_answer(call)
        panels = db.query("SELECT id, name FROM panels ORDER BY id")
        if not panels:
            bot.send_message(chat_id, "❌ No panels.")
            return
        markup = types.InlineKeyboardMarkup(row_width=1)
        for p in panels:
            markup.add(types.InlineKeyboardButton(f"🗑️ {p[1]}", callback_data=f"adm_delp_{p[0]}", style='danger'))
        markup.add(types.InlineKeyboardButton("🔙 Back", callback_data="adm_panels", style='danger'))
        try:
            bot.edit_message_text("🗑️ Pick a panel to DELETE:", chat_id, msg_id,
                                  parse_mode='Markdown', reply_markup=markup)
        except:
            pass
        return

    if data.startswith("adm_delp_"):
        pid = int(data.split("_")[2])
        safe_answer(call, "Deleting...")
        services = db.query("SELECT id FROM services WHERE panel_id=?", [pid])
        for s in services:
            db.execute("DELETE FROM numbers WHERE country_id IN (SELECT id FROM countries WHERE service_id=?)", [s[0]])
            db.execute("DELETE FROM countries WHERE service_id=?", [s[0]])
        db.execute("DELETE FROM services WHERE panel_id=?", [pid])
        db.execute("DELETE FROM panels WHERE id=?", [pid])
        bot.send_message(chat_id, f"✅ Panel #{pid} deleted.")
        log_event('admin_delete_panel', f"panel {pid}")
        send_admin_panel(chat_id)
        return

    if data == "adm_toggle_panel":
        safe_answer(call)
        panels = db.query("SELECT id, name, active FROM panels ORDER BY id")
        if not panels:
            bot.send_message(chat_id, "❌ No panels.")
            return
        markup = types.InlineKeyboardMarkup(row_width=1)
        for p in panels:
            icon = "🟢" if p[2] and int(p[2]) == 1 else "🔴"
            markup.add(types.InlineKeyboardButton(f"{icon} {p[1]}", callback_data=f"adm_togp_{p[0]}", style='primary'))
        markup.add(types.InlineKeyboardButton("🔙 Back", callback_data="adm_panels", style='danger'))
        try:
            bot.edit_message_text("🔄 Toggle panel active:", chat_id, msg_id,
                                  parse_mode='Markdown', reply_markup=markup)
        except:
            pass
        return

    if data.startswith("adm_togp_"):
        pid = int(data.split("_")[2])
        row = db.query_one("SELECT active FROM panels WHERE id=?", [pid])
        new_val = 0 if (row and row[0] and int(row[0]) == 1) else 1
        db.execute("UPDATE panels SET active=? WHERE id=?", [new_val, pid])
        safe_answer(call, "✅ Toggled")
        log_event('admin_toggle_panel', f"panel {pid} active={new_val}")
        send_admin_panel(chat_id)
        return

    # ---- UPLOAD WIZARD ----
    if data == "adm_upload":
        safe_answer(call)
        panels = db.query("SELECT id, name FROM panels WHERE active=1 ORDER BY name")
        if not panels:
            bot.send_message(chat_id, "❌ Add a panel first.")
            return
        markup = types.InlineKeyboardMarkup(row_width=1)
        for p in panels:
            markup.add(types.InlineKeyboardButton(f"🎛️ {p[1]}", callback_data=f"adm_up_panel_{p[0]}", style='success'))
        markup.add(types.InlineKeyboardButton("🔙 Back", callback_data="adm_back", style='danger'))
        try:
            bot.edit_message_text("➕ *Upload Numbers*\n\nStep 1: Pick a panel:",
                                  chat_id, msg_id, parse_mode='Markdown', reply_markup=markup)
        except:
            pass
        return

    if data.startswith("adm_up_panel_"):
        pid = int(data.split("_")[3])
        safe_answer(call)
        msg = bot.send_message(chat_id,
                               "➕ *Step 2/5* - Send the *Service name*:\n\nEx: `WhatsApp`",
                               parse_mode='Markdown',
                               reply_markup=types.ForceReply(selective=True))
        bot.register_next_step_handler(msg, wizard_service, pid)
        return

    # ---- DELETE NUMBERS ----
    if data == "adm_delete":
        safe_answer(call)
        services = db.query("SELECT s.id, s.name, COUNT(n.id) FROM services s LEFT JOIN countries c ON c.service_id = s.id LEFT JOIN numbers n ON n.country_id = c.id GROUP BY s.id ORDER BY s.name")
        if not services:
            bot.send_message(chat_id, "❌ No services to delete.")
            return
        markup = types.InlineKeyboardMarkup(row_width=1)
        for s in services:
            markup.add(types.InlineKeyboardButton(f"🗑️ {s[1]} - {s[2]} nums", callback_data=f"adm_delsvc_{s[0]}", style='danger'))
        markup.add(types.InlineKeyboardButton("🔙 Back", callback_data="adm_back", style='danger'))
        try:
            bot.edit_message_text("🗑️ *Delete Number Files*\n\nPick a service:",
                                  chat_id, msg_id, parse_mode='Markdown', reply_markup=markup)
        except:
            pass
        return

    if data.startswith("adm_delsvc_"):
        sid = int(data.split("_")[2])
        safe_answer(call)
        countries = db.query("SELECT id, name, code FROM countries WHERE service_id=?", [sid])
        if not countries:
            db.execute("DELETE FROM services WHERE id=?", [sid])
            bot.send_message(chat_id, "✅ Service deleted.")
            send_admin_panel(chat_id)
            return
        markup = types.InlineKeyboardMarkup(row_width=1)
        for c in countries:
            count = db.query_one("SELECT COUNT(*) FROM numbers WHERE country_id=?", [c[0]])
            cnt = int(count[0]) if count else 0
            markup.add(types.InlineKeyboardButton(f"🗑️ {c[1]} ({c[2]}) - {cnt}", callback_data=f"adm_delctry_{c[0]}", style='danger'))
        markup.add(types.InlineKeyboardButton("🔙 Back", callback_data="adm_delete", style='danger'))
        try:
            bot.edit_message_text("🗑️ Pick country:", chat_id, msg_id, parse_mode='Markdown', reply_markup=markup)
        except:
            pass
        return

    if data.startswith("adm_delctry_"):
        cid = int(data.split("_")[2])
        safe_answer(call, "Deleting...")
        db.execute("DELETE FROM numbers WHERE country_id=?", [cid])
        db.execute("DELETE FROM countries WHERE id=?", [cid])
        bot.send_message(chat_id, "✅ Country file deleted.")
        log_event('admin_delete_numbers', f"country {cid}")
        send_admin_panel(chat_id)
        return

    # ---- STOCK ----
    if data == "adm_stock":
        safe_answer(call)
        rows = db.query("SELECT p.name, s.name, c.name, c.code, (SELECT COUNT(*) FROM numbers WHERE country_id=c.id AND assigned_to=0), (SELECT COUNT(*) FROM numbers WHERE country_id=c.id AND assigned_to!=0) FROM countries c JOIN services s ON c.service_id = s.id LEFT JOIN panels p ON s.panel_id = p.id ORDER BY p.name, s.name, c.name")
        if not rows:
            bot.send_message(chat_id, "📊 No files yet.")
            return
        text = "📊 *Stock Overview*\n\n"
        for pname, sname, cname, ccode, avail, assigned in rows:
            text += f"🎛️ *{pname or '?'}* → ⚙️ *{sname}*\n   🌍 *{cname}* ({ccode})\n   📦 `{int(avail)}` | 🔒 `{int(assigned)}`\n\n"
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 Back", callback_data="adm_back", style='danger'))
        try:
            bot.edit_message_text(text[:4000], chat_id, msg_id, parse_mode='Markdown', reply_markup=markup)
        except:
            bot.send_message(chat_id, text[:4000], parse_mode='Markdown', reply_markup=markup)
        return

    # ---- OTP GROUPS ----
    if data == "adm_otp_groups":
        safe_answer(call)
        current = get_setting('otp_link', 'https://t.me/alohaotp')
        force_on = get_setting('force_join_enabled', '0') == '1'
        channels = get_force_join_channels()
        text = f"📢 *OTP Groups*\n\n🔗 Main Link: `{current}`\n\n🔒 Force-Join: {'🟢 ON' if force_on else '🔴 OFF'}\n\n*Channels ({len(channels)}):*\n"
        for ch in channels:
            text += f"  • {ch[2] or 'Channel'}\n"
        if not channels:
            text += "  _None_\n"
        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.add(
            types.InlineKeyboardButton("🔗 Set OTP Link", callback_data="adm_set_otp", style='primary'),
            types.InlineKeyboardButton("🔄 Toggle Force", callback_data="adm_toggle_force", style='primary')
        )
        markup.add(
            types.InlineKeyboardButton("➕ Add Channel", callback_data="adm_add_channel", style='success'),
            types.InlineKeyboardButton("🗑️ Remove", callback_data="adm_rem_channel", style='danger')
        )
        markup.add(types.InlineKeyboardButton("🔙 Back", callback_data="adm_back", style='danger'))
        try:
            bot.edit_message_text(text[:4000], chat_id, msg_id, parse_mode='Markdown', reply_markup=markup)
        except:
            bot.send_message(chat_id, text[:4000], parse_mode='Markdown', reply_markup=markup)
        return

    if data == "adm_set_otp":
        safe_answer(call)
        msg = bot.send_message(chat_id, "🔗 Send new OTP link:",
                               reply_markup=types.ForceReply(selective=True))
        bot.register_next_step_handler(msg, admin_save_otp_link)
        return

    if data == "adm_toggle_force":
        current = get_setting('force_join_enabled', '0')
        new_val = '0' if current == '1' else '1'
        set_setting('force_join_enabled', new_val)
        safe_answer(call, f"Force: {'ON' if new_val == '1' else 'OFF'}")
        send_admin_panel(chat_id)
        return

    if data == "adm_add_channel":
        safe_answer(call)
        msg = bot.send_message(chat_id, "➕ Send: `chat_id | Name | link`\n\nEx: `@mychan | My Channel | https://t.me/mychan`",
                               parse_mode='Markdown', reply_markup=types.ForceReply(selective=True))
        bot.register_next_step_handler(msg, admin_save_channel)
        return

    if data == "adm_rem_channel":
        safe_answer(call)
        channels = get_force_join_channels()
        if not channels:
            bot.send_message(chat_id, "❌ No channels.")
            return
        markup = types.InlineKeyboardMarkup(row_width=1)
        for ch in channels:
            markup.add(types.InlineKeyboardButton(f"🗑️ {ch[2] or 'Channel'}", callback_data=f"adm_remch_{ch[0]}", style='danger'))
        markup.add(types.InlineKeyboardButton("🔙 Back", callback_data="adm_otp_groups", style='danger'))
        try:
            bot.edit_message_text("🗑️ Pick channel:", chat_id, msg_id, parse_mode='Markdown', reply_markup=markup)
        except:
            pass
        return

    if data.startswith("adm_remch_"):
        cid = int(data.split("_")[2])
        db.execute("DELETE FROM force_join WHERE id=?", [cid])
        safe_answer(call, "✅ Removed")
        send_admin_panel(chat_id)
        return

    # ---- USERS / STATS ----
    if data == "adm_users":
        safe_answer(call)
        total = db.query_one("SELECT COUNT(*) FROM users")
        users = db.query("SELECT user_id, username, first_name, otp_count FROM users ORDER BY otp_count DESC LIMIT 50")
        text = f"👥 *Users* - Total: `{int(total[0]) if total else 0}`\n\n"
        for u in users:
            uname = f"@{u[1]}" if u[1] else (u[2] or 'Unknown')
            text += f"• `{u[0]}` - {uname} - OTPs: `{int(u[3]) if u[3] else 0}`\n"
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 Back", callback_data="adm_back", style='danger'))
        try:
            bot.edit_message_text(text[:4000], chat_id, msg_id, parse_mode='Markdown', reply_markup=markup)
        except:
            bot.send_message(chat_id, text[:4000], parse_mode='Markdown', reply_markup=markup)
        return

    if data == "adm_stats":
        safe_answer(call)
        total_otp = db.query_one("SELECT SUM(otp_count) FROM users")
        total_users = db.query_one("SELECT COUNT(*) FROM users")
        total_numbers = db.query_one("SELECT COUNT(*) FROM numbers")
        assigned = db.query_one("SELECT COUNT(*) FROM numbers WHERE assigned_to!=0")
        top = db.query("SELECT user_id, username, first_name, otp_count FROM users ORDER BY otp_count DESC LIMIT 10")
        text = f"📊 *Stats*\n\n📬 OTPs: `{int(total_otp[0]) if total_otp and total_otp[0] else 0}`\n👥 Users: `{int(total_users[0]) if total_users else 0}`\n📞 Numbers: `{int(total_numbers[0]) if total_numbers else 0}`\n🔒 Assigned: `{int(assigned[0]) if assigned else 0}`\n\n🏆 *Top 10:*\n\n"
        for i, u in enumerate(top, 1):
            uname = f"@{u[1]}" if u[1] else (u[2] or 'Unknown')
            text += f"{i}. {uname} - `{int(u[3]) if u[3] else 0}`\n"
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 Back", callback_data="adm_back", style='danger'))
        try:
            bot.edit_message_text(text[:4000], chat_id, msg_id, parse_mode='Markdown', reply_markup=markup)
        except:
            bot.send_message(chat_id, text[:4000], parse_mode='Markdown', reply_markup=markup)
        return

    # ---- BROADCAST ----
    if data == "adm_broadcast":
        safe_answer(call)
        msg = bot.send_message(chat_id, "📢 Send message:",
                               reply_markup=types.ForceReply(selective=True))
        bot.register_next_step_handler(msg, admin_send_broadcast)
        return

    if data == "adm_announce":
        safe_answer(call)
        msg = bot.send_message(chat_id, "📣 Send announcement:",
                               reply_markup=types.ForceReply(selective=True))
        bot.register_next_step_handler(msg, admin_send_announcement)
        return

    # ---- BALANCE ----
    if data == "adm_balance":
        safe_answer(call, "Loading...")
        panels = get_all_panels()
        text = "💰 *Panels Balance*\n\n"
        if not panels:
            text += "_No active panels._"
        for p in panels:
            bal = p.get_balance() if p.is_logged_in else "N/A"
            text += f"🎛️ *{p.name}*: `{bal or 'N/A'}`\n"
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 Back", callback_data="adm_back", style='danger'))
        try:
            bot.edit_message_text(text[:4000], chat_id, msg_id, parse_mode='Markdown', reply_markup=markup)
        except:
            bot.send_message(chat_id, text[:4000], parse_mode='Markdown', reply_markup=markup)
        return

    # ---- WITHDRAWALS / SUPPORT ----
    if data == "adm_withdrawals":
        safe_answer(call, "Coming soon")
        return

    if data == "adm_support":
        safe_answer(call)
        msg = bot.send_message(chat_id, f"💬 Current: `{get_setting('support_contact', '@your_username')}`\n\nSend new:",
                               parse_mode='Markdown', reply_markup=types.ForceReply(selective=True))
        bot.register_next_step_handler(msg, admin_save_support)
        return

    # ---- ADMINS ----
    if data == "adm_admins":
        safe_answer(call)
        admins = get_all_admins()
        text = "👑 *Admins*\n\n"
        for a in admins:
            tag = " _(super)_" if a == SUPER_ADMIN else ""
            text += f"• `{a}`{tag}\n"
        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.add(types.InlineKeyboardButton("➕ Add", callback_data="adm_add_admin", style='success'))
        if user_id == SUPER_ADMIN:
            markup.add(types.InlineKeyboardButton("➖ Remove", callback_data="adm_rem_admin", style='danger'))
        markup.add(types.InlineKeyboardButton("🔙 Back", callback_data="adm_back", style='danger'))
        try:
            bot.edit_message_text(text, chat_id, msg_id, parse_mode='Markdown', reply_markup=markup)
        except:
            pass
        return

    if data == "adm_add_admin":
        safe_answer(call)
        msg = bot.send_message(chat_id, "👑 Send user ID:",
                               reply_markup=types.ForceReply(selective=True))
        bot.register_next_step_handler(msg, admin_save_admin)
        return

    if data == "adm_rem_admin":
        if user_id != SUPER_ADMIN:
            safe_answer(call, "⛔ Super admin only")
            return
        safe_answer(call)
        rows = db.query("SELECT user_id FROM admins")
        if not rows:
            bot.send_message(chat_id, "No additional admins.")
            return
        markup = types.InlineKeyboardMarkup(row_width=1)
        for r in rows:
            markup.add(types.InlineKeyboardButton(f"❌ {r[0]}", callback_data=f"adm_rema_{r[0]}", style='danger'))
        markup.add(types.InlineKeyboardButton("🔙 Back", callback_data="adm_admins", style='danger'))
        try:
            bot.edit_message_text("Pick admin:", chat_id, msg_id, parse_mode='Markdown', reply_markup=markup)
        except:
            pass
        return

    if data.startswith("adm_rema_"):
        rid = int(data.split("_")[2])
        if user_id != SUPER_ADMIN:
            safe_answer(call, "⛔")
            return
        db.execute("DELETE FROM admins WHERE user_id=?", [rid])
        safe_answer(call, "✅ Removed")
        send_admin_panel(chat_id)
        return

    # ---- SETTINGS ----
    if data == "adm_settings":
        safe_answer(call)
        text = f"⚙️ *Settings*\n\n📞 N/user: `{get_setting('numbers_per_user', '3')}`\n💬 Support: `{get_setting('support_contact')}`\n🤖 Bot: `{get_setting('bot_name')}`\n🔗 OTP: `{get_setting('otp_link')}`"
        markup = types.InlineKeyboardMarkup(row_width=1)
        markup.add(types.InlineKeyboardButton("📞 Set N/User", callback_data="adm_set_npu", style='primary'))
        markup.add(types.InlineKeyboardButton("🤖 Set Bot Name", callback_data="adm_set_botname", style='primary'))
        markup.add(types.InlineKeyboardButton("🔙 Back", callback_data="adm_back", style='danger'))
        try:
            bot.edit_message_text(text, chat_id, msg_id, parse_mode='Markdown', reply_markup=markup)
        except:
            pass
        return

    if data == "adm_set_npu":
        safe_answer(call)
        msg = bot.send_message(chat_id, "Send N per user (1-10):",
                               reply_markup=types.ForceReply(selective=True))
        bot.register_next_step_handler(msg, admin_save_npu)
        return

    if data == "adm_set_botname":
        safe_answer(call)
        msg = bot.send_message(chat_id, "Send bot name:",
                               reply_markup=types.ForceReply(selective=True))
        bot.register_next_step_handler(msg, admin_save_botname)
        return

    # ---- LOGS / BACKUP / RESTORE ----
    if data == "adm_logs":
        safe_answer(call)
        logs = db.query("SELECT event, details, timestamp FROM logs ORDER BY id DESC LIMIT 30")
        text = "📄 *Recent Logs*\n\n"
        if not logs:
            text += "_No logs._"
        for l in logs:
            text += f"🕐 {l[2][:19]}\n🔹 *{l[0]}*: {l[1]}\n\n"
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 Back", callback_data="adm_back", style='danger'))
        try:
            bot.edit_message_text(text[:4000], chat_id, msg_id, parse_mode='Markdown', reply_markup=markup)
        except:
            bot.send_message(chat_id, text[:4000], parse_mode='Markdown', reply_markup=markup)
        return

    if data == "adm_backup":
        safe_answer(call, "Generating...")
        try:
            tables = ['users', 'admins', 'panels', 'services', 'countries', 'numbers', 'settings', 'force_join']
            dump = {'backup_at': datetime.now().isoformat(), 'tables': {}}
            for t in tables:
                dump['tables'][t] = db.query(f"SELECT * FROM {t}")
            payload = json.dumps(dump)
            if len(payload) < 3900:
                bot.send_message(chat_id, f"💾 *Backup*\n\n```\n{payload}\n```", parse_mode='Markdown')
            else:
                bot.send_message(chat_id, f"💾 Backup size: {len(payload)} chars")
                for i in range(0, len(payload), 3900):
                    bot.send_message(chat_id, f"```\n{payload[i:i+3900]}\n```", parse_mode='Markdown')
            log_event('backup', 'ok')
        except Exception as e:
            bot.send_message(chat_id, f"❌ Backup failed: {e}")
        return

    if data == "adm_restore":
        safe_answer(call)
        msg = bot.send_message(chat_id, "📥 Send backup JSON:",
                               reply_markup=types.ForceReply(selective=True))
        bot.register_next_step_handler(msg, admin_restore)
        return


# ==================== ADMIN SAVE HANDLERS ====================
def admin_save_panel(message):
    if not is_admin(message.from_user.id):
        return
    try:
        lines = [l.strip() for l in message.text.strip().split('\n') if l.strip()]
        if len(lines) < 4:
            bot.send_message(message.chat.id, "❌ Need 4 lines.")
            return
        name, base_url, username, password = lines[0], lines[1], lines[2], lines[3]
        if not base_url.startswith('http'):
            bot.send_message(message.chat.id, "❌ URL must start with http.")
            return
        db.execute(
            "INSERT INTO panels (name, base_url, username, password, active, created_at) VALUES (?, ?, ?, ?, 1, ?)",
            [name, base_url, username, password, datetime.now().isoformat()]
        )
        bot.send_message(message.chat.id, f"✅ Panel *{name}* added!", parse_mode='Markdown')
        log_event('admin_add_panel', name)
        load_all_dashboards()
        send_admin_panel(message.chat.id)
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Error: {e}")


def admin_update_panel(message, pid):
    if not is_admin(message.from_user.id):
        return
    try:
        lines = [l.strip() for l in message.text.strip().split('\n') if l.strip()]
        if len(lines) < 4:
            bot.send_message(message.chat.id, "❌ Need 4 lines.")
            return
        name, base_url, username, password = lines[0], lines[1], lines[2], lines[3]
        db.execute("UPDATE panels SET name=?, base_url=?, username=?, password=? WHERE id=?",
                   [name, base_url, username, password, pid])
        bot.send_message(message.chat.id, f"✅ Panel #{pid} updated.")
        load_all_dashboards()
        send_admin_panel(message.chat.id)
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Error: {e}")


def admin_save_otp_link(message):
    if not is_admin(message.from_user.id):
        return
    link = message.text.strip()
    if not link.startswith(('https://t.me/', 'http://t.me/')):
        bot.send_message(message.chat.id, "❌ Must start with https://t.me/")
        return
    set_setting('otp_link', link)
    bot.send_message(message.chat.id, f"✅ OTP link updated.")
    log_event('admin_set_otp_link', link)


def admin_save_channel(message):
    if not is_admin(message.from_user.id):
        return
    try:
        parts = [p.strip() for p in message.text.split('|')]
        if len(parts) < 3:
            bot.send_message(message.chat.id, "❌ Format: `chat_id | Name | link`", parse_mode='Markdown')
            return
        chat_id, title, link = parts[0], parts[1], parts[2]
        db.execute("INSERT INTO force_join (chat_id, chat_title, invite_link, added_at) VALUES (?, ?, ?, ?)",
                   [chat_id, title, link, datetime.now().isoformat()])
        bot.send_message(message.chat.id, f"✅ Channel *{title}* added.", parse_mode='Markdown')
        log_event('admin_add_channel', title)
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Error: {e}")


def admin_save_npu(message):
    if not is_admin(message.from_user.id):
        return
    try:
        n = int(message.text.strip())
        if 1 <= n <= 10:
            set_setting('numbers_per_user', str(n))
            bot.send_message(message.chat.id, f"✅ N/user = `{n}`", parse_mode='Markdown')
        else:
            bot.send_message(message.chat.id, "❌ Must be 1-10.")
    except:
        bot.send_message(message.chat.id, "❌ Send a number.")


def admin_save_botname(message):
    if not is_admin(message.from_user.id):
        return
    name = message.text.strip()
    if name and len(name) < 50:
        set_setting('bot_name', name)
        bot.send_message(message.chat.id, f"✅ Bot name set.")
    else:
        bot.send_message(message.chat.id, "❌ Invalid.")


def admin_save_support(message):
    if not is_admin(message.from_user.id):
        return
    set_setting('support_contact', message.text.strip())
    bot.send_message(message.chat.id, "✅ Support updated.")


def admin_save_admin(message):
    if not is_admin(message.from_user.id):
        return
    try:
        uid = int(message.text.strip())
        db.execute("INSERT OR IGNORE INTO admins (user_id, added_by, added_at) VALUES (?, ?, ?)",
                   [uid, message.from_user.id, datetime.now().isoformat()])
        bot.send_message(message.chat.id, f"✅ `{uid}` added.", parse_mode='Markdown')
    except:
        bot.send_message(message.chat.id, "❌ Invalid ID.")


def admin_send_broadcast(message):
    if not is_admin(message.from_user.id):
        return
    text = message.text.strip()
    users = db.query("SELECT user_id FROM users")
    sent = failed = 0
    for u in users:
        try:
            bot.send_message(int(u[0]), f"📢 *Announcement*\n\n{text}", parse_mode='Markdown')
            sent += 1
            time.sleep(0.05)
        except:
            failed += 1
    bot.send_message(message.chat.id, f"✅ Sent: `{sent}`. Failed: `{failed}`", parse_mode='Markdown')
    log_event('broadcast', f"sent={sent} failed={failed}")


def admin_send_announcement(message):
    if not is_admin(message.from_user.id):
        return
    text = message.text.strip()
    try:
        sent = bot.send_message(CHAT_ID, f"📣 *ANNOUNCEMENT*\n\n{text}", parse_mode='Markdown')
        try:
            bot.pin_chat_message(CHAT_ID, sent.message_id)
        except:
            pass
        bot.send_message(message.chat.id, "✅ Announcement sent.")
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Failed: {e}")


def admin_restore(message):
    if not is_admin(message.from_user.id):
        return
    try:
        raw = message.text.strip().replace('```json', '').replace('```', '').strip()
        data = json.loads(raw)
        tables = data.get('tables', {})
        for t, rows in tables.items():
            if not rows:
                continue
            cols_count = len(rows[0])
            placeholders = ','.join(['?'] * cols_count)
            for row in rows:
                db.execute(f"INSERT OR REPLACE INTO {t} VALUES ({placeholders})", row)
        bot.send_message(message.chat.id, f"✅ Restored {len(tables)} tables.")
        log_event('restore', 'ok')
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Restore failed: {e}")


# ==================== UPLOAD WIZARD ====================
def wizard_service(message, panel_id):
    if not is_admin(message.from_user.id):
        return
    service_name = message.text.strip()
    if not service_name or len(service_name) > 40:
        bot.send_message(message.chat.id, "❌ Invalid name.")
        return
    existing = db.query_one("SELECT id FROM services WHERE name=? AND panel_id=?", [service_name, panel_id])
    if existing:
        sid = int(existing[0])
    else:
        db.execute("INSERT INTO services (panel_id, name, created_at) VALUES (?, ?, ?)",
                   [panel_id, service_name, datetime.now().isoformat()])
        row = db.query_one("SELECT id FROM services WHERE name=? AND panel_id=?", [service_name, panel_id])
        sid = int(row[0]) if row else None
    if not sid:
        bot.send_message(message.chat.id, "❌ Could not create service.")
        return
    msg = bot.send_message(message.chat.id,
                           f"✅ Service: *{service_name}*\n\n➕ *Step 3/5* - Send *Country and Code*:\n\nEx: `Nigeria | +234`",
                           parse_mode='Markdown',
                           reply_markup=types.ForceReply(selective=True))
    bot.register_next_step_handler(msg, wizard_country, sid)


def wizard_country(message, service_id):
    if not is_admin(message.from_user.id):
        return
    try:
        parts = [p.strip() for p in message.text.split('|')]
        if len(parts) < 2:
            bot.send_message(message.chat.id, "❌ Format: `Country | +Code`")
            return
        name, code = parts[0], parts[1]
        if not code.startswith('+'):
            code = '+' + code.lstrip('+')
        db.execute("INSERT INTO countries (service_id, name, code, numbers_per_user, created_at) VALUES (?, ?, ?, ?, ?)",
                   [service_id, name, code, int(get_setting('numbers_per_user', '3')), datetime.now().isoformat()])
        row = db.query_one("SELECT id FROM countries WHERE service_id=? AND name=?", [service_id, name])
        cid = int(row[0]) if row else None
        if not cid:
            bot.send_message(message.chat.id, "❌ Could not create country.")
            return
        msg = bot.send_message(message.chat.id,
                       f"✅ Country: *{name}* ({code})\n\n➕ *Step 4/5* - Send numbers:\n\n• *Paste* numbers (commas/spaces/newlines)\n• OR *upload a .txt file*\n\n💡 _For large lists, use .txt upload._",
                       parse_mode='Markdown',
                       reply_markup=types.ForceReply(selective=True))
bot.register_next_step_handler(msg, wizard_numbers, cid)
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Error: {e}")

def wizard_numbers(message, country_id):
    if not is_admin(message.from_user.id):
        return
    try:
        raw = ""
        if message.document:
            try:
                file_info = bot.get_file(message.document.file_id)
                downloaded = bot.download_file(file_info.file_path)
                raw = downloaded.decode('utf-8', errors='ignore')
                bot.send_message(message.chat.id, f"📄 File received: {message.document.file_name}")
            except Exception as e:
                bot.send_message(message.chat.id, f"❌ Could not read file: {e}")
                return
        elif message.text:
            raw = message.text.strip()
        else:
            bot.send_message(message.chat.id, "❌ Send numbers as text OR upload a .txt file.")
            return

        parts = re.split(r'[\n,\s;]+', raw)
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

        msg = bot.send_message(message.chat.id,
                               f"✅ Added *{added}* numbers (skipped {len(numbers) - added} dupes).\n\n➕ *Step 5/5* - Numbers *per user*?\n\nSend 1-10 or `skip`.",
                               parse_mode='Markdown',
                               reply_markup=types.ForceReply(selective=True))
        bot.register_next_step_handler(msg, wizard_npu, country_id)
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Error: {e}")



def wizard_npu(message, country_id):
    if not is_admin(message.from_user.id):
        return
    txt = message.text.strip().lower()
    if txt != 'skip':
        try:
            n = int(txt)
            if 1 <= n <= 10:
                db.execute("UPDATE countries SET numbers_per_user=? WHERE id=?", [n, country_id])
        except:
            pass
    bot.send_message(message.chat.id, "✅ *Number file added!* 🎉", parse_mode='Markdown')
    log_event('admin_upload_numbers', f"country {country_id}")
    send_admin_panel(message.chat.id)


# ==================== COMMANDS ====================
def setup_bot_commands():
    commands = [
        types.BotCommand("start", "Start the bot"),
        types.BotCommand("getnumber", "Get a virtual number"),
        types.BotCommand("balance", "Check your balance"),
        types.BotCommand("withdraw", "Request withdrawal"),
        types.BotCommand("support", "Contact support"),
        types.BotCommand("status", "Bot status"),
        types.BotCommand("admin", "Open admin panel"),
    ]
    try:
        bot.set_my_commands(commands)
        print("Bot commands set")
    except Exception as e:
        print(f"Could not set commands: {e}")


@bot.message_handler(commands=['admin'])
def cmd_admin(message):
    if not is_admin(message.from_user.id):
        bot.send_message(message.chat.id, "⛔ Admins only.")
        return
    send_admin_panel(message.chat.id)


@bot.message_handler(commands=['getnumber'])
def cmd_getnumber(message):
    kb_get_number(message)


@bot.message_handler(commands=['balance'])
def cmd_balance(message):
    kb_balance(message)


@bot.message_handler(commands=['withdraw'])
def cmd_withdraw(message):
    kb_withdraw(message)


@bot.message_handler(commands=['support'])
def cmd_support(message):
    kb_support(message)


@bot.message_handler(commands=['status'])
def cmd_status(message):
    panels = get_all_panels()
    total_users = db.query_one("SELECT COUNT(*) FROM users")
    total_numbers = db.query_one("SELECT COUNT(*) FROM numbers")
    text = f"📊 *Status*\n\n🎛️ Panels: `{len(panels)}`\n📞 Numbers: `{int(total_numbers[0]) if total_numbers else 0}`\n👥 Users: `{int(total_users[0]) if total_users else 0}`"
    bot.send_message(message.chat.id, text, parse_mode='Markdown')


@bot.message_handler(commands=['debugdb'])
def cmd_debugdb(message):
    if not is_admin(message.from_user.id):
        return
    try:
        panels = db.query("SELECT id, name, base_url, active FROM panels")
        users = db.query("SELECT user_id, first_name FROM users LIMIT 5")
        text = f"🔍 *DB Debug*\n\n*Panels:* `{len(panels)}`\n"
        for p in panels:
            text += f"  • `{p[0]}` {p[1]}\n"
        text += f"\n*Users:* `{len(users)}`\n"
        for u in users:
            text += f"  • `{u[0]}` {u[1]}\n"
        bot.send_message(message.chat.id, text[:4000], parse_mode='Markdown')
    except Exception as e:
        bot.send_message(message.chat.id, f"Debug error: {e}")


# ==================== STARTUP ====================
if __name__ == '__main__':
    print('=' * 50)
    print('NBHC OTP Bot v6.0')
    print('=' * 50)

    db.init_tables()
    setup_bot_commands()

    print('Loading dashboards...')
    load_all_dashboards()

    print('Initial login...')
    for p in get_all_panels():
        threading.Thread(target=lambda d=p: d.login(), daemon=True).start()

    time.sleep(3)
    start_all_monitors()

    print('Bot is ready!')
    print('Polling started')

    while True:
        try:
            bot.polling(none_stop=True, timeout=60)
        except Exception as e:
            print(f'Polling error: {e}')
            time.sleep(5)
