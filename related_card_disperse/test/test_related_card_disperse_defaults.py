"""The default sibling dispersal: rules derived from note types, never stored."""

from related_card_disperse.configuration import (
    DEFAULT_RELATED_CARD_QUERY,
    Config,
    default_rule,
    derived_sibling_rule,
    derived_sibling_rules,
    is_derived_rule,
    note_type_has_siblings,
    targeted_note_type_names,
)
from related_card_disperse.logic import get_applicable_rules

CLOZE = 1
STANDARD = 0


def make_model(name, templates=1, model_type=STANDARD, model_id=None):
    return {
        "id": model_id if model_id is not None else abs(hash(name)),
        "name": name,
        "type": model_type,
        "tmpls": [{"name": f"Card {i + 1}"} for i in range(templates)],
    }


def make_rule(**overrides):
    rule = default_rule()
    rule.update(overrides)
    return rule


def make_config(rules, enabled=True):
    config = Config()
    config.data["rules"] = rules
    config.data["disperse_siblings_default"] = enabled
    return config


def test_only_note_types_with_more_than_one_card_qualify():
    assert not note_type_has_siblings(make_model("Basic"))
    assert note_type_has_siblings(make_model("Basic (and reversed)", templates=2))


def test_cloze_qualifies_on_its_single_template():
    """A cloze note type's cards come from the fields, not from its templates."""
    assert note_type_has_siblings(make_model("Cloze", templates=1, model_type=CLOZE))


def test_derived_rule_disperses_the_notes_own_cards():
    rule = derived_sibling_rule(make_model("Cloze", model_type=CLOZE))
    assert rule["related_card_query"] == DEFAULT_RELATED_CARD_QUERY
    assert rule["target_note_types"] == '"Cloze"'
    assert rule["target_card_types"] == ""
    assert rule["enabled"] and rule["on_review"] and rule["on_sync"]
    assert is_derived_rule(rule)
    assert not is_derived_rule(default_rule())


def test_derived_guids_are_stable_and_distinct():
    """Runs are keyed by guid, so a note type's derived rule needs its own, every time."""
    model = make_model("Cloze", model_type=CLOZE, model_id=17)
    other = make_model("Reversed", templates=2, model_id=18)
    assert derived_sibling_rule(model)["guid"] == derived_sibling_rule(model)["guid"]
    assert derived_sibling_rule(model)["guid"] != derived_sibling_rule(other)["guid"]


def test_stored_rules_take_their_note_type_out_of_the_default():
    models = [make_model("Cloze", model_type=CLOZE), make_model("Reversed", templates=2)]
    stored = [make_rule(target_note_types='"Cloze"')]
    assert [r["name"] for r in derived_sibling_rules(stored, models)] == ["Reversed"]


def test_a_disabled_stored_rule_opts_its_note_type_out_too():
    """How the dialog turns the default off for one note type: save it disabled."""
    models = [make_model("Cloze", model_type=CLOZE)]
    stored = [make_rule(target_note_types='"Cloze"', enabled=False)]
    assert derived_sibling_rules(stored, models) == []
    assert targeted_note_type_names(stored) == {"Cloze"}


def test_one_card_note_types_never_get_a_default():
    models = [make_model("Basic"), make_model("Basic (typing)")]
    assert derived_sibling_rules([], models) == []


def test_derived_rules_are_ordered_by_name():
    models = [make_model("zeta", templates=2), make_model("Alpha", templates=2)]
    assert [r["name"] for r in derived_sibling_rules([], models)] == ["Alpha", "zeta"]


def test_rules_for_model_adds_the_default_only_where_it_applies():
    cloze = make_model("Cloze", model_type=CLOZE)
    basic = make_model("Basic")
    config = make_config([])
    assert [r["name"] for r in config.rules_for_model(cloze)] == ["Cloze"]
    assert config.rules_for_model(basic) == []
    assert config.rules_for_model(None) == []


def test_rules_for_model_leaves_a_covered_note_type_alone():
    cloze = make_model("Cloze", model_type=CLOZE)
    stored = [make_rule(name="mine", target_note_types='"Cloze"')]
    config = make_config(stored)
    assert config.rules_for_model(cloze) == stored


def test_rules_for_model_adds_nothing_while_the_toggle_is_off():
    config = make_config([], enabled=False)
    assert config.rules_for_model(make_model("Cloze", model_type=CLOZE)) == []
    assert not config.has_any_rules()


def test_has_any_rules_counts_the_toggle():
    assert make_config([]).has_any_rules()
    assert make_config([make_rule()], enabled=False).has_any_rules()


def test_a_derived_rule_runs_for_every_card_type_of_its_note_type():
    """It targets no card types, so nothing gates which card anchors a run."""
    config = make_config([])
    rules = config.rules_for_model(make_model("Reversed", templates=2))
    for card_type in ("Card 1", "Card 2"):
        assert get_applicable_rules(rules, "Reversed", card_type) == rules
    assert get_applicable_rules(rules, "Reversed", "Card 1", on_review=True) == rules
    assert get_applicable_rules(rules, "Reversed", "Card 1", on_sync=True) == rules


def test_derived_rules_never_reach_the_config(monkeypatch):
    """The dialog filters them out; the config layer refuses them regardless."""
    config = Config()
    monkeypatch.setattr(config, "save", lambda: None)
    config.replace_rules(
        [
            make_rule(name="mine", target_note_types='"Basic"'),
            derived_sibling_rule(make_model("Cloze", model_type=CLOZE)),
        ]
    )
    assert [r["name"] for r in config.rules] == ["mine"]
