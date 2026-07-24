export default function TweetCard({ tweet }) {
  return (
    <article className="tweet">
      <header>
        <strong>@{tweet.account}</strong>
        <span className="type">{tweet.type}</span>
        {tweet.created_at && (
          <time>{new Date(tweet.created_at).toLocaleString()}</time>
        )}
      </header>
      <p>{tweet.text}</p>
      <footer>
        <span>❤ {tweet.likes}</span>
        <span>🔁 {tweet.retweets}</span>
        <span>💬 {tweet.replies}</span>
        <span>👁 {tweet.views}</span>
        {tweet.url && (
          <a href={tweet.url} target="_blank" rel="noreferrer">open</a>
        )}
      </footer>
    </article>
  );
}
