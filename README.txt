# Vynexa News Bot hotfix
France24 Iran source switched to a domain-restricted Google News RSS query to avoid direct-feed HTTP 403.
First startup now queues a limited batch of current headlines rather than silently marking all current items old.
Newest headlines are processed first. Translation failures no longer block publication.
No OPENAI_API_KEY is required. Keep BOT_TOKEN, ADMIN_ID and the persistent secret_society.sqlite3 database.
Run: python bot.py
