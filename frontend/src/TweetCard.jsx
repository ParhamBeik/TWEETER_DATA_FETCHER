import { useEffect, useState } from "react";
import { Avatar } from "./filters";
import { absoluteTime, compact, permalink, relativeTime, statusLink } from "./format";

/** Aspect-ratio box so an image reserves its real shape instead of a fixed crop. */
function aspect(item, count) {
  // Multi-image grids are uniform tiles; a lone image keeps its own proportions
  // so a tall screenshot is not cropped to a letterbox.
  if (count > 1) return { aspectRatio: "16 / 10" };
  const [w, h] = item.aspect_ratio || [];
  if (w && h) return { aspectRatio: `${w} / ${h}` };
  if (item.width && item.height) return { aspectRatio: `${item.width} / ${item.height}` };
  return { aspectRatio: "16 / 10" };
}

/**
 * An image that degrades to a caption instead of a broken-icon glyph.
 *
 * X serves media from pbs.twimg.com; when that is blocked, or the row is old
 * enough that the asset is gone, the default rendering is a broken thumbnail on
 * a black slab. Saying so is more useful than showing it.
 */
function MediaImage({ item, alt }) {
  const [failed, setFailed] = useState(false);
  if (failed) {
    return (
      <span className="media-missing">
        {item.alt_text || "Image unavailable — open on X"}
      </span>
    );
  }
  return <img src={item.url} alt={alt} loading="lazy" onError={() => setFailed(true)} />;
}

/** Best playable MP4 for a video item: highest bitrate X offers, or none. */
function bestVariant(item) {
  const mp4s = (item.variants || []).filter(
    (variant) => variant.url && variant.content_type === "video/mp4",
  );
  if (!mp4s.length) return null;
  // Bitrate is absent on the stream manifest and on some GIF variants; treating
  // that as 0 keeps those as the last resort rather than accidentally the pick.
  return mp4s.reduce((best, v) => ((v.bitrate || 0) > (best.bitrate || 0) ? v : best));
}

/**
 * A video that plays in place, falling back to a link out.
 *
 * The poster frame comes from pbs.twimg.com and the stream from
 * video.twimg.com; both hotlink fine (verified: the MP4 answers a range request
 * with 206). If either is blocked or gone, the reader still gets a real link
 * rather than a dead black slab.
 */
function MediaVideo({ item, label, permalinkUrl, style }) {
  const [failed, setFailed] = useState(false);
  const variant = bestVariant(item);
  const isGif = item.type === "animated_gif";

  if (failed || !variant) {
    return (
      <a
        className="media-cell media-video"
        style={style}
        href={permalinkUrl}
        target="_blank"
        rel="noreferrer"
        aria-label={`Watch on X: ${label}`}
      >
        {item.url && <MediaImage item={item} alt={label} />}
        <span className="play-badge" aria-hidden="true">▶</span>
        <span className="media-note">{isGif ? "GIF" : "Watch on X"}</span>
      </a>
    );
  }

  return (
    <div className="media-cell media-video" style={style}>
      <video
        // An X "GIF" is really a silent looping MP4, so it gets GIF semantics
        // rather than a scrub bar. Muted is not cosmetic: browsers refuse to
        // autoplay anything with audio, so without it the loop never starts.
        controls={!isGif}
        loop={isGif}
        muted={isGif}
        autoPlay={isGif}
        preload="metadata"
        playsInline
        poster={item.url || undefined}
        aria-label={label}
        // src on the element, not a <source> child: a failing <source> fires
        // `error` at itself and does not bubble, and once candidates run out the
        // media element enters NETWORK_NO_SOURCE *without* firing error at all.
        // With a child element this handler never runs, so a 403 or a deleted
        // asset would leave a stuck player and no way out to X.
        src={variant.url}
        onError={() => setFailed(true)}
      />
      {isGif && <span className="media-note">GIF</span>}
    </div>
  );
}

function Media({ items = [], onOpen, permalinkUrl }) {
  if (!items.length) return null;
  const shown = items.slice(0, 4);
  return (
    <div className={`tweet-media media-${Math.min(shown.length, 4)}`}>
      {shown.map((item, index) => {
        const key = item.id || item.url || index;
        const isVideo = item.type === "video" || item.type === "animated_gif";
        const label = item.alt_text || (isVideo ? "Video" : "Photo");
        if (isVideo) {
          return (
            <MediaVideo
              key={key}
              item={item}
              label={label}
              permalinkUrl={permalinkUrl}
              style={aspect(item, shown.length)}
            />
          );
        }
        return (
          <button
            key={key}
            type="button"
            className="media-cell"
            style={aspect(item, shown.length)}
            onClick={() => onOpen?.(item)}
            aria-label={`Open image: ${label}`}
          >
            <MediaImage item={item} alt={item.alt_text || ""} />
          </button>
        );
      })}
      {items.length > 4 && <span className="media-more">+{items.length - 4}</span>}
    </div>
  );
}

function Lightbox({ item, onClose }) {
  useEffect(() => {
    const escape = (event) => event.key === "Escape" && onClose();
    document.addEventListener("keydown", escape);
    return () => document.removeEventListener("keydown", escape);
  }, [onClose]);
  if (!item) return null;
  return (
    <div className="lightbox" role="dialog" aria-modal="true" aria-label="Image viewer" onClick={onClose}>
      <button type="button" className="lightbox-close" onClick={onClose} aria-label="Close image">
        ✕
      </button>
      <img src={item.url} alt={item.alt_text || ""} onClick={(event) => event.stopPropagation()} />
      {item.alt_text && <p className="lightbox-alt">{item.alt_text}</p>}
    </div>
  );
}

/** The quoted or reposted original, rendered as a real post rather than a stub. */
function EmbeddedTweet({ tweet, onOpen }) {
  if (!tweet) return null;
  const author = tweet.author || {};
  const url = statusLink(author.handle, tweet.id);
  return (
    <div className="embedded-tweet">
      <div className="embedded-head">
        <Avatar account={author} size={20} />
        <strong>{author.display_name || author.handle || "Unknown author"}</strong>
        {author.handle && <span className="handle">@{author.handle}</span>}
        {tweet.created_at && (
          <time dateTime={tweet.created_at} title={absoluteTime(tweet.created_at)}>
            · {relativeTime(tweet.created_at)}
          </time>
        )}
      </div>
      {tweet.text && <p>{tweet.text}</p>}
      <Media items={tweet.media || []} onOpen={onOpen} permalinkUrl={url} />
      {url && (
        <a className="embedded-link" href={url} target="_blank" rel="noreferrer">
          Open original on X ↗
        </a>
      )}
    </div>
  );
}

function Metrics({ tweet }) {
  return (
    <footer aria-label="Post metrics">
      <span title={`${tweet.replies || 0} replies`}>◯ {compact(tweet.replies)}</span>
      <span title={`${tweet.retweets || 0} reposts`}>↻ {compact(tweet.retweets)}</span>
      <span title={`${tweet.likes || 0} likes`}>♡ {compact(tweet.likes)}</span>
      <span title={`${tweet.views || 0} views`}>▥ {compact(tweet.views)}</span>
      <span title={`${tweet.bookmarks || 0} bookmarks`}>♧ {compact(tweet.bookmarks)}</span>
      {tweet.velocity != null && (
        <span className="velocity-chip" title="Engagement gained in this window">
          ↗ {compact(tweet.velocity)}
        </span>
      )}
    </footer>
  );
}

export default function TweetCard({ tweet }) {
  const [lightbox, setLightbox] = useState(null);

  const reposted = tweet.type === "Retweet" ? tweet.retweeted_tweet : null;
  // A repost is the original author's post; the reposter belongs in the byline
  // above it, not in the author slot. Rendering the reposter as the author was
  // why boosted posts showed the wrong name over a raw "RT @…" string.
  const source = reposted || tweet;
  const author = source.author || tweet.author || { handle: tweet.account };
  const media = source.media || tweet.media || tweet.entities?.media || [];
  const metrics = reposted?.metrics
    ? { ...tweet, ...reposted.metrics, velocity: tweet.velocity }
    : tweet;
  const text = reposted ? reposted.text : tweet.text;
  const postedAt = reposted?.created_at || tweet.created_at;
  const url = permalink(tweet);
  const isSelfThread =
    tweet.type === "Reply" &&
    tweet.reply_to?.handle &&
    tweet.reply_to.handle.toLowerCase() === String(tweet.account || "").toLowerCase();

  return (
    <article className={`tweet tweet-${(tweet.type || "tweet").toLowerCase()}`}>
      {reposted && (
        <div className="tweet-context">
          ↻ {tweet.author?.display_name || `@${tweet.account}`} reposted
        </div>
      )}
      {isSelfThread && <div className="tweet-context">🧵 Part of a thread</div>}
      <div className="tweet-layout">
        <Avatar account={author} />
        <div className="tweet-body">
          <header>
            <strong>{author.display_name || `@${author.handle || tweet.account}`}</strong>
            {author.verified && (
              <span className="verified" title={author.verified_type || "Verified"} aria-label="Verified">
                ✓
              </span>
            )}
            <span className="handle">@{author.handle || tweet.account}</span>
            {postedAt && url && (
              // The timestamp is the permalink: the most-wanted link on the card
              // should be the biggest target on it, not a glyph in the footer.
              <a href={url} target="_blank" rel="noreferrer" className="tweet-time">
                <time dateTime={postedAt} title={absoluteTime(postedAt)}>
                  · {relativeTime(postedAt)}
                </time>
              </a>
            )}
            <span className="tweet-badges">
              {(tweet.searches || []).map((slug) => (
                <span className="badge badge-search" key={slug} title="Found by a saved search">
                  🔍 {slug}
                </span>
              ))}
            </span>
          </header>
          {!reposted && tweet.reply_to?.handle && !isSelfThread && (
            <div className="replying">
              Replying to{" "}
              <a
                href={statusLink(tweet.reply_to.handle, tweet.reply_to.tweet_id)}
                target="_blank"
                rel="noreferrer"
              >
                @{tweet.reply_to.handle}
              </a>
            </div>
          )}
          {text && <p>{text}</p>}
          {(tweet.entities?.urls || []).map(
            (link) =>
              link.expanded && (
                <a
                  className="expanded-link"
                  key={link.expanded}
                  href={link.expanded}
                  target="_blank"
                  rel="noreferrer"
                >
                  {link.display || link.expanded}
                </a>
              ),
          )}
          {tweet.possibly_sensitive && media.length ? (
            <details className="sensitive">
              <summary>Show potentially sensitive media</summary>
              <Media items={media} onOpen={setLightbox} permalinkUrl={url} />
            </details>
          ) : (
            <Media items={media} onOpen={setLightbox} permalinkUrl={url} />
          )}
          {tweet.card && (
            <a className="link-card" href={tweet.card.url || url} target="_blank" rel="noreferrer">
              {tweet.card.image_url && <img src={tweet.card.image_url} alt="" loading="lazy" />}
              <span>
                <strong>{tweet.card.title}</strong>
                <small>{tweet.card.description}</small>
              </span>
            </a>
          )}
          <EmbeddedTweet tweet={tweet.quoted_tweet} onOpen={setLightbox} />
          <Metrics tweet={metrics} />
        </div>
      </div>
      <Lightbox item={lightbox} onClose={() => setLightbox(null)} />
    </article>
  );
}
