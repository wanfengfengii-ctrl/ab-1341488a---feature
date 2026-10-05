"""Exact calibration-graph engine.

Each directed relation states that a reading ``x`` on the ``frm`` instrument
corresponds to a reading ``y`` on the ``to`` instrument via ``y = a*x + b``,
where ``a`` and ``b`` are exact rational numbers and ``a != 0``.

Every coefficient is kept as :class:`fractions.Fraction`, so no floating
point rounding can mask a contradiction: all paths between two instruments
must induce *exactly* the same affine transform, otherwise the request is
rejected as a conflict.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from fractions import Fraction
from typing import Iterable, Mapping, Sequence


MIN_INSTRUMENTS = 2
MAX_INSTRUMENTS = 50
MAX_RELATIONS = 100
MAX_READINGS = 10_000
MAX_TIMED_READINGS = 100

# Strict RFC3339 timestamp with a mandatory offset (``Z`` or ``±HH:MM``);
# values are normalised to UTC (see :func:`parse_utc`).
_RFC3339_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d+))?(Z|[+-]\d{2}:\d{2})$"
)


class CalibrationError(Exception):
    """Base class for calibration request failures."""

    code = "calibration_error"
    status = 400


class InvalidRequest(CalibrationError):
    """The request payload failed validation."""

    code = "invalid_request"
    status = 400


class Unreachable(CalibrationError):
    """Source and target lie in different connected components."""

    code = "unreachable"
    status = 404

    def __init__(self, source: str, target: str) -> None:
        super().__init__(f"no calibration path connects {source!r} to {target!r}")
        self.source = source
        self.target = target


class CalibrationConflict(CalibrationError):
    """A cycle induces two different exact transforms."""

    code = "conflict"
    status = 409

    def __init__(self, cycle: Sequence[str], detail: str) -> None:
        super().__init__(detail)
        self.cycle = list(cycle)
        self.detail = detail


class TimedItemError(CalibrationError):
    """Base class for failures attributed to a single timed reading."""

    def __init__(self, detail: str, index: int, observed_at: str) -> None:
        super().__init__(detail)
        self.index = index
        self.observed_at = observed_at


class TimedUnreachable(TimedItemError):
    """No valid calibration path exists for this reading's instant."""

    code = "unreachable"
    status = 404


class TimedConflict(TimedItemError):
    """Relations valid at the reading's instant form an exact conflict."""

    code = "conflict"
    status = 409

    def __init__(
        self, detail: str, index: int, observed_at: str, cycle: Sequence[str]
    ) -> None:
        super().__init__(detail, index, observed_at)
        self.cycle = list(cycle)


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

    def valid_at(self, instant: datetime) -> bool:
        """Half-open validity interval: ``[valid_from, valid_until)``."""
        if self.valid_from is not None and instant < self.valid_from:
            return False
        if self.valid_until is not None and instant >= self.valid_until:
            return False
        return True


def parse_utc(value: object, field_name: str) -> datetime:
    """Parse an RFC3339 timestamp into an aware UTC :class:`datetime`.

    A ``Z`` suffix or an explicit offset is mandatory; a naive timestamp
    would be ambiguous for historical replay and is rejected.  Non-UTC
    offsets are accepted as input but normalised to UTC so that interval
    comparisons are unambiguous.
    """
    if not isinstance(value, str) or not value.strip():
        raise InvalidRequest(f"{field_name} must be an RFC3339 UTC timestamp string")
    text = value.strip()
    match = _RFC3339_RE.match(text)
    if not match:
        raise InvalidRequest(f"{field_name}={value!r} is not a valid RFC3339 UTC timestamp")
    fraction = match.group(2)
    if fraction is not None and len(fraction) > 6:
        # datetime's microsecond resolution would silently truncate, which
        # could move a reading across a validity boundary.
        raise InvalidRequest(
            f"{field_name}={value!r} must use at most microsecond (6 digit) precision"
        )
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise InvalidRequest(f"{field_name}={value!r} is not a valid RFC3339 UTC timestamp")
    if parsed.tzinfo is None:  # pragma: no cover - regex already requires an offset
        raise InvalidRequest(f"{field_name}={value!r} must include a UTC offset")
    return parsed.astimezone(timezone.utc)


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


def _require_str(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvalidRequest(f"{field_name} must be a non-empty string")
    return value.strip()


def _extract_readings(payload: Mapping[str, object]) -> list:
    if "timedReadings" in payload and "readings" in payload:
        raise InvalidRequest("fields 'readings' and 'timedReadings' are mutually exclusive")
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


def _extract_timed_readings(payload: Mapping[str, object]) -> list[tuple[Fraction, datetime]]:
    if "timedReadings" not in payload:
        return []
    if "readings" in payload:
        raise InvalidRequest("fields 'readings' and 'timedReadings' are mutually exclusive")
    items = payload["timedReadings"]
    if not isinstance(items, list):
        raise InvalidRequest("field 'timedReadings' must be a list")
    if not items:
        raise InvalidRequest("field 'timedReadings' must contain at least one value")
    if len(items) > MAX_TIMED_READINGS:
        raise InvalidRequest(
            f"field 'timedReadings' may contain at most {MAX_TIMED_READINGS} values"
        )
    parsed: list[tuple[Fraction, datetime]] = []
    for index, item in enumerate(items):
        where = f"timedReadings[{index}]"
        if not isinstance(item, Mapping):
            raise InvalidRequest(f"{where} must be an object")
        if "value" not in item:
            raise InvalidRequest(f"{where}.value is required")
        if "observedAt" not in item:
            raise InvalidRequest(f"{where}.observedAt is required")
        value = parse_rational(item["value"], f"{where}.value")
        observed_at = parse_utc(item["observedAt"], f"{where}.observedAt")
        parsed.append((value, observed_at))
    return parsed


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
class TimedReadingResult:
    value: Fraction
    observed_at: datetime
    transform: Affine
    result: Fraction

    def to_json(self) -> dict:
        return {
            "value": render_fraction(self.value),
            "observedAt": format_utc(self.observed_at),
            "coefficient": render_fraction(self.transform.a),
            "result": render_fraction(self.result),
        }


@dataclass
class TimedTranslationResult:
    source: str
    target: str
    items: list[TimedReadingResult]

    def to_json(self) -> dict:
        return {
            "source": self.source,
            "target": self.target,
            "timedReadings": [item.to_json() for item in self.items],
        }


def format_utc(instant: datetime) -> str:
    """Render an aware UTC instant canonically with a ``Z`` suffix."""
    text = instant.astimezone(timezone.utc).isoformat()
    return text[:-6] + "Z" if text.endswith("+00:00") else text


def translate(payload: Mapping[str, object]) -> TranslationResult | TimedTranslationResult:
    """Validate a request and translate readings from source to target.

    Raises :class:`InvalidRequest`, :class:`Unreachable` or
    :class:`CalibrationConflict`.  The answer never depends on the order in
    which relations were entered: every edge is checked against every path
    already derived, and a mismatch rejects the whole request.

    With ``timedReadings`` instead of ``readings`` each reading is replayed
    against only the relations valid at that reading's instant; failures of
    a single item carry the item's index and ``observedAt`` and reject the
    whole batch.
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
    timed_mode = "timedReadings" in payload
    if timed_mode:
        raw_timed = _extract_timed_readings(payload)
    else:
        raw_readings = _extract_readings(payload)

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
        valid_from = (
            parse_utc(item["validFrom"], f"{where}.validFrom")
            if "validFrom" in item and item["validFrom"] is not None
            else None
        )
        valid_until = (
            parse_utc(item["validUntil"], f"{where}.validUntil")
            if "validUntil" in item and item["validUntil"] is not None
            else None
        )
        if (
            valid_from is not None
            and valid_until is not None
            and not valid_from < valid_until
        ):
            raise InvalidRequest(
                f"{where}.validFrom must be strictly before {where}.validUntil"
            )
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

    if timed_mode:
        return _translate_timed(relations, source, target, raw_timed)

    readings = [parse_rational(v, f"readings[{i}]") for i, v in enumerate(raw_readings)]

    # Legacy semantics ignore validity windows entirely: every relation is
    # in force for untimed requests, even when the request also happens to
    # carry validity fields.
    total = _solve_component(relations, source).get(target)
    if total is None:
        raise Unreachable(source, target)

    results = [total.apply(x) for x in readings]
    return TranslationResult(source, target, total, readings, results)


def _translate_timed(
    relations: Sequence[Relation],
    source: str,
    target: str,
    raw_timed: Sequence[tuple[Fraction, datetime]],
) -> TimedTranslationResult:
    items: list[TimedReadingResult] = []
    for index, (value, observed_at) in enumerate(raw_timed):
        active = [rel for rel in relations if rel.valid_at(observed_at)]
        try:
            transforms = _solve_component(active, source)
        except CalibrationConflict as exc:
            raise TimedConflict(
                f"relations valid at {format_utc(observed_at)} form a contradictory "
                f"calibration: {exc.detail}",
                index,
                format_utc(observed_at),
                exc.cycle,
            ) from exc
        total = transforms.get(target)
        if total is None:
            raise TimedUnreachable(
                f"no valid calibration path connects {source!r} to {target!r} "
                f"at {format_utc(observed_at)}",
                index,
                format_utc(observed_at),
            )
        items.append(TimedReadingResult(value, observed_at, total, total.apply(value)))
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
