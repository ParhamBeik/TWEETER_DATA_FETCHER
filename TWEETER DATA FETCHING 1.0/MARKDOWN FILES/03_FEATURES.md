# Features

Complete list of features and data extracted by the Twitter scraper.

---

## ✨ Core Features

### 1. Tweet Types Supported
- ✅ **Original Tweets** - User's own tweets
- ✅ **Retweets** - Full original content with author info
- ✅ **Replies** - Replies to other tweets with parent context
- ✅ **Quote Tweets** - Tweets with quoted content (both shown)

### 2. Data Extraction
- ✅ **Full Text** - Complete tweet text (no truncation)
- ✅ **Expanded URLs** - Real URLs instead of t.co links
- ✅ **Media Links** - Photos and videos with direct URLs
- ✅ **Hashtags** - All #tags in the tweet
- ✅ **User Mentions** - @mentions with full names
- ✅ **Engagement Stats** - Replies, retweets, likes, views
- ✅ **Timestamps** - Both Jalali and UTC formats

### 3. Organization
- ✅ **Daily Files** - One file per day per account
- ✅ **Jalali Calendar** - Persian calendar dates (Tehran timezone)
- ✅ **Newest First** - Most recent tweets at the top
- ✅ **Duplicate Prevention** - No repeated tweets

### 4. Error Handling
- ✅ **Deleted Tweets** - Gracefully skipped with warning
- ✅ **Unavailable Content** - Handled without crashing
- ✅ **Rate Limits** - Automatic waiting and retry
- ✅ **Network Errors** - Timeout handling and recovery

---

## 📊 Extracted Data Fields

### Basic Information
| Field | Description | Example |
|-------|-------------|---------|
| Tweet ID | Unique identifier | `1234567890` |
| Author Name | Display name | `Elon Musk` |
| Author Handle | Username | `@elonmusk` |
| Tweet Type | Type indicator | `💬 TWEET`, `🔁 RETWEET`, `↩️ REPLY`, `📎 QUOTE` |
| Timestamp | Jalali + UTC | `1405-02-11 14:30:25 (2026-05-01 14:30:25 UTC)` |

### Content
| Field | Description | Example |
|-------|-------------|---------|
| Full Text | Complete tweet text | `This is a sample tweet...` |
| Hashtags | All #tags | `#AI #Technology #Innovation` |
| Mentions | @users with names | `@OpenAI (OpenAI) \| @sama (Sam Altman)` |
| URLs | Expanded links | `https://example.com/article` |
| Media | Photos/videos | `📷 Photo: https://pbs.twimg.com/media/...` |

### Engagement
| Field | Description | Example |
|-------|-------------|---------|
| Replies | Reply count | `💬 Replies: 1,234` |
| Retweets | Retweet count | `🔁 Retweets: 5,678` |
| Likes | Like count | `❤️ Likes: 12,345` |
| Views | View count | `👁️ Views: 1,234,567` |

### Retweet-Specific
| Field | Description | Example |
|-------|-------------|---------|
| Original Author | Who posted originally | `👤 Original: John Doe (@johndoe)` |
| Original Text | Full original tweet | Complete untruncated text |
| Original Timestamp | When originally posted | `📅 Original: 1405-02-10 12:00:00` |
| Original Engagement | Original stats | Replies, retweets, likes, views |

### Reply-Specific
| Field | Description | Example |
|-------|-------------|---------|
| Replying To | Parent tweet author | `↩️ Replying to: @username` |
| Parent Link | Link to parent tweet | `🔗 Parent: https://x.com/...` |

### Quote Tweet-Specific
| Field | Description | Example |
|-------|-------------|---------|
| Quoted Author | Original tweet author | `📎 Quoting: @username (Name)` |
| Quoted Text | Original tweet text | Full quoted content |
| Quoted Media | Media in quoted tweet | Photos/videos from quote |
| Quoted Engagement | Original tweet stats | Engagement of quoted tweet |

---

## 🎨 Output Format

### Tweet Structure

```
════════════════════════════════════════════════════════════════════════════════
💬 TWEET
════════════════════════════════════════════════════════════════════════════════
🆔 ID: 1234567890
👤 Author: Elon Musk (@elonmusk)
📅 Date: 1405-02-11 14:30:25 (2026-05-01 14:30:25 UTC)
📝 Text: Sample tweet text here

🏷️ Hashtags: #AI #Technology
👥 Mentions: @OpenAI (OpenAI) | @sama (Sam Altman)
🔗 Links:
   → https://example.com/article
📷 Media:
   → Photo: https://pbs.twimg.com/media/...

📊 Engagement:
   💬 Replies: 1,234
   🔁 Retweets: 5,678
   ❤️ Likes: 12,345
   👁️ Views: 1,234,567

🔗 Tweet: https://x.com/elonmusk/status/1234567890
```

### Retweet Structure

```
════════════════════════════════════════════════════════════════════════════════
🔁 RETWEET
════════════════════════════════════════════════════════════════════════════════
🆔 ID: 1234567890
👤 Retweeted by: Elon Musk (@elonmusk)
📅 Retweeted: 1405-02-11 14:30:25 (2026-05-01 14:30:25 UTC)

┌─ Original Tweet ─────────────────────────────────────────────────────────────┐
│ 👤 Original: John Doe (@johndoe)
│ 📅 Original: 1405-02-10 12:00:00 (2026-04-30 12:00:00 UTC)
│ 📝 Text: This is the original tweet that was retweeted
│
│ 🏷️ Hashtags: #Example
│ 📊 Engagement:
│    💬 Replies: 100
│    🔁 Retweets: 500
│    ❤️ Likes: 1,000
│    👁️ Views: 50,000
└──────────────────────────────────────────────────────────────────────────────┘

🔗 Tweet: https://x.com/elonmusk/status/1234567890
```

### Reply Structure

```
════════════════════════════════════════════════════════════════════════════════
↩️ REPLY
════════════════════════════════════════════════════════════════════════════════
🆔 ID: 1234567890
👤 Author: Elon Musk (@elonmusk)
📅 Date: 1405-02-11 14:30:25 (2026-05-01 14:30:25 UTC)
↩️ Replying to: @johndoe
🔗 Parent: https://x.com/johndoe/status/9876543210

📝 Text: This is a reply to another tweet

📊 Engagement:
   💬 Replies: 50
   🔁 Retweets: 100
   ❤️ Likes: 500
   👁️ Views: 10,000

🔗 Tweet: https://x.com/elonmusk/status/1234567890
```

### Quote Tweet Structure

```
════════════════════════════════════════════════════════════════════════════════
📎 QUOTE TWEET
════════════════════════════════════════════════════════════════════════════════
🆔 ID: 1234567890
👤 Author: Elon Musk (@elonmusk)
📅 Date: 1405-02-11 14:30:25 (2026-05-01 14:30:25 UTC)
📝 Text: Adding my thoughts to this tweet

┌─ Quoted Tweet ───────────────────────────────────────────────────────────────┐
│ 📎 Quoting: @johndoe (John Doe)
│ 📅 Original: 1405-02-10 12:00:00 (2026-04-30 12:00:00 UTC)
│ 📝 Text: This is the original tweet being quoted
│
│ 🏷️ Hashtags: #Example
│ 📊 Engagement:
│    💬 Replies: 100
│    🔁 Retweets: 500
│    ❤️ Likes: 1,000
│    👁️ Views: 50,000
└──────────────────────────────────────────────────────────────────────────────┘

📊 Engagement:
   💬 Replies: 200
   🔁 Retweets: 1,000
   ❤️ Likes: 5,000
   👁️ Views: 100,000

🔗 Tweet: https://x.com/elonmusk/status/1234567890
```

---

## 🔍 URL Expansion

**Before** (shortened):
```
https://t.co/abc123xyz
```

**After** (expanded):
```
https://www.nytimes.com/2026/04/30/technology/ai-breakthrough.html
```

All t.co links are automatically expanded to show the real destination.

---

## 📅 Date Format

**Jalali Calendar** (Persian):
- Format: `YYYY-MM-DD HH:MM:SS`
- Timezone: Asia/Tehran (IRST/IRDT)
- Example: `1405-02-11 14:30:25`

**Gregorian Calendar** (UTC):
- Format: `YYYY-MM-DD HH:MM:SS UTC`
- Timezone: UTC
- Example: `2026-05-01 14:30:25 UTC`

Both formats are shown for every timestamp.

---

## 🎯 What's NOT Included

- ❌ Tweet analytics (impressions, profile visits)
- ❌ Follower/following lists
- ❌ Direct messages
- ❌ Bookmarks
- ❌ Lists
- ❌ Spaces (audio rooms)
- ❌ Fleets (stories)
- ❌ Twitter Blue/Premium features

This scraper focuses on **public timeline content only**.

---

## ➡️ Next Steps

- See [04_TROUBLESHOOTING.md](04_TROUBLESHOOTING.md) for error handling
- See [05_API_REFERENCE.md](05_API_REFERENCE.md) for technical details
