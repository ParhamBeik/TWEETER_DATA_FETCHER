# API Reference

Technical details about Twitter's GraphQL API and how the scraper works.

---

## 🔌 Twitter GraphQL API

Twitter uses GraphQL endpoints for fetching user data. The scraper interacts with these endpoints.

---

## 📍 API Endpoints

### 1. UserByScreenName

**Purpose**: Convert username to user ID

**Endpoint**:
```
https://x.com/i/api/graphql/{QUERY_ID}/UserByScreenName
```

**Current Query ID**: `sLVLhk0bGj3MVFEKTdax1w`

**Parameters**:
```json
{
  "screen_name": "elonmusk",
  "withSafetyModeUserFields": true
}
```

**Response**: User object with ID, name, handle, profile info

---

### 2. UserTweets

**Purpose**: Fetch user's tweets and retweets

**Endpoint**:
```
https://x.com/i/api/graphql/{QUERY_ID}/UserTweets
```

**Current Query ID**: `naBcZ4al-iTCFBYGOAMzBQ`

**Parameters**:
```json
{
  "userId": "44196397",
  "count": 20,
  "includePromotedContent": true,
  "withQuickPromoteEligibilityTweetFields": true,
  "withVoice": true,
  "cursor": "..."
}
```

**Features**:
```json
{
  "rweb_video_screen_enabled": false,
  "rweb_cashtags_enabled": true,
  "responsive_web_graphql_timeline_navigation_enabled": true,
  "view_counts_everywhere_api_enabled": true,
  "longform_notetweets_consumption_enabled": true,
  ...
}
```

**Response**: Timeline with tweets, retweets, and cursor for pagination

---

### 3. UserTweetsAndReplies

**Purpose**: Fetch user's replies to other tweets

**Endpoint**:
```
https://x.com/i/api/graphql/{QUERY_ID}/UserTweetsAndReplies
```

**Current Query ID**: `naBcZ4al-iTCFBYGOAMzBQ`

**Parameters**: Same as UserTweets

**Status**: Currently returns 404 (endpoint may be deprecated or renamed)

---

## 🔑 Authentication

### Required Headers

```python
headers = {
    "authorization": "Bearer {BEARER_TOKEN}",
    "x-csrf-token": "{CT0_COOKIE}",
    "x-twitter-active-user": "yes",
    "x-twitter-auth-type": "OAuth2Session",
    "x-twitter-client-language": "en",
    "user-agent": "Mozilla/5.0 ...",
    "referer": "https://x.com/"
}
```

### Required Cookies

```python
cookies = {
    "auth_token": "...",
    "ct0": "...",
    "twid": "...",
    "guest_id": "...",
    ...
}
```

---

## 📦 Response Structure

### Tweet Object

```json
{
  "rest_id": "1234567890",
  "core": {
    "user_results": {
      "result": {
        "legacy": {
          "name": "Elon Musk",
          "screen_name": "elonmusk"
        }
      }
    }
  },
  "legacy": {
    "created_at": "Wed May 01 14:30:25 +0000 2026",
    "full_text": "Tweet text here",
    "entities": {
      "hashtags": [...],
      "user_mentions": [...],
      "urls": [...],
      "media": [...]
    },
    "reply_count": 1234,
    "retweet_count": 5678,
    "favorite_count": 12345,
    "in_reply_to_status_id_str": "...",
    "in_reply_to_screen_name": "username",
    "retweeted_status_result": {...},
    "quoted_status_result": {...}
  },
  "views": {
    "count": "1234567"
  }
}
```

### Retweet Structure

```json
{
  "legacy": {
    "retweeted_status_result": {
      "result": {
        "rest_id": "...",
        "core": {...},
        "legacy": {
          "full_text": "Original tweet text",
          ...
        }
      }
    }
  }
}
```

### Quote Tweet Structure

```json
{
  "legacy": {
    "quoted_status_result": {
      "result": {
        "rest_id": "...",
        "core": {...},
        "legacy": {
          "full_text": "Quoted tweet text",
          ...
        }
      }
    }
  }
}
```

### Deleted/Unavailable Tweets

```json
{
  "quoted_status_result": {
    "result": {
      "__typename": "TweetTombstone",
      "tombstone": {
        "text": {
          "text": "This Tweet is unavailable."
        }
      }
    }
  }
}
```

Or:

```json
{
  "quoted_status_result": {
    "result": {
      "__typename": "TweetUnavailable",
      "reason": "NmfApiError"
    }
  }
}
```

---

## 🔄 Pagination

Twitter uses cursor-based pagination:

**First request**: No cursor
```
?variables={"userId":"44196397","count":20}
```

**Subsequent requests**: Include cursor from previous response
```
?variables={"userId":"44196397","count":20,"cursor":"DAABCgABGc..."}
```

**Cursor location in response**:
```json
{
  "data": {
    "user": {
      "result": {
        "timeline_v2": {
          "timeline": {
            "instructions": [
              {
                "type": "TimelineAddEntries",
                "entries": [
                  {
                    "content": {
                      "cursorType": "Bottom",
                      "value": "DAABCgABGc..."
                    }
                  }
                ]
              }
            ]
          }
        }
      }
    }
  }
}
```

---

## ⏱️ Rate Limits

### Limits

| Endpoint | Limit | Window |
|----------|-------|--------|
| UserByScreenName | 150 requests | 15 minutes |
| UserTweets | 50 requests | 15 minutes |
| UserTweetsAndReplies | 50 requests | 15 minutes |

### Rate Limit Headers

```
x-rate-limit-limit: 50
x-rate-limit-remaining: 47
x-rate-limit-reset: 1777461206
```

**Reset time**: Unix timestamp (seconds since epoch)

---

## 🔍 Finding Query IDs

When Twitter updates their API, query IDs change. Here's how to find new ones:

### Method 1: Browser Network Tab

1. Open Twitter in browser
2. Open Developer Tools (F12)
3. Go to **Network** tab
4. Visit a profile: `https://x.com/elonmusk`
5. Filter by `graphql`
6. Look for requests like:
   - `graphql/sLVLhk0bGj3MVFEKTdax1w/UserByScreenName`
   - `graphql/naBcZ4al-iTCFBYGOAMzBQ/UserTweets`
7. Copy the query ID (the long string between `graphql/` and `/UserTweets`)

### Method 2: Inspect Request URL

Click on a GraphQL request and look at the **Request URL**:
```
https://x.com/i/api/graphql/naBcZ4al-iTCFBYGOAMzBQ/UserTweets?variables=...
                              ^^^^^^^^^^^^^^^^^^^^^^^^
                              This is the query ID
```

### Method 3: Check Twitter's JavaScript

Query IDs are embedded in Twitter's JavaScript files, but this is harder to find.

---

## 📂 API Response Examples

See `api_references/` folder for complete API response examples:

- **api_response_user_tweets.txt** - Regular tweets structure
- **api_response_retweet.txt** - Retweet structure
- **api_response_reply.txt** - Reply structure
- **tweet_details.txt** - Quote tweet example
- **tweet_details2.txt** - Additional examples

---

## 🛠️ How the Scraper Works

### 1. User ID Resolution
```python
username → UserByScreenName API → user_id
```

### 2. Timeline Fetching
```python
user_id → UserTweets API → tweets + cursor
cursor → UserTweets API → more tweets + cursor
...repeat until no more tweets or date limit reached
```

### 3. Tweet Parsing
```python
raw_tweet → extract fields → format output → save to file
```

### 4. Data Extraction

**Basic fields**:
- `rest_id` → Tweet ID
- `legacy.full_text` → Tweet text
- `legacy.created_at` → Timestamp
- `core.user_results.result.legacy` → Author info

**Entities**:
- `legacy.entities.hashtags` → Hashtags
- `legacy.entities.user_mentions` → Mentions
- `legacy.entities.urls` → URLs
- `legacy.entities.media` → Media

**Engagement**:
- `legacy.reply_count` → Replies
- `legacy.retweet_count` → Retweets
- `legacy.favorite_count` → Likes
- `views.count` → Views

**Special types**:
- `legacy.retweeted_status_result` → Retweet data
- `legacy.quoted_status_result` → Quote tweet data
- `legacy.in_reply_to_status_id_str` → Reply parent

---

## 🔐 Security Notes

- **Bearer token**: Public token, same for all users
- **Cookies**: User-specific, must be kept secret
- **CSRF token (ct0)**: Must match cookie value
- **User agent**: Should match a real browser

---

## 🔄 API Changes

Twitter frequently updates their API. Common changes:

1. **Query IDs change** → Update in `config.json`
2. **Endpoints renamed** → Update endpoint URLs in script
3. **Response structure changes** → Update parsing logic
4. **New required parameters** → Add to request variables
5. **New required features** → Add to features object

**When this happens**:
- Script will get 404 errors
- Check browser Network tab for new query IDs
- Update `config.json` with new IDs
- Test with a single account first

---

## 📊 Current Working Configuration

**As of May 2026**:

```json
{
  "api_config": {
    "user_by_screen_name_query_id": "sLVLhk0bGj3MVFEKTdax1w",
    "user_tweets_query_id": "naBcZ4al-iTCFBYGOAMzBQ",
    "user_tweets_and_replies_query_id": "naBcZ4al-iTCFBYGOAMzBQ"
  }
}
```

**Bearer Token**:
```
AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA
```

---

## ➡️ Related Documentation

- [01_SETUP_GUIDE.md](01_SETUP_GUIDE.md) - How to get cookies and tokens
- [04_TROUBLESHOOTING.md](04_TROUBLESHOOTING.md) - Fix 404 and 401 errors
- `api_references/` - Real API response examples
