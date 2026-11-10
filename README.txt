SECRET SOCIETY + BLUPAL XP PAYMENTS

Environment variables required:
BOT_TOKEN=your_telegram_bot_token
ADMIN_ID=your_numeric_telegram_user_id
BLUPAL_API_KEY=blu_live_your_api_key

Install:
pip install -r requirements.txt

Run:
python secret_society_bot_final.py

Setup:
1. Add BLUPAL_API_KEY to the server environment. Never send the API key in Telegram or commit it to GitHub.
2. Start the bot and open the admin panel.
3. Choose "قیمت‌گذاری XP" and set the price of 100 XP in toman (minimum 10,000 toman due to Blupal's invoice minimum).
4. Users can open "خرید XP", choose 100/500/1000 XP, pay the invoice, then press "بررسی پرداخت".
5. XP is added only after the bot independently checks the invoice status through Blupal's API and confirms PAID, live mode, and matching amounts. Each invoice can be credited only once.

The bot uses the existing SQLite database file secret_society.sqlite3. Back it up before deployment. The payment table is added automatically without deleting existing user/game data.
