"""Paused collection preserves state while browsing, configuration and exports work."""
from datetime import timedelta
from unittest.mock import Mock, patch

import pytest
from django.contrib.auth.models import User
from django.utils import timezone
from rest_framework.test import APIClient

from config.celery import setup_periodic_tasks
from fetching import tasks
from fetching.ingest import upsert_tweet
from fetching.media import archive_batch
from fetching.runner import run_fetcher
from tweets.models import EndpointState, FetchRun, Search, Tweet, TwitterUser


@pytest.mark.django_db
def test_archive_mode_preserves_state_and_serves_data(settings, tmp_path):
    settings.INGESTION_ENABLED = False
    settings.MEDIA_ROOT = tmp_path
    client = APIClient()
    client.force_authenticate(User.objects.create_user('operator', is_staff=True))
    with patch('fetching.tasks.run_search.delay') as search_delay, \
         patch('fetching.tasks.fetch_account_live.delay') as live_delay, \
         patch('fetching.tasks.fetch_account_historical.delay') as history_delay:
        account = client.post('/api/accounts/', {'handle': 'jack'}, format='json')
        saved = client.post('/api/searches/', {'name': 'Archive', 'raw_query': 'test'}, format='json')
        assert account.status_code == saved.status_code == 201
        search = Search.objects.get(pk=saved.data['id'])
        for route, body in [
            ('/api/accounts/jack/fetch/', {}),
            (f'/api/searches/{search.pk}/refresh/', {}),
            ('/api/cycles/', {'subsystem': 'live'}),
            ('/api/cycles/', {'subsystem': 'historical'}),
            ('/api/cycles/', {'subsystem': 'search'}),
        ]:
            response = client.post(route, body, format='json')
            assert response.status_code == 409
            assert response.data['code'] == 'ingestion_disabled'
        search_delay.assert_not_called()
        live_delay.assert_not_called()
        history_delay.assert_not_called()
    search.queued_task_id = 'preserved'
    search.last_run_at = timezone.now() - timedelta(days=100)
    search.save()
    state = EndpointState.objects.create(account='jack', endpoint='UserTweets', data={'backfill_cursor': 'preserved'})
    with patch('fetching.runner.subprocess.Popen') as process, patch('fetching.media.archive_batch') as media:
        for task, args in [
            (tasks.fetch_account_live, ('jack',)), (tasks.fetch_account_historical, ('jack',)),
            (tasks.poll_live_all, ()), (tasks.backfill_historical_all, ()),
            (tasks.run_search, (search.pk,)), (tasks.dispatch_due_searches, ()),
            (tasks.repoll_searches, ()), (tasks.archive_media, ()),
            (tasks.purge_expired_search_tweets, ()), (tasks.purge_old_fetch_runs, ()),
            (tasks.purge_old_raw_pages, ()), (tasks.purge_old_tweet_metrics, ()),
            (tasks.recompute_poll_intervals, ()),
        ]:
            assert task(*args) == 0
        with pytest.raises(RuntimeError, match='ingestion_disabled'):
            run_fetcher('engine.live', ['--once'], 'live')
        process.assert_not_called()
        media.assert_not_called()
    assert archive_batch(25) == 0
    search.refresh_from_db()
    state.refresh_from_db()
    assert search.queued_task_id == 'preserved'
    assert search.last_run_at is not None
    assert state.data == {'backfill_cursor': 'preserved'}
    assert FetchRun.objects.count() == 0
    schedule = client.get(f'/api/searches/{search.pk}/schedule/').data
    assert schedule['state'] == 'paused' and not schedule['is_due']
    assert client.get('/api/auth/config/').data['ingestion_enabled'] is False
    sender = Mock()
    setup_periodic_tasks(sender)
    sender.add_periodic_task.assert_not_called()

    upsert_tweet({'rest_id': '123', 'author_id': '1', 'account': 'jack', 'text': 'Saved locally',
                  'created_at': 'Wed Oct 10 20:19:24 +0000 2018'})
    TwitterUser.objects.filter(handle='jack').update(tracking=True)
    assert len(client.get('/api/feed/').data['results']) == 1
    export = client.post('/api/export/', {'format': 'jsonl'}, format='json')
    assert export.status_code == 202 and export.data['status'] == 'completed'
    assert export.data['row_count'] == Tweet.objects.count() == 1
    assert client.get(export.data['download_url']).status_code == 200
