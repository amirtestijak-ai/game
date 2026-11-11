SECRET SOCIETY - Wallet Update

Run command: python bot.py

Required environment variables:
BOT_TOKEN=your Telegram bot token
ADMIN_ID=your numeric Telegram user ID
BLUPAL_API_KEY=your BluPal live API key
CHANNEL_LINK=optional channel link

IMPORTANT:
- Keep the existing secret_society.sqlite3 database. Do not delete it.
- Keep the existing environment variables and BluPal API key.
- The update adds wallet_balance to users and creates wallet_topups and wallet_transactions tables automatically.
- Existing XP purchases continue through the same BluPal invoice flow. Wallet top-ups use the same BluPal invoice API and are verified server-side before balance is credited.
- Install dependencies from requirements.txt.
