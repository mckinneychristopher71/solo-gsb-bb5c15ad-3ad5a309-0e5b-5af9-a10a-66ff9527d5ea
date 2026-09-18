"""Evaluation preview: per-subject decision diff between two config versions.

Pure, framework-free functions (plain dicts in, plain dicts out), mirroring
``evaluator.py``/``comparison.py``, so the logic can be unit-tested without
FastAPI.

For every ``(subject, flag)`` pair the flag is evaluated against both the
``from`` and the ``to`` version. A pair is **changed** when

  * the flag exists in one version but not the other, or
  * the decided ``value`` differs in JSON type or JSON value. The comparison
    is structural: types must match at every nesting level, so a boolean
    ``true`` never equals a number ``1`` (JSON numbers ``1`` and ``1.0``
    remain equal, as usual).

A difference in ``reason`` or ``rule_id`` alone does **not** count as a
change: the preview answers "would any subject see a different value?",
not "would the explanation string differ?".

Evaluator scoping
-----------------
One :class:`~app.evaluator.FlagEvaluator` serves exactly one
``(version, subject)`` pair: all flags of that subject share its segment
memo, so a segment referenced by several flags is computed once per
subject. The memo is never reused across subjects — attributes differ per
subject, so cross-subject reuse would leak one subject's audience decision
into another's.
"""

from __future__ import annotations

from typing import Any

from .evaluator import FlagEvaluator, FlagNotFound


def _json_type(value: Any) -> str:
    """Return the JSON type name of a decoded value."""
    if value is None:
        return "null"
    if isinstance(value, bool):  # before int: bool is a subclass of int
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "unknown"


def _json_equal(a: Any, b: Any) -> bool:
    """Structural JSON equality: types must match at every nesting level."""
    ta, tb = _json_type(a), _json_type(b)
    if ta != tb:
        return False
    if ta == "array":
        return len(a) == len(b) and all(_json_equal(x, y) for x, y in zip(a, b))
    if ta == "object":
        return a.keys() == b.keys() and all(_json_equal(a[k], b[k]) for k in a)
    return a == b


def _evaluate_subject(
    config: dict[str, Any],
    flag_names: list[str],
    user_id: Any,
    attributes: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """Evaluate every requested flag for one subject against one version.

    A single evaluator serves the whole ``(version, subject)`` pair, so a
    segment referenced by several flags is computed once per subject; the
    instance (and its memo) is discarded afterwards and never shared across
    subjects.
    """
    evaluator = FlagEvaluator(
        config.get("flags", {}), segments=config.get("segments")
    )
    outcomes: dict[str, dict[str, Any]] = {}
    for name in flag_names:
        try:
            res = evaluator.evaluate(name, user_id=user_id, attributes=attributes)
            outcomes[name] = {
                "exists": True,
                "value": res["value"],
                "reason": res["reason"],
                "rule_id": res["rule_id"],
            }
        except FlagNotFound:
            outcomes[name] = {
                "exists": False,
                "value": None,
                "reason": None,
                "rule_id": None,
            }
    return outcomes


def _is_changed(
    from_side: dict[str, Any], to_side: dict[str, Any]
) -> bool:
    """Existence or JSON type/value difference -> changed; reason/rule_id
    differences alone leave the pair unchanged."""
    if from_side["exists"] != to_side["exists"]:
        return True
    if not from_side["exists"]:
        return False  # missing in both versions
    return not _json_equal(from_side["value"], to_side["value"])


def build_evaluation_preview(
    from_config: dict[str, Any],
    to_config: dict[str, Any],
    flag_names: list[str],
    subjects: list[dict[str, Any]],
) -> dict[str, Any]:
    """Diff per-subject decisions between two pinned config documents.

    ``subjects`` are plain dicts with ``subject_id``, ``user_id`` and
    ``attributes``. Returns ``{"changed": [...], "summary": [...]}`` where
    the changed details follow the request order (subjects first, then
    flags) and the summary aggregates changed/unchanged counts per flag in
    first-appearance order.
    """
    summary: dict[str, dict[str, Any]] = {}
    for name in flag_names:
        summary.setdefault(name, {"flag": name, "changed": 0, "unchanged": 0})

    changed: list[dict[str, Any]] = []
    for subject in subjects:
        user_id = subject.get("user_id")
        attributes = subject.get("attributes") or {}
        from_out = _evaluate_subject(from_config, flag_names, user_id, attributes)
        to_out = _evaluate_subject(to_config, flag_names, user_id, attributes)
        for name in flag_names:
            from_side = from_out[name]
            to_side = to_out[name]
            if _is_changed(from_side, to_side):
                summary[name]["changed"] += 1
                changed.append(
                    {
                        "subject_id": subject.get("subject_id"),
                        "flag": name,
                        "from": from_side,
                        "to": to_side,
                    }
                )
            else:
                summary[name]["unchanged"] += 1

    return {"changed": changed, "summary": list(summary.values())}
