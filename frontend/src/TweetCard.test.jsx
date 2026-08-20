import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import TweetCard from "./TweetCard";

// Unit tests: TweetCard is a pure presentational component over one tweet object,
// so rendering it in isolation covers every branch without a router or a server.

const baseTweet = {
  id: "1",
  account: "elonmusk",
  text: "Hello world",
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
});

describe("TweetCard context labels", () => {
  it("labels a retweet as reposted", () => {
    renderCard({ type: "Retweet" });
    expect(screen.getByText(/reposted/)).toBeInTheDocument();
  });

  it("labels a reply as replied", () => {
    renderCard({ type: "Reply" });
    expect(screen.getByText(/replied/)).toBeInTheDocument();
  });

  it("shows no context banner for an original tweet", () => {
    renderCard({ type: "Tweet" });
    expect(screen.queryByText(/reposted|replied/)).toBeNull();
  });

  it("names the account being replied to", () => {
    renderCard({ reply_to: { handle: "jack" } });
    expect(screen.getByText("Replying to @jack")).toBeInTheDocument();
  });
});

describe("TweetCard metrics", () => {
  it("renders every engagement counter", () => {
    renderCard();
    const metrics = screen.getByLabelText("Tweet metrics");
    expect(within(metrics).getByTitle("Replies")).toHaveTextContent("1");
    expect(within(metrics).getByTitle("Reposts")).toHaveTextContent("2");
    expect(within(metrics).getByTitle("Likes")).toHaveTextContent("3");
    expect(within(metrics).getByTitle("Views")).toHaveTextContent("4");
    expect(within(metrics).getByTitle("Bookmarks")).toHaveTextContent("5");
  });

  it("renders zero rather than blank for absent counters", () => {
    render(<TweetCard tweet={{ id: "1", account: "a", text: "t" }} />);
    const metrics = screen.getByLabelText("Tweet metrics");
    expect(within(metrics).getByTitle("Likes")).toHaveTextContent("0");
  });

  it("links out to the tweet on X with an accessible name", () => {
    renderCard({ url: "https://x.com/elonmusk/status/1" });
    const link = screen.getByLabelText("Open on X");
    expect(link).toHaveAttribute("href", "https://x.com/elonmusk/status/1");
    expect(link).toHaveAttribute("rel", "noreferrer");
  });
});

describe("TweetCard media", () => {
  it("renders photos with alt text when provided", () => {
    renderCard({ media: [{ id: "m1", type: "photo", url: "http://img/1.jpg", alt_text: "A chart" }] });
    expect(screen.getByAltText("A chart")).toHaveAttribute("src", "http://img/1.jpg");
  });

  it("falls back to a generic alt when the payload has none", () => {
    renderCard({ media: [{ id: "m1", type: "photo", url: "http://img/1.jpg" }] });
    expect(screen.getByAltText("Tweet media")).toBeInTheDocument();
  });

  it("caps the media grid at four items", () => {
    renderCard({
      media: Array.from({ length: 6 }, (_, i) => ({ id: `m${i}`, type: "photo", url: `http://img/${i}.jpg` })),
    });
    expect(screen.getAllByAltText("Tweet media")).toHaveLength(4);
  });

  it("renders a video source for the highest available bitrate", () => {
    renderCard({
      media: [{
        id: "m1",
        type: "video",
        url: "http://img/poster.jpg",
        variants: [
          { content_type: "video/mp4", bitrate: 320, url: "http://v/low.mp4" },
          { content_type: "video/mp4", bitrate: 2176, url: "http://v/high.mp4" },
          { content_type: "application/x-mpegURL", url: "http://v/stream.m3u8" },
        ],
      }],
    });
    expect(document.querySelector("source")).toHaveAttribute("src", "http://v/high.mp4");
  });

  it("hides sensitive media behind a disclosure the user must open", () => {
    renderCard({
      possibly_sensitive: true,
      media: [{ id: "m1", type: "photo", url: "http://img/1.jpg" }],
    });
    const disclosure = screen.getByText("Show potentially sensitive media");
    expect(disclosure).toBeInTheDocument();
    expect(disclosure.closest("details")).not.toHaveAttribute("open");
  });

  it("renders nothing for an empty media list", () => {
    renderCard({ media: [] });
    expect(document.querySelector(".tweet-media")).toBeNull();
  });
});

describe("TweetCard embedded content", () => {
  it("renders a quoted tweet's author and text", () => {
    renderCard({ quoted_tweet: { text: "Quoted body", author: { handle: "jack", display_name: "Jack" } } });
    expect(screen.getByText("Quoted body")).toBeInTheDocument();
    expect(screen.getByText("Jack")).toBeInTheDocument();
  });

  it("names an unattributed quoted tweet rather than rendering blank", () => {
    renderCard({ quoted_tweet: { text: "Quoted body" } });
    expect(screen.getByText("Unknown author")).toBeInTheDocument();
  });

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
