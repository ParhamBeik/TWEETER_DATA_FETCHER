from rest_framework import serializers

from apps.tweets.models import TwitterUser

from .models import Follow


class FollowSerializer(serializers.ModelSerializer):
    handle = serializers.CharField(source="account.handle", read_only=True)
    display_name = serializers.CharField(source="account.display_name", read_only=True)

    class Meta:
        model = Follow
        fields = ["id", "handle", "display_name", "created_at"]
