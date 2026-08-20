from rest_framework import serializers

from fetching.accounts import account_ops, live_state_map

from .models import FetchRun, Search, Tweet, TwitterUser


class TwitterUserSerializer(serializers.ModelSerializer):
    class Meta:
        model = TwitterUser
        fields = [
            "id", "handle", "display_name", "avatar_url", "verified", "verified_type",
            "rest_id", "tracking", "priority", "quarantined", "quarantine_reason",
        ]


class AccountOpsSerializer(TwitterUserSerializer):
    poll_interval_seconds = serializers.IntegerField(read_only=True)
    live_window_hours = serializers.IntegerField(read_only=True)
    historical_window_days = serializers.IntegerField(read_only=True)
    last_checked_at = serializers.DateTimeField(read_only=True, allow_null=True)
    last_status = serializers.CharField(read_only=True)
    recent_tweet_count = serializers.IntegerField(read_only=True)
    watermarks = serializers.DictField(read_only=True)

    class Meta(TwitterUserSerializer.Meta):
        fields = TwitterUserSerializer.Meta.fields + [
            "poll_interval_seconds",
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


class TweetSerializer(serializers.ModelSerializer):
    author = TwitterUserSerializer(read_only=True)
    media = serializers.SerializerMethodField()
    card = serializers.SerializerMethodField()
    reply_to = serializers.SerializerMethodField()
    quoted_tweet = serializers.SerializerMethodField()
    retweeted_tweet = serializers.SerializerMethodField()
    possibly_sensitive = serializers.SerializerMethodField()

    class Meta:
        model = Tweet
        fields = [
            "id", "tweet_id", "account", "text", "url", "type", "created_at",
            "author", "likes", "retweets", "replies", "quotes", "bookmarks", "views",
            "source_language", "entities", "media", "card", "reply_to",
            "quoted_tweet", "retweeted_tweet", "possibly_sensitive",
        ]

    def _extra(self, obj: Tweet, key: str, default=None):
        extras = obj.extras if isinstance(obj.extras, dict) else {}
        if key in extras:
            return extras.get(key, default)
        payload = obj.payload if isinstance(getattr(obj, "payload", None), dict) else {}
        return payload.get(key, default)

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


class SearchSerializer(serializers.ModelSerializer):
    name = serializers.CharField(required=False, allow_blank=True)

    class Meta:
        model = Search
        fields = [
            "id", "name", "slug", "raw_query", "product", "pagination_depth", "rolling_hours",
            "enabled", "last_run_at", "created_at",
        ]
        read_only_fields = ["slug", "last_run_at", "created_at"]


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
