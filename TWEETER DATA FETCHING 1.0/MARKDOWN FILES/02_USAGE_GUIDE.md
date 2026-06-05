# Usage Guide

How to run the Twitter scraper scripts.

---

## 🚀 Quick Start

```bash
# Fetch historical tweets (last 2 weeks)
python fetch_historical_tweets.py

# Monitor live tweets (real-time)
python monitor_live_tweets.py
```

---

## 📥 Historical Tweet Fetcher

**Script**: `fetch_historical_tweets.py`

**What it does**:
- Fetches tweets from the last 2 weeks
- Includes tweets, retweets, replies, and quote tweets
- Saves to daily files organized by Jalali calendar dates
- Automatically handles pagination and rate limits

### Configuration

Edit the script to change target accounts (line ~1050):

```python
accounts = [
    "elonmusk",
    "whale_alert",
    "paulg"
]
```

### Running

```bash
python fetch_historical_tweets.py
```

### Output

Files are saved to `TWEETS/{username}/{date}.txt`:
```
TWEETS/
├── elonmusk/
│   ├── 1405-02-11.txt
│   ├── 1405-02-10.txt
│   └── 1405-02-09.txt
├── whale_alert/
│   └── 1405-02-11.txt
└── paulg/
    └── 1405-02-11.txt
```

### Options

**Debug mode** (shows detailed API requests):
- Already enabled by default in the script
- Shows cookies, headers, response status

**Change date range**:
- Currently hardcoded to 2 weeks
- Modify `cutoff_date` calculation in the script (line ~850)

---

## 📡 Live Tweet Monitor

**Script**: `monitor_live_tweets.py`

**What it does**:
- Monitors accounts in real-time
- Checks for new tweets every 60 seconds
- Appends new tweets to daily files
- Prevents duplicates

### Configuration

Edit the script to change target accounts (line ~950):

```python
accounts = [
    "elonmusk",
    "whale_alert",
    "paulg"
]
```

### Running

```bash
python monitor_live_tweets.py
```

Press `Ctrl+C` to stop monitoring.

### Output

Same structure as historical fetcher:
```
TWEETS/{username}/{date}.txt
```

New tweets are appended to existing files (newest first).

---

## 🔧 Configuration Wizard

**Script**: `setup_api_cookies.py`

**What it does**:
- Interactive setup for `config.json`
- Updates cookies, bearer token, and query IDs
- Two modes: full setup or quick cookie update

### Running

```bash
python setup_api_cookies.py
```

### Options

1. **Full setup** - Configure everything from scratch
2. **Quick cookie update** - Only update cookies (when they expire)
3. **Exit** - Quit without changes

---

## 📊 Understanding Output Files

Each daily file contains tweets in this format:

```
════════════════════════════════════════════════════════════════════════════════
💬 TWEET
════════════════════════════════════════════════════════════════════════════════
🆔 ID: 1234567890
👤 Author: Elon Musk (@elonmusk)
📅 Date: 1405-02-11 14:30:25 (2026-05-01 14:30:25 UTC)
📝 Text: This is a sample tweet with #hashtags and @mentions

🏷️ Hashtags: #AI #Technology
👥 Mentions: @OpenAI (OpenAI) | @sama (Sam Altman)
🔗 Links:
   → https://example.com/article

📊 Engagement:
   💬 Replies: 1,234
   🔁 Retweets: 5,678
   ❤️ Likes: 12,345
   👁️ Views: 1,234,567

🔗 Tweet: https://x.com/elonmusk/status/1234567890
```

### Tweet Types

- **💬 TWEET** - Original tweet
- **🔁 RETWEET** - Retweeted content (shows original author and full text)
- **↩️ REPLY** - Reply to another tweet (shows parent tweet link)
- **📎 QUOTE TWEET** - Quote tweet (shows both original and quoted content)

---

## ⏱️ Rate Limits

Twitter API has rate limits:
- **150 requests per 15 minutes** for user lookups
- **50 requests per 15 minutes** for timeline fetches

The scripts automatically:
- Wait when rate limits are hit
- Show countdown timers
- Resume when limits reset

**Tip**: Don't run multiple instances simultaneously to avoid hitting limits faster.

---

## 🔄 Updating Tweets

**Historical fetcher**:
- Overwrites existing files
- Fetches last 2 weeks every time
- Use for initial data collection or full refresh

**Live monitor**:
- Appends new tweets only
- Checks for duplicates
- Use for continuous monitoring

---

## 🛑 Stopping Scripts

**Historical fetcher**:
- Runs until complete (all accounts processed)
- Press `Ctrl+C` to interrupt (progress is saved)

**Live monitor**:
- Runs indefinitely
- Press `Ctrl+C` to stop gracefully

---

## 📝 Common Workflows

### Initial Setup
```bash
# 1. Configure
python setup_api_cookies.py

# 2. Fetch historical data
python fetch_historical_tweets.py

# 3. Start live monitoring
python monitor_live_tweets.py
```

### Daily Maintenance
```bash
# Just keep live monitor running
python monitor_live_tweets.py
```

### Cookie Expired
```bash
# 1. Update cookies
python setup_api_cookies.py  # Choose option 2

# 2. Resume monitoring
python monitor_live_tweets.py
```

---

## ➡️ Next Steps

- See [03_FEATURES.md](03_FEATURES.md) for complete feature list
- See [04_TROUBLESHOOTING.md](04_TROUBLESHOOTING.md) if you encounter errors
