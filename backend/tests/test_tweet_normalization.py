from fetcher.processing import TweetSetProcessor


def test_current_user_note_and_video_shapes_are_normalized():
    tweet = {
        "rest_id": "123",
        "core": {"user_results": {"result": {
            "rest_id": "42",
            "core": {"screen_name": "DailyLoud", "name": "Daily Loud"},
            "avatar": {"image_url": "https://img.example/avatar.jpg"},
            "verification": {"verified": True, "verified_type": "Business"},
        }}},
        "legacy": {
            "created_at": "Sun Aug 09 12:00:00 +0000 2026",
            "full_text": "truncated",
            "favorite_count": 5,
            "extended_entities": {"media": [{
                "id_str": "m1",
                "type": "video",
                "media_url_https": "https://img.example/preview.jpg",
                "original_info": {"width": 1280, "height": 720},
                "video_info": {"duration_millis": 1200, "variants": [{
                    "url": "https://video.example/video.mp4",
                    "content_type": "video/mp4",
                    "bitrate": 832000,
                }]},
            }]},
        },
        "note_tweet": {"note_tweet_results": {"result": {"text": "complete note text"}}},
    }

    normalized = TweetSetProcessor()._normalize_tweet(tweet, source_endpoint="SearchTimeline")

    assert normalized["account"] == "DailyLoud"
    assert normalized["author_display_name"] == "Daily Loud"
    assert normalized["author_avatar_url"] == "https://img.example/avatar.jpg"
    assert normalized["author_verified"] is True
    assert normalized["text"] == "complete note text"
    assert normalized["media"][0]["variants"][0]["content_type"] == "video/mp4"


def test_legacy_user_shape_remains_supported():
    tweet = {
        "rest_id": "456",
        "core": {"user_results": {"result": {
            "rest_id": "84",
            "legacy": {
                "screen_name": "legacy_user",
                "name": "Legacy User",
                "profile_image_url_https": "https://img.example/legacy.jpg",
                "verified": True,
            },
        }}},
        "legacy": {"full_text": "legacy tweet"},
    }

    normalized = TweetSetProcessor()._normalize_tweet(tweet)

    assert normalized["account"] == "legacy_user"
    assert normalized["author"]["display_name"] == "Legacy User"
    assert normalized["author"]["verified"] is True
