# SECRET SOCIETY Telegram Bot

## Environment variables
- `BOT_TOKEN`: Telegram bot token
- `ADMIN_ID`: numeric Telegram user ID of the owner/admin

## Start command
`python secret_society_bot.py`

## Required channel membership
The bot checks membership in `@dsgjnjjyfg`. Add the bot to that channel as an administrator (no posting permissions are needed) so Telegram allows it to check member status. Users must join and press the membership verification button before using the bot.

## Button colors
Telegram inline buttons support preset styles only: `primary` (blue), `success` (green), and `danger` (red). Each configurable menu button has its own saved style.

## Referral reports
The admin panel includes a referral report showing inviter/invitee details and top inviters. Referral credit is applied after the invited user passes the channel membership check.

## Persistent storage
When `/data` exists, SQLite and mission images are stored there; otherwise they are stored beside the script.

Never commit your bot token or database to GitHub.
