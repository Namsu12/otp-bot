#!/usr/bin/env python3
import requests
import telebot
import time
import re
import hashlib
import threading
import os
from datetime import datetime
from bs4 import BeautifulSoup
from telebot import types
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ==================== CONFIGURATION ====================
BOT_TOKEN = os.getenv('BOT_TOKEN', '')
CHAT_ID = os.getenv('CHAT_ID', '')
ADMIN_ID = int(os.getenv('ADMIN_ID', '0'))
USERNAME = os.getenv('USERNAME', '')
PASSWORD = os.getenv('PASSWORD', '')

BASE_URL = 'http://51.77.52.79/ints'
LOGIN_URL = f'{BASE_URL}/login'
# =======================================================

if not BOT_TOKEN or not CHAT_ID:
    print("❌ ERROR: BOT_TOKEN and CHAT_ID must be set!")
    exit(1)

bot = telebot.TeleBot(BOT_TOKEN)


# ==================== DASHBOARD BOT ====================
class DashboardBot:
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
        match = re.search(r'(\d+)\s*([+\-*/])\s*(\d+)', text)
        if match:
            a, op, b = int(match.group(1)), match.group(2), int(match.group(3))
            result = {'+': a+b, '-': a-b, '*': a*b, '/': a//b if b else 0}.get(op)
            return str(result) if result is not None else None
        return None

    def login(self):
        try:
            self.session.cookies.clear()
            page = self.session.get(LOGIN_URL, timeout=15)
            soup = BeautifulSoup(page.text, 'html.parser')
            form = soup.find('form')
            if not form:
                return False

            action = form.get('action', '')
            submit_url = action if action.startswith('http') else f'{BASE_URL}/{action.lstrip("/")}' if action else LOGIN_URL

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
                if captcha_input:
                    parent_text = captcha_input.find_parent().get_text(strip=True)
                    m = re.search(r'(\d+)\s*([+\-*/])\s*(\d+)', parent_text)
                    if m:
                        answer = self.solve_captcha(m.group(0))
                        if answer:
                            data[captcha_field] = answer

            if captcha_field and captcha_field not in data:
                m = re.search(r'(\d+)\s*([+\-*/])\s*(\d+)', page_text)
                if m:
                    answer = self.solve_captcha(m.group(0))
                    if answer:
                        data[captcha_field] = answer

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
            print(f'❌ Page fetch error ({path}): {e}')
        return None

    def clean_text(self, text):
        return re.sub(r'\s+', ' ', text).strip()

    # ---- OTP ----
    def fetch_sms(self):
        if not self.is_logged_in:
            if not self.login():
                return []
        try:
            time.sleep(2)
            resp = self.session.get(f'{BASE_URL}/agent/SMSCDRReports', timeout=20)
            if resp.status_code == 200:
                self.last_check = datetime.now()
                return self.extract_otps(resp.text)
            elif resp.status_code in [401, 403]:
                self.is_logged_in = False
        except Exception as e:
            print(f'⚠️ Fetch error: {e}')
        return []

    def extract_otps(self, html):
        soup = BeautifulSoup(html, 'html.parser')
        results = []
        skip = ['date range', 'number', 'cli', 'client', 'currency', 'payout',
                'search', 'filter', 'export', 'entries', 'previous', 'next', 'page']
        for row in soup.find_all('tr'):
            cells = [c.get_text(strip=True) for c in row.find_all(['td', 'th'])]
            text = ' '.join(cells)
            if len(text) < 10 or any(k in text.lower() for k in skip):
                continue
            otp = self.find_otp(text)
            if otp:
                h = hashlib.md5(text.encode()).hexdigest()
                if h not in self.processed_sms:
                    results.append({'text': text, 'otp': otp, 'hash': h})
        return results

    def find_otp(self, text):
        patterns = [
            r'(?:OTP|code|verification)[:\s]*(\d{4,6})',
            r'(?:^|\s)(\d{6})(?:\s|$)',
            r'(?:^|\s)(\d{5})(?:\s|$)',
            r'(?:^|\s)(\d{4})(?:\s|$)',
        ]
        for p in patterns:
            m = re.search(p, text, re.IGNORECASE)
            if m:
                otp = m.group(1) if m.lastindex else m.group(0)
                if otp.isdigit() and 4 <= len(otp) <= 6:
                    return otp
        return None

    # ---- Data Extractors ----
    def get_profile(self):
        html = self.get_page('Profile')
        if not html:
            return None
        soup = BeautifulSoup(html, 'html.parser')
        data = {}
        for row in soup.find_all('tr'):
            cells = row.find_all(['td', 'th'])
            if len(cells) >= 2:
                key = cells[0].get_text(strip=True)
                val = cells[1].get_text(strip=True)
                if key and val and len(key) < 50 and len(val) < 200:
                    data[key] = val
        return data

    def get_notifications(self):
        html = self.get_page('Notifications')
        if not html:
            return []
        soup = BeautifulSoup(html, 'html.parser')
        notices = []
        for row in soup.find_all('tr'):
            text = self.clean_text(row.get_text())
            if text and 10 < len(text) < 500:
                notices.append(text)
        return notices[:10]

    def get_numbers(self):
        html = self.get_page('MySMSNumbers')
        if not html:
            return []
        soup = BeautifulSoup(html, 'html.parser')
        numbers = []
        for row in soup.find_all('tr'):
            cells = [c.get_text(strip=True) for c in row.find_all(['td', 'th'])]
            text = ' | '.join(cells)
            if text and len(text) > 5:
                numbers.append(text)
        return numbers[:20]

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


dashboard = DashboardBot()


# ==================== BUTTON MENU ====================
def main_menu():
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("📱 Get Number", callback_data="get_number")
    )
    markup.add(
        types.InlineKeyboardButton("💸 Withdraw", callback_data="withdraw"),
        types.InlineKeyboardButton("💵 Balance", callback_data="balance")
    )
    markup.add(
        types.InlineKeyboardButton("🌍 Available Country", callback_data="country"),
        types.InlineKeyboardButton("📊 Status", callback_data="status")
    )
    markup.add(
        types.InlineKeyboardButton("❓ Help", callback_data="help")
    )
    return markup


def back_menu():
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🔙 Back to Menu", callback_data="back_menu"))
    return markup


@bot.message_handler(commands=['start', 'menu'])
def cmd_start(message):
    text = """
👋 *Welcome to NBHC OTP Bot*

Get virtual numbers, receive OTPs, and manage your account — all from here.

Select an option below 👇
"""
    bot.send_message(message.chat.id, text, parse_mode='Markdown', reply_markup=main_menu())


@bot.callback_query_handler(func=lambda call: True)
def handle_button(call):
    try:
        if call.data == "get_number":
            bot.answer_callback_query(call.id, "📱 Loading...")
            text = "📱 *Get Number*\n\n_No numbers available yet._\n\nAdmin needs to add numbers first."
            bot.send_message(call.message.chat.id, text, parse_mode='Markdown', reply_markup=back_menu())

        elif call.data == "withdraw":
            bot.answer_callback_query(call.id, "💸 Loading...")
            text = "💸 *Withdraw*\n\n_Coming soon._\n\nYou'll be able to cash out your earnings here."
            bot.send_message(call.message.chat.id, text, parse_mode='Markdown', reply_markup=back_menu())

        elif call.data == "balance":
            bot.answer_callback_query(call.id, "💵 Loading...")
            bot.send_message(call.message.chat.id, "💵 *Fetching balance...*", parse_mode='Markdown')
            bal = dashboard.get_balance()
            if bal:
                text = f"💵 *Your Balance*\n\n💰 `{bal}`"
            else:
                text = "💵 *Your Balance*\n\n💰 $0.00"
            bot.send_message(call.message.chat.id, text, parse_mode='Markdown', reply_markup=back_menu())

        elif call.data == "country":
            bot.answer_callback_query(call.id, "🌍 Loading...")
            text = "🌍 *Available Countries*\n\n_No countries added yet._\n\nAdmin needs to add countries."
            bot.send_message(call.message.chat.id, text, parse_mode='Markdown', reply_markup=back_menu())

        elif call.data == "status":
            bot.answer_callback_query(call.id, "📊 Loading...")
            text = f"""
📊 *Bot Status*

🔐 Login: {'✅ Yes' if dashboard.is_logged_in else '❌ No'}
📬 OTPs Found: {len(dashboard.sms_history)}
🕐 Last Check: {dashboard.last_check.strftime('%H:%M:%S') if dashboard.last_check else 'Never'}
🔄 Monitoring: Active
☁️ Host: Koyeb Cloud
"""
            bot.send_message(call.message.chat.id, text, parse_mode='Markdown', reply_markup=back_menu())

        elif call.data == "help":
            bot.answer_callback_query(call.id, "❓ Loading...")
            text = """
❓ *Help*

Tap any button to get started.

• *Get Number* — Request a virtual number
• *Withdraw* — Cash out your earnings
• *Balance* — Check your balance
• *Available Country* — See supported countries
• *Status* — Bot health check

Need help? Contact admin.
"""
            bot.send_message(call.message.chat.id, text, parse_mode='Markdown', reply_markup=back_menu())

        elif call.data == "back_menu":
            bot.answer_callback_query(call.id)
            try:
                bot.delete_message(call.message.chat.id, call.message.message_id)
            except:
                pass
            cmd_start(call.message)

    except Exception as e:
        print(f"Button error: {e}")
        bot.answer_callback_query(call.id, "⚠️ Error, try again")


# ==================== TEXT COMMANDS ====================
@bot.message_handler(commands=['history'])
def cmd_history(message):
    if not dashboard.sms_history:
        bot.send_message(message.chat.id, '📭 No OTP history')
        return
    text = '*Recent OTPs:*\n\n'
    for item in dashboard.sms_history[-10:]:
        text += f"🕐 {item['time']}\n🔑 OTP: `{item['otp']}`\n➖➖➖➖➖➖\n"
    bot.send_message(message.chat.id, text, parse_mode='Markdown')


@bot.message_handler(commands=['check'])
def cmd_check(message):
    bot.send_message(message.chat.id, '🔍 Checking for OTPs...')
    messages = dashboard.fetch_sms()
    found = False
    for msg in messages:
        if msg['hash'] not in dashboard.processed_sms:
            dashboard.processed_sms.add(msg['hash'])
            send_otp_to_chat(msg)
            found = True
    if not found:
        bot.send_message(message.chat.id, '❌ No new OTPs found!')


@bot.message_handler(commands=['login'])
def cmd_login(message):
    bot.send_message(message.chat.id, '🔐 Logging in...')
    if dashboard.login():
        bot.send_message(message.chat.id, '✅ Login successful!')
    else:
        bot.send_message(message.chat.id, '❌ Login failed!')


# ==================== MONITOR ====================
def send_otp_to_chat(msg):
    try:
        text = f"""
🔐 *NEW OTP RECEIVED!*

📱 *SMS:*
{msg['text'][:300]}

🔑 *OTP CODE:* `{msg['otp']}`
🕐 *Time:* {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

⚠️ _This OTP will expire soon!_
"""
        bot.send_message(CHAT_ID, text, parse_mode='Markdown')
        print(f'✅ OTP sent: {msg["otp"]}')
    except Exception as e:
        print(f'❌ Telegram error: {e}')
        try:
            bot.send_message(CHAT_ID, f'🔐 OTP: {msg["otp"]}')
        except:
            pass


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
                        'text': msg['text'],
                        'time': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                    })
                    send_otp_to_chat(msg)
        except Exception as e:
            print(f'❌ Monitor error: {e}')
        time.sleep(10)


# ==================== MAIN ====================
if __name__ == '__main__':
    print('=' * 40)
    print('🤖 NBHC OTP Bot - Koyeb Edition')
    print('=' * 40)

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