import { fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import TweetCard from "./TweetCard";

// Unit tests: TweetCard is a pure presentational component over one tweet object,
// so rendering it in isolation covers every branch without a router or a server.

const baseTweet = {
  id: "1",
  tweet_id: "1",
  account: "elonmusk",
  text: "Hello world",
  type: "Tweet",
  created_at: "2026-01-02T03:04:05Z",
  replies: 1,
  retweets: 2,
  likes: 3,
  views: 4,
  bookmarks: 5,
};

const renderCard = (overrides = {}) =>
  render(<TweetCard tweet={{ ...baseTweet, ...overrides }} />);

describe("TweetCard identity", () => {
  it("renders the tweet text", () => {
    renderCard();
    expect(screen.getByText("Hello world")).toBeInTheDocument();
  });

  it("prefers the author display name over the raw account handle", () => {
    renderCard({ author: { handle: "elonmusk", display_name: "Elon Musk" } });
    expect(screen.getByText("Elon Musk")).toBeInTheDocument();
  });

  it("falls back to @account when the payload carries no author record", () => {
    renderCard();
    // With no author the display name and the handle both degrade to @account.
    expect(screen.getAllByText("@elonmusk")).toHaveLength(2);
  });

  it("exposes the verification badge to assistive technology", () => {
    renderCard({ author: { handle: "elonmusk", verified: true, verified_type: "Business" } });
    expect(screen.getByLabelText("Verified")).toBeInTheDocument();
  });

  it("renders a machine-readable timestamp", () => {
    renderCard();
    // `time` has no implicit ARIA role, so query the element directly.
    const time = document.querySelector("time");
    expect(time).toHaveAttribute("dateTime", "2026-01-02T03:04:05Z");
  });

  it("omits the timestamp entirely when created_at is missing", () => {
    renderCard({ created_at: null });
    expect(document.querySelector("time")).toBeNull();
  });

  it("shows an initials avatar when the author has no picture", () => {
    renderCard();
    expect(screen.getByText("E")).toBeInTheDocument();
  });

  it("renders the author's avatar when there is one", () => {
    renderCard({
      author: { handle: "elonmusk", avatar_url: "https://pbs.twimg.com/a_normal.jpg" },
    });
    expect(document.querySelector("img.avatar")).toHaveAttribute(
      "src",
      "https://pbs.twimg.com/a_normal.jpg",
    );
  });
});

describe("TweetCard permalinks", () => {
  it("makes the timestamp the link to the exact post on X", () => {
    renderCard({ url: "https://x.com/elonmusk/status/1" });
    const link = document.querySelector("a.tweet-time");
    expect(link).toHaveAttribute("href", "https://x.com/elonmusk/status/1");
    expect(link).toHaveAttribute("rel", "noreferrer");
  });

  // Regression: `Tweet.url` is blank when the engine could not build one, and the
  // card used to render a dead link rather than reconstructing it.
  it("rebuilds the permalink from the handle and id when the URL is blank", () => {
    renderCard({ url: "" });
    expect(document.querySelector("a.tweet-time")).toHaveAttribute(
      "href",
      "https://x.com/elonmusk/status/1",
    );
  });

  it("links a reply's parent to that status on X", () => {
    renderCard({ type: "Reply", reply_to: { handle: "jack", tweet_id: "99" } });
    expect(screen.getByRole("link", { name: "@jack" })).toHaveAttribute(
      "href",
      "https://x.com/jack/status/99",
    );
  });
});

describe("TweetCard post shapes", () => {
  const reposted = {
    type: "Retweet",
    text: "RT @jack: the original words",
    author: { handle: "elonmusk", display_name: "Elon Musk" },
    retweeted_tweet: {
      id: "77",
      text: "the original words",
      author: { handle: "jack", display_name: "Jack Dorsey" },
      created_at: "2026-01-01T00:00:00Z",
      metrics: { likes: 900, retweets: 40, replies: 3, views: 5000, bookmarks: 1 },
    },
  };

  // Regression: the reposter used to be rendered as the author, over the raw
  // "RT @…" string, so a boosted post showed the wrong name and wrong text.
  it("credits a repost to the original author, not the reposter", () => {
    renderCard(reposted);
    expect(screen.getByText("Jack Dorsey")).toBeInTheDocument();
    expect(screen.getByText("the original words")).toBeInTheDocument();
    expect(screen.queryByText(/^RT @jack:/)).toBeNull();
  });

  it("names the reposter in the context banner above the post", () => {
    renderCard(reposted);
    expect(screen.getByText(/Elon Musk reposted/)).toBeInTheDocument();
  });

  it("shows the original's engagement on a repost", () => {
    renderCard(reposted);
    const metrics = screen.getByLabelText("Post metrics");
    expect(within(metrics).getByTitle("900 likes")).toHaveTextContent("900");
  });

  it("renders a quote as its own post plus the quoted original", () => {
    renderCard({
      type: "Quote",
      text: "my take",
      quoted_tweet: {
        id: "88",
        text: "Quoted body",
        author: { handle: "jack", display_name: "Jack" },
      },
    });
    expect(screen.getByText("my take")).toBeInTheDocument();
    expect(screen.getByText("Quoted body")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Open original on X/ })).toHaveAttribute(
      "href",
      "https://x.com/jack/status/88",
    );
  });

  it("names an unattributed quoted tweet rather than rendering blank", () => {
    renderCard({ quoted_tweet: { text: "Quoted body" } });
    expect(screen.getByText("Unknown author")).toBeInTheDocument();
  });

  it("names the account being replied to", () => {
    renderCard({ type: "Reply", reply_to: { handle: "jack", tweet_id: "9" } });
    expect(screen.getByText(/Replying to/)).toBeInTheDocument();
  });

  it("marks a self-reply as a thread instead of 'replying to yourself'", () => {
    renderCard({ type: "Reply", reply_to: { handle: "elonmusk", tweet_id: "9" } });
    expect(screen.getByText(/Part of a thread/)).toBeInTheDocument();
    expect(screen.queryByText(/Replying to/)).toBeNull();
  });

  it("shows no context banner for an original post", () => {
    renderCard();
    expect(screen.queryByText(/reposted|thread/)).toBeNull();
  });

  // The "found by search" badge is gone with the collector split: search hits
  // live in their own table and are only ever rendered on the search page, so a
  // card in the feed can no longer have been found by a saved query.
});

describe("TweetCard metrics", () => {
  it("renders every engagement counter", () => {
    renderCard();
    const metrics = screen.getByLabelText("Post metrics");
    expect(within(metrics).getByTitle("1 replies")).toHaveTextContent("1");
    expect(within(metrics).getByTitle("2 reposts")).toHaveTextContent("2");
    expect(within(metrics).getByTitle("3 likes")).toHaveTextContent("3");
    expect(within(metrics).getByTitle("4 views")).toHaveTextContent("4");
    expect(within(metrics).getByTitle("5 bookmarks")).toHaveTextContent("5");
  });

  it("compacts large counters so the row keeps its width", () => {
    renderCard({ likes: 1234, views: 3400000 });
    const metrics = screen.getByLabelText("Post metrics");
    expect(within(metrics).getByTitle("1234 likes")).toHaveTextContent("1.2K");
    expect(within(metrics).getByTitle("3400000 views")).toHaveTextContent("3.4M");
  });

  it("renders zero rather than blank for absent counters", () => {
    render(<TweetCard tweet={{ id: "1", account: "a", text: "t" }} />);
    const metrics = screen.getByLabelText("Post metrics");
    expect(within(metrics).getByTitle("0 likes")).toHaveTextContent("0");
  });

  it("shows the velocity gained when the analytics view supplies it", () => {
    renderCard({ velocity: 890 });
    expect(screen.getByTitle("Engagement gained in this window")).toHaveTextContent("890");
  });
});

describe("TweetCard media", () => {
  const photo = (over = {}) => ({ id: "m1", type: "photo", url: "http://img/1.jpg", ...over });

  it("renders photos with their alt text", () => {
    renderCard({ media: [photo({ alt_text: "A chart" })] });
    expect(screen.getByAltText("A chart")).toHaveAttribute("src", "http://img/1.jpg");
  });

  it("leaves alt empty rather than inventing a description", () => {
    renderCard({ media: [photo()] });
    expect(screen.getByRole("button", { name: "Open image: Photo" })).toBeInTheDocument();
  });

  it("reserves the image's real aspect ratio instead of a fixed crop", () => {
    renderCard({ media: [photo({ width: 1200, height: 675 })] });
    expect(document.querySelector(".media-cell")).toHaveStyle({ aspectRatio: "1200 / 675" });
  });

  it("opens a photo in a lightbox and closes it again", async () => {
    const user = userEvent.setup();
    renderCard({ media: [photo({ alt_text: "A chart" })] });

    await user.click(screen.getByRole("button", { name: "Open image: A chart" }));
    expect(screen.getByRole("dialog", { name: "Image viewer" })).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Close image" }));
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  // X answers every video.twimg.com request with 403 from any origin that is not
  // x.com, so only a file the archiver stored locally is ever played.
  it("plays the locally archived file, never X's CDN", () => {
    renderCard({
      url: "https://x.com/elonmusk/status/1",
      media: [
        {
          id: "m1",
          type: "video",
          url: "/media/ab/poster.jpg",
          src: "/media/ab/clip.mp4",
          variants: [
            { content_type: "application/x-mpegURL", url: "http://v/stream.m3u8" },
            { content_type: "video/mp4", bitrate: 2176, url: "http://v/high.mp4" },
          ],
        },
      ],
    });
    const video = document.querySelector("video");
    expect(video).toHaveAttribute("poster", "/media/ab/poster.jpg");
    expect(video).toHaveAttribute("src", "/media/ab/clip.mp4");
    expect(video).toHaveAttribute("controls");
  });

  it("links out instead of offering a play button we cannot honour", () => {
    // Variants exist, but none is archived. Hotlinking them 403s, so the card
    // must not imply in-place playback.
    renderCard({
      url: "https://x.com/elonmusk/status/1",
      media: [
        {
          id: "m1",
          type: "video",
          url: "http://img/poster.jpg",
          variants: [{ content_type: "video/mp4", bitrate: 2176, url: "http://v/high.mp4" }],
        },
      ],
    });
    expect(document.querySelector("video")).toBeNull();
    expect(screen.getByRole("link", { name: /Watch on X/ })).toHaveAttribute(
      "href",
      "https://x.com/elonmusk/status/1",
    );
    expect(screen.queryByText("\u25b6")).toBeNull();
  });

  it("falls back to the X link when the video itself fails to load", () => {
    // The src must live on <video>, not a <source> child: a failing <source>
    // fires error at itself without bubbling, so the handler would never run
    // and the reader would be stuck with a dead player.
    renderCard({
      url: "https://x.com/elonmusk/status/1",
      media: [
        {
          id: "m1",
          type: "video",
          url: "http://img/poster.jpg",
          src: "/media/ab/gone.mp4",
        },
      ],
    });

    fireEvent.error(document.querySelector("video"));

    expect(screen.getByRole("link", { name: /Watch on X/ })).toHaveAttribute(
      "href",
      "https://x.com/elonmusk/status/1",
    );
    expect(document.querySelector("video")).toBeNull();
  });

  it("links out to X when a video has no playable MP4", () => {
    renderCard({
      url: "https://x.com/elonmusk/status/1",
      media: [
        {
          id: "m1",
          type: "video",
          url: "http://img/poster.jpg",
          variants: [{ content_type: "application/x-mpegURL", url: "http://v/stream.m3u8" }],
        },
      ],
    });
    const link = screen.getByRole("link", { name: /Watch on X/ });
    expect(link).toHaveAttribute("href", "https://x.com/elonmusk/status/1");
    expect(within(link).getByRole("img")).toHaveAttribute("src", "http://img/poster.jpg");
    expect(document.querySelector("video")).toBeNull();
  });

  it("loops an animated GIF silently instead of giving it a scrub bar", () => {
    renderCard({
      media: [
        {
          id: "m1",
          type: "animated_gif",
          url: "http://img/poster.jpg",
          src: "/media/ab/gif.mp4",
        },
      ],
    });
    const video = document.querySelector("video");
    expect(video).toHaveAttribute("loop");
    expect(video).not.toHaveAttribute("controls");
    expect(screen.getByText("GIF")).toBeInTheDocument();
  });

  it("labels an animated GIF as such when it has no archived file", () => {
    renderCard({ media: [{ id: "m1", type: "animated_gif", url: "http://img/poster.jpg" }] });
    expect(screen.getByText(/GIF/)).toBeInTheDocument();
  });

  it("caps the media grid at four and counts the remainder", () => {
    renderCard({
      media: Array.from({ length: 6 }, (_, i) => photo({ id: `m${i}`, url: `http://img/${i}.jpg` })),
    });
    expect(screen.getAllByRole("button", { name: /Open image/ })).toHaveLength(4);
    expect(screen.getByText("+2")).toBeInTheDocument();
  });

  it("hides sensitive media behind a disclosure the user must open", () => {
    renderCard({ possibly_sensitive: true, media: [photo()] });
    const disclosure = screen.getByText("Show potentially sensitive media");
    expect(disclosure).toBeInTheDocument();
    expect(disclosure.closest("details")).not.toHaveAttribute("open");
  });

  it("renders nothing for an empty media list", () => {
    renderCard({ media: [] });
    expect(document.querySelector(".tweet-media")).toBeNull();
  });
});

describe("TweetCard links", () => {
  it("renders expanded entity URLs as outbound links", () => {
    renderCard({ entities: { urls: [{ expanded: "https://example.com/a", display: "example.com/a" }] } });
    expect(screen.getByText("example.com/a")).toHaveAttribute("href", "https://example.com/a");
  });

  it("renders a link card with its title and description", () => {
    renderCard({ card: { url: "https://example.com", title: "Card title", description: "Card body" } });
    expect(screen.getByText("Card title")).toBeInTheDocument();
    expect(screen.getByText("Card body")).toBeInTheDocument();
  });
});
