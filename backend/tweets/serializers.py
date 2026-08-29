from rest_framework import serializers

from fetching.accounts import account_ops, live_state_map
from fetching.media import lookup_local_urls, photo_urls_from_extras, rewrite_media_blob

from .models import FetchRun, Search, SearchTweet, Tweet, TwitterUser


class TwitterUserSerializer(serializers.ModelSerializer):
    class Meta:
        model = TwitterUser
        fields = [
            "id", "handle", "display_name", "avatar_url", "verified", "verified_type",
            "rest_id", "tracking", "priority", "quarantined", "quarantine_reason",
        ]


class AccountOpsSerializer(TwitterUserSerializer):
    poll_interval_seconds = serializers.IntegerField(read_only=True)
    observed_median_gap_seconds = serializers.IntegerField(read_only=True, allow_null=True)
    live_window_hours = serializers.IntegerField(read_only=True)
    historical_window_days = serializers.IntegerField(read_only=True)
    last_checked_at = serializers.DateTimeField(read_only=True, allow_null=True)
    last_status = serializers.CharField(read_only=True)
    recent_tweet_count = serializers.IntegerField(read_only=True)
    watermarks = serializers.DictField(read_only=True)

    class Meta(TwitterUserSerializer.Meta):
        fields = TwitterUserSerializer.Meta.fields + [
            "poll_interval_seconds",
            "observed_median_gap_seconds",
            "live_window_hours",
            "historical_window_days",
            "last_checked_at",
            "last_status",
            "recent_tweet_count",
            "watermarks",
        ]

    def to_representation(self, instance):
        data = super().to_representation(instance)
        live = self.context.get("live_state")
        if live is None:
            live = live_state_map()
        data.update(account_ops(instance, live))
        last_checked = data.get("last_checked_at")
        if last_checked and hasattr(last_checked, "isoformat"):
            data["last_checked_at"] = last_checked.isoformat()
        return data


# Every field both tweet tables share. SearchTweet is the same normalized shape
# minus source_subsystem, so the console's TweetCard renders either one without
# knowing which collector it came from.
_SHARED_TWEET_FIELDS = [
    # `text` stays verbatim so the API remains a faithful record of what X
    # served; `text_clean` is what the console renders and what search matches.
    "id", "tweet_id", "account", "text", "text_clean", "url", "type", "created_at",
    "author", "likes", "retweets", "replies", "quotes", "bookmarks", "views",
    "source_language", "entities", "media", "card", "reply_to",
    "quoted_tweet", "retweeted_tweet", "possibly_sensitive",
    "source_endpoint", "conversation_id",
]


class BaseTweetSerializer(serializers.ModelSerializer):
    """Media/card/quote unpacking shared by Tweet and SearchTweet."""

    author = TwitterUserSerializer(read_only=True)
    entities = serializers.SerializerMethodField()
    media = serializers.SerializerMethodField()
    card = serializers.SerializerMethodField()

    def get_entities(self, obj):
        """Stored entities, with X's repeated link entries collapsed.

        X frequently sends the same t.co twice in `entities.urls` (once for the
        author's link, once for the card it generated), which rendered the same
        expanded URL twice under every Reuters post. Same reasoning as
        text_clean: the column keeps what arrived, the API presents it once.
        """
        entities = obj.entities if isinstance(getattr(obj, "entities", None), dict) else {}
        urls = entities.get("urls")
        if not isinstance(urls, list):
            return entities
        seen, unique = set(), []
        for item in urls:
            key = item.get("short") if isinstance(item, dict) else str(item)
            if key in seen:
                continue
            seen.add(key)
            unique.append(item)
        return {**entities, "urls": unique}
    reply_to = serializers.SerializerMethodField()
    quoted_tweet = serializers.SerializerMethodField()
    retweeted_tweet = serializers.SerializerMethodField()
    possibly_sensitive = serializers.SerializerMethodField()

    def _extra(self, obj, key: str, default=None):
        extras = self._rewritten_extras(obj)
        if key in extras:
            return extras.get(key, default)
        payload = obj.payload if isinstance(getattr(obj, "payload", None), dict) else {}
        return payload.get(key, default)

    def _rewritten_extras(self, obj) -> dict:
        """Prefer locally archived photo URLs; fall back to the X URL."""
        cache = getattr(self, "_rewritten_extras_cache", None)
        if cache is None:
            self._rewritten_extras_cache = cache = {}
        key = obj.pk
        if key in cache:
            return cache[key]
        extras = obj.extras if isinstance(obj.extras, dict) else {}
        assets = lookup_local_urls(photo_urls_from_extras(extras))
        cache[key] = rewrite_media_blob(extras, assets) if assets else extras
        return cache[key]

    def get_media(self, obj):
        return self._extra(obj, "media", []) or []

    def get_card(self, obj):
        return self._extra(obj, "card", None)

    def get_reply_to(self, obj):
        return self._extra(obj, "reply_to", None)

    def get_quoted_tweet(self, obj):
        return self._extra(obj, "quoted_tweet", None)

    def get_retweeted_tweet(self, obj):
        return self._extra(obj, "retweeted_tweet", None)

    def get_possibly_sensitive(self, obj):
        return bool(self._extra(obj, "possibly_sensitive", False))


class TweetSerializer(BaseTweetSerializer):
    """A tracked-account tweet, as the feed and the analytics endpoints serve it."""

    class Meta:
        model = Tweet
        fields = _SHARED_TWEET_FIELDS + ["source_subsystem"]


class SearchTweetSerializer(BaseTweetSerializer):
    """A SearchTimeline hit. Same wire shape as TweetSerializer.

    `source_subsystem` is reported as a constant rather than omitted: the console
    colours a post by which collector found it, and a missing key there would
    render as "before tracking" instead of "saved search".
    """

    source_subsystem = serializers.SerializerMethodField()

    class Meta:
        model = SearchTweet
        fields = _SHARED_TWEET_FIELDS + ["source_subsystem"]

    def get_source_subsystem(self, obj):
        return "search"


def _new_tweets(run) -> int:
    """Rows this run added, falling back to rows it touched for older runs.

    Runs recorded before `new_tweets` existed only know how many rows they
    upserted, so they keep reporting that rather than claiming zero.
    """
    summary = run.summary or {}
    if "new_tweets" in summary:
        return int(summary.get("new_tweets") or 0)
    return int(summary.get("ingested_tweets") or 0)


class SearchSerializer(serializers.ModelSerializer):
    name = serializers.CharField(required=False, allow_blank=True)
    hit_count = serializers.SerializerMethodField()
    schedule = serializers.SerializerMethodField()
    last_run = serializers.SerializerMethodField()

    class Meta:
        model = Search
        fields = [
            "id", "name", "slug", "raw_query", "product", "pagination_depth", "rolling_hours",
            "enabled", "interval_seconds", "last_run_at", "created_at",
            "hit_count", "schedule", "last_run",
        ]
        read_only_fields = ["slug", "last_run_at", "created_at"]

    def get_hit_count(self, obj) -> int:
        # Annotated by the viewset for list views so N rows cost one query; the
        # fallback keeps a bare SearchSerializer(search) usable anywhere else.
        annotated = getattr(obj, "hit_count_annotated", None)
        return int(annotated if annotated is not None else obj.hits.count())

    def get_schedule(self, obj) -> dict:
        from fetching.searches import schedule_for

        return schedule_for(obj, running=getattr(obj, "is_running_annotated", None))

    def get_last_run(self, obj):
        run = obj.runs.order_by("-started_at").first()
        if run is None:
            return None
        return {
            "run_id": run.run_id,
            "status": run.status,
            "started_at": run.started_at,
            "finished_at": run.finished_at,
            "ingested_tweets": int((run.summary or {}).get("ingested_tweets") or 0),
            "new_tweets": _new_tweets(run),
        }


class FetchRunSerializer(serializers.ModelSerializer):
    class Meta:
        model = FetchRun
        fields = [
            "run_id", "task_id", "subsystem", "target", "status", "return_code",
            "summary", "failure_ledger", "started_at", "finished_at",
        ]


from fetching.redaction import redact_text as _redact_text  # noqa: E402

# Redaction now happens before the excerpt is written (fetching.runner), so
# this read-time pass only still matters for rows stored before that change --
# and for the admin, which renders log_excerpt without going through DRF.


class FetchRunDetailSerializer(FetchRunSerializer):
    class Meta(FetchRunSerializer.Meta):
        fields = FetchRunSerializer.Meta.fields + ["log_excerpt"]

    def to_representation(self, instance):
        data = super().to_representation(instance)
        data["log_excerpt"] = _redact_text(instance.log_excerpt)
        return data
