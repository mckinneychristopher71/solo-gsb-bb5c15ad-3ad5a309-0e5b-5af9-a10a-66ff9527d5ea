"""Unit tests that run with the Python standard library only.

Run with:  python3 -m unittest discover -s tests -v
"""

import os
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db as db_mod  # noqa: E402
from app import evaluator as evaluator_mod  # noqa: E402
from app.comparison import (  # noqa: E402
    build_reverse_graph,
    compare_versions,
    diff_flags,
    impact_analysis,
)
from app.evaluator import FlagEvaluator, evaluate_flag, hash_bucket  # noqa: E402
from app.preview import _json_equal, build_evaluation_preview  # noqa: E402
from app.validation import ConfigValidationError, validate_config  # noqa: E402


def flag(**kw):
    base = {"enabled": True, "default": False, "prerequisites": [], "rules": []}
    base.update(kw)
    return base


def rule(conditions, value=True, id=None):
    r = {"conditions": conditions, "value": value}
    if id is not None:
        r["id"] = id
    return r


class RuleEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.flags = {
            "beta": flag(
                rules=[
                    rule([{"attr": "tier", "op": "eq", "value": "gold"}], value="gold-banner", id="gold"),
                    rule([{"attr": "tier", "op": "in", "value": ["silver", "bronze"]}], id="tiers"),
                ],
                default="off",
            ),
        }

    def test_first_matching_rule_wins_in_order(self):
        res = evaluate_flag("beta", self.flags, attributes={"tier": "gold"})
        self.assertTrue(res["hit"])
        self.assertEqual(res["value"], "gold-banner")
        self.assertEqual(res["reason"], "rule_match")
        self.assertEqual(res["rule_id"], "gold")

    def test_second_rule_matches(self):
        res = evaluate_flag("beta", self.flags, attributes={"tier": "silver"})
        self.assertEqual(res["rule_id"], "tiers")

    def test_default_when_no_rule_matches(self):
        res = evaluate_flag("beta", self.flags, attributes={"tier": "free"})
        self.assertEqual(res["reason"], "default")
        self.assertEqual(res["value"], "off")
        # "off" is a non-empty string variant; hit is the boolean coercion of
        # the value. Boolean-style flags should use true/false defaults.
        self.assertTrue(res["hit"])

    def test_boolean_default_flag_is_off(self):
        flags = {"f": flag(default=False, rules=[rule([{"attr": "x", "op": "eq", "value": 1}])])}
        res = evaluate_flag("f", flags, attributes={"x": 0})
        self.assertEqual(res["reason"], "default")
        self.assertFalse(res["hit"])

    def test_missing_attribute_is_none(self):
        res = evaluate_flag("beta", self.flags, attributes={})
        self.assertEqual(res["reason"], "default")

    def test_comparison_and_string_ops(self):
        flags = {
            "f": flag(
                rules=[
                    rule([{"attr": "age", "op": "ge", "value": 18}], id="adult"),
                ]
            ),
            "p": flag(
                rules=[rule([{"attr": "email", "op": "suffix", "value": "@acme.com"}], id="acme")]
            ),
        }
        self.assertEqual(evaluate_flag("f", flags, attributes={"age": 21})["rule_id"], "adult")
        self.assertFalse(evaluate_flag("f", flags, attributes={"age": 5})["hit"])
        self.assertTrue(evaluate_flag("p", flags, attributes={"email": "a@acme.com"})["hit"])
        self.assertFalse(evaluate_flag("p", flags, attributes={"email": "a@other.com"})["hit"])

    def test_catch_all_rule_with_empty_conditions(self):
        flags = {"f": flag(rules=[{"conditions": [], "value": True, "id": "all"}])}
        self.assertEqual(evaluate_flag("f", flags, attributes={})["rule_id"], "all")


class DisabledFlagTests(unittest.TestCase):
    def test_disabled_returns_default_even_with_rules(self):
        flags = {"f": flag(enabled=False, default=False, rules=[rule([], id="r")])}
        res = evaluate_flag("f", flags, attributes={"x": 1})
        self.assertEqual(res["reason"], "disabled")
        self.assertFalse(res["hit"])


class PrerequisiteTests(unittest.TestCase):
    def setUp(self):
        self.flags = {
            "logged_in": flag(default=False, rules=[rule([{"attr": "uid", "op": "ne", "value": None}], id="has-uid")]),
            "v2_api": flag(
                prerequisites=[{"flag": "logged_in", "expect": True}],
                default=False,
                rules=[rule([{"attr": "tenant", "op": "eq", "value": "acme"}], id="acme")],
            ),
        }

    def test_chain_when_prerequisite_passes(self):
        res = evaluate_flag("v2_api", self.flags, attributes={"uid": "u-1", "tenant": "acme"})
        self.assertTrue(res["hit"])
        names = [n["flag"] for n in res["chain"]]
        self.assertEqual(names, ["logged_in", "v2_api"])
        self.assertEqual(res["chain"][0]["prerequisite_of"], "v2_api")
        self.assertTrue(res["chain"][0]["expected"])
        self.assertEqual(res["chain"][1]["reason"], "rule_match")

    def test_prerequisite_fails_short_circuits(self):
        res = evaluate_flag("v2_api", self.flags, attributes={"tenant": "acme"})
        self.assertFalse(res["hit"])
        self.assertEqual(res["reason"], "prerequisite_failed")
        # v2's rules must never have been evaluated
        self.assertEqual([n["flag"] for n in res["chain"]], ["logged_in", "v2_api"])
        self.assertEqual(res["chain"][1]["reason"], "prerequisite_failed")

    def test_negated_expect(self):
        flags = {
            "kill": flag(default=False),
            "feature": flag(
                default="on",
                prerequisites=[{"flag": "kill", "expect": False}],
            ),
        }
        res = evaluate_flag("feature", flags, attributes={})
        self.assertEqual(res["value"], "on")

    def test_deep_chain_is_leaf_first(self):
        flags = {
            "a": flag(default=True),
            "b": flag(default=True, prerequisites=[{"flag": "a"}]),
            "c": flag(default="yes", prerequisites=[{"flag": "b"}]),
        }
        res = evaluate_flag("c", flags, attributes={})
        self.assertEqual([n["flag"] for n in res["chain"]], ["a", "b", "c"])
        self.assertEqual(res["value"], "yes")

    def test_transitive_failure_chain_has_no_duplicate_flags(self):
        flags = {
            "a": flag(default=False),
            "b": flag(default=True, prerequisites=[{"flag": "a"}]),
            "c": flag(default="yes", prerequisites=[{"flag": "b"}]),
        }
        res = evaluate_flag("c", flags, attributes={})
        names = [n["flag"] for n in res["chain"]]
        self.assertEqual(names, ["a", "b", "c"])
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(res["reason"], "prerequisite_failed")
        self.assertEqual(res["chain"][0]["prerequisite_of"], "b")
        self.assertEqual(res["chain"][1]["reason"], "prerequisite_failed")
        self.assertEqual(res["chain"][2]["reason"], "prerequisite_failed")

    def test_diamond_dependency_evaluates_shared_flag_once(self):
        flags = {
            "a": flag(rules=[rule([{"attr": "tier", "op": "eq", "value": "gold"}], id="gold")]),
            "b": flag(default=True, prerequisites=[{"flag": "a"}]),
            "c": flag(default="on", prerequisites=[{"flag": "a"}, {"flag": "b"}]),
        }
        res = evaluate_flag("c", flags, attributes={"tier": "gold"})
        names = [n["flag"] for n in res["chain"]]
        self.assertEqual(names, ["a", "b", "c"])
        self.assertEqual(res["value"], "on")


class RolloutTests(unittest.TestCase):
    def test_stable_bucket_for_same_flag_and_user(self):
        b1 = hash_bucket("new_checkout", "user-42")
        b2 = hash_bucket("new_checkout", "user-42")
        self.assertEqual(b1, b2)
        self.assertIsInstance(b1, int)
        self.assertGreaterEqual(b1, 0)
        self.assertLess(b1, 100)

    def test_different_flags_distribute_independently(self):
        # Extremely unlikely that all flags collide for the same user.
        user = "user-42"
        buckets = {hash_bucket(f"flag-{i}", user) for i in range(20)}
        self.assertGreater(len(buckets), 1)

    def test_zero_percentage_never_in(self):
        flags = {"f": flag(rollout={"percentage": 0, "key": "user_id", "value": True})}
        for uid in range(50):
            res = evaluate_flag("f", flags, user_id=str(uid))
            self.assertFalse(res["hit"])
            self.assertEqual(res["reason"], "rollout_out")

    def test_hundred_percentage_always_in(self):
        flags = {"f": flag(rollout={"percentage": 100, "key": "user_id", "value": "x"})}
        for uid in range(50):
            res = evaluate_flag("f", flags, user_id=str(uid))
            self.assertEqual(res["reason"], "rollout_in")
            self.assertEqual(res["value"], "x")

    def test_inclusion_is_monotonic_as_percentage_grows(self):
        flags_cfg = lambda p: {"f": flag(rollout={"percentage": p, "key": "user_id", "value": True})}
        for uid in range(300):
            bucket = hash_bucket("f", str(uid))
            in_at = lambda p: evaluate_flag("f", flags_cfg(p), user_id=str(uid))["hit"]
            if bucket < 10:
                self.assertTrue(in_at(10))
            if not in_at(30):
                self.assertFalse(in_at(10))
            self.assertTrue(in_at(100))

    def test_stability_repeated_evaluation(self):
        flags = {"f": flag(rollout={"percentage": 50, "key": "user_id"})}
        first = evaluate_flag("f", flags, user_id="alice")
        for _ in range(20):
            again = evaluate_flag("f", flags, user_id="alice", attributes={"plan": "pro"})
            self.assertEqual(again["hit"], first["hit"])
            self.assertEqual(again["rollout"]["bucket"], first["rollout"]["bucket"])

    def test_no_user_id_falls_out_of_rollout(self):
        flags = {"f": flag(rollout={"percentage": 100, "key": "user_id", "value": True})}
        res = evaluate_flag("f", flags, attributes={})
        self.assertFalse(res["hit"])
        self.assertEqual(res["reason"], "rollout_out")

    def test_numeric_user_id_is_supported(self):
        flags = {"f": flag(rollout={"percentage": 100, "key": "user_id", "value": True})}
        res = evaluate_flag("f", flags, user_id=12345)
        self.assertTrue(res["hit"])
        self.assertEqual(res["rollout"]["bucket"], hash_bucket("f", "12345"))


class SegmentEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.segments = {
            "acme-staff": {
                "conditions": [
                    {"attr": "email", "op": "suffix", "value": "@acme.com"},
                ]
            },
            "valuable": {
                "conditions": [
                    {"attr": "tier", "op": "in", "value": ["gold", "platinum"]},
                    {"attr": "age", "op": "ge", "value": 18},
                ]
            },
            # A segment may reference other segments (ANDed with its own
            # attribute conditions).
            "vip": {
                "conditions": [
                    {"op": "segment", "value": "valuable"},
                    {"attr": "region", "op": "ne", "value": "eu"},
                ]
            },
        }
        self.flags = {
            "concierge": flag(
                default=False,
                rules=[
                    rule([{"op": "segment", "value": "acme-staff"}], id="staff"),
                    rule([{"op": "segment", "value": "vip"}], id="vip"),
                ],
            ),
        }

    def test_segment_reference_matches(self):
        res = evaluate_flag(
            "concierge",
            self.flags,
            segments=self.segments,
            attributes={"email": "ada@acme.com"},
        )
        self.assertEqual(res["reason"], "rule_match")
        self.assertEqual(res["rule_id"], "staff")
        self.assertTrue(res["hit"])

    def test_nested_segment_reference(self):
        res = evaluate_flag(
            "concierge",
            self.flags,
            segments=self.segments,
            attributes={"tier": "gold", "age": 30, "region": "us"},
        )
        self.assertEqual(res["rule_id"], "vip")

    def test_segment_mismatch_is_just_a_failed_condition(self):
        # Not staff and not vip -> both rules miss -> declared default.
        res = evaluate_flag(
            "concierge",
            self.flags,
            segments=self.segments,
            attributes={"email": "a@other.com", "tier": "free"},
        )
        self.assertEqual(res["reason"], "default")
        self.assertFalse(res["hit"])

    def test_inner_segment_failure_fails_outer_segment(self):
        # tier/age fail "valuable", so "vip" fails even though region is fine.
        res = evaluate_flag(
            "concierge",
            self.flags,
            segments=self.segments,
            attributes={"tier": "free", "age": 40, "region": "us"},
        )
        self.assertEqual(res["reason"], "default")

    def test_segment_conditions_are_anded(self):
        evaluator = FlagEvaluator(self.flags, segments=self.segments)
        # age below 18 -> "valuable" (and thus "vip") does not match.
        res = evaluator.evaluate(
            "concierge", attributes={"tier": "gold", "age": 12, "region": "us"}
        )
        self.assertEqual(res["reason"], "default")

    def test_segment_reference_without_attr_in_rule(self):
        # Reference conditions carry no attr; mixing with attribute
        # conditions in the same rule keeps AND semantics.
        flags = {
            "f": flag(
                rules=[
                    rule(
                        [
                            {"op": "segment", "value": "acme-staff"},
                            {"attr": "beta", "op": "eq", "value": True},
                        ],
                        id="staff-beta",
                    )
                ]
            )
        }
        res = evaluate_flag(
            "f",
            flags,
            segments=self.segments,
            attributes={"email": "ada@acme.com", "beta": True},
        )
        self.assertEqual(res["rule_id"], "staff-beta")
        res = evaluate_flag(
            "f",
            flags,
            segments=self.segments,
            attributes={"email": "ada@acme.com", "beta": False},
        )
        self.assertEqual(res["reason"], "default")

    def test_first_matching_rule_wins_with_segments(self):
        # Both segments match; the first rule in order still wins.
        res = evaluate_flag(
            "concierge",
            self.flags,
            segments=self.segments,
            attributes={
                "email": "ada@acme.com",
                "tier": "gold",
                "age": 30,
                "region": "us",
            },
        )
        self.assertEqual(res["rule_id"], "staff")

    def test_segment_results_are_reused_within_one_request(self):
        # Two flags referencing the same segment: one evaluator (one request)
        # computes the segment once and memoizes it.
        flags = {
            "a": flag(rules=[rule([{"op": "segment", "value": "acme-staff"}], id="ra")]),
            "b": flag(rules=[rule([{"op": "segment", "value": "acme-staff"}], id="rb")]),
        }
        evaluator = FlagEvaluator(flags, segments=self.segments)
        attrs = {"email": "ada@acme.com"}
        with mock.patch.object(
            evaluator_mod, "_compare", wraps=evaluator_mod._compare
        ) as spy:
            self.assertEqual(evaluator.evaluate("a", attributes=attrs)["rule_id"], "ra")
            self.assertEqual(evaluator.evaluate("b", attributes=attrs)["rule_id"], "rb")
        # The segment holds a single attribute condition; it ran exactly once.
        self.assertEqual(spy.call_count, 1)
        self.assertEqual(evaluator._segment_memo, {"acme-staff": True})

    def test_fresh_evaluator_recomputes_segments(self):
        # Memoization is request-scoped: a new evaluator starts empty.
        evaluator = FlagEvaluator(self.flags, segments=self.segments)
        evaluator.evaluate("concierge", attributes={"email": "ada@acme.com"})
        self.assertIn("acme-staff", evaluator._segment_memo)
        self.assertEqual(FlagEvaluator(self.flags, segments=self.segments)._segment_memo, {})

    def test_missing_segment_definition_fails_closed(self):
        # Defensive: publishing rejects dangling references; if a hand-rolled
        # document slips through, the condition simply fails.
        flags = {"f": flag(rules=[rule([{"op": "segment", "value": "ghost"}])])}
        res = evaluate_flag("f", flags, segments={}, attributes={})
        self.assertEqual(res["reason"], "default")
        self.assertFalse(res["hit"])

    def test_segment_cycle_never_loops(self):
        # Defensive: publishing rejects cycles; evaluation must still terminate.
        segments = {
            "a": {"conditions": [{"op": "segment", "value": "b"}]},
            "b": {"conditions": [{"op": "segment", "value": "a"}]},
        }
        flags = {"f": flag(rules=[rule([{"op": "segment", "value": "a"}])])}
        res = evaluate_flag("f", flags, segments=segments, attributes={})
        self.assertEqual(res["reason"], "default")

    def test_empty_segment_matches_everyone(self):
        flags = {"f": flag(rules=[rule([{"op": "segment", "value": "all"}], id="all")])}
        res = evaluate_flag("f", flags, segments={"all": {"conditions": []}}, attributes={})
        self.assertEqual(res["rule_id"], "all")


class ValidationTests(unittest.TestCase):
    def _expect(self, config, code):
        with self.assertRaises(ConfigValidationError) as ctx:
            validate_config(config)
        codes = [e["code"] for e in ctx.exception.errors]
        self.assertIn(code, codes, msg=f"expected {code} in {codes}")

    def test_cycle_of_two_is_rejected(self):
        config = {
            "flags": {
                "a": {"prerequisites": [{"flag": "b"}], "rules": [], "default": False},
                "b": {"prerequisites": [{"flag": "a"}], "rules": [], "default": False},
            }
        }
        self._expect(config, "circular_dependency")

    def test_self_cycle_is_rejected(self):
        config = {"flags": {"a": {"prerequisites": [{"flag": "a"}]}}}
        self._expect(config, "self_dependency")

    def test_three_node_cycle_is_rejected(self):
        config = {
            "flags": {
                "a": {"prerequisites": [{"flag": "b"}]},
                "b": {"prerequisites": [{"flag": "c"}]},
                "c": {"prerequisites": [{"flag": "a"}]},
            }
        }
        self._expect(config, "circular_dependency")

    def test_dag_is_accepted(self):
        config = {
            "flags": {
                "a": {},
                "b": {"prerequisites": [{"flag": "a"}]},
                "c": {"prerequisites": [{"flag": "a"}, {"flag": "b"}]},
            }
        }
        validate_config(config)  # should not raise

    def test_percentage_bounds(self):
        for bad in (-1, 101):
            self._expect(
                {"flags": {"f": {"rollout": {"percentage": bad}}}},
                "invalid_percentage",
            )

    def test_percentage_must_be_integer(self):
        self._expect(
            {"flags": {"f": {"rollout": {"percentage": "50"}}}},
            "invalid_percentage",
        )
        # bool must not be accepted as int
        self._expect(
            {"flags": {"f": {"rollout": {"percentage": True}}}},
            "invalid_percentage",
        )

    def test_boundary_percentages_accepted(self):
        for good in (0, 50, 100):
            validate_config({"flags": {"f": {"rollout": {"percentage": good}}}})

    def test_unknown_operator_rejected(self):
        self._expect(
            {"flags": {"f": {"rules": [{"conditions": [{"attr": "x", "op": "regex"}]}]}}},
            "unknown_operator",
        )

    def test_missing_prerequisite_rejected(self):
        self._expect(
            {"flags": {"f": {"prerequisites": [{"flag": "ghost"}]}}},
            "missing_prerequisite",
        )

    def test_duplicate_rule_id_rejected(self):
        config = {
            "flags": {
                "f": {
                    "rules": [
                        {"id": "r1", "conditions": []},
                        {"id": "r1", "conditions": [{"attr": "a", "op": "eq", "value": 1}]},
                    ]
                }
            }
        }
        self._expect(config, "duplicate_rule_id")

    def test_unsupported_rollout_key_rejected(self):
        self._expect(
            {"flags": {"f": {"rollout": {"percentage": 10, "key": "email"}}}},
            "invalid_rollout_key",
        )

    def test_empty_config_is_valid(self):
        validate_config({"flags": {}})


class SegmentValidationTests(unittest.TestCase):
    def _expect(self, config, code):
        with self.assertRaises(ConfigValidationError) as ctx:
            validate_config(config)
        codes = [e["code"] for e in ctx.exception.errors]
        self.assertIn(code, codes, msg=f"expected {code} in {codes}")
        return ctx.exception.errors

    def test_valid_segments_accepted(self):
        config = {
            "segments": {
                "staff": {"conditions": [{"attr": "email", "op": "suffix", "value": "@acme.com"}]},
                "vip": {"conditions": [{"op": "segment", "value": "staff"}]},
            },
            "flags": {
                "f": {"rules": [{"conditions": [{"op": "segment", "value": "vip"}]}]},
            },
        }
        validate_config(config)  # should not raise

    def test_missing_segment_in_rule_condition_rejected(self):
        errors = self._expect(
            {"flags": {"f": {"rules": [{"conditions": [{"op": "segment", "value": "ghost"}]}]}}},
            "missing_segment",
        )
        # The error locates the exact rule condition and its flag.
        err = next(e for e in errors if e["code"] == "missing_segment")
        self.assertEqual(err["flag"], "f")
        self.assertIn("rules[0].conditions[0]", err["message"])
        self.assertIn("ghost", err["message"])

    def test_missing_segment_in_segment_condition_rejected(self):
        errors = self._expect(
            {
                "segments": {"vip": {"conditions": [{"op": "segment", "value": "ghost"}]}},
                "flags": {},
            },
            "missing_segment",
        )
        err = next(e for e in errors if e["code"] == "missing_segment")
        self.assertEqual(err["segment"], "vip")
        self.assertIn("segments.vip.conditions[0]", err["message"])

    def test_segment_reference_must_be_a_name(self):
        for bad in (None, "", 42):
            self._expect(
                {
                    "segments": {"s": {"conditions": [{"op": "segment", "value": bad}]}},
                    "flags": {},
                },
                "invalid_segment_reference",
            )

    def test_direct_self_cycle_rejected(self):
        errors = self._expect(
            {
                "segments": {"a": {"conditions": [{"op": "segment", "value": "a"}]}},
                "flags": {},
            },
            "circular_segment",
        )
        err = next(e for e in errors if e["code"] == "circular_segment")
        self.assertEqual(err["segment"], "a")
        self.assertIn("a -> a", err["message"])

    def test_indirect_cycle_rejected(self):
        config = {
            "segments": {
                "a": {"conditions": [{"op": "segment", "value": "b"}]},
                "b": {"conditions": [{"op": "segment", "value": "c"}]},
                "c": {"conditions": [{"op": "segment", "value": "a"}]},
            },
            "flags": {},
        }
        errors = self._expect(config, "circular_segment")
        err = next(e for e in errors if e["code"] == "circular_segment")
        self.assertIn("a -> b -> c -> a", err["message"])

    def test_dag_of_segments_accepted(self):
        config = {
            "segments": {
                "a": {"conditions": [{"attr": "x", "op": "eq", "value": 1}]},
                "b": {"conditions": [{"op": "segment", "value": "a"}]},
                "c": {"conditions": [{"op": "segment", "value": "a"}, {"op": "segment", "value": "b"}]},
            },
            "flags": {},
        }
        validate_config(config)  # should not raise

    def test_unknown_operator_in_segment_rejected(self):
        errors = self._expect(
            {
                "segments": {"s": {"conditions": [{"attr": "x", "op": "regex"}]}},
                "flags": {},
            },
            "unknown_operator",
        )
        err = next(e for e in errors if e["code"] == "unknown_operator")
        self.assertEqual(err["segment"], "s")
        self.assertIn("segments.s.conditions[0]", err["message"])

    def test_segment_definition_must_be_object(self):
        self._expect(
            {"segments": {"s": ["not", "an", "object"]}, "flags": {}},
            "invalid_segment",
        )

    def test_segments_must_be_object(self):
        with self.assertRaises(ConfigValidationError) as ctx:
            validate_config({"segments": ["nope"], "flags": {}})
        self.assertEqual(ctx.exception.errors[0]["code"], "invalid_config")

    def test_segment_conditions_must_be_list(self):
        self._expect(
            {"segments": {"s": {"conditions": {"attr": "x"}}}, "flags": {}},
            "invalid_segment",
        )

    def test_missing_segment_does_not_mask_cycle(self):
        # A dangling reference and a real cycle are both reported.
        config = {
            "segments": {
                "a": {"conditions": [{"op": "segment", "value": "b"}]},
                "b": {"conditions": [{"op": "segment", "value": "a"}]},
                "c": {"conditions": [{"op": "segment", "value": "ghost"}]},
            },
            "flags": {},
        }
        with self.assertRaises(ConfigValidationError) as ctx:
            validate_config(config)
        codes = [e["code"] for e in ctx.exception.errors]
        self.assertIn("missing_segment", codes)
        self.assertIn("circular_segment", codes)


class VersionComparisonTests(unittest.TestCase):
    def test_diff_added_removed_modified_sorted(self):
        old = {
            "keep": flag(default=1),
            "mod": flag(default="a"),
            "gone_b": flag(),
            "gone_a": flag(),
        }
        new = {
            "keep": flag(default=1),
            "mod": flag(default="b"),
            "new_b": flag(),
            "new_a": flag(),
        }
        added, removed, modified = diff_flags(old, new)
        self.assertEqual(added, ["new_a", "new_b"])
        self.assertEqual(removed, ["gone_a", "gone_b"])
        self.assertEqual(modified, ["mod"])

    def test_key_order_is_not_a_modification(self):
        a = {"f": {"enabled": True, "default": False, "rules": []}}
        b = {"f": {"default": False, "rules": [], "enabled": True}}
        self.assertEqual(diff_flags(a, b), ([], [], []))

    def test_chain_impact_reports_shortest_paths(self):
        flags = {
            "a": flag(),
            "b": flag(prerequisites=[{"flag": "a"}]),
            "c": flag(prerequisites=[{"flag": "b"}]),
        }
        adj = build_reverse_graph(flags)
        impacts = impact_analysis(["a"], adj)
        self.assertEqual([i["flag"] for i in impacts], ["a"])
        impacted = impacts[0]["impacted"]
        self.assertEqual([i["flag"] for i in impacted], ["b", "c"])
        self.assertEqual(impacted[0]["path"], ["a", "b"])
        self.assertEqual(impacted[1]["path"], ["a", "b", "c"])

    def test_diamond_prefers_direct_shortest_path(self):
        flags = {
            "a": flag(),
            "b": flag(prerequisites=[{"flag": "a"}]),
            "c": flag(prerequisites=[{"flag": "a"}, {"flag": "b"}]),
        }
        adj = build_reverse_graph(flags)
        impacts = impact_analysis(["a"], adj)
        paths = {i["flag"]: i["path"] for i in impacts[0]["impacted"]}
        self.assertEqual(paths["b"], ["a", "b"])
        self.assertEqual(paths["c"], ["a", "c"])

    def test_equal_length_paths_pick_lexicographically_smallest(self):
        flags = {
            "x": flag(),
            "q": flag(prerequisites=[{"flag": "x"}]),
            "p": flag(prerequisites=[{"flag": "x"}]),
            "z": flag(prerequisites=[{"flag": "p"}, {"flag": "q"}]),
        }
        adj = build_reverse_graph(flags)
        impacts = impact_analysis(["x"], adj)
        paths = {i["flag"]: i["path"] for i in impacts[0]["impacted"]}
        self.assertEqual(paths["z"], ["x", "p", "z"])

    def test_impact_uses_union_of_edges_from_both_versions(self):
        old = {"a": flag(), "b": flag(prerequisites=[{"flag": "a"}])}
        new = {"a": flag(default="changed"), "b": flag()}  # edge removed in new
        result = compare_versions(old, new)
        self.assertEqual(result["modified"], ["a", "b"])
        impacts = {i["flag"]: i for i in result["impacts"]}
        # The edge b->a only exists in the old version, yet b is still
        # reported as impacted by the change in a.
        self.assertEqual([x["flag"] for x in impacts["a"]["impacted"]], ["b"])

    def test_union_cycle_tolerated_and_source_not_self_impacted(self):
        old = {"a": flag(prerequisites=[{"flag": "b"}]), "b": flag()}
        new = {"a": flag(), "b": flag(prerequisites=[{"flag": "a"}])}
        result = compare_versions(old, new)
        self.assertEqual(result["modified"], ["a", "b"])
        impacts = {i["flag"]: i for i in result["impacts"]}
        self.assertEqual([x["flag"] for x in impacts["a"]["impacted"]], ["b"])
        self.assertEqual([x["flag"] for x in impacts["b"]["impacted"]], ["a"])

    def test_duplicate_impacts_reported_once(self):
        flags = {
            "a": flag(),
            "b": flag(prerequisites=[{"flag": "a"}]),
            "c": flag(prerequisites=[{"flag": "a"}, {"flag": "b"}]),
            "d": flag(prerequisites=[{"flag": "c"}]),
        }
        adj = build_reverse_graph(flags)
        impacts = impact_analysis(["a"], adj)
        names = [i["flag"] for i in impacts[0]["impacted"]]
        self.assertEqual(names, ["b", "c", "d"])
        self.assertEqual(len(names), len(set(names)))

    def test_changed_flag_without_dependents_has_empty_impacted(self):
        result = compare_versions({}, {"solo": flag()})
        self.assertEqual(result["added"], ["solo"])
        self.assertEqual(result["impacts"], [{"flag": "solo", "impacted": []}])

    def test_impacts_sorted_by_changed_flag_name(self):
        result = compare_versions({}, {"zeta": flag(), "alpha": flag()})
        self.assertEqual([i["flag"] for i in result["impacts"]], ["alpha", "zeta"])


class EvaluationPreviewTests(unittest.TestCase):
    """Per-subject decision diffs between two pinned config versions."""

    def subject(self, subject_id, user_id=None, **attributes):
        return {
            "subject_id": subject_id,
            "user_id": user_id,
            "attributes": attributes,
        }

    def test_value_change_reports_both_sides(self):
        from_config = {"flags": {"f": flag(default="old", rules=[])}}
        to_config = {"flags": {"f": flag(default="new", rules=[])}}
        preview = build_evaluation_preview(
            from_config, to_config, ["f"], [self.subject("s1")]
        )
        self.assertEqual(
            preview["changed"],
            [
                {
                    "subject_id": "s1",
                    "flag": "f",
                    "from": {
                        "exists": True,
                        "value": "old",
                        "reason": "default",
                        "rule_id": None,
                    },
                    "to": {
                        "exists": True,
                        "value": "new",
                        "reason": "default",
                        "rule_id": None,
                    },
                }
            ],
        )
        self.assertEqual(
            preview["summary"], [{"flag": "f", "changed": 1, "unchanged": 0}]
        )

    def test_reason_and_rule_id_differences_alone_are_unchanged(self):
        # Same value, different explanation: rule_match/r1 vs plain default.
        from_config = {
            "flags": {
                "f": flag(
                    default=False,
                    rules=[rule([{"attr": "x", "op": "eq", "value": 1}], value=False, id="r1")],
                )
            }
        }
        to_config = {"flags": {"f": flag(default=False, rules=[])}}
        preview = build_evaluation_preview(
            from_config, to_config, ["f"], [self.subject("s1", x=1)]
        )
        self.assertEqual(preview["changed"], [])
        self.assertEqual(
            preview["summary"], [{"flag": "f", "changed": 0, "unchanged": 1}]
        )

    def test_json_type_difference_counts_as_changed(self):
        # true (boolean) vs 1 (number): equal under ==, different JSON types.
        from_config = {"flags": {"f": flag(default=True)}}
        to_config = {"flags": {"f": flag(default=1)}}
        preview = build_evaluation_preview(
            from_config, to_config, ["f"], [self.subject("s1")]
        )
        self.assertEqual(len(preview["changed"]), 1)
        self.assertEqual(preview["changed"][0]["from"]["value"], True)
        self.assertEqual(preview["changed"][0]["to"]["value"], 1)

    def test_nested_json_type_difference_counts_as_changed(self):
        self.assertFalse(_json_equal([1, True], [1, 1]))
        self.assertFalse(_json_equal({"a": True}, {"a": 1}))
        self.assertTrue(_json_equal({"a": [1, "x"]}, {"a": [1, "x"]}))
        self.assertTrue(_json_equal(1, 1.0))  # both JSON numbers
        self.assertFalse(_json_equal({"a": 1}, {"a": 1, "b": 2}))

    def test_existence_difference_is_changed(self):
        from_config = {"flags": {"f": flag(default="v1")}}
        to_config = {"flags": {}}
        preview = build_evaluation_preview(
            from_config, to_config, ["f"], [self.subject("s1")]
        )
        self.assertEqual(len(preview["changed"]), 1)
        entry = preview["changed"][0]
        self.assertEqual(
            entry["from"],
            {"exists": True, "value": "v1", "reason": "default", "rule_id": None},
        )
        self.assertEqual(
            entry["to"],
            {"exists": False, "value": None, "reason": None, "rule_id": None},
        )

    def test_missing_in_both_versions_is_unchanged(self):
        preview = build_evaluation_preview(
            {"flags": {}}, {"flags": {}}, ["ghost"], [self.subject("s1")]
        )
        self.assertEqual(preview["changed"], [])
        self.assertEqual(
            preview["summary"], [{"flag": "ghost", "changed": 0, "unchanged": 1}]
        )

    def test_changed_entries_follow_request_order(self):
        # Subjects drive the outer order, flags the inner order — not sorted.
        from_config = {"flags": {"b": flag(default=1), "a": flag(default=1)}}
        to_config = {"flags": {"b": flag(default=2), "a": flag(default=2)}}
        subjects = [self.subject("z-subject"), self.subject("a-subject")]
        preview = build_evaluation_preview(
            from_config, to_config, ["b", "a"], subjects
        )
        order = [(c["subject_id"], c["flag"]) for c in preview["changed"]]
        self.assertEqual(
            order,
            [
                ("z-subject", "b"),
                ("z-subject", "a"),
                ("a-subject", "b"),
                ("a-subject", "a"),
            ],
        )
        self.assertEqual(
            preview["summary"],
            [
                {"flag": "b", "changed": 2, "unchanged": 0},
                {"flag": "a", "changed": 2, "unchanged": 0},
            ],
        )

    def test_summary_aggregates_per_flag_in_request_order(self):
        from_config = {"flags": {"x": flag(default=1), "y": flag(default=1)}}
        to_config = {"flags": {"x": flag(default=2), "y": flag(default=1)}}
        subjects = [self.subject("s1"), self.subject("s2"), self.subject("s3")]
        preview = build_evaluation_preview(
            from_config, to_config, ["y", "x"], subjects
        )
        self.assertEqual(
            preview["summary"],
            [
                {"flag": "y", "changed": 0, "unchanged": 3},
                {"flag": "x", "changed": 3, "unchanged": 0},
            ],
        )

    def test_rollout_change_uses_each_subjects_user_id(self):
        from_config = {
            "flags": {"f": flag(rollout={"percentage": 0, "key": "user_id", "value": True})}
        }
        to_config = {
            "flags": {"f": flag(rollout={"percentage": 100, "key": "user_id", "value": True})}
        }
        subjects = [self.subject(f"s{i}", user_id=f"user-{i}") for i in range(5)]
        preview = build_evaluation_preview(from_config, to_config, ["f"], subjects)
        # 0% -> always the default (False); 100% -> always the rollout value.
        self.assertEqual(len(preview["changed"]), 5)
        for entry in preview["changed"]:
            self.assertEqual(entry["from"]["value"], False)
            self.assertEqual(entry["from"]["reason"], "rollout_out")
            self.assertEqual(entry["to"]["value"], True)
            self.assertEqual(entry["to"]["reason"], "rollout_in")

    def test_segment_memo_shared_within_subject_but_not_across_subjects(self):
        # Two flags referencing one segment, two subjects, two versions:
        # the segment's single condition must run exactly once per
        # (version, subject) pair — 1 * 2 * 2 = 4 comparisons.
        segments = {
            "staff": {
                "conditions": [{"attr": "email", "op": "suffix", "value": "@acme.com"}]
            }
        }
        flags = {
            "a": flag(rules=[rule([{"op": "segment", "value": "staff"}], id="ra")]),
            "b": flag(rules=[rule([{"op": "segment", "value": "staff"}], id="rb")]),
        }
        config = {"segments": segments, "flags": flags}
        subjects = [
            self.subject("staff", email="ada@acme.com"),
            self.subject("other", email="bob@example.com"),
        ]
        with mock.patch.object(
            evaluator_mod, "_compare", wraps=evaluator_mod._compare
        ) as spy:
            preview = build_evaluation_preview(config, config, ["a", "b"], subjects)
        self.assertEqual(spy.call_count, 4)
        # Same version on both sides -> nothing changed, but per-subject
        # audience decisions were computed independently.
        self.assertEqual(preview["changed"], [])
        self.assertEqual(
            preview["summary"],
            [
                {"flag": "a", "changed": 0, "unchanged": 2},
                {"flag": "b", "changed": 0, "unchanged": 2},
            ],
        )

    def test_subjects_do_not_share_segment_decisions(self):
        # A cached cross-subject memo would leak "staff" from s1 into s2.
        segments = {
            "staff": {
                "conditions": [{"attr": "email", "op": "suffix", "value": "@acme.com"}]
            }
        }
        from_config = {
            "segments": segments,
            "flags": {"f": flag(rules=[rule([{"op": "segment", "value": "staff"}])])},
        }
        to_config = {
            "segments": segments,
            "flags": {"f": flag(default=False, rules=[])},
        }
        subjects = [
            self.subject("s1", email="ada@acme.com"),
            self.subject("s2", email="bob@example.com"),
        ]
        preview = build_evaluation_preview(from_config, to_config, ["f"], subjects)
        # s1 matched the segment in the from version (True -> False: changed);
        # s2 did not (False -> False: unchanged).
        self.assertEqual([c["subject_id"] for c in preview["changed"]], ["s1"])
        self.assertEqual(
            preview["summary"], [{"flag": "f", "changed": 1, "unchanged": 1}]
        )


class ImmutableVersionStorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["FLAG_DB_PATH"] = os.path.join(self.tmp.name, "test.db")
        self.conn = db_mod.connect()
        db_mod.init_db(self.conn)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()
        os.environ.pop("FLAG_DB_PATH", None)

    def test_versions_are_immutable_and_history_is_preserved(self):
        v1 = {"flags": {"f": {"enabled": True, "default": "v1", "rules": []}}}
        v2 = {"flags": {"f": {"enabled": True, "default": "v2", "rules": []}}}

        ts1 = db_mod.insert_version(self.conn, "v1", v1)
        self.assertIsNotNone(ts1)
        self.assertIsNone(db_mod.insert_version(self.conn, "v1", v2))  # duplicate rejected

        loaded_v1 = db_mod.get_version(self.conn, "v1")
        self.assertEqual(loaded_v1["config"]["flags"]["f"]["default"], "v1")

        db_mod.insert_version(self.conn, "v2", v2)
        self.assertEqual(db_mod.get_version(self.conn, "v2")["config"]["flags"]["f"]["default"], "v2")

        # Publishing v2 must not change evaluations pinned to v1.
        still_v1 = db_mod.get_version(self.conn, "v1")["config"]
        res = evaluate_flag("f", still_v1["flags"], attributes={})
        self.assertEqual(res["value"], "v1")

        versions = db_mod.list_versions(self.conn)
        self.assertEqual([v["version"] for v in versions], ["v1", "v2"])
        self.assertEqual(versions[0]["flag_count"], 1)

    def test_segments_are_stored_and_echoed_back(self):
        config = {
            "segments": {
                "staff": {
                    "conditions": [{"attr": "email", "op": "suffix", "value": "@acme.com"}]
                }
            },
            "flags": {
                "f": {
                    "enabled": True,
                    "default": False,
                    "rules": [
                        {"id": "staff", "conditions": [{"op": "segment", "value": "staff"}], "value": True}
                    ],
                }
            },
        }
        db_mod.insert_version(self.conn, "v1", config)
        loaded = db_mod.get_version(self.conn, "v1")["config"]
        # Version reads echo the segment definitions verbatim...
        self.assertEqual(loaded["segments"], config["segments"])
        # ...and the stored document evaluates identically.
        res = evaluate_flag(
            "f",
            loaded["flags"],
            segments=loaded["segments"],
            attributes={"email": "ada@acme.com"},
        )
        self.assertEqual(res["rule_id"], "staff")
        self.assertTrue(res["hit"])


class EndToEndPinnedVersionTests(unittest.TestCase):
    def test_pinned_version_result_is_stable(self):
        flags_v1 = {
            "checkout": flag(
                rollout={"percentage": 50, "key": "user_id"},
                prerequisites=[{"flag": "signed_in"}],
            ),
            "signed_in": flag(rules=[rule([{"attr": "user_id", "op": "ne", "value": None}])]),
        }
        for uid in range(100):
            r1 = evaluate_flag("checkout", flags_v1, user_id=str(uid))
            r2 = evaluate_flag("checkout", flags_v1, user_id=str(uid), attributes={"x": "y"})
            self.assertEqual(r1["hit"], r2["hit"])
            self.assertEqual([n["flag"] for n in r1["chain"]], ["signed_in", "checkout"])


class EnvironmentPointerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["FLAG_DB_PATH"] = os.path.join(self.tmp.name, "test.db")
        self.conn = db_mod.connect()
        db_mod.init_db(self.conn)
        self.config = {"flags": {"f": {"enabled": True, "default": "v1", "rules": []}}}
        db_mod.insert_version(self.conn, "v1", self.config)
        db_mod.insert_version(
            self.conn, "v2", {"flags": {"f": {"enabled": True, "default": "v2", "rules": []}}}
        )

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()
        os.environ.pop("FLAG_DB_PATH", None)

    def _upsert(self, name, version, expected_revision):
        env, created = db_mod.upsert_environment(self.conn, name, version, expected_revision)
        self.assertEqual(created, expected_revision == 0)
        return env

    def test_create_requires_revision_zero_and_starts_at_one(self):
        env = self._upsert("prod", "v1", 0)
        self.assertEqual(env["name"], "prod")
        self.assertEqual(env["version"], "v1")
        self.assertEqual(env["revision"], 1)
        self.assertEqual(env["created_at"], env["updated_at"])

        loaded = db_mod.get_environment(self.conn, "prod")
        self.assertEqual(loaded["revision"], 1)
        self.assertEqual(loaded["version"], "v1")

    def test_create_then_update_increments_revision(self):
        self._upsert("prod", "v1", 0)
        env = self._upsert("prod", "v2", 1)
        self.assertEqual(env["version"], "v2")
        self.assertEqual(env["revision"], 2)
        self.assertEqual(db_mod.get_environment(self.conn, "prod")["revision"], 2)

    def test_create_with_nonzero_revision_on_missing_env_is_stale(self):
        with self.assertRaises(db_mod.StaleRevision) as ctx:
            db_mod.upsert_environment(self.conn, "prod", "v1", 1)
        self.assertIsNone(ctx.exception.current)
        self.assertIsNone(db_mod.get_environment(self.conn, "prod"))

    def test_create_on_existing_env_with_revision_zero_is_stale_and_untouched(self):
        db_mod.upsert_environment(self.conn, "prod", "v1", 0)
        with self.assertRaises(db_mod.StaleRevision):
            db_mod.upsert_environment(self.conn, "prod", "v2", 0)
        # Pointer must not have moved.
        self.assertEqual(db_mod.get_environment(self.conn, "prod")["version"], "v1")
        self.assertEqual(db_mod.get_environment(self.conn, "prod")["revision"], 1)

    def test_stale_update_does_not_move_pointer(self):
        db_mod.upsert_environment(self.conn, "prod", "v1", 0)
        db_mod.upsert_environment(self.conn, "prod", "v2", 1)  # revision now 2
        with self.assertRaises(db_mod.StaleRevision) as ctx:
            db_mod.upsert_environment(self.conn, "prod", "v1", 1)
        self.assertEqual(ctx.exception.current, 2)
        env = db_mod.get_environment(self.conn, "prod")
        self.assertEqual(env["version"], "v2")
        self.assertEqual(env["revision"], 2)

    def test_missing_version_on_create_returns_not_found_without_row(self):
        with self.assertRaises(db_mod.VersionNotFound):
            db_mod.upsert_environment(self.conn, "prod", "ghost", 0)
        self.assertIsNone(db_mod.get_environment(self.conn, "prod"))

    def test_missing_version_on_update_returns_not_found_and_keeps_revision(self):
        db_mod.upsert_environment(self.conn, "prod", "v1", 0)
        with self.assertRaises(db_mod.VersionNotFound):
            db_mod.upsert_environment(self.conn, "prod", "ghost", 1)
        env = db_mod.get_environment(self.conn, "prod")
        self.assertEqual(env["version"], "v1")
        self.assertEqual(env["revision"], 1)

    def test_stale_revision_takes_precedence_over_missing_version(self):
        db_mod.upsert_environment(self.conn, "prod", "v1", 0)
        # Both stale revision and unknown version: revision is checked first.
        with self.assertRaises(db_mod.StaleRevision):
            db_mod.upsert_environment(self.conn, "prod", "ghost", 99)
        self.assertEqual(db_mod.get_environment(self.conn, "prod")["version"], "v1")

    def test_get_missing_environment_returns_none(self):
        self.assertIsNone(db_mod.get_environment(self.conn, "nope"))
        self.assertIsNone(db_mod.get_environment_snapshot(self.conn, "nope"))

    def test_snapshot_returns_pointer_and_pinned_config(self):
        db_mod.upsert_environment(self.conn, "prod", "v1", 0)
        snap = db_mod.get_environment_snapshot(self.conn, "prod")
        self.assertEqual(snap["version"], "v1")
        self.assertEqual(snap["revision"], 1)
        self.assertEqual(snap["config"]["flags"]["f"]["default"], "v1")

        # Moving the pointer changes the snapshot but never the old version.
        db_mod.upsert_environment(self.conn, "prod", "v2", 1)
        snap2 = db_mod.get_environment_snapshot(self.conn, "prod")
        self.assertEqual(snap2["version"], "v2")
        self.assertEqual(snap2["revision"], 2)
        self.assertEqual(snap2["config"]["flags"]["f"]["default"], "v2")
        # Snapshot captured earlier still describes v1.
        self.assertEqual(snap["config"]["flags"]["f"]["default"], "v1")

    def test_concurrent_updates_exactly_one_wins_per_revision(self):
        """N racing writers all observe revision 1; exactly one must win."""
        db_mod.upsert_environment(self.conn, "prod", "v1", 0)
        n = 8
        barrier = threading.Barrier(n)
        outcomes: list[str] = []
        outcomes_lock = threading.Lock()

        def race() -> None:
            conn = db_mod.connect()
            try:
                barrier.wait()
                try:
                    db_mod.upsert_environment(conn, "prod", "v2", 1)
                    with outcomes_lock:
                        outcomes.append("ok")
                except db_mod.StaleRevision:
                    with outcomes_lock:
                        outcomes.append("stale")
            finally:
                conn.close()

        threads = [threading.Thread(target=race) for _ in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(sorted(outcomes), ["ok"] + ["stale"] * (n - 1))
        env = db_mod.get_environment(self.conn, "prod")
        self.assertEqual(env["version"], "v2")
        self.assertEqual(env["revision"], 2)

    def test_concurrent_chain_no_lost_updates(self):
        """Re-read-and-retry writers must produce exactly one increment each."""
        db_mod.upsert_environment(self.conn, "prod", "v1", 0)

        versions = ["v1", "v2"]
        target_revisions = [2, 3]  # worker 0 ends at rev 2, worker 1 at rev 3

        def worker(worker_id: int) -> None:
            conn = db_mod.connect()
            try:
                while True:
                    current = db_mod.get_environment(conn, "prod")
                    try:
                        db_mod.upsert_environment(
                            conn,
                            "prod",
                            versions[worker_id],
                            current["revision"],
                        )
                        return
                    except db_mod.StaleRevision:
                        continue
            finally:
                conn.close()

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        env = db_mod.get_environment(self.conn, "prod")
        self.assertEqual(env["revision"], 3)
        self.assertIn(env["version"], versions)


class EnvironmentHistoryTests(unittest.TestCase):
    """The immutable audit trail written alongside every pointer write."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["FLAG_DB_PATH"] = os.path.join(self.tmp.name, "test.db")
        self.conn = db_mod.connect()
        db_mod.init_db(self.conn)
        for version in ("v1", "v2", "v3"):
            db_mod.insert_version(
                self.conn,
                version,
                {"flags": {"f": {"enabled": True, "default": version, "rules": []}}},
            )

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()
        os.environ.pop("FLAG_DB_PATH", None)

    def _history(self, name="prod", **kwargs):
        return db_mod.list_environment_history(self.conn, name, **kwargs)

    def test_create_writes_one_create_record(self):
        db_mod.upsert_environment(self.conn, "prod", "v1", 0)
        history = self._history()
        self.assertEqual(len(history), 1)
        entry = history[0]
        self.assertEqual(entry["environment"], "prod")
        self.assertEqual(entry["revision"], 1)
        self.assertEqual(entry["version"], "v1")
        self.assertEqual(entry["operation"], "create")
        # The record shares the pointer row's timestamp (same transaction).
        env = db_mod.get_environment(self.conn, "prod")
        self.assertEqual(entry["created_at"], env["created_at"])
        self.assertEqual(entry["created_at"], env["updated_at"])

    def test_successful_update_appends_update_record(self):
        db_mod.upsert_environment(self.conn, "prod", "v1", 0)
        db_mod.upsert_environment(self.conn, "prod", "v2", 1)
        history = self._history()
        self.assertEqual(len(history), 2)
        self.assertEqual([h["revision"] for h in history], [1, 2])
        self.assertEqual([h["operation"] for h in history], ["create", "update"])
        self.assertEqual(history[1]["version"], "v2")
        # The newest record's timestamp matches the pointer's updated_at.
        env = db_mod.get_environment(self.conn, "prod")
        self.assertEqual(history[-1]["created_at"], env["updated_at"])

    def test_history_is_ordered_by_revision_ascending(self):
        db_mod.upsert_environment(self.conn, "prod", "v1", 0)
        db_mod.upsert_environment(self.conn, "prod", "v2", 1)
        db_mod.upsert_environment(self.conn, "prod", "v3", 2)
        self.assertEqual([h["revision"] for h in self._history()], [1, 2, 3])
        self.assertEqual([h["version"] for h in self._history()], ["v1", "v2", "v3"])

    def test_rollback_creates_new_record_instead_of_mutating(self):
        # Moving the pointer back to v1 must append revision 3; the earlier
        # records stay untouched (append-only audit trail).
        db_mod.upsert_environment(self.conn, "prod", "v1", 0)
        db_mod.upsert_environment(self.conn, "prod", "v2", 1)
        db_mod.upsert_environment(self.conn, "prod", "v1", 2)
        history = self._history()
        self.assertEqual([h["version"] for h in history], ["v1", "v2", "v1"])
        self.assertEqual(history[0]["operation"], "create")
        self.assertEqual([h["operation"] for h in history[1:]], ["update", "update"])

    def test_failed_writes_leave_no_history(self):
        db_mod.upsert_environment(self.conn, "prod", "v1", 0)
        baseline = self._history()
        self.assertEqual(len(baseline), 1)

        # Stale create (env already exists).
        with self.assertRaises(db_mod.StaleRevision):
            db_mod.upsert_environment(self.conn, "prod", "v2", 0)
        # Stale update (wrong expected revision).
        with self.assertRaises(db_mod.StaleRevision):
            db_mod.upsert_environment(self.conn, "prod", "v2", 7)
        # Unknown target version on update.
        with self.assertRaises(db_mod.VersionNotFound):
            db_mod.upsert_environment(self.conn, "prod", "ghost", 1)
        # Create with non-zero revision on a missing environment.
        with self.assertRaises(db_mod.StaleRevision):
            db_mod.upsert_environment(self.conn, "staging", "v1", 3)
        # Unknown target version on create.
        with self.assertRaises(db_mod.VersionNotFound):
            db_mod.upsert_environment(self.conn, "staging", "ghost", 0)

        self.assertEqual(self._history(), baseline)
        self.assertEqual(self._history("staging"), [])

    def test_revision_sequence_is_gap_free_after_failures(self):
        # Interleave failed writes between successful ones; the recorded
        # revisions must still be exactly 1..N with no gaps or duplicates.
        db_mod.upsert_environment(self.conn, "prod", "v1", 0)
        with self.assertRaises(db_mod.StaleRevision):
            db_mod.upsert_environment(self.conn, "prod", "v2", 5)
        db_mod.upsert_environment(self.conn, "prod", "v2", 1)
        with self.assertRaises(db_mod.VersionNotFound):
            db_mod.upsert_environment(self.conn, "prod", "ghost", 2)
        db_mod.upsert_environment(self.conn, "prod", "v3", 2)
        revisions = [h["revision"] for h in self._history()]
        self.assertEqual(revisions, [1, 2, 3])

    def test_from_revision_filters_inclusively(self):
        db_mod.upsert_environment(self.conn, "prod", "v1", 0)
        db_mod.upsert_environment(self.conn, "prod", "v2", 1)
        db_mod.upsert_environment(self.conn, "prod", "v3", 2)
        history = self._history(from_revision=2)
        self.assertEqual([h["revision"] for h in history], [2, 3])
        self.assertEqual(self._history(from_revision=4), [])

    def test_limit_caps_results(self):
        db_mod.upsert_environment(self.conn, "prod", "v1", 0)
        db_mod.upsert_environment(self.conn, "prod", "v2", 1)
        db_mod.upsert_environment(self.conn, "prod", "v3", 2)
        history = self._history(limit=2)
        self.assertEqual([h["revision"] for h in history], [1, 2])
        # from_revision + limit together implement forward pagination.
        self.assertEqual(
            [h["revision"] for h in self._history(from_revision=3, limit=1)], [3]
        )

    def test_histories_are_independent_per_environment(self):
        db_mod.upsert_environment(self.conn, "prod", "v1", 0)
        db_mod.upsert_environment(self.conn, "staging", "v2", 0)
        db_mod.upsert_environment(self.conn, "prod", "v3", 1)
        self.assertEqual(
            [(h["revision"], h["version"]) for h in self._history("prod")],
            [(1, "v1"), (2, "v3")],
        )
        self.assertEqual(
            [(h["revision"], h["version"]) for h in self._history("staging")],
            [(1, "v2")],
        )

    def test_empty_history_for_unknown_environment(self):
        self.assertEqual(self._history("ghost"), [])

    def test_concurrent_losers_leave_no_history(self):
        # N racing writers all present revision 1; exactly one wins, so the
        # trail gains exactly one record for revision 2.
        db_mod.upsert_environment(self.conn, "prod", "v1", 0)
        n = 8
        barrier = threading.Barrier(n)
        outcomes: list[str] = []
        outcomes_lock = threading.Lock()

        def race() -> None:
            conn = db_mod.connect()
            try:
                barrier.wait()
                try:
                    db_mod.upsert_environment(conn, "prod", "v2", 1)
                    with outcomes_lock:
                        outcomes.append("ok")
                except db_mod.StaleRevision:
                    with outcomes_lock:
                        outcomes.append("stale")
            finally:
                conn.close()

        threads = [threading.Thread(target=race) for _ in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(sorted(outcomes), ["ok"] + ["stale"] * (n - 1))
        history = self._history()
        self.assertEqual([h["revision"] for h in history], [1, 2])
        self.assertEqual(history[-1]["operation"], "update")
        self.assertEqual(history[-1]["version"], "v2")


if __name__ == "__main__":
    unittest.main()
