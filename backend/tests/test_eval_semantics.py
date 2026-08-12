from tests.eval.run_eval import _semantic_expectation_passes


def inventory_case(query="Is the black midi dress in a medium?"):
    return {
        "query": query,
        "must_contain_any": ["out of stock", "medium"],
    }


def test_medium_and_m_are_treated_equivalently():
    assert _semantic_expectation_passes(
        inventory_case(),
        "Black Midi Dress is not currently available in size M in black.",
        [],
    )


def test_unavailable_not_currently_available_and_out_of_stock_are_accepted():
    case = inventory_case()

    assert _semantic_expectation_passes(case, "Black Midi Dress is unavailable in medium.", [])
    assert _semantic_expectation_passes(case, "Black Midi Dress is not currently available in size M.", [])
    assert _semantic_expectation_passes(case, "Black Midi Dress is out of stock in size medium.", [])


def test_wrong_size_still_fails():
    assert not _semantic_expectation_passes(
        inventory_case(),
        "Black Midi Dress is out of stock in size large.",
        [],
    )


def test_available_true_still_fails():
    assert not _semantic_expectation_passes(
        inventory_case(),
        "Black Midi Dress is available in size medium.",
        [],
    )


def test_semantic_checks_do_not_hide_non_inventory_failure():
    assert not _semantic_expectation_passes(
        {"query": "Recommend a dress under $80.", "must_contain_any": ["dress"]},
        "Here is a safe fallback.",
        [],
    )
