# Live Monitoring vs Historical Fetching

Comparison between `monitor_live_tweets.py` and `fetch_historical_tweets.py`.

---

## 🔄 How Live Monitoring Works

### Mechanism

The live monitor **does NOT** use real-time WebSocket connections or streaming APIs. Instead, it uses **polling**:

1. **Fetches latest 20 tweets** from each account
2. **Compares with seen IDs** (cached in memory)
3. **Saves only new tweets** to daily files
4. **Waits 25-35 seconds** (randomized)
5. **Repeats** the cycle

### No Page Refresh Needed

The script doesn't need to "refresh a page" because:
- It directly calls Twitter's GraphQL API (same as historical fetcher)
- Each API call returns the latest 20 tweets
- No browser or page rendering involved
- Pure HTTP requests every 30 seconds

---

## 📊 Comparison Table

| Feature | `fetch_historical_tweets.py` | `monitor_live_tweets.py` |
|---------|------------------------------|--------------------------|
| **Purpose** | Fetch last 2 weeks of tweets | Monitor for new tweets continuously |
| **Execution** | Runs once, then exits | Runs indefinitely (until stopped) |
| **API Calls** | Paginated (fetches all pages) | Only first page (latest 20 tweets) |
| **Duplicate Handling** | Overwrites existing files | Checks `self.seen` cache to skip duplicates |
| **Time Range** | Last 2 weeks | Real-time (new tweets only) |
| **Output** | Overwrites daily files | Appends to daily files |
| **Threading** | Single-threaded | Multi-threaded (one thread per account) |
| **Check Interval** | N/A (runs once) | 25-35 seconds (randomized) |
| **Rate Limits** | Hits limits during bulk fetch | Rarely hits limits (slow polling) |

---

## 🔍 Detailed Differences

### 1. Duplicate Detection

**Historical Fetcher:**
```python
# No duplicate checking - overwrites files
# Fetches everything in date range
```

**Live Monitor:**
```python
# Loads existing tweet IDs on startup
self.seen = {"elonmusk": {"1234567890", "1234567891", ...}}

# Checks before saving
if tweet_id not in self.seen[username]:
    save_tweet()
    self.seen[username].add(tweet_id)
```

### 2. API Fetching Strategy

**Historical Fetcher:**
```python
# Fetches ALL pages until date limit
while has_more_tweets and not reached_date_limit:
    fetch_page()
    cursor = get_next_cursor()
    # Continues until 2 weeks ago
```

**Live Monitor:**
```python
# Only fetches FIRST page (latest 20 tweets)
def get_latest_tweets(user_id):
    # No cursor, no pagination
    # Just the most recent 20 tweets
    return api_call(count=20)
```

### 3. Execution Model

**Historical Fetcher:**
```python
# Sequential processing
for account in accounts:
    fetch_all_tweets(account)  # Blocks until complete
    
# Then exits
```

**Live Monitor:**
```python
# Parallel processing with threads
threads = []
for account in accounts:
    thread = Thread(target=monitor_account, args=(account,))
    thread.start()
    threads.append(thread)

# Runs forever (until Ctrl+C)
for thread in threads:
    thread.join()
```

### 4. Sleep/Wait Behavior

**Historical Fetcher:**
```python
# Only waits when rate limited
if rate_limit_hit:
    wait_15_minutes()
```

**Live Monitor:**
```python
# Always waits between checks
while True:
    fetch_latest_tweets()
    time.sleep(random.uniform(25, 35))  # Randomized delay
```

---

## 🎯 When to Use Each

### Use Historical Fetcher When:
- ✅ Initial data collection (first time setup)
- ✅ Backfilling missing data
- ✅ Full refresh needed
- ✅ Analyzing past tweets
- ✅ One-time data export

### Use Live Monitor When:
- ✅ Continuous monitoring
- ✅ Real-time tweet collection
- ✅ Daily updates
- ✅ Long-running data collection
- ✅ Keeping data current

---

## 🔧 Configuration Differences

### Historical Fetcher
```python
# Hardcoded in script (line ~1050)
accounts = ["elonmusk", "whale_alert", "paulg"]

# No check interval (runs once)
```

### Live Monitor
```python
# Hardcoded in script (line ~950)
accounts = ["elonmusk", "whale_alert", "paulg"]

# Configurable check interval (line ~30)
CHECK_INTERVAL_SECONDS = 30
MIN_CHECK_DELAY = 25
MAX_CHECK_DELAY = 35
```

---

## 🧠 Memory Management

### Historical Fetcher
```python
# Processes all tweets in memory
# Then writes to file
# Memory usage: High during processing
```

### Live Monitor
```python
# Maintains seen IDs cache in memory
self.seen = {
    "elonmusk": {tweet_ids...},  # Can grow large over time
    "whale_alert": {tweet_ids...},
    ...
}
# Memory usage: Grows over time (but slowly)
```

**Note**: Live monitor loads existing tweet IDs on startup by scanning all output files. This can take time if you have many tweets.

---

## 🔄 How Live Monitor Detects New Tweets

### Step-by-Step Process

1. **Startup**:
   ```python
   # Scan all existing files
   for file in TWEETS/elonmusk/*.txt:
       extract_tweet_ids()
       add_to_seen_cache()
   
   # Now knows: "I've already saved tweets 123, 456, 789..."
   ```

2. **First Check** (at T=0 seconds):
   ```python
   # Fetch latest 20 tweets from API
   tweets = get_latest_tweets("elonmusk")
   
   # Check each tweet
   for tweet in tweets:
       if tweet.id not in self.seen["elonmusk"]:
           save_tweet()  # NEW TWEET!
           self.seen["elonmusk"].add(tweet.id)
   ```

3. **Wait**:
   ```python
   time.sleep(30)  # Wait 30 seconds
   ```

4. **Second Check** (at T=30 seconds):
   ```python
   # Fetch latest 20 tweets again
   tweets = get_latest_tweets("elonmusk")
   
   # Most will be duplicates (already in self.seen)
   # But if Elon tweeted in last 30 seconds:
   for tweet in tweets:
       if tweet.id not in self.seen["elonmusk"]:
           save_tweet()  # NEW TWEET!
           self.seen["elonmusk"].add(tweet.id)
   ```

5. **Repeat Forever**

### Why This Works

- Twitter's API returns tweets in **reverse chronological order** (newest first)
- Fetching the latest 20 tweets captures any new activity
- The `self.seen` cache prevents re-saving old tweets
- 30-second interval is fast enough to catch most tweets

### Limitations

- **Misses tweets if >20 tweets posted in 30 seconds** (rare for most accounts)
- **Not true real-time** (30-second delay)
- **No push notifications** (must poll API)

---

## 🔐 Authentication & API Usage

### Both Scripts Use:
- ✅ Same `config.json` file
- ✅ Same cookies and bearer token
- ✅ Same GraphQL endpoints
- ✅ Same query IDs
- ✅ Same parsing logic (mostly)

### Differences:
- **Historical**: Uses pagination cursors
- **Live**: No cursors (only first page)

---

## 🚀 Running Both Together

**Can you run both at the same time?**

⚠️ **Not recommended** - They share rate limits:
- Historical fetcher will consume rate limits quickly
- Live monitor will start failing with 429 errors

**Best practice:**
1. Run historical fetcher first (one-time)
2. Wait for it to complete
3. Then start live monitor (continuous)

---

## 📝 Code Similarity

### Shared Components (95% similar)
- ✅ Configuration loading
- ✅ Session setup
- ✅ Cookie handling
- ✅ User ID resolution
- ✅ Tweet parsing logic
- ✅ Entity extraction (URLs, hashtags, mentions)
- ✅ Engagement stats
- ✅ Jalali date conversion
- ✅ File output format

### Unique to Historical Fetcher
- ❌ Pagination with cursors
- ❌ Date range limiting (2 weeks)
- ❌ Progress tracking (page numbers)

### Unique to Live Monitor
- ❌ Threading (parallel monitoring)
- ❌ Duplicate detection (`self.seen` cache)
- ❌ Continuous loop with sleep
- ❌ Existing ID loading on startup
- ❌ Error counting per account

---

## 🎯 Summary

**Question 1: Does monitor follow same guidelines?**
✅ **YES** - 95% of the code is identical:
- Same API endpoints
- Same authentication
- Same parsing logic
- Same output format
- Same configuration file

**Question 2: How does it detect new tweets without refresh?**
✅ **Polling mechanism**:
- Calls API every 30 seconds
- Fetches latest 20 tweets
- Compares with cached IDs
- Saves only new ones
- No browser/page refresh needed (direct API calls)

---

## ➡️ Related Documentation

- [02_USAGE_GUIDE.md](02_USAGE_GUIDE.md) - How to run both scripts
- [05_API_REFERENCE.md](05_API_REFERENCE.md) - API details
- [04_TROUBLESHOOTING.md](04_TROUBLESHOOTING.md) - Common issues
