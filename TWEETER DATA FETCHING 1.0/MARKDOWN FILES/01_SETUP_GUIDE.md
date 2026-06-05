# Setup Guide

Complete setup instructions for the Twitter Historical Tweet Scraper.

---

## 📋 Prerequisites

- Python 3.7+
- Twitter/X account
- Web browser (Chrome, Firefox, Safari, etc.)

---

## 🔧 Installation

1. **Install required packages:**
```bash
pip install requests jdatetime
```

2. **Run the configuration wizard:**
```bash
python setup_api_cookies.py
```

---

## 🍪 Getting Twitter Cookies

The scraper needs your Twitter session cookies to authenticate API requests.

### Step-by-Step:

1. **Open Twitter in your browser**
   - Go to https://x.com
   - Log in to your account

2. **Open Developer Tools**
   - Chrome/Edge: Press `F12` or `Ctrl+Shift+I` (Windows) / `Cmd+Option+I` (Mac)
   - Firefox: Press `F12` or `Ctrl+Shift+I` (Windows) / `Cmd+Option+I` (Mac)
   - Safari: Enable Developer menu first (Preferences → Advanced → Show Develop menu), then press `Cmd+Option+I`

3. **Navigate to Cookies**
   - **Chrome/Edge**: Click `Application` tab → `Cookies` → `https://x.com`
   - **Firefox**: Click `Storage` tab → `Cookies` → `https://x.com`
   - **Safari**: Click `Storage` tab → `Cookies` → `x.com`

4. **Copy Required Cookies**
   
   You need these cookies (copy the `Value` column):
   
   | Cookie Name | Required | Description |
   |-------------|----------|-------------|
   | `auth_token` | ✅ Yes | Main authentication token |
   | `ct0` | ✅ Yes | CSRF token |
   | `twid` | ✅ Yes | Twitter user ID |
   | `guest_id` | ⚠️ Recommended | Guest session ID |
   | `kdt` | ⚠️ Recommended | Additional auth token |
   | Others | ℹ️ Optional | Copy all visible cookies for best results |

5. **Paste into Configuration Wizard**
   - Run `python setup_api_cookies.py`
   - Choose option `1` (Full setup)
   - Paste each cookie value when prompted

---

## 🔑 Bearer Token

The bearer token is Twitter's API authentication key.

**Default token** (usually works):
```
AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA
```

**If you need to find it manually:**

1. Open Developer Tools → `Network` tab
2. Refresh Twitter page
3. Filter by `graphql` or `api`
4. Click any request
5. Look in `Request Headers` for `authorization: Bearer ...`
6. Copy the token after `Bearer `

---

## 🔍 Query IDs

Query IDs are Twitter's GraphQL endpoint identifiers. They change occasionally when Twitter updates their API.

**Current working IDs** (as of May 2026):
```json
{
  "user_by_screen_name_query_id": "sLVLhk0bGj3MVFEKTdax1w",
  "user_tweets_query_id": "naBcZ4al-iTCFBYGOAMzBQ",
  "user_tweets_and_replies_query_id": "naBcZ4al-iTCFBYGOAMzBQ"
}
```

**How to find new Query IDs** (if you get 404 errors):

1. Open Developer Tools → `Network` tab
2. Go to a Twitter profile (e.g., https://x.com/elonmusk)
3. Look for requests to `graphql/.../UserTweets` or `UserByScreenName`
4. The query ID is in the URL path: `graphql/{QUERY_ID}/UserTweets`
5. Update `config.json` with the new ID

---

## ✅ Verify Configuration

After setup, your `config.json` should look like:

```json
{
  "api_cookies": {
    "auth_token": "your_auth_token_here",
    "ct0": "your_ct0_token_here",
    "twid": "u%3D1234567890",
    "guest_id": "v1%3A...",
    "kdt": "...",
    ...
  },
  "api_auth": {
    "bearer_token": "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
  },
  "api_config": {
    "user_by_screen_name_query_id": "sLVLhk0bGj3MVFEKTdax1w",
    "user_tweets_query_id": "naBcZ4al-iTCFBYGOAMzBQ",
    "user_tweets_and_replies_query_id": "naBcZ4al-iTCFBYGOAMzBQ"
  }
}
```

---

## 🔄 Updating Configuration

**Quick cookie update** (when cookies expire):
```bash
python setup_api_cookies.py
# Choose option 2: Quick cookie update
```

**Full reconfiguration**:
```bash
python setup_api_cookies.py
# Choose option 1: Full setup
```

**Manual editing**:
```bash
# Edit config.json directly
nano config.json
# or
code config.json
```

---

## 🔒 Security Notes

- ⚠️ **Never share your `config.json` file** - it contains your authentication credentials
- ⚠️ **Add `config.json` to `.gitignore`** if using version control
- ⚠️ Cookies expire after 30-90 days - you'll need to update them periodically
- ⚠️ If you log out of Twitter, cookies become invalid immediately

---

## ➡️ Next Steps

Once configured, proceed to [02_USAGE_GUIDE.md](02_USAGE_GUIDE.md) to learn how to run the scraper.
