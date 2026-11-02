import pytest

from core.intent_recognizer import IntentCategory, IntentRecognizer


def make_recognizer(threshold=0.5):
    recognizer = IntentRecognizer.__new__(IntentRecognizer)
    recognizer._embedding_enabled = True
    recognizer.threshold = threshold
    return recognizer


def vote(recognizer, llm, emb, pat):
    return recognizer._vote(
        {"intent": llm[0], "confidence": llm[1]},
        {"intent": emb[0], "confidence": emb[1]},
        {"intent": pat[0], "confidence": pat[1]},
    )


def test_pattern_cannot_refine_a_different_intent_group():
    intent, confidence, sources = vote(
        make_recognizer(),
        (IntentCategory.ACCESS, 0.9),
        (IntentCategory.OTHER, 0.0),
        (IntentCategory.APPLICATION_ERROR, 0.5),
    )

    assert intent is IntentCategory.ACCESS
    assert confidence == pytest.approx(0.63)
    assert "refined_by_pattern" not in sources


def test_coarse_and_fine_scores_choose_the_same_group_together():
    intent, confidence, sources = vote(
        make_recognizer(threshold=0.2),
        (IntentCategory.ACCESS, 0.3),       # weighted score 0.21
        (IntentCategory.APPLICATION_ERROR, 0.9),  # weighted score 0.18
        (IntentCategory.INCIDENT, 0.4),     # weighted score 0.04
    )

    assert intent is IntentCategory.APPLICATION_ERROR
    assert confidence == pytest.approx(0.22)
    assert "refined_by_pattern" not in sources


def test_pattern_can_still_refine_within_the_winning_group():
    intent, confidence, sources = vote(
        make_recognizer(),
        (IntentCategory.INCIDENT, 0.8),
        (IntentCategory.OTHER, 0.0),
        (IntentCategory.APPLICATION_ERROR, 0.5),
    )

    assert intent is IntentCategory.APPLICATION_ERROR
    assert confidence == pytest.approx(0.61)
    assert sources["refined_by_pattern"] == pytest.approx(0.5)
