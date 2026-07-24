from django.contrib import admin

from .models import Follow, SearchSubscription


@admin.register(Follow)
class FollowAdmin(admin.ModelAdmin):
    list_display = ("user", "account", "created_at")
    search_fields = ("user__username", "account__handle")


@admin.register(SearchSubscription)
class SearchSubscriptionAdmin(admin.ModelAdmin):
    list_display = ("user", "search", "created_at")
