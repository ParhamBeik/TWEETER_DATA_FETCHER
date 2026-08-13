function Media({ items = [] }) {
  if (!items.length) return null;
  return (
    <div className={`tweet-media media-${Math.min(items.length, 4)}`}>
      {items.slice(0, 4).map((item, index) => {
        const variants = (item.variants || [])
          .filter((variant) => variant.content_type === "video/mp4")
          .sort((a, b) => (b.bitrate || 0) - (a.bitrate || 0));
        const key = item.id || item.url || index;
        if ((item.type === "video" || item.type === "animated_gif") && variants[0]) {
          return (
            <video
              key={key}
              controls={item.type === "video"}
              autoPlay={item.type === "animated_gif"}
              loop={item.type === "animated_gif"}
              muted={item.type === "animated_gif"}
              playsInline
              poster={item.url}
            >
              <source src={variants[0].url} type="video/mp4" />
            </video>
          );
        }
        return <img key={key} src={item.url} alt={item.alt_text || "Tweet media"} loading="lazy" />;
      })}
    </div>
  );
}

function EmbeddedTweet({ tweet }) {
  if (!tweet) return null;
  const author = tweet.author || {};
  return (
    <div className="embedded-tweet">
      <strong>{author.display_name || author.handle || "Unknown author"}</strong>
      {author.handle && <span> @{author.handle}</span>}
      <p>{tweet.text}</p>
      <Media items={tweet.media || []} />
    </div>
  );
}

export default function TweetCard({ tweet }) {
  const author = tweet.author || { handle: tweet.account };
  const media = tweet.media || tweet.entities?.media || [];
  const typeLabel = tweet.type === "Retweet" ? "reposted" : tweet.type === "Reply" ? "replied" : null;
  return (
    <article className="tweet">
      {typeLabel && <div className="tweet-context">↻ {author.display_name || `@${tweet.account}`} {typeLabel}</div>}
      <div className="tweet-layout">
        {author.avatar_url ? (
          <img className="avatar" src={author.avatar_url} alt="" loading="lazy" />
        ) : <div className="avatar avatar-fallback" aria-hidden="true">@</div>}
        <div className="tweet-body">
          <header>
            <strong>{author.display_name || `@${tweet.account}`}</strong>
            {author.verified && <span className="verified" title={author.verified_type || "Verified"} aria-label="Verified">✓</span>}
            <span className="handle">@{author.handle || tweet.account}</span>
            {tweet.created_at && <time dateTime={tweet.created_at}>· {new Date(tweet.created_at).toLocaleString()}</time>}
          </header>
          {tweet.reply_to?.handle && <div className="replying">Replying to @{tweet.reply_to.handle}</div>}
          <p>{tweet.text}</p>
          {(tweet.entities?.urls || []).map((url) => url.expanded && (
            <a className="expanded-link" key={url.expanded} href={url.expanded} target="_blank" rel="noreferrer">{url.display || url.expanded}</a>
          ))}
          {tweet.possibly_sensitive && media.length ? (
            <details className="sensitive"><summary>Show potentially sensitive media</summary><Media items={media} /></details>
          ) : <Media items={media} />}
          {tweet.card && (
            <a className="link-card" href={tweet.card.url || tweet.url} target="_blank" rel="noreferrer">
              {tweet.card.image_url && <img src={tweet.card.image_url} alt="" loading="lazy" />}
              <span><strong>{tweet.card.title}</strong><small>{tweet.card.description}</small></span>
            </a>
          )}
          <EmbeddedTweet tweet={tweet.quoted_tweet || tweet.retweeted_tweet} />
          <footer aria-label="Tweet metrics">
            <span title="Replies">◯ {tweet.replies || 0}</span>
            <span title="Reposts">↻ {tweet.retweets || 0}</span>
            <span title="Likes">♡ {tweet.likes || 0}</span>
            <span title="Views">▥ {tweet.views || 0}</span>
            <span title="Bookmarks">♧ {tweet.bookmarks || 0}</span>
            {tweet.url && <a href={tweet.url} target="_blank" rel="noreferrer" aria-label="Open on X">↗</a>}
          </footer>
        </div>
      </div>
    </article>
  );
}
