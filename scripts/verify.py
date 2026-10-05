"""One-shot verification service.

Runs after the API container is healthy (see docker-compose.yml) and:

1. runs the code test suite,
2. performs a build sanity check (byte-compilation of every module),
3. exercises the HTTP API with a consistent chain (including two paths
   that must agree), a contradictory cycle, an unreachable pair and an
   invalid relation,
4. replays timed readings across calibration activation/revocation
   boundaries, including missing-path and exact-conflict instants,

then prints a summary and exits non-zero if anything failed.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

BASE_URL = os.environ.get("BASE_URL", "http://127.0.0.1:8000")
HEALTH_URL = f"{BASE_URL}/health"
TRANSLATE_URL = f"{BASE_URL}/api/calibrations/translate"

failures: list[str] = []


def wait_for_healthy(timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(HEALTH_URL, timeout=2) as resp:
                if resp.status == 200:
                    return True
        except Exception as exc:  # noqa: BLE001 - retry any startup error
            last_error = exc
        time.sleep(0.5)
    print(f"[wait] service never became healthy: {last_error}", file=sys.stderr)
    return False


def run_step(name: str, cmd: list[str]) -> bool:
    print(f"\n=== {name}: {' '.join(cmd)} ===", flush=True)
    proc = subprocess.run(cmd, cwd="/app" if os.path.isdir("/app") else ".")
    ok = proc.returncode == 0
    print(f"--- {name}: {'PASS' if ok else 'FAIL'} (exit {proc.returncode}) ---")
    if not ok:
        failures.append(name)
    return ok


def post(payload: dict) -> tuple[int, dict]:
    req = urllib.request.Request(
        TRANSLATE_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"[smoke] {name}: {'PASS' if condition else 'FAIL'} {detail}".rstrip())
    if not condition:
        failures.append(name)


def smoke() -> None:
    print("\n=== API smoke tests ===", flush=True)

    consistent = {
        "source": "A",
        "target": "C",
        "relations": [
            {"from": "A", "to": "B", "a": "2", "b": "1"},
            {"from": "B", "to": "C", "a": "1/2", "b": "1/3"},
        ],
        "readings": ["0", "1", "5/2"],
    }
    status, body = post(consistent)
    check(
        "consistent chain returns 200 and exact transform",
        status == 200
        and body.get("coefficients") == {"a": "1", "b": "5/6"}
        and body.get("results") == ["5/6", "11/6", "10/3"],
        f"status={status} body={body}",
    )

    diamond = {
        "source": "A",
        "target": "C",
        "relations": [
            {"from": "A", "to": "B", "a": "2", "b": "1"},
            {"from": "A", "to": "D", "a": "4", "b": "2"},
            {"from": "B", "to": "C", "a": "1/2", "b": "1/3"},
            {"from": "D", "to": "C", "a": "1/4", "b": "1/3"},
        ],
        "readings": ["2"],
    }
    status, body = post(diamond)
    check(
        "two agreeing paths both resolve to x + 5/6",
        status == 200
        and body.get("coefficients") == {"a": "1", "b": "5/6"}
        and body.get("results") == ["17/6"],
        f"status={status} body={body}",
    )

    contradictory = {
        "source": "A",
        "target": "C",
        "relations": [
            {"from": "A", "to": "B", "a": "2", "b": "1"},
            {"from": "B", "to": "C", "a": "3", "b": "0"},
            {"from": "C", "to": "A", "a": "1/6", "b": "0"},
        ],
        "readings": ["1"],
    }
    status, body = post(contradictory)
    error = body.get("error", {})
    check(
        "contradictory cycle is rejected (409 conflict, no results)",
        status == 409
        and error.get("code") == "conflict"
        and set(error.get("cycle", [])) == {"A", "B", "C"}
        and "results" not in body,
        f"status={status} body={body}",
    )

    disconnected = {
        "source": "A",
        "target": "E",
        "relations": [
            {"from": "A", "to": "B", "a": "2", "b": "1"},
            {"from": "D", "to": "E", "a": "1", "b": "0"},
        ],
        "readings": ["1"],
    }
    status, body = post(disconnected)
    error = body.get("error", {})
    check(
        "unreachable pair returns recognizable error",
        status == 404 and error.get("code") == "unreachable"
        and error.get("source") == "A" and error.get("target") == "E",
        f"status={status} body={body}",
    )

    invalid = {
        "source": "A",
        "target": "B",
        "relations": [{"from": "A", "to": "B", "a": "0", "b": "0"}],
        "readings": ["1"],
    }
    status, body = post(invalid)
    check(
        "zero multiplier is rejected",
        status == 400 and body.get("error", {}).get("code") == "invalid_request",
        f"status={status} body={body}",
    )


def timed_smoke() -> None:
    print("\n=== timed replay smoke tests (across validity boundaries) ===", flush=True)

    # Phased calibrations between A and C via B:
    #   era 1 [2026-01-01, 2026-01-02): A->B = 2x+1  -> A->C = x + 5/6
    #   era 2 [2026-01-02, unbounded):  A->B = 3x    -> A->C = 3/2 x + 1/3
    #   B->C = (1/2)x + 1/3 is always in force.
    relations = [
        {
            "from": "A", "to": "B", "a": "2", "b": "1",
            "validFrom": "2026-01-01T00:00:00Z",
            "validUntil": "2026-01-02T00:00:00Z",
        },
        {
            "from": "A", "to": "B", "a": "3", "b": "0",
            "validFrom": "2026-01-02T00:00:00Z",
        },
        {"from": "B", "to": "C", "a": "1/2", "b": "1/3"},
    ]

    replay = {
        "source": "A",
        "target": "C",
        "relations": relations,
        "timedReadings": [
            # exactly at the inclusive lower bound
            {"value": "1", "observedAt": "2026-01-01T00:00:00Z"},
            # inside era 1, given with a non-UTC offset -> normalised
            {"value": "1", "observedAt": "2026-01-01T13:00:00-11:00"},
            # exactly at the exclusive end / next era's inclusive start
            {"value": "1", "observedAt": "2026-01-02T00:00:00Z"},
            # deep into era 2
            {"value": "4/3", "observedAt": "2026-06-01T00:00:00Z"},
        ],
    }
    status, body = post(replay)
    expected_items = [
        {"value": "1", "observedAt": "2026-01-01T00:00:00Z",
         "coefficient": "1", "result": "11/6"},
        {"value": "1", "observedAt": "2026-01-02T00:00:00Z",
         "coefficient": "3/2", "result": "11/6"},
        {"value": "1", "observedAt": "2026-01-02T00:00:00Z",
         "coefficient": "3/2", "result": "11/6"},
        {"value": "4/3", "observedAt": "2026-06-01T00:00:00Z",
         "coefficient": "3/2", "result": "7/3"},
    ]
    check(
        "timed replay resolves each reading with the era-correct coefficient",
        status == 200 and body.get("timedReadings") == expected_items,
        f"status={status} body={body}",
    )

    # Old calibration revoked, new one not yet active -> no valid path.
    no_path = {
        "source": "A", "target": "C",
        "relations": relations,
        "timedReadings": [
            {"value": "1", "observedAt": "2026-01-01T00:00:00Z"},
            {"value": "1", "observedAt": "2025-12-31T23:59:59Z"},
        ],
    }
    status, body = post(no_path)
    error = body.get("error", {})
    check(
        "reading before any activation is unreachable and locates the item",
        status == 404 and error.get("code") == "unreachable"
        and error.get("index") == 1
        and error.get("observedAt") == "2025-12-31T23:59:59Z"
        and "timedReadings" not in body,
        f"status={status} body={body}",
    )

    # An exact contradiction that exists only inside one validity window.
    conflict_rels = [
        {"from": "A", "to": "B", "a": "2", "b": "1",
         "validFrom": "2026-01-01T00:00:00Z"},
        {"from": "B", "to": "C", "a": "3", "b": "0",
         "validFrom": "2026-01-01T00:00:00Z"},
        {"from": "C", "to": "A", "a": "1/6", "b": "0",
         "validFrom": "2026-01-02T00:00:00Z",
         "validUntil": "2026-01-03T00:00:00Z"},
    ]
    conflict_batch = {
        "source": "A", "target": "C",
        "relations": conflict_rels,
        "timedReadings": [
            {"value": "7/3", "observedAt": "2026-01-01T00:00:00Z"},
            {"value": "7/3", "observedAt": "2026-01-02T12:00:00Z"},
            {"value": "7/3", "observedAt": "2026-01-03T00:00:00Z"},
        ],
    }
    status, body = post(conflict_batch)
    error = body.get("error", {})
    check(
        "exact conflict in one era rejects the batch and locates the item",
        status == 409 and error.get("code") == "conflict"
        and error.get("index") == 1
        and error.get("observedAt") == "2026-01-02T12:00:00Z"
        and set(error.get("cycle", [])) == {"A", "B", "C"}
        and "timedReadings" not in body,
        f"status={status} body={body}",
    )

    # The same readings before activation and after revocation are fine.
    resolved = dict(conflict_batch)
    resolved["timedReadings"] = [
        {"value": "7/3", "observedAt": "2026-01-01T00:00:00Z"},
        {"value": "7/3", "observedAt": "2026-01-03T00:00:00Z"},
    ]
    status, body = post(resolved)
    check(
        "graph is consistent again outside the contradictory window",
        status == 200
        and [item["result"] for item in body.get("timedReadings", [])] == ["17", "17"],
        f"status={status} body={body}",
    )

    # Mutually exclusive input fields.
    bad = dict(replay)
    bad["readings"] = ["1"]
    status, body = post(bad)
    check(
        "readings and timedReadings are mutually exclusive",
        status == 400 and body.get("error", {}).get("code") == "invalid_request",
        f"status={status} body={body}",
    )

    # Legacy response shape must be byte-for-byte unchanged even when a
    # relation carries a validity window: windows are ignored and the one
    # relation is in force exactly as before.
    legacy = {
        "source": "A", "target": "C",
        "relations": [
            {"from": "A", "to": "B", "a": "2", "b": "1",
             "validFrom": "2030-01-01T00:00:00Z"},
            {"from": "B", "to": "C", "a": "1/2", "b": "1/3"},
        ],
        "readings": ["0", "1", "5/2"],
    }
    status, body = post(legacy)
    check(
        "legacy readings request keeps original shape and semantics",
        status == 200
        and body == {
            "source": "A", "target": "C",
            "coefficients": {"a": "1", "b": "5/6"},
            "readings": ["0", "1", "5/2"],
            "results": ["5/6", "11/6", "10/3"],
        },
        f"status={status} body={body}",
    )


def main() -> int:
    if not wait_for_healthy():
        return 1
    run_step("unit tests", [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"])
    run_step("build sanity (compileall)", [sys.executable, "-m", "compileall", "-q", "app", "tests"])
    smoke()
    timed_smoke()

    print("\n=== verification summary ===")
    if failures:
        print(f"FAILED ({len(failures)}): {', '.join(failures)}", file=sys.stderr)
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
