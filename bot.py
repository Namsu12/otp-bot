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
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ==================== CONFIGURATION (from Environment Variables) ====================
BOT_TOKEN = os.getenv('BOT_TOKEN', '')
CHAT_ID = os.getenv('CHAT_ID', '')
USERNAME = os.getenv('USERNAME', '')
PASSWORD = os.getenv('PASSWORD', '')

BASE_URL = 'http://51.77.52.79/ints'
LOGIN_URL = f'{BASE_URL}/login'
SMS_CDR_URL = f'{BASE_URL}/agent/SMSCDRReports'
# ====================================================================================

if not BOT_TOKEN or not CHAT_ID:
    print("❌ ERROR: BOT_TOKEN and CHAT_ID must be set as environment variables!")
    exit(1)

bot = telebot.TeleBot(BOT_TOKEN)

class OTPForwarder:
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
            captcha_question = None
            
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
                    if re.search(r'[+\-*/]', parent_text):
                        captcha_question = parent_text
            
            if not captcha_question:
                match = re.search(r'(\d+\s*[+\-*/]\s*\d+\s*=?\s*)', page_text)
                if match:
                    captcha_question = match.group(0)
            
            if captcha_question and captcha_field:
                answer = self.solve_captcha(captcha_question)
                if answer:
                    data[captcha_field] = answer
            
            data.setdefault('submit', 'Login')
            
            self.session.headers.update({'Referer': LOGIN_URL, 'Origin': BASE_URL})
            resp = self.session.post(submit_url, data=data, timeout=15, allow_redirects=True)
            
            self.is_logged_in = 'login' not in resp.url.lower()
            
            if self.is_logged_in:
                print("✅ Login successful!")
            else:
                print("❌ Login failed")
            
            return self.is_logged_in
            
        except Exception as e:
            print(f'❌ Login error: {e}')
            return False

    def fetch_sms(self):
        if not self.is_logged_in:
            if not self.login():
                return []
        
        try:
            time.sleep(2)
            resp = self.session.get(SMS_CDR_URL, timeout=20)
            
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

    def monitor(self):
        print("🔄 Monitor thread started")
        while self.running:
            try:
                if not self.is_logged_in:
                    self.login()
                    time.sleep(15)
                    continue
                
                messages = self.fetch_sms()
                
                for msg in messages:
                    if msg['hash'] not in self.processed_sms:
                        self.processed_sms.add(msg['hash'])
                        self.sms_history.append({
                            'otp': msg['otp'],
                            'text': msg['text'],
                            'time': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                        })
                        self.send_otp(msg)
                        
            except Exception as e:
                print(f'❌ Monitor error: {e}')
            
            time.sleep(10)

    def send_otp(self, msg):
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

forwarder = OTPForwarder()

# ==================== TELEGRAM COMMANDS ====================
@bot.message_handler(commands=['start', 'help'])
def cmd_start(message):
    bot.send_message(message.chat.id, """
🤖 *SMS OTP Forwarder*

✅ Bot is running 24/7!

*Commands:*
/status - Check bot status
/login - Re-login to dashboard
/check - Check for new OTP
/history - View OTP history
/clear - Clear history
""", parse_mode='Markdown')

@bot.message_handler(commands=['status'])
def cmd_status(message):
    status = f"""
📊 *Bot Status*

🔐 Login: {'✅ Yes' if forwarder.is_logged_in else '❌ No'}
📬 OTPs Found: {len(forwarder.sms_history)}
🕐 Last Check: {forwarder.last_check.strftime('%H:%M:%S') if forwarder.last_check else 'Never'}
🔄 Monitoring: Active
☁️ Host: Koyeb Cloud
"""
    bot.send_message(message.chat.id, status, parse_mode='Markdown')

@bot.message_handler(commands=['login'])
def cmd_login(message):
    bot.send_message(message.chat.id, '🔐 Logging in...')
    if forwarder.login():
        bot.send_message(message.chat.id, '✅ Login successful!')
    else:
        bot.send_message(message.chat.id, '❌ Login failed!')

@bot.message_handler(commands=['check'])
def cmd_check(message):
    bot.send_message(message.chat.id, '🔍 Checking for OTPs...')
    messages = forwarder.fetch_sms()
    
    found = False
    for msg in messages:
        if msg['hash'] not in forwarder.processed_sms:
            forwarder.processed_sms.add(msg['hash'])
            forwarder.send_otp(msg)
            found = True
    
    if not found:
        bot.send_message(message.chat.id, '❌ No new OTPs found!')

@bot.message_handler(commands=['history'])
def cmd_history(message):
    if not forwarder.sms_history:
        bot.send_message(message.chat.id, '📭 No OTP history')
        return
    
    text = '*Recent OTPs:*\n\n'
    for item in forwarder.sms_history[-10:]:
        text += f"🕐 {item['time']}\n🔑 OTP: `{item['otp']}`\n➖➖➖➖➖➖\n"
    
    bot.send_message(message.chat.id, text, parse_mode='Markdown')

@bot.message_handler(commands=['clear'])
def cmd_clear(message):
    forwarder.processed_sms.clear()
    forwarder.sms_history.clear()
    bot.send_message(message.chat.id, '✅ History cleared!')

# ==================== MAIN ====================
if __name__ == '__main__':
    print('=' * 40)
    print('🤖 SMS OTP Forwarder - Koyeb Edition')
    print('=' * 40)
    
    # Initial login
    print('\n🔐 Attempting initial login...')
    forwarder.login()
    
    # Start monitoring thread
    threading.Thread(target=forwarder.monitor, daemon=True).start()
    print('✅ Monitor thread started')
    print('📱 Telegram bot ready')
    
    # Start Telegram polling with error recovery
    while True:
        try:
            bot.polling(none_stop=True, timeout=60)
        except Exception as e:
            print(f'⚠️ Polling error: {e}')
            time.sleep(5)
