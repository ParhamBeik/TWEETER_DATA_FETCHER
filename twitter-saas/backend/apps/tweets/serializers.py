from rest_framework import serializers

from .models import Search, Tweet, TwitterUser


class TwitterUserSerializer(serializers.ModelSerializer):
    class Meta:
        model = TwitterUser
        fields = ["id", "handle", "display_name", "rest_id", "tracking"]


class TweetSerializer(serializers.ModelSerializer):
    class Meta:
        model = Tweet
        fields = [
            "id", "tweet_id", "account", "text", "url", "type", "created_at",
            "likes", "retweets", "replies", "quotes", "views",
            "source_language", "source_endpoint", "entities",
        ]


class SearchSerializer(serializers.ModelSerializer):
    # Optional: the view derives a name from raw_query when omitted.
    name = serializers.CharField(required=False, allow_blank=True)

    class Meta:
        model = Search
        fields = ["id", "name", "slug", "raw_query", "product", "enabled", "last_run_at", "created_at"]
        read_only_fields = ["slug", "last_run_at", "created_at"]
