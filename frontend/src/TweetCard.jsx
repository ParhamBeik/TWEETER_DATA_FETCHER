import { useEffect, useState } from "react";
import { BadgeCheck, Bookmark, Eye, Heart, MessageCircle, Repeat2, TrendingUp } from "lucide-react";
import { Avatar } from "./filters";
import { cn } from "@/lib/cn";
import { absoluteTime, compact, permalink, relativeTime, statusLink } from "./format";

// The reading surface. Everywhere else in this console is a cold instrument
// panel; a post is the thing the instrument caught, so it sits on warm paper
// with a wider measure and the neutral body face. The semantic class names below
// (.tweet-media, .media-cell, .tweet-time, .avatar) are kept as behavioural
// hooks -- they name real parts of a post and the suite queries them.

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
      <span className="flex h-full items-center justify-center p-3 text-center text-xs text-fg-dim">
        {item.alt_text || "Image unavailable — open on X"}
      </span>
    );
  }
  return (
    <img
      className="size-full object-cover"
      src={item.url}
      alt={alt}
      loading="lazy"
      onError={() => setFailed(true)}
    />
  );
}

/**
 * The URL to actually play, which is only ever a file we archived ourselves.
 *
 * X's `video.twimg.com` variants are deliberately not used as a fallback. The
 * earlier note here claimed they hotlink fine; they do not -- X answers every
 * one of them with 403 from any origin that is not x.com. Trying anyway gave a
 * play button that could never play, and re-requested each rejected file on
 * every render. Until the archiver has stored the mp4 (fetching/media.py), the
 * honest control is a link to X.
 */
function playableSource(item) {
  return typeof item.src === "string" && item.src ? item.src : null;
}

const CELL = "media-cell relative overflow-hidden rounded-sm border border-paper-line bg-ink-900";
const NOTE =
  "absolute bottom-1.5 left-1.5 rounded-xs bg-ink-900/85 px-1.5 py-0.5 font-mono text-2xs text-fg-muted";

/**
 * A video that plays in place when we hold the file, and links out when we do
 * not -- with no play button in that case, because there is nothing to play.
 */
function MediaVideo({ item, label, permalinkUrl, style }) {
  const [failed, setFailed] = useState(false);
  const source = playableSource(item);
  const isGif = item.type === "animated_gif";

  if (failed || !source) {
    return (
      <a
        className={cn(CELL, "media-video block")}
        style={style}
        href={permalinkUrl}
        target="_blank"
        rel="noreferrer"
        aria-label={`Watch on X: ${label}`}
      >
        {item.url && <MediaImage item={item} alt={label} />}
        {/* No ▶ overlay here: it promised in-place playback that this branch
            cannot deliver, which is what made the dead player feel broken
            rather than simply "not archived". */}
        <span className={NOTE}>{isGif ? "GIF · watch on X" : "Watch on X"}</span>
      </a>
    );
  }

  return (
    <div className={cn(CELL, "media-video")} style={style}>
      <video
        className="size-full object-cover"
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
        src={source}
        onError={() => setFailed(true)}
      />
      {isGif && <span className={NOTE}>GIF</span>}
    </div>
  );
}

function Media({ items = [], onOpen, permalinkUrl }) {
  if (!items.length) return null;
  const shown = items.slice(0, 4);
  return (
    <div
      className={cn(
        "tweet-media relative mt-2.5 grid gap-1",
        shown.length > 1 && "grid-cols-2",
        `media-${Math.min(shown.length, 4)}`,
      )}
    >
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
            className={cn(CELL, "block p-0")}
            style={aspect(item, shown.length)}
            onClick={() => onOpen?.(item)}
            aria-label={`Open image: ${label}`}
          >
            <MediaImage item={item} alt={item.alt_text || ""} />
          </button>
        );
      })}
      {items.length > 4 && (
        <span className="absolute bottom-1.5 right-1.5 rounded-xs bg-ink-900/85 px-1.5 py-0.5 font-mono text-2xs">
          +{items.length - 4}
        </span>
      )}
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
    <div
      className="fixed inset-0 z-50 flex flex-col items-center justify-center gap-3 bg-ink-900/95 p-6"
      role="dialog"
      aria-modal="true"
      aria-label="Image viewer"
      onClick={onClose}
    >
      <button
        type="button"
        className="absolute right-4 top-4 text-fg-muted hover:text-fg"
        onClick={onClose}
        aria-label="Close image"
      >
        ✕
      </button>
      <img
        className="max-h-[85vh] max-w-full object-contain"
        src={item.url}
        alt={item.alt_text || ""}
        onClick={(event) => event.stopPropagation()}
      />
      {item.alt_text && <p className="max-w-prose text-xs text-fg-muted">{item.alt_text}</p>}
    </div>
  );
}

/** The quoted or reposted original, rendered as a real post rather than a stub. */
function EmbeddedTweet({ tweet, onOpen }) {
  if (!tweet) return null;
  const author = tweet.author || {};
  const url = statusLink(author.handle, tweet.id);
  return (
    <div className="embedded-tweet mt-2.5 rounded-sm border border-paper-line p-2.5">
      <div className="flex flex-wrap items-center gap-1.5 text-xs">
        <Avatar account={author} size={20} />
        <strong className="font-medium">
          {author.display_name || author.handle || "Unknown author"}
        </strong>
        {author.handle && <span className="font-mono text-fg-dim">@{author.handle}</span>}
        {tweet.created_at && (
          <time
            className="text-fg-dim"
            dateTime={tweet.created_at}
            title={absoluteTime(tweet.created_at)}
          >
            · {relativeTime(tweet.created_at)}
          </time>
        )}
      </div>
      {(tweet.text_clean || tweet.text) && (
        <p dir="auto" className="mt-1.5 font-sans text-sm text-fg-muted">
          {tweet.text_clean || tweet.text}
        </p>
      )}
      <Media items={tweet.media || []} onOpen={onOpen} permalinkUrl={url} />
      {url && (
        <a
          className="mt-2 inline-block text-xs text-accent hover:underline"
          href={url}
          target="_blank"
          rel="noreferrer"
        >
          Open original on X ↗
        </a>
      )}
    </div>
  );
}

const METRICS = [
  ["replies", MessageCircle, "replies"],
  ["retweets", Repeat2, "reposts"],
  ["likes", Heart, "likes"],
  ["views", Eye, "views"],
  ["bookmarks", Bookmark, "bookmarks"],
];

function Metrics({ tweet }) {
  return (
    <footer
      aria-label="Post metrics"
      className="mt-2.5 flex flex-wrap items-center gap-x-4 gap-y-1 font-mono text-xs tabular text-fg-dim"
    >
      {METRICS.map(([key, Icon, noun]) => (
        <span
          key={key}
          className="inline-flex items-center gap-1"
          title={`${tweet[key] || 0} ${noun}`}
        >
          <Icon className="size-3.5" aria-hidden="true" />
          {compact(tweet[key])}
        </span>
      ))}
      {tweet.velocity != null && (
        <span
          className="inline-flex items-center gap-1 text-accent"
          title="Engagement gained in this window"
        >
          <TrendingUp className="size-3.5" aria-hidden="true" />
          {compact(tweet.velocity)}
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
  // text_clean is the same string with X's HTML entities decoded and its
  // duplicated t.co collapsed; `text` stays the verbatim archive and is the
  // fallback for rows the backfill has not reached.
  const text = reposted
    ? reposted.text_clean || reposted.text
    : tweet.text_clean || tweet.text;
  const postedAt = reposted?.created_at || tweet.created_at;
  const url = permalink(tweet);
  const isSelfThread =
    tweet.type === "Reply" &&
    tweet.reply_to?.handle &&
    tweet.reply_to.handle.toLowerCase() === String(tweet.account || "").toLowerCase();

  return (
    <article
      className={cn(
        "tweet border-b border-paper-line px-4 py-3.5 last:border-b-0",
        `tweet-${(tweet.type || "tweet").toLowerCase()}`,
      )}
      // Long feeds are the one place this app renders thousands of nodes;
      // skipping layout for off-screen posts is what keeps scrolling smooth.
      style={{ contentVisibility: "auto", containIntrinsicSize: "auto 220px" }}
    >
      {reposted && (
        <div className="mb-1.5 pl-[3.25rem] text-xs text-fg-dim">
          <Repeat2 className="mr-1 inline size-3.5" aria-hidden="true" />
          {tweet.author?.display_name || `@${tweet.account}`} reposted
        </div>
      )}
      {isSelfThread && (
        <div className="mb-1.5 pl-[3.25rem] text-xs text-fg-dim">Part of a thread</div>
      )}
      <div className="flex gap-3">
        <Avatar account={author} />
        <div className="min-w-0 flex-1">
          <header className="flex flex-wrap items-baseline gap-x-1.5 gap-y-0.5">
            <strong className="font-semibold">
              {author.display_name || `@${author.handle || tweet.account}`}
            </strong>
            {author.verified && (
              <BadgeCheck
                className="size-3.5 shrink-0 self-center text-accent"
                title={author.verified_type || "Verified"}
                aria-label="Verified"
              />
            )}
            <span className="font-mono text-xs text-fg-dim">
              @{author.handle || tweet.account}
            </span>
            {postedAt && url && (
              // The timestamp is the permalink: the most-wanted link on the card
              // should be the biggest target on it, not a glyph in the footer.
              <a
                href={url}
                target="_blank"
                rel="noreferrer"
                className="tweet-time text-xs text-fg-dim hover:text-accent hover:underline"
              >
                <time dateTime={postedAt} title={absoluteTime(postedAt)}>
                  · {relativeTime(postedAt)}
                </time>
              </a>
            )}
          </header>
          {!reposted && tweet.reply_to?.handle && !isSelfThread && (
            <div className="mt-0.5 text-xs text-fg-dim">
              Replying to{" "}
              <a
                className="text-accent hover:underline"
                href={statusLink(tweet.reply_to.handle, tweet.reply_to.tweet_id)}
                target="_blank"
                rel="noreferrer"
              >
                @{tweet.reply_to.handle}
              </a>
            </div>
          )}
          {text && (
            // dir="auto" lets the browser pick direction from the first strong
            // character. This archive tracks Persian and Arabic accounts heavily
            // and every one of their posts was being laid out left-to-right.
            <p
              dir="auto"
              className="mt-1 whitespace-pre-wrap break-words font-sans text-md leading-relaxed"
            >
              {text}
            </p>
          )}
          {(tweet.entities?.urls || []).map(
            (link) =>
              link.expanded && (
                <a
                  className="mt-1 block truncate text-xs text-accent hover:underline"
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
            <details className="mt-2.5 rounded-sm border border-paper-line p-2.5">
              <summary className="cursor-pointer text-xs text-fg-muted">
                Show potentially sensitive media
              </summary>
              <Media items={media} onOpen={setLightbox} permalinkUrl={url} />
            </details>
          ) : (
            <Media items={media} onOpen={setLightbox} permalinkUrl={url} />
          )}
          {tweet.card && (
            <a
              className="mt-2.5 flex gap-2.5 overflow-hidden rounded-sm border border-paper-line hover:border-line-strong"
              href={tweet.card.url || url}
              target="_blank"
              rel="noreferrer"
            >
              {tweet.card.image_url && (
                <img
                  className="size-20 shrink-0 object-cover"
                  src={tweet.card.image_url}
                  alt=""
                  loading="lazy"
                />
              )}
              <span className="min-w-0 py-2 pr-2.5">
                <strong className="block truncate text-sm">{tweet.card.title}</strong>
                <small className="mt-0.5 block truncate text-xs text-fg-muted">
                  {tweet.card.description}
                </small>
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
