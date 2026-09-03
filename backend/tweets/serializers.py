from rest_framework import serializers

from fetching.accounts import (
    account_ops,
    live_state_map,
    recent_tweet_counts,
    watermark_map,
)
from fetching.media import (
    avatar_urls,
    lookup_local_urls,
    photo_urls_from_extras,
    rewrite_media_blob,
)

from .models import FetchRun, Search, SearchTweet, Tweet, TwitterUser


def page_rows(serializer) -> list:
    """Every instance being serialized alongside this one, or [].

    A `many=True` serializer builds one child and reuses it for the whole page,
    so the child can reach the page through its parent and resolve a per-page
    lookup once instead of once per row. Empty for a single-object serializer,
    which is the signal to fall back to a per-object lookup rather than cache a
    map built from one row and hand it to the next.
    """
    parent = serializer.parent
    rows = parent.instance if isinstance(parent, serializers.ListSerializer) else None
    if rows is None:
        return []
    return list(rows)


class TwitterUserSerializer(serializers.ModelSerializer):
    class Meta:
        model = TwitterUser
        fields = [
            "id", "handle", "display_name", "avatar_url", "verified", "verified_type",
            "rest_id", "tracking", "priority", "quarantined", "quarantine_reason",
        ]

    def _archived_avatars(self) -> dict:
        """remote avatar URL → local path, resolved once per serializer instance.

        One instance serves every author on a page, so this is one lookup for
        the whole feed rather than one per post -- the difference between a
        constant and an N+1.
        """
        cache = getattr(self, "_avatar_cache", None)
        if cache is None:
            cache = self._avatar_cache = lookup_local_urls(avatar_urls())
        return cache

    def to_representation(self, instance):
        data = super().to_representation(instance)
        # Fall back to X's URL when the file is not archived yet, so a new
        # account shows a picture from the first render rather than a gap.
        local = self._archived_avatars().get(data.get("avatar_url"))
        if local:
            data["avatar_url"] = local
        return data


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

    def _ops_maps(self, instance) -> tuple[dict, dict]:
        """Watermarks and recent-post counts for the whole roster page.

        The roster is served unpaginated, so resolving these per row was two
        queries per account -- ~130 for a 64-account roster. Cached on the child
        serializer, which `many=True` reuses for every row.
        """
        cache = getattr(self, "_ops_maps_cache", None)
        if cache is not None:
            return cache
        rows = page_rows(self) or [instance]
        handles = [row.handle for row in rows]
        cache = self._ops_maps_cache = (
            watermark_map(handles),
            recent_tweet_counts(handles),
        )
        return cache

    def to_representation(self, instance):
        data = super().to_representation(instance)
        live = self.context.get("live_state")
        if live is None:
            live = live_state_map()
        watermarks, recent_counts = self._ops_maps(instance)
        data.update(
            account_ops(
                instance, live, watermarks=watermarks, recent_counts=recent_counts
            )
        )
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

    @staticmethod
    def _extras_of(row) -> dict:
        extras = getattr(row, "extras", None)
        return extras if isinstance(extras, dict) else {}

    def _asset_map(self, obj) -> dict:
        """remote photo/video URL → local path, for the whole page in one query.

        Same reasoning as `TwitterUserSerializer._archived_avatars`, which was
        already batched: resolving this per row meant one MediaAsset query per
        tweet on the feed -- the single hottest endpoint here, and one the
        console re-polls every 30 seconds.
        """
        cache = getattr(self, "_asset_cache", None)
        if cache is not None:
            return cache
        rows = page_rows(self)
        if not rows:
            # Single-object serializer: no page to batch over, and caching a map
            # built from this row would be wrong for the next one.
            return lookup_local_urls(photo_urls_from_extras(self._extras_of(obj)))
        urls = [url for row in rows for url in photo_urls_from_extras(self._extras_of(row))]
        cache = self._asset_cache = lookup_local_urls(urls)
        return cache

    def _rewritten_extras(self, obj) -> dict:
        """Prefer locally archived photo URLs; fall back to the X URL."""
        cache = getattr(self, "_rewritten_extras_cache", None)
        if cache is None:
            self._rewritten_extras_cache = cache = {}
        key = obj.pk
        if key in cache:
            return cache[key]
        extras = self._extras_of(obj)
        # The map covers the whole page; rewrite_media_blob only swaps URLs it
        # actually finds, so passing more than this row needs is harmless.
        assets = self._asset_map(obj)
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

    def _last_runs(self, obj):
        """pk → FetchRun for every search on the page, in one query.

        The viewset annotates `last_run_pk` (a correlated subquery) for exactly
        this; without the batch each row still cost its own `.runs.first()`, and
        the console re-polls this list every 20 seconds.
        """
        cache = getattr(self, "_last_run_cache", None)
        if cache is not None:
            return cache
        rows = page_rows(self) or [obj]
        pks = [
            pk for pk in (getattr(row, "last_run_pk", None) for row in rows) if pk
        ]
        cache = self._last_run_cache = {
            run.pk: run for run in FetchRun.objects.filter(pk__in=pks)
        }
        return cache

    def get_last_run(self, obj):
        # `last_run_pk` is absent on a bare SearchSerializer(search); fall back to
        # the direct lookup so this stays usable outside the list view.
        if hasattr(obj, "last_run_pk"):
            run = self._last_runs(obj).get(obj.last_run_pk)
        else:
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
