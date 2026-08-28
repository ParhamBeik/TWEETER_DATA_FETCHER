"""Unit tests for the topic ranking.

Unit level, and no database at all: tweets.topics is pure arithmetic over rows
the grouping SQL hands it, with no collaborators to integrate against. That is
also the point of extracting it -- the raw-SQL analytics paths are skipped
entirely on the SQLite test database, so ranking logic left inside the query
would have had no coverage whatsoever.
"""
import pytest

from tweets.topics import MAX_DOC_RATIO, TermStats, rank_terms


def _rank(candidates, total=1000, previous_total=1000, **kwargs):
    return [
        row["topic"]
        for row in rank_terms(
            candidates, total_docs=total, previous_total_docs=previous_total, **kwargs
        )
    ]


def test_filler_loses_to_a_term_that_actually_moved():
    """The headline failure: common verbs used to own the top of the chart.

    "said" is in six times as many posts as "gold price", and still loses,
    because it is in exactly as many as it always was.
    """
    filler = TermStats("said", "phrase", docs=60, authors=25, previous_docs=58)
    surge = TermStats("gold price", "phrase", docs=10, authors=6, previous_docs=1)

    assert _rank([filler, surge])[0] == "gold price"


def test_corpus_wide_filler_is_dropped_without_a_word_list():
    """A term in a quarter of all posts is structural, in any language.

    This is what covers the non-English archive: the old hand-typed English
    stopword list could never have caught a Persian or Arabic equivalent.
    """
    ratio_breaker = TermStats("چیزی", "phrase", docs=300, authors=40, previous_docs=300)

    assert MAX_DOC_RATIO < 0.3  # the rule below only means anything under 1.0
    assert _rank([ratio_breaker]) == []


def test_one_account_cannot_invent_a_trend():
    lonely = TermStats("mycatname", "phrase", docs=40, authors=1, previous_docs=0)
    shared = TermStats("port strike", "phrase", docs=8, authors=5, previous_docs=0)

    assert _rank([lonely, shared]) == ["port strike"]


def test_a_term_seen_twice_is_not_a_topic():
    assert _rank([TermStats("blip", "phrase", docs=2, authors=2, previous_docs=0)]) == []


def test_the_bigram_survives_and_takes_its_fragments_with_it():
    """"gold", "price" and "gold price" used to be three separate rows."""
    candidates = [
        TermStats("gold", "phrase", docs=13, authors=6, previous_docs=1),
        TermStats("price", "phrase", docs=12, authors=6, previous_docs=1),
        TermStats("gold price", "phrase", docs=12, authors=6, previous_docs=1),
    ]

    assert _rank(candidates) == ["gold price"]


def test_a_unigram_with_a_life_of_its_own_is_kept():
    """Containment is about explaining usage, not about sharing a word."""
    candidates = [
        TermStats("gold", "phrase", docs=40, authors=15, previous_docs=4),
        TermStats("gold price", "phrase", docs=12, authors=6, previous_docs=1),
    ]

    assert sorted(_rank(candidates)) == ["gold", "gold price"]


def test_containment_matches_whole_words_only():
    """Substring matching alone would call "art" a fragment of "start up"."""
    candidates = [
        TermStats("art", "phrase", docs=10, authors=5, previous_docs=1),
        TermStats("start up", "phrase", docs=10, authors=5, previous_docs=1),
    ]

    assert len(_rank(candidates)) == 2


def test_hashtags_and_phrases_do_not_suppress_each_other():
    candidates = [
        TermStats("gold", "hashtag", docs=12, authors=6, previous_docs=1),
        TermStats("gold price", "phrase", docs=12, authors=6, previous_docs=1),
    ]

    assert len(_rank(candidates)) == 2


def test_volume_ordering_still_answers_the_other_question():
    steady = TermStats("markets", "phrase", docs=30, authors=9, previous_docs=29)
    surge = TermStats("port strike", "phrase", docs=8, authors=4, previous_docs=1)

    assert _rank([steady, surge], order="volume") == ["markets", "port strike"]
    assert _rank([steady, surge]) == ["port strike", "markets"]


def test_a_hidden_term_never_reaches_the_chart():
    surge = TermStats("Gold Price", "phrase", docs=12, authors=6, previous_docs=1)

    assert _rank([surge], blocklist={"gold price"}) == []


def test_support_scales_confidence():
    """Both terms tripled; the one with real support should rank higher.

    Ratio alone would tie them, which is why the score is z-scored by its
    variance rather than being a bare log-odds.
    """
    thin = TermStats("thin", "phrase", docs=3, authors=2, previous_docs=1)
    thick = TermStats("thick", "phrase", docs=60, authors=20, previous_docs=20)

    assert _rank([thin, thick], total=5000, previous_total=5000)[0] == "thick"


def test_rows_carry_the_numbers_that_justify_them():
    rows = rank_terms(
        [TermStats("port strike", "phrase", docs=10, authors=5, previous_docs=2)],
        total_docs=1000,
        previous_total_docs=1000,
    )

    assert rows[0]["docs"] == 10
    assert rows[0]["authors"] == 5
    assert rows[0]["previous_docs"] == 2
    assert rows[0]["delta"] == 8
    assert rows[0]["share"] == pytest.approx(0.01)
    assert rows[0]["baseline_share"] == pytest.approx(0.002)


def test_an_empty_corpus_does_not_divide_by_zero():
    assert rank_terms([], total_docs=0, previous_total_docs=0) == []
