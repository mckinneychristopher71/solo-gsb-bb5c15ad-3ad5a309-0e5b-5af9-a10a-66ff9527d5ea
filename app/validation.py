"""Semantic validation for uploaded flag configurations.

The validator is intentionally framework-free (plain dicts in, exception out)
so it can be unit-tested without FastAPI and reused by any client.

Checks performed:
  * rollout percentage must be an int in [0, 100], rollout key must be
    ``user_id`` (the only stable identity supported);
  * condition operators must be known (attribute operators or ``segment``);
  * rule ids must be unique within a flag;
  * prerequisites must point at flags that exist;
  * the prerequisite graph must be acyclic;
  * ``segment`` references (in segment definitions and in flag rules) must
    point at segments declared in the top-level ``segments`` object;
  * the segment-reference graph must be acyclic (direct and indirect).

Every error carries a ``code`` and a ``message`` that locates the offending
element (e.g. ``segments.vip.conditions[0]`` or ``rules[1].conditions[2]``);
segment-level errors also carry a ``segment`` field, flag-level ones a
``flag`` field.
"""

from __future__ import annotations

from typing import Any, Optional

KNOWN_OPS = {"eq", "ne", "in", "nin", "gt", "ge", "lt", "le", "prefix", "suffix"}

# ``segment`` is a reference operator: its ``value`` names a top-level segment
# instead of comparing an attribute.
REFERENCE_OPS = {"segment"}

ALLOWED_OPS = KNOWN_OPS | REFERENCE_OPS


class ConfigValidationError(Exception):
    def __init__(self, errors: list[dict[str, Any]]):
        self.errors = errors
        super().__init__("; ".join(e["message"] for e in errors))


def _err(
    errors: list[dict[str, Any]],
    flag: Optional[str],
    code: str,
    message: str,
    *,
    segment: Optional[str] = None,
) -> None:
    err: dict[str, Any] = {"flag": flag, "code": code, "message": message}
    if segment is not None:
        err["segment"] = segment
    errors.append(err)


def _detect_cycles(graph: dict[str, list[str]]) -> list[list[str]]:
    """Return a list of cycles found in ``graph`` (each as a node list).

    Classic DFS with a grey/black colour set. A back edge to a grey node
    closes a cycle; the cycle slice is reconstructed from the DFS stack.
    Used for both the flag prerequisite graph and the segment reference
    graph; edges to missing nodes are skipped (checked separately).
    """
    WHITE, GREY, BLACK = 0, 1, 2
    color: dict[str, int] = {node: WHITE for node in graph}
    stack: list[str] = []
    cycles: list[list[str]] = []

    def visit(node: str) -> None:
        color[node] = GREY
        stack.append(node)
        for dep in graph.get(node, ()):  # edges to missing flags were checked earlier
            if dep not in color:
                continue
            if color[dep] == GREY:
                idx = stack.index(dep)
                cycles.append(stack[idx:] + [dep])
            elif color[dep] == WHITE:
                visit(dep)
        stack.pop()
        color[node] = BLACK

    for node in graph:
        if color[node] == WHITE:
            visit(node)
    return cycles


def _check_condition(
    errors: list[dict[str, Any]],
    flag: Optional[str],
    cond: Any,
    location: str,
    segments: dict[str, Any],
    *,
    segment: Optional[str] = None,
) -> Optional[str]:
    """Validate one condition object located at ``location``.

    ``location`` is a path like ``rules[1].conditions[2]`` or
    ``segments.vip.conditions[0]`` so the error points at the exact
    condition. Returns the referenced segment name when the condition is a
    valid ``segment`` reference (used to build the reference graph).
    """
    if not isinstance(cond, dict):
        _err(errors, flag, "invalid_condition", f"{location} must be an object", segment=segment)
        return None
    op = cond.get("op")
    if op == "segment":
        ref = cond.get("value")
        if not isinstance(ref, str) or not ref:
            _err(
                errors,
                flag,
                "invalid_segment_reference",
                f"{location} segment reference must be a non-empty segment name,"
                f" got {ref!r}",
                segment=segment,
            )
            return None
        if ref not in segments:
            _err(
                errors,
                flag,
                "missing_segment",
                f"{location} references unknown segment {ref!r}",
                segment=segment,
            )
            return None
        return ref
    if op not in KNOWN_OPS:
        _err(
            errors,
            flag,
            "unknown_operator",
            f"{location} uses unknown operator {op!r};"
            f" allowed: {', '.join(sorted(ALLOWED_OPS))}",
            segment=segment,
        )
    return None


def validate_config(config: dict[str, Any]) -> None:
    """Raise :class:`ConfigValidationError` if ``config`` is invalid."""
    errors: list[dict[str, Any]] = []

    flags = config.get("flags")
    if not isinstance(flags, dict):
        raise ConfigValidationError(
            [{"flag": None, "code": "invalid_config", "message": "config must contain a 'flags' object"}]
        )

    segments = config.get("segments") or {}
    if not isinstance(segments, dict):
        raise ConfigValidationError(
            [{"flag": None, "code": "invalid_config", "message": "'segments' must be an object"}]
        )

    # --- segments: shape, condition operators, reference existence --------
    segment_graph: dict[str, list[str]] = {}
    for seg_name, segment in segments.items():
        if not isinstance(segment, dict):
            _err(
                errors,
                None,
                "invalid_segment",
                f"segment {seg_name!r} must be an object",
                segment=seg_name,
            )
            continue
        segment_graph[seg_name] = []
        conds = segment.get("conditions", []) or []
        if not isinstance(conds, list):
            _err(
                errors,
                None,
                "invalid_segment",
                f"segments.{seg_name}.conditions must be a list",
                segment=seg_name,
            )
            continue
        for c_idx, cond in enumerate(conds):
            ref = _check_condition(
                errors,
                None,
                cond,
                f"segments.{seg_name}.conditions[{c_idx}]",
                segments,
                segment=seg_name,
            )
            if ref is not None:
                segment_graph[seg_name].append(ref)

    # Segment references must be acyclic (direct self-reference included);
    # the graph only holds edges to existing segments, so cycle detection is
    # safe even when some references dangled.
    for cycle in _detect_cycles(segment_graph):
        _err(
            errors,
            None,
            "circular_segment",
            "circular segment reference: " + " -> ".join(cycle),
            segment=cycle[0],
        )

    graph: dict[str, list[str]] = {}

    for name, flag in flags.items():
        if not isinstance(flag, dict):
            _err(errors, name, "invalid_flag", "flag definition must be an object")
            continue

        graph[name] = []

        rollout = flag.get("rollout")
        if rollout is not None:
            if not isinstance(rollout, dict):
                _err(errors, name, "invalid_rollout", "rollout must be an object")
            else:
                pct = rollout.get("percentage")
                if isinstance(pct, bool) or not isinstance(pct, int):
                    _err(
                        errors,
                        name,
                        "invalid_percentage",
                        f"rollout.percentage must be an integer, got {pct!r}",
                    )
                elif not 0 <= pct <= 100:
                    _err(
                        errors,
                        name,
                        "invalid_percentage",
                        f"rollout.percentage must be between 0 and 100, got {pct}",
                    )
                key = rollout.get("key", "user_id")
                if key != "user_id":
                    _err(
                        errors,
                        name,
                        "invalid_rollout_key",
                        f"rollout.key only supports 'user_id', got {key!r}",
                    )

        seen_rule_ids: set[str] = set()
        for idx, rule in enumerate(flag.get("rules", []) or []):
            if not isinstance(rule, dict):
                _err(errors, name, "invalid_rule", f"rules[{idx}] must be an object")
                continue
            rule_id = rule.get("id")
            if rule_id is not None:
                if not isinstance(rule_id, str) or not rule_id:
                    _err(errors, name, "invalid_rule_id", f"rules[{idx}].id must be a non-empty string")
                elif rule_id in seen_rule_ids:
                    _err(errors, name, "duplicate_rule_id", f"duplicate rule id {rule_id!r}")
                else:
                    seen_rule_ids.add(rule_id)
            for c_idx, cond in enumerate(rule.get("conditions", []) or []):
                _check_condition(
                    errors,
                    name,
                    cond,
                    f"rules[{idx}].conditions[{c_idx}]",
                    segments,
                )

        prereqs = flag.get("prerequisites", []) or []
        for p_idx, prereq in enumerate(prereqs):
            if not isinstance(prereq, dict):
                _err(errors, name, "invalid_prerequisite", f"prerequisites[{p_idx}] must be an object")
                continue
            dep = prereq.get("flag")
            if dep == name:
                _err(errors, name, "self_dependency", f"flag {name!r} declares itself as a prerequisite")
                continue
            if dep not in flags:
                _err(
                    errors,
                    name,
                    "missing_prerequisite",
                    f"prerequisite {dep!r} (prerequisites[{p_idx}]) does not exist",
                )
                continue
            graph[name].append(dep)

    if not any(e["code"] == "missing_prerequisite" for e in errors):
        cycles = _detect_cycles(graph)
        for cycle in cycles:
            _err(
                errors,
                cycle[0],
                "circular_dependency",
                "circular prerequisite chain: " + " -> ".join(cycle),
            )

    if errors:
        raise ConfigValidationError(errors)
