# Troubleshooting Guide

Common issues and solutions for the Twitter scraper.

---

## 🔴 Common Errors

### 1. 401 Unauthorized

**Error message:**
```
✗ API request failed: 401 Client Error: Unauthorized
```

**Cause**: Invalid or expired cookies

**Solution:**
1. Update your cookies:
   ```bash
   python setup_api_cookies.py
   ```
2. Choose option 2 (Quick cookie update)
3. Get fresh cookies from your browser (see [01_SETUP_GUIDE.md](01_SETUP_GUIDE.md))
4. Run the scraper again

**Why this happens:**
- You logged out of Twitter
- Cookies expired (typically after 30-90 days)
- Twitter detected suspicious activity and invalidated session

---

### 2. 404 Not Found

**Error message:**
```
✗ API request failed: 404 Client Error: Not Found for url: https://x.com/i/api/graphql/.../UserTweets
```

**Cause**: Outdated query IDs (Twitter changed their API endpoints)

**Solution:**
1. Find new query IDs from browser:
   - Open Developer Tools → Network tab
   - Visit a Twitter profile
   - Look for `graphql/.../UserTweets` requests
   - Copy the query ID from the URL
2. Update `config.json`:
   ```json
   {
     "api_config": {
       "user_tweets_query_id": "NEW_QUERY_ID_HERE"
     }
   }
   ```
3. Or use the wizard:
   ```bash
   python setup_api_cookies.py
   ```

**Current working IDs** (May 2026):
- UserByScreenName: `sLVLhk0bGj3MVFEKTdax1w`
- UserTweets: `naBcZ4al-iTCFBYGOAMzBQ`
- UserTweetsAndReplies: `naBcZ4al-iTCFBYGOAMzBQ`

---

### 3. 429 Rate Limit Exceeded

**Error message:**
```
⚠️ Rate limit hit. Waiting 15 minutes...
```

**Cause**: Too many API requests in a short time

**Solution:**
- **Wait it out** - Script automatically waits and retries
- **Don't run multiple instances** - Only run one scraper at a time
- **Reduce accounts** - Monitor fewer accounts simultaneously

**Rate limits:**
- 150 requests per 15 minutes (user lookups)
- 50 requests per 15 minutes (timeline fetches)

**Not an error** - This is normal behavior. The script will resume automatically.

---

### 4. Connection Timeout

**Error message:**
```
✗ API request failed: HTTPSConnectionPool(host='x.com', port=443): Read timed out
```

**Cause**: Network issues or Twitter server problems

**Solution:**
- **Wait and retry** - Usually temporary
- **Check internet connection**
- **Check Twitter status**: https://status.x.com
- Script will continue with next account/page

**Not critical** - Partial data is still saved.

---

### 5. Parse Errors

**Error message:**
```
⚠️ Error parsing quoted tweet: 'result'
⚠️ Error parsing retweeted_status_result: 'result'
```

**Cause**: Deleted or unavailable tweets in the timeline

**Solution:**
- **No action needed** - These are warnings, not errors
- Script automatically skips problematic tweets
- Other tweets are still processed normally

**Why this happens:**
- Original tweet was deleted
- Tweet is from suspended account
- Tweet is age-restricted or private

---

### 6. Config File Not Found

**Error message:**
```
FileNotFoundError: [Errno 2] No such file or directory: 'config.json'
```

**Cause**: Configuration file doesn't exist

**Solution:**
```bash
python setup_api_cookies.py
```
Choose option 1 (Full setup) to create the config file.

---

### 7. Invalid JSON in Config

**Error message:**
```
json.decoder.JSONDecodeError: Expecting property name enclosed in double quotes
```

**Cause**: Malformed `config.json` file

**Solution:**
1. **Backup current config** (if it has valid data):
   ```bash
   cp config.json config.json.backup
   ```
2. **Recreate config**:
   ```bash
   python setup_api_cookies.py
   ```
3. Or **fix manually**:
   - Open `config.json` in a text editor
   - Validate JSON syntax at https://jsonlint.com
   - Common issues: missing commas, trailing commas, unescaped quotes

---

### 8. No Tweets Found

**Error message:**
```
✓ Total unique items: 0
```

**Cause**: Account has no tweets in the last 2 weeks, or account is private

**Solution:**
- **Check account exists**: Visit https://x.com/{username}
- **Check account is public**: Private accounts can't be scraped
- **Check date range**: Script only fetches last 2 weeks
- **Try different account**: Test with a known active account

---

### 9. AttributeError: '_parse_quoted_tweet'

**Error message:**
```
AttributeError: 'TwitterUnifiedFetcher' object has no attribute '_parse_quoted_tweet'
```

**Cause**: Outdated script version

**Solution:**
- **This should be fixed** in the current version
- If you still see this, the method is missing from the class
- Check that `_parse_quoted_tweet()` is defined inside the `TwitterUnifiedFetcher` class (around line 539)

---

## 🔍 Debugging Tips

### Enable Debug Mode

Debug mode is already enabled by default. It shows:
- Cookies loaded
- Bearer token
- CSRF token
- Request URLs
- Response status codes
- Response headers

### Check Config File

```bash
cat config.json
```

Verify:
- ✅ All required cookies present
- ✅ Bearer token is set
- ✅ Query IDs are set
- ✅ Valid JSON syntax

### Test with Known Account

Try fetching from a known active account:
```python
accounts = ["elonmusk"]  # Very active account
```

### Check Network Tab

1. Open Twitter in browser
2. Open Developer Tools → Network tab
3. Refresh page
4. Look for failed requests (red)
5. Compare with script's requests

---

## 🛠️ Manual Fixes

### Reset Configuration

```bash
# Backup old config
mv config.json config.json.old

# Create fresh config
python setup_api_cookies.py
```

### Clear Cache

```bash
# Remove Python cache
rm -rf __pycache__

# Remove .pyc files
find . -name "*.pyc" -delete
```

### Reinstall Dependencies

```bash
pip install --upgrade requests jdatetime
```

---

## 📊 Understanding Warnings vs Errors

### ⚠️ Warnings (Safe to Ignore)
- `Parse error: 'result'` - Deleted/unavailable tweets
- `Rate limit hit` - Automatic retry
- `Reached 2-week limit` - Normal completion

### ❌ Errors (Need Action)
- `401 Unauthorized` - Update cookies
- `404 Not Found` - Update query IDs
- `FileNotFoundError` - Run setup wizard
- `JSONDecodeError` - Fix config.json

---

## 🆘 Still Having Issues?

### Check These Files

1. **config.json** - Configuration
2. **api_references/** - API response examples
3. **documentation/05_API_REFERENCE.md** - Technical details

### Collect Debug Info

When reporting issues, include:
- Error message (full traceback)
- Script output (with debug info)
- Config structure (without sensitive values)
- Twitter account being scraped
- Python version: `python --version`

### Common Causes

- 🔴 **90% of issues**: Expired cookies → Update cookies
- 🟡 **8% of issues**: Outdated query IDs → Update query IDs
- 🟢 **2% of issues**: Network/Twitter problems → Wait and retry

---

## ➡️ Next Steps

- See [05_API_REFERENCE.md](05_API_REFERENCE.md) for API details
- See [01_SETUP_GUIDE.md](01_SETUP_GUIDE.md) to reconfigure
