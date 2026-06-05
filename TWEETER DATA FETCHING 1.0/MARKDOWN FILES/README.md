# Twitter Scraper Documentation

Complete documentation for the Twitter Historical Tweet Scraper.

---

## 📚 Documentation Index

Read these documents in order:

0. **[00_START_HERE.md](00_START_HERE.md)** ⭐ **START HERE** - Quick start guide

1. **[01_SETUP_GUIDE.md](01_SETUP_GUIDE.md)** - Initial setup and configuration
2. **[02_USAGE_GUIDE.md](02_USAGE_GUIDE.md)** - How to run the scripts
3. **[03_FEATURES.md](03_FEATURES.md)** - Complete feature list and output format
4. **[04_TROUBLESHOOTING.md](04_TROUBLESHOOTING.md)** - Common issues and fixes
5. **[05_API_REFERENCE.md](05_API_REFERENCE.md)** - API structure and query IDs
6. **[06_LIVE_MONITORING.md](06_LIVE_MONITORING.md)** - Live monitoring vs historical fetching

---

## 🚀 Quick Start

1. Run `python setup_api_cookies.py` to configure
2. Run `python fetch_historical_tweets.py` to fetch tweets
3. Check `TWEETS/` folder for output files

---

## 📁 Project Structure

```
TWEETER DATA FETCHING/
├── config.json                    # Configuration (cookies, tokens, query IDs)
├── fetch_historical_tweets.py     # Main historical scraper
├── monitor_live_tweets.py         # Live tweet monitoring
├── setup_api_cookies.py           # Interactive configuration wizard
├── TWEETS/                        # Output folder (organized by account/date)
├── documentation/                 # All documentation files
└── api_references/                # API response examples
```

---

## 🔧 Configuration File

`config.json` contains:
- **api_cookies**: Twitter session cookies (auth_token, ct0, etc.)
- **api_auth**: Bearer token for API requests
- **api_config**: GraphQL query IDs (change when Twitter updates API)

---

## 📊 Output Format

Tweets are saved in daily files with Jalali calendar dates:
- Format: `YYYY-MM-DD.txt` (e.g., `1405-02-10.txt`)
- Location: `TWEETS/{username}/`
- Sorted: Newest tweets first
- Includes: Full text, media, URLs, hashtags, mentions, engagement stats

---

## 🆘 Need Help?

- **Setup issues**: See [01_SETUP_GUIDE.md](01_SETUP_GUIDE.md)
- **Errors**: See [04_TROUBLESHOOTING.md](04_TROUBLESHOOTING.md)
- **API changes**: See [05_API_REFERENCE.md](05_API_REFERENCE.md)

---

## 📝 Notes

- All markdown documentation is now in `documentation/` folder
- API response examples are in `api_references/` folder
- Configuration file is `config.json` in root directory
