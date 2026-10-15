import logging

import pytest

from news_officer.daily_report import select_daily_episodes
from news_officer.models import DailyItem, Episode


def item(index, priority='B', advice='值得看全文', show=None, stars=4):
    return DailyItem(Episode(str(index), f'Episode {index}', f'https://example.org/{index}',
                            show or f'Show {index}', metadata={'source_priority': priority}),
                     'summarized', f'阅读建议：{advice}\n推荐星级：' + '★' * stars + '☆' * (5-stars))


@pytest.fixture(autouse=True)
def default_cap(monkeypatch):
    monkeypatch.delenv('NEWS_OFFICER_DAILY_MAX_EPISODES', raising=False)


def test_source_dedupe_ranking_and_unchanged(caplog):
    candidates = [item(0, show='Same'), item(1, 'A', show='same'),
                  item(2, 'A', '值得编译', show='Same'),
                  item(3, 'A', '值得编译', show='Same', stars=5)]
    before = [i.to_persisted_dict() for i in candidates]
    with caplog.at_level(logging.INFO):
        assert select_daily_episodes(candidates) == [candidates[3]]
    assert 'per-source cap dropped' in caplog.text
    assert before == [i.to_persisted_dict() for i in candidates]


def test_default_cap_and_log(caplog):
    with caplog.at_level(logging.INFO):
        assert len(select_daily_episodes([item(i) for i in range(9)])) == 5
    assert 'total cap dropped 5' in caplog.text


@pytest.mark.parametrize('priority,advice,expected', [
    ('A', '值得编译', 6), ('B', '值得编译', 5), ('A', '值得看全文', 5)])
def test_sixth_strict_bar_and_log(priority, advice, expected, caplog):
    candidates = [item(i, priority, advice) for i in range(8)]
    with caplog.at_level(logging.INFO):
        assert len(select_daily_episodes(candidates)) == expected
    assert ('sixth episode admitted' in caplog.text) == (expected == 6)
    if expected == 6:
        assert 'source priority A and reading level 值得编译' in caplog.text
        assert caplog.text.count('sixth episode admitted') == 1


@pytest.mark.parametrize('count', [0, 1, 2])
def test_no_padding(count):
    assert len(select_daily_episodes([item(i) for i in range(count)])) == count


@pytest.mark.parametrize('value,expected', [('3', 3), ('2', 2), ('99', 5), ('bad', 5)])
def test_env_override(monkeypatch, value, expected):
    monkeypatch.setenv('NEWS_OFFICER_DAILY_MAX_EPISODES', value)
    assert len(select_daily_episodes([item(i) for i in range(8)])) == expected
