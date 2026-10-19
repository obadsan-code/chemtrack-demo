# chemtrack

Operations platform for a chemical manufacturing plant: a Telegram bot plus a web dashboard.
It replaced spreadsheets and scattered chat messages with a single system the team uses every day.

*Versión en español: [README.es.md](README.es.md)*

## What it does
- **Inventory** of raw materials and finished goods, with batches (lots) and a stock ledger (kardex)
- **Recipes / formulas**, with automatic raw-material deduction when producing
- **Customer orders** with order numbers, line items and VAT
- **Returnable drum tracking** per customer
- **Employee attendance** logged from Telegram
- **Weekly PDF reports** and **automatic backups** (SQL + CSV) delivered via Telegram
- **Role-based permissions** (admin, supervisor, operators) per action
- **Web dashboard** (React + Express) for inventory, orders, attendance and drums

## Stack
Python 3.10 · python-telegram-bot 20 · PostgreSQL · reportlab · React 19 · Express 5 · recharts · Railway

## Getting started
1. `cp .env.example .env` and fill in the values
2. Create the database: `psql $DATABASE_URL -f db/schema.sql` (optional: `db/seed_demo.sql`)
3. Bot: `pip install -r requirements.txt && python bot.py`
4. Dashboard: `cd dashboard && npm install && node server.js` (and `npm start` for the front end, with `REACT_APP_API_URL` set)

All credentials and Telegram user IDs come from environment variables; there are no secrets in the code.
The bot's user-facing messages are in Spanish, since that is the team's working language.
