"""No network: timeout isolation and hard bounds for user-selected Astra work."""
from types import SimpleNamespace

import pytest

from fund_kb.settings import Settings
from fund_kb.wiki import _generation_timeout


@pytest.mark.parametrize("options,semantic,expected", [
    ({}, True, 150),
    ({"wiki_semantic_timeout_seconds": 100}, True, 100),
    ({"wiki_semantic_timeout_seconds": 300}, True, 180),
    ({"model_timeout_seconds": 1}, True, 150),
    ({"wiki_semantic_timeout_seconds": 150, "model_timeout_seconds": 600}, False, 45),
    ({"model_timeout_seconds": 20}, False, 20),
    ({}, False, 45),
])
def test_semantic_budget_is_independent_but_bounded(options, semantic, expected):
    assert _generation_timeout(SimpleNamespace(**options), semantic) == expected


def test_default_does_not_change_generic_or_answer_budgets():
    assert Settings.model_fields["wiki_semantic_timeout_seconds"].default == 150
    assert Settings.model_fields["model_timeout_seconds"].default == 60
    assert Settings.model_fields["answer_model_timeout_seconds"].default == 180
    # The finite compatibility budget is not used by the default Wiki reader.
    assert Settings.model_fields["answer_engine"].default == "wiki_reader"
