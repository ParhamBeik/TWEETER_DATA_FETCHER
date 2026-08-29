"""Rank mined terms by how unusual they are, not by how often they occur.

The previous version ranked by `count(*)` over token occurrences, which is why
the panel filled with filler verbs and meaningless word pairs:

- a word repeated five times in one post scored five;
- a viral repost duplicated across 200 rows scored 200;
- a unigram always outscored the bigram it lives inside, so "gold", "price" and
  "gold price" all appeared as three separate "topics";
- the stopword list was 100 hand-typed English words against a deliberately
  multilingual archive, so nothing filtered Persian or Arabic filler at all;
- and the previous-window delta was computed but never used for ranking, on a
  page whose own headline asks what is *accelerating*.

Volume answers "what does this corpus talk about", which for a fixed set of
tracked accounts is the same boring answer every day. Surprise answers "what
changed", which is the only question worth a chart.

Deliberately pure: the SQL in analytics.py does the grouping, everything here is
arithmetic over the rows it returns, so the whole ranking is testable without
Postgres. That matters -- the raw-SQL analytics paths are skipped entirely on the
SQLite test database.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

# A term has to appear in this many distinct posts, from this many distinct
# accounts, before it is a topic rather than one person's turn of phrase. The
# author floor is the cheap defence against a single chatty account inventing a
# trend by itself. Raised from 3/2: at those floors a term in 7 posts from 3
# accounts was being published as "8.2x its usual rate", which is a number the
# sample size cannot support.
MIN_DOCS = 5
MIN_AUTHORS = 3

# Hashtags are an explicit authorial signal, not a term mined out of prose, so
# they need less corroboration to be worth showing. They are also rare here --
# about 1.7% of this archive uses one -- so the phrase floors would leave the
# hashtag dimension permanently empty rather than selective.
KIND_FLOORS = {"hashtag": (2, 2)}

# Anything in more than this share of documents is structural filler in whatever
# language it happens to be. Derived from the corpus, so it needs no word list
# and covers every language the archive collects -- which is the whole reason the
# hand-typed English list could never work here.
#
# Was 0.25, which let almost everything through: "says" in 61 of 891 posts is
# 6.8%, so filler cleared the bar by a factor of four and the panel filled with
# month/king/chair/big/long/comes. A term carried by more than one post in
# twenty is the corpus talking, not a topic.
MAX_DOC_RATIO = 0.05

# Drop the shorter term when a longer one accounts for this much of its usage.
# "gold" appearing in 40 posts of which 34 also say "gold price" is not a topic
# in its own right.
#
# Was 0.8, which was too strict to merge the case that actually shows up: one Fed
# story surfaced as five rows (fed, chair, warsh, kevin warsh, fed chair) because
# "kevin warsh" covered 22 of "warsh"'s 35 posts and 22 < 0.8*35. At 0.5 the
# bigram absorbs its fragments and the story appears once.
CONTAINMENT = 0.5

# Smoothing for the log-odds. Without it a term absent from the previous window
# divides by zero, and every brand-new term would tie at infinity regardless of
# whether it appeared in four posts or four hundred.
PRIOR = 0.5


@dataclass(frozen=True)
class TermStats:
    """One candidate term as the grouping SQL returns it."""

    term: str
    kind: str  # "hashtag" | "phrase"
    docs: int  # distinct posts in the current window
    authors: int  # distinct accounts in the current window
    previous_docs: int  # distinct posts in the preceding window of equal length


def _log_odds_z(docs: int, total: int, previous_docs: int, previous_total: int) -> float:
    """How surprising this term's current rate is against its own baseline.

    A log-odds ratio with an informative Dirichlet prior, z-scored by its
    variance (Monroe, Colaresi & Quinn, "Fightin' Words"). Two properties are
    what make it the right tool here:

    - it is a *rate* comparison, so a common verb cannot win by being common --
      its baseline rate is already high, and matching it scores zero;
    - the variance term scales with support, so a term seen in 4 posts and one
      seen in 400 are not treated as equally confident when both tripled.
    """
    a = docs + PRIOR
    b = max(total - docs, 0) + PRIOR
    c = previous_docs + PRIOR
    d = max(previous_total - previous_docs, 0) + PRIOR
    delta = math.log(a / b) - math.log(c / d)
    variance = 1 / a + 1 / b + 1 / c + 1 / d
    return delta / math.sqrt(variance)


def _tokens(term: str) -> list[str]:
    return term.split(" ")


def _suppress_nested(rows: list[dict]) -> list[dict]:
    """Drop a term whose usage a longer, better-scoring term already explains.

    Compares document counts rather than document sets -- the sets would mean
    carrying every post id per term out of the database, and the counts settle it
    for the case that actually happens: a unigram whose count barely exceeds the
    bigram containing it is that bigram, seen one word at a time.

    ponytail: unigram/bigram only. Trigrams are the upgrade path if bigram rows
    still read as fragments; nothing else changes if they are added.
    """
    # Longest first, then best scoring. Length leads deliberately: when a unigram
    # and the bigram containing it have near-identical support, the bigram is the
    # informative one even though the unigram usually scores a hair higher for
    # having a few extra documents. Sorting by score alone kept "gold" and
    # discarded "gold price", which is the exact failure being fixed.
    ordered = sorted(rows, key=lambda row: (-len(_tokens(row["topic"])), -row["score"]))
    kept: list[dict] = []
    for row in ordered:
        parts = _tokens(row["topic"])
        redundant = False
        for winner in kept:
            if winner["kind"] != row["kind"]:
                continue
            winner_parts = _tokens(winner["topic"])
            if len(parts) >= len(winner_parts) or not _is_word_run(parts, winner_parts):
                continue
            # The longer term explains most of this one's posts, so this row adds
            # nothing but a second entry for the same story.
            if winner["docs"] >= CONTAINMENT * row["docs"]:
                redundant = True
                break
        if not redundant:
            kept.append(row)
    return kept


def _is_word_run(shorter: list[str], longer: list[str]) -> bool:
    """True when `shorter` appears in `longer` as consecutive whole words.

    Substring matching alone would call "art" a fragment of "start up".
    """
    span = len(shorter)
    return any(longer[i:i + span] == shorter for i in range(len(longer) - span + 1))


def rank_terms(
    candidates: list[TermStats],
    *,
    total_docs: int,
    previous_total_docs: int,
    blocklist: set[str] | None = None,
    order: str = "surging",
    limit: int = 50,
) -> list[dict]:
    """Turn raw term counts into the ranked rows the console renders.

    Filters run cheapest-first, and every surviving row carries the numbers that
    justify it -- `docs`, `authors` and `baseline_share` are what let a reader
    decide the ranking is honest instead of taking the score on faith.
    """
    blocked = {term.lower() for term in (blocklist or set())}
    scored: list[dict] = []
    for row in candidates:
        if row.term.lower() in blocked:
            continue
        min_docs, min_authors = KIND_FLOORS.get(row.kind, (MIN_DOCS, MIN_AUTHORS))
        if row.docs < min_docs or row.authors < min_authors:
            continue
        # Structural filler, judged against both windows so a term is not
        # exempted just because it happens to be quiet right now.
        pooled_total = total_docs + previous_total_docs
        if pooled_total and (row.docs + row.previous_docs) / pooled_total > MAX_DOC_RATIO:
            continue
        scored.append({
            "topic": row.term,
            "kind": row.kind,
            "docs": row.docs,
            "authors": row.authors,
            "previous_docs": row.previous_docs,
            "delta": row.docs - row.previous_docs,
            "share": _share(row.docs, total_docs),
            "baseline_share": _share(row.previous_docs, previous_total_docs),
            "score": round(
                _log_odds_z(row.docs, total_docs, row.previous_docs, previous_total_docs), 3
            ),
        })

    scored = _suppress_nested(scored)
    if order == "volume":
        scored.sort(key=lambda row: (-row["docs"], row["topic"]))
    else:
        scored.sort(key=lambda row: (-row["score"], -row["docs"], row["topic"]))
    return scored[:limit]


def _share(docs: int, total: int) -> float:
    return round(docs / total, 5) if total else 0.0


def _self_check() -> None:
    """Runnable check for the three rules that make or break the ranking."""
    common = TermStats("said", "phrase", docs=60, authors=20, previous_docs=58)
    surge = TermStats("gold price", "phrase", docs=12, authors=6, previous_docs=1)
    fragment = TermStats("gold", "phrase", docs=13, authors=6, previous_docs=1)
    lonely = TermStats("mycatname", "phrase", docs=9, authors=1, previous_docs=0)
    rows = rank_terms(
        [common, surge, fragment, lonely], total_docs=200, previous_total_docs=200
    )
    topics = [row["topic"] for row in rows]
    # Filler is dropped by the corpus-frequency rule, not by a word list.
    assert "said" not in topics, topics
    # One account cannot make a trend by itself.
    assert "mycatname" not in topics, topics
    # The bigram survives and takes the unigram it explains with it.
    assert topics == ["gold price"], topics

    # Volume ordering still available, and still support-filtered. Totals are
    # large enough that "alpha" is a real term rather than structural filler --
    # at MAX_DOC_RATIO it is the ratio, not the raw count, that decides.
    pair = [TermStats("alpha", "phrase", 30, 9, 29), TermStats("beta", "phrase", 8, 4, 1)]
    volume = rank_terms(
        pair, total_docs=2000, previous_total_docs=2000, order="volume"
    )
    assert [row["topic"] for row in volume] == ["alpha", "beta"], volume
    # ...but surging ordering puts the term that actually moved first.
    surging = rank_terms(pair, total_docs=2000, previous_total_docs=2000)
    assert [row["topic"] for row in surging] == ["beta", "alpha"], surging

    # The filler rule is a rate, and it is strict enough to catch real filler:
    # "says" in 61 of 891 posts is 6.8% of the corpus, which is the corpus
    # talking. It must not reach the chart no matter how it scores.
    assert not rank_terms(
        [TermStats("says", "phrase", 61, 15, 40)],
        total_docs=891,
        previous_total_docs=1000,
    ), "filler at 6.8% of documents must be dropped"

    # Support floors, both of them: 4 posts is too thin regardless of spread,
    # and 2 accounts is too few regardless of volume.
    assert not rank_terms(
        [TermStats("debut", "phrase", 4, 4, 1)], total_docs=891, previous_total_docs=1000
    ), "below MIN_DOCS must be dropped"
    assert not rank_terms(
        [TermStats("chip", "phrase", 10, 2, 1)], total_docs=891, previous_total_docs=1000
    ), "below MIN_AUTHORS must be dropped"

    # Hashtags clear a lower bar than mined phrases: the same support that is too
    # thin for a phrase is a deliberate label when someone typed it.
    thin = dict(docs=3, authors=2, previous_docs=0)
    assert not rank_terms(
        [TermStats("thin", "phrase", **thin)], total_docs=891, previous_total_docs=1000
    ), "a 3-post phrase is still below the phrase floor"
    assert rank_terms(
        [TermStats("thin", "hashtag", **thin)], total_docs=891, previous_total_docs=1000
    ), "the same support should qualify as a hashtag"

    # One story, one row: a bigram absorbs the unigram it explains even when the
    # unigram has noticeably more documents ("warsh" 35 vs "kevin warsh" 22).
    fed = rank_terms(
        [
            TermStats("warsh", "phrase", 35, 9, 2),
            TermStats("kevin warsh", "phrase", 22, 8, 1),
        ],
        total_docs=2000,
        previous_total_docs=2000,
    )
    assert [row["topic"] for row in fed] == ["kevin warsh"], fed

    # A unigram with support the bigram cannot explain stays: 12 of 40 posts is
    # not "gold is really just gold price".
    broad = rank_terms(
        [TermStats("gold", "phrase", 40, 15, 4), TermStats("gold price", "phrase", 12, 6, 1)],
        total_docs=900,
        previous_total_docs=900,
    )
    assert sorted(row["topic"] for row in broad) == ["gold", "gold price"], broad

    # Containment is whole words, not substrings: "art" is not part of "start up".
    unrelated = rank_terms(
        [TermStats("art", "phrase", 10, 5, 1), TermStats("start up", "phrase", 10, 5, 1)],
        total_docs=500,
        previous_total_docs=500,
    )
    assert len(unrelated) == 2, unrelated

    # A blocked term never reaches the chart regardless of how well it scores.
    assert not rank_terms([surge], total_docs=200, previous_total_docs=200,
                          blocklist={"Gold Price"})
    print("tweets.topics self-check passed")


if __name__ == "__main__":  # pragma: no cover - manual check
    _self_check()
