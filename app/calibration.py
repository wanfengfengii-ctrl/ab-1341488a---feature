"""Exact calibration-graph engine.

Each directed relation states that a reading ``x`` on the ``frm`` instrument
corresponds to a reading ``y`` on the ``to`` instrument via ``y = a*x + b``,
where ``a`` and ``b`` are exact rational numbers and ``a != 0``.

Every coefficient is kept as :class:`fractions.Fraction`, so no floating
point rounding can mask a contradiction: all paths between two instruments
must induce *exactly* the same affine transform, otherwise the request is
rejected as a conflict.

Relations may carry an optional validity window (``validFrom``/``validUntil``,
half-open ``[validFrom, validUntil)``, either end omitted means unbounded).
Windows only take effect for ``timedReadings`` requests: each timed reading
is replayed against exactly the relations in force at its ``observedAt``
moment, so historical readings are never converted with relations that were
not yet (or no longer) active.  Plain ``readings`` requests ignore validity
windows entirely and behave exactly as before.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from fractions import Fraction
from typing import Iterable, Mapping, Sequence


MIN_INSTRUMENTS = 2
MAX_INSTRUMENTS = 50
MAX_RELATIONS = 100
MAX_READINGS = 10_000
MAX_TIMED_READINGS = 100


class CalibrationError(Exception):
    """Base class for calibration request failures."""

    code = "calibration_error"
    status = 400


class InvalidRequest(CalibrationError):
    """The request payload failed validation."""

    code = "invalid_request"
    status = 400


class Unreachable(CalibrationError):
    """Source and target lie in different connected components.

    For timed requests this means no valid path existed at that reading's
    moment; ``index``/``observed_at`` then locate the failing entry.
    """

    code = "unreachable"
    status = 404

    def __init__(
        self,
        source: str,
        target: str,
        *,
        index: int | None = None,
        observed_at: str | None = None,
    ) -> None:
        message = f"no calibration path connects {source!r} to {target!r}"
        if index is not None:
            message += f" at {observed_at} (timedReadings[{index}])"
        super().__init__(message)
        self.source = source
        self.target = target
        self.index = index
        self.observed_at = observed_at


class CalibrationConflict(CalibrationError):
    """A cycle induces two different exact transforms.

    For timed requests only the relations in force at the failing moment
    take part; ``index``/``observed_at`` then locate the failing entry.
    """

    code = "conflict"
    status = 409

    def __init__(
        self,
        cycle: Sequence[str],
        detail: str,
        *,
        index: int | None = None,
        observed_at: str | None = None,
    ) -> None:
        super().__init__(detail)
        self.cycle = list(cycle)
        self.detail = detail
        self.index = index
        self.observed_at = observed_at


@dataclass(frozen=True)
class Affine:
    """An affine transform ``y = a*x + b`` with exact rational coefficients."""

    a: Fraction
    b: Fraction

    def apply(self, x: Fraction) -> Fraction:
        return self.a * x + self.b

    def compose(self, other: "Affine") -> "Affine":
        """Return ``self ∘ other``: apply ``other`` first, then ``self``."""
        return Affine(self.a * other.a, self.a * other.b + self.b)

    def inverse(self) -> "Affine":
        """Invert the transform (valid because ``a`` is never zero)."""
        inv_a = Fraction(1, 1) / self.a
        return Affine(inv_a, -inv_a * self.b)


IDENTITY = Affine(Fraction(1), Fraction(0))


@dataclass(frozen=True)
class Relation:
    frm: str
    to: str
    a: Fraction
    b: Fraction
    valid_from: datetime | None = None
    valid_until: datetime | None = None

    def as_adjacency(self) -> tuple[str, str, Affine]:
        return self.frm, self.to, Affine(self.a, self.b)

    def is_active_at(self, moment: datetime) -> bool:
        """Half-open window ``[validFrom, validUntil)``; open ends are unbounded."""
        if self.valid_from is not None and moment < self.valid_from:
            return False
        if self.valid_until is not None and moment >= self.valid_until:
            return False
        return True


def parse_rational(value: object, field_name: str) -> Fraction:
    """Parse an integer or a canonical ``p/q`` string exactly.

    Accepts ``int`` and ``str`` values.  Strings must be either a signed
    integer or a signed fraction ``p/q`` with a non-zero denominator.
    Floating point numbers are rejected on purpose: callers must state
    exact rational values instead of relying on binary rounding.
    """
    if isinstance(value, bool):
        raise InvalidRequest(f"{field_name} must be an integer or p/q string, got boolean")
    if isinstance(value, int):
        return Fraction(value)
    if isinstance(value, Fraction):
        return value
    if not isinstance(value, str):
        raise InvalidRequest(
            f"{field_name} must be an integer or p/q string, got {type(value).__name__}"
        )
    text = value.strip()
    if not text:
        raise InvalidRequest(f"{field_name} must be a non-empty rational value")
    if "/" in text:
        num_text, sep, den_text = text.partition("/")
        if not sep or "/" in den_text:
            raise InvalidRequest(f"{field_name}={value!r} is not a valid p/q fraction")
        try:
            numerator = int(num_text, 10)
            denominator = int(den_text, 10)
        except ValueError:
            raise InvalidRequest(f"{field_name}={value!r} is not a valid p/q fraction")
        if denominator == 0:
            raise InvalidRequest(f"{field_name}={value!r} has a zero denominator")
        if denominator < 0:
            numerator, denominator = -numerator, -denominator
        return Fraction(numerator, denominator)
    try:
        return Fraction(int(text, 10))
    except ValueError:
        raise InvalidRequest(f"{field_name}={value!r} is not a valid integer or p/q fraction")


def render_fraction(value: Fraction) -> str:
    """Render a reduced fraction; omit the denominator when it is one."""
    if value.denominator == 1:
        return str(value.numerator)
    return f"{value.numerator}/{value.denominator}"


_RFC3339_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[Zz]|[+-]\d{2}:\d{2})$"
)
_ZERO_OFFSETS = ("+00:00", "-00:00")


def parse_timestamp(value: object, field_name: str) -> datetime:
    """Parse an RFC3339 timestamp that must be expressed in UTC.

    Accepts ``Z``/``z`` or an explicit zero offset (``+00:00``/``-00:00``);
    any other offset is rejected so that moments are never silently shifted
    across validity windows by a timezone conversion.
    """
    if not isinstance(value, str):
        raise InvalidRequest(
            f"{field_name} must be an RFC3339 UTC timestamp string, "
            f"got {type(value).__name__}"
        )
    text = value.strip()
    if not _RFC3339_RE.match(text):
        raise InvalidRequest(
            f"{field_name}={value!r} is not a valid RFC3339 timestamp "
            "(expected e.g. 2024-01-31T12:30:00Z)"
        )
    if text[-1] not in "Zz" and text[-6:] not in _ZERO_OFFSETS:
        raise InvalidRequest(
            f"{field_name}={value!r} must be expressed in UTC (Z or +00:00)"
        )
    try:
        return datetime.fromisoformat(text.upper().replace("Z", "+00:00"))
    except ValueError:
        raise InvalidRequest(f"{field_name}={value!r} is not a valid calendar date/time")


def _require_str(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvalidRequest(f"{field_name} must be a non-empty string")
    return value.strip()


def _extract_readings(payload: Mapping[str, object]) -> list:
    if "readings" not in payload:
        raise InvalidRequest("field 'readings' is required")
    readings = payload["readings"]
    if not isinstance(readings, list):
        raise InvalidRequest("field 'readings' must be a list")
    if not readings:
        raise InvalidRequest("field 'readings' must contain at least one value")
    if len(readings) > MAX_READINGS:
        raise InvalidRequest(f"field 'readings' may contain at most {MAX_READINGS} values")
    return readings


def _extract_timed_readings(payload: Mapping[str, object]) -> list:
    timed = payload["timedReadings"]
    if not isinstance(timed, list):
        raise InvalidRequest("field 'timedReadings' must be a list")
    if not timed:
        raise InvalidRequest("field 'timedReadings' must contain at least one entry")
    if len(timed) > MAX_TIMED_READINGS:
        raise InvalidRequest(
            f"field 'timedReadings' may contain at most {MAX_TIMED_READINGS} entries"
        )
    return timed


@dataclass
class TranslationResult:
    source: str
    target: str
    transform: Affine
    readings_in: list[Fraction] = field(default_factory=list)
    readings_out: list[Fraction] = field(default_factory=list)

    def to_json(self) -> dict:
        return {
            "source": self.source,
            "target": self.target,
            "coefficients": {
                "a": render_fraction(self.transform.a),
                "b": render_fraction(self.transform.b),
            },
            "readings": [render_fraction(x) for x in self.readings_in],
            "results": [render_fraction(y) for y in self.readings_out],
        }


@dataclass
class TimedItemResult:
    """One replayed reading: the echoed input plus its moment-specific answer."""

    index: int
    value: object  # original JSON value, echoed verbatim
    observed_at: str  # original RFC3339 string, echoed verbatim
    transform: Affine
    result: Fraction

    def to_json(self) -> dict:
        return {
            "index": self.index,
            "value": self.value,
            "observedAt": self.observed_at,
            "result": render_fraction(self.result),
            "coefficients": {
                "a": render_fraction(self.transform.a),
                "b": render_fraction(self.transform.b),
            },
        }


@dataclass
class TimedTranslationResult:
    source: str
    target: str
    items: list[TimedItemResult] = field(default_factory=list)

    def to_json(self) -> dict:
        return {
            "source": self.source,
            "target": self.target,
            "timedResults": [item.to_json() for item in self.items],
        }


def translate(payload: Mapping[str, object]) -> TranslationResult | TimedTranslationResult:
    """Validate a request and translate readings from source to target.

    Raises :class:`InvalidRequest`, :class:`Unreachable` or
    :class:`CalibrationConflict`.  The answer never depends on the order in
    which relations were entered: every edge is checked against every path
    already derived, and a mismatch rejects the whole request.

    With ``timedReadings`` each entry is replayed against the relations in
    force at its own ``observedAt`` moment; the batch is all-or-nothing, so
    the first entry without a valid path (or with a conflicting set of
    active relations) aborts the request with its index and moment attached.
    """
    if not isinstance(payload, Mapping):
        raise InvalidRequest("request body must be a JSON object")

    relations_raw = payload.get("relations")
    if not isinstance(relations_raw, list):
        raise InvalidRequest("field 'relations' must be a list")
    if not 1 <= len(relations_raw) <= MAX_RELATIONS:
        raise InvalidRequest(
            f"field 'relations' must contain between 1 and {MAX_RELATIONS} relations"
        )

    source = _require_str(payload.get("source"), "field 'source'")
    target = _require_str(payload.get("target"), "field 'target'")

    has_readings = "readings" in payload
    has_timed = "timedReadings" in payload
    if has_readings and has_timed:
        raise InvalidRequest("fields 'readings' and 'timedReadings' are mutually exclusive")
    raw_timed = _extract_timed_readings(payload) if has_timed else None
    raw_readings = None if has_timed else _extract_readings(payload)

    relations: list[Relation] = []
    vertices: set[str] = set()
    for index, item in enumerate(relations_raw):
        where = f"relations[{index}]"
        if not isinstance(item, Mapping):
            raise InvalidRequest(f"{where} must be an object")
        frm = _require_str(item.get("from"), f"{where}.from")
        to = _require_str(item.get("to"), f"{where}.to")
        if frm == to:
            raise InvalidRequest(f"{where} relates an instrument to itself")
        a = parse_rational(item.get("a"), f"{where}.a")
        if a == 0:
            raise InvalidRequest(f"{where}.a must not be zero")
        b = parse_rational(item.get("b", 0), f"{where}.b")
        valid_from = None
        if "validFrom" in item:
            valid_from = parse_timestamp(item.get("validFrom"), f"{where}.validFrom")
        valid_until = None
        if "validUntil" in item:
            valid_until = parse_timestamp(item.get("validUntil"), f"{where}.validUntil")
        relations.append(Relation(frm, to, a, b, valid_from, valid_until))
        vertices.add(frm)
        vertices.add(to)

    if not MIN_INSTRUMENTS <= len(vertices) <= MAX_INSTRUMENTS:
        raise InvalidRequest(
            f"between {MIN_INSTRUMENTS} and {MAX_INSTRUMENTS} unique instruments "
            f"are required, found {len(vertices)}"
        )
    if source not in vertices:
        raise InvalidRequest(f"source {source!r} does not appear in any relation")
    if target not in vertices:
        raise InvalidRequest(f"target {target!r} does not appear in any relation")

    if raw_timed is not None:
        return _translate_timed(relations, source, target, raw_timed)

    readings = [parse_rational(v, f"readings[{i}]") for i, v in enumerate(raw_readings)]

    total = _solve_component(relations, source).get(target)
    if total is None:
        raise Unreachable(source, target)

    results = [total.apply(x) for x in readings]
    return TranslationResult(source, target, total, readings, results)


def _translate_timed(
    relations: list[Relation],
    source: str,
    target: str,
    raw_items: list,
) -> TimedTranslationResult:
    """Replay each timed reading against the relations in force at its moment.

    Every entry is validated up front; then entries are solved in input
    order.  The first entry whose moment has no valid source->target path,
    or whose active relations contradict each other, fails the whole batch:
    no partial results are ever returned.
    """
    parsed: list[tuple[int, object, str, datetime, Fraction]] = []
    for index, raw in enumerate(raw_items):
        where = f"timedReadings[{index}]"
        if not isinstance(raw, Mapping):
            raise InvalidRequest(f"{where} must be an object")
        if "value" not in raw:
            raise InvalidRequest(f"{where}.value is required")
        if "observedAt" not in raw:
            raise InvalidRequest(f"{where}.observedAt is required")
        observed_raw = raw["observedAt"]
        moment = parse_timestamp(observed_raw, f"{where}.observedAt")
        value = parse_rational(raw["value"], f"{where}.value")
        parsed.append((index, raw["value"], observed_raw, moment, value))

    items: list[TimedItemResult] = []
    for index, raw_value, observed_raw, moment, value in parsed:
        active = [rel for rel in relations if rel.is_active_at(moment)]
        try:
            transforms = _solve_component(active, source)
        except CalibrationConflict as exc:
            exc.index = index
            exc.observed_at = observed_raw
            raise
        total = transforms.get(target)
        if total is None:
            raise Unreachable(source, target, index=index, observed_at=observed_raw)
        items.append(TimedItemResult(index, raw_value, observed_raw, total, total.apply(value)))
    return TimedTranslationResult(source, target, items)


def _solve_component(relations: Iterable[Relation], source: str) -> dict[str, Affine]:
    """Derive the source->instrument transform for every reachable node.

    Edges are processed in a deterministic traversal, so results do not
    depend on entry order: each edge asserts an equality between two node
    transforms, and any disagreement on a closure edge is a contradictory
    cycle.
    """
    # adjacency: node -> list of (neighbour, transform node->neighbour)
    adjacency: dict[str, list[tuple[str, Affine]]] = {}
    for rel in relations:
        u, v, forward = rel.as_adjacency()
        adjacency.setdefault(u, []).append((v, forward))
        adjacency.setdefault(v, []).append((u, forward.inverse()))

    # Deterministic traversal regardless of the caller's relation ordering.
    for neighbours in adjacency.values():
        neighbours.sort(key=lambda item: item[0])

    transforms: dict[str, Affine] = {source: IDENTITY}
    parent: dict[str, str | None] = {source: None}
    order = [source]
    head = 0
    while head < len(order):
        node = order[head]
        head += 1
        node_tf = transforms[node]
        for neighbour, edge_tf in adjacency.get(node, ()):
            candidate = edge_tf.compose(node_tf)
            existing = transforms.get(neighbour)
            if existing is None:
                transforms[neighbour] = candidate
                parent[neighbour] = node
                order.append(neighbour)
            elif existing != candidate:
                cycle = _cycle_path(parent, node, neighbour)
                detail = (
                    f"contradictory calibration cycle {cycle}: "
                    f"{source}->{neighbour} is both {_fmt(existing)} and {_fmt(candidate)}"
                )
                raise CalibrationConflict(cycle, detail)
    return transforms


def _cycle_path(parent: Mapping[str, str | None], u: str, v: str) -> list[str]:
    """Return the vertices of the fundamental cycle closing with edge v->u.

    It is the spanning-tree path from ``u`` up to their lowest common
    ancestor and back down to ``v``; the contradicting edge itself closes
    the loop (``v`` -> ``u``), so ``u`` is not repeated at the tail.
    """

    def depth_of(node: str) -> int:
        depth = 0
        while parent[node] is not None:
            node = parent[node]  # type: ignore[assignment]
            depth += 1
        return depth

    # Walk both pointers upward in lockstep until they coincide at the LCA.
    pu, pv = u, v
    du, dv = depth_of(u), depth_of(v)
    u_chain: list[str] = []
    v_chain: list[str] = []
    while du > dv:
        u_chain.append(pu)
        pu = parent[pu]  # type: ignore[assignment]
        du -= 1
    while dv > du:
        v_chain.append(pv)
        pv = parent[pv]  # type: ignore[assignment]
        dv -= 1
    while pu != pv:
        u_chain.append(pu)
        v_chain.append(pv)
        pu = parent[pu]  # type: ignore[assignment]
        pv = parent[pv]  # type: ignore[assignment]
    lca = pu

    return u_chain + [lca] + list(reversed(v_chain))


def _fmt(tf: Affine) -> str:
    return f"a={render_fraction(tf.a)}, b={render_fraction(tf.b)}"
