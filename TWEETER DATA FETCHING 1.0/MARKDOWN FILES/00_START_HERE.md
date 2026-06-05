# 🚀 Start Here

Welcome to the Twitter Historical Tweet Scraper!

---

## ⚡ Quick Start (3 Steps)

### 1️⃣ Configure
```bash
python setup_api_cookies.py
```
Follow the wizard to set up your Twitter cookies and API tokens.

### 2️⃣ Fetch Tweets
```bash
python fetch_historical_tweets.py
```
Fetches last 2 weeks of tweets from configured accounts.

### 3️⃣ Check Output
```bash
ls TWEETS/
```
Daily files organized by account and Jalali calendar dates.

---

## 📚 Full Documentation

Read these guides in order:

1. **[01_SETUP_GUIDE.md](01_SETUP_GUIDE.md)** ← Start here for detailed setup
2. **[02_USAGE_GUIDE.md](02_USAGE_GUIDE.md)** - How to run the scripts
3. **[03_FEATURES.md](03_FEATURES.md)** - What data is extracted
4. **[04_TROUBLESHOOTING.md](04_TROUBLESHOOTING.md)** - Fix common errors
5. **[05_API_REFERENCE.md](05_API_REFERENCE.md)** - Technical API details

---

## 🆘 Common Issues

**401 Unauthorized?**
→ Update cookies: `python setup_api_cookies.py` (option 2)

**404 Not Found?**
→ Update query IDs in `config.json` (see [04_TROUBLESHOOTING.md](04_TROUBLESHOOTING.md))

**No tweets found?**
→ Check account is public and has recent tweets

---

## 📁 Project Structure

```
TWEETER DATA FETCHING/
├── config.json                    # Your configuration
├── fetch_historical_tweets.py     # Main scraper
├── monitor_live_tweets.py         # Live monitoring
├── setup_api_cookies.py           # Setup wizard
├── TWEETS/                        # Output folder
├── documentation/                 # All guides (you are here)
└── api_references/                # API examples for debugging
```

---

## 🎯 What This Does

- ✅ Fetches tweets, retweets, replies, and quote tweets
- ✅ Expands shortened URLs (t.co → real URLs)
- ✅ Extracts hashtags, mentions, and media
- ✅ Shows full engagement stats (likes, retweets, views)
- ✅ Organizes by Jalali calendar dates
- ✅ Handles deleted/unavailable tweets gracefully

---

## 💡 Tips

- Run `setup_api_cookies.py` whenever cookies expire (every 30-90 days)
- Use `monitor_live_tweets.py` for continuous monitoring
- Check `api_references/` folder for API response examples
- All documentation is in `documentation/` folder

---

## ➡️ Next Step

Go to **[01_SETUP_GUIDE.md](01_SETUP_GUIDE.md)** for detailed setup instructions.
