import staypresent
import os

if __name__ == '__main__':
    port = int(os.getenv("PORT", 8080))
    staypresent.run("bot.py", port=port)
