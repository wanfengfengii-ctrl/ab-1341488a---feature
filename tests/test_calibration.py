import json
import threading
import unittest
import urllib.request
from fractions import Fraction
from http.server import ThreadingHTTPServer

from app import calibration as cal
from app.server import CalibrationHandler


def P(**over):
    payload = {
        "source": "A",
        "target": "C",
        "relations": [
            {"from": "A", "to": "B", "a": "2", "b": "1"},
            {"from": "B", "to": "C", "a": "1/2", "b": "1/3"},
        ],
        "readings": ["0", "1", "5/2"],
    }
    payload.update(over)
    return payload


class ParseRationalTests(unittest.TestCase):
    def test_integers_and_fractions(self):
        self.assertEqual(cal.parse_rational(3, "x"), Fraction(3))
        self.assertEqual(cal.parse_rational("-4", "x"), Fraction(-4))
        self.assertEqual(cal.parse_rational(" 2/3 ", "x"), Fraction(2, 3))
        self.assertEqual(cal.parse_rational("-6/9", "x"), Fraction(-2, 3))
        self.assertEqual(cal.parse_rational("1/-2", "x"), Fraction(-1, 2))

    def test_rejects_floats_and_garbage(self):
        for bad in [1.5, True, "1.5", "1/0", "1/2/3", "", None, "abc"]:
            with self.assertRaises(cal.InvalidRequest):
                cal.parse_rational(bad, "x")


class TranslationTests(unittest.TestCase):
    def test_simple_chain_exact(self):
        # A->B: y=2x+1 ; B->C: y=(1/2)x+1/3
        # A->C: y = (1/2)(2x+1)+1/3 = x + 5/6
        r = cal.translate(P())
        self.assertEqual(r.transform, cal.Affine(Fraction(1), Fraction(5, 6)))
        self.assertEqual(r.readings_out, [Fraction(5, 6), Fraction(11, 6), Fraction(10, 3)])
        body = r.to_json()
        self.assertEqual(body["coefficients"], {"a": "1", "b": "5/6"})
        self.assertEqual(body["results"], ["5/6", "11/6", "10/3"])

    def test_reverse_direction_uses_inverse(self):
        # converting a B-reading back to an A-reading: A = (B-1)/2
        r = cal.translate(P(source="B", target="A", readings=["4"]))
        self.assertEqual(r.transform, cal.Affine(Fraction(1, 2), Fraction(-1, 2)))
        self.assertEqual(r.readings_out, [Fraction(3, 2)])

    def test_consistent_cycle_accepted_regardless_of_order(self):
        # A->B: 2x+1 ; B->C: 3x ; C->A must be inverse of composition:
        # A->C: 6x+3, so C->A: (x-3)/6 = 1/6 x - 1/2
        rels = [
            {"from": "A", "to": "B", "a": "2", "b": "1"},
            {"from": "B", "to": "C", "a": "3", "b": "0"},
            {"from": "C", "to": "A", "a": "1/6", "b": "-1/2"},
        ]
        answers = []
        for ordering in (rels, list(reversed(rels)), [rels[2], rels[0], rels[1]]):
            r = cal.translate(P(relations=ordering, target="C", readings=["7/3"]))
            answers.append((r.transform.a, r.transform.b, r.readings_out[0]))
        self.assertEqual(len(set(answers)), 1)
        self.assertEqual(answers[0], (Fraction(6), Fraction(3), Fraction(17)))

    def test_diamond_two_paths_must_agree(self):
        rels = [
            {"from": "A", "to": "B", "a": "2", "b": "1"},
            {"from": "A", "to": "D", "a": "4", "b": "2"},
            {"from": "B", "to": "C", "a": "1/2", "b": "1/3"},
            {"from": "D", "to": "C", "a": "1/4", "b": "1/3"},
        ]
        # both paths yield x + 5/6
        r = cal.translate(P(relations=rels))
        self.assertEqual(r.transform.b, Fraction(5, 6))

    def test_contradictory_cycle_conflict(self):
        rels = [
            {"from": "A", "to": "B", "a": "2", "b": "1"},
            {"from": "B", "to": "C", "a": "3", "b": "0"},
            # claims C = (1/6) A (i.e. A=6C), but path says A->C is 6x+3
            {"from": "C", "to": "A", "a": "1/6", "b": "0"},
        ]
        with self.assertRaises(cal.CalibrationConflict) as ctx:
            cal.translate(P(relations=rels))
        self.assertEqual(ctx.exception.code, "conflict")
        self.assertEqual(set(ctx.exception.cycle), {"A", "B", "C"})

    def test_conflict_does_not_leak_results_in_any_order(self):
        rels = [
            {"from": "A", "to": "B", "a": "2", "b": "1"},
            {"from": "B", "to": "C", "a": "3", "b": "0"},
            {"from": "C", "to": "A", "a": "1/6", "b": "0"},
        ]
        for ordering in (rels, list(reversed(rels)), [rels[0], rels[2], rels[1]]):
            with self.assertRaises(cal.CalibrationConflict):
                cal.translate(P(relations=ordering))

    def test_tiny_rational_difference_is_still_conflict(self):
        # Paths agree to within 10**-30; float rounding would hide this,
        # exact arithmetic must not.
        rels = [
            {"from": "A", "to": "B", "a": "1", "b": "0"},
            {"from": "B", "to": "C", "a": "1", "b": "1/1000000000000000000000000000001"},
            {"from": "A", "to": "C", "a": "1", "b": "0"},
        ]
        with self.assertRaises(cal.CalibrationConflict):
            cal.translate(P(relations=rels))

    def test_unreachable(self):
        rels = [
            {"from": "A", "to": "B", "a": "2", "b": "1"},
            {"from": "D", "to": "E", "a": "1", "b": "0"},
        ]
        with self.assertRaises(cal.Unreachable) as ctx:
            cal.translate(P(relations=rels, target="E"))
        self.assertEqual(ctx.exception.code, "unreachable")

    def test_validation(self):
        with self.assertRaises(cal.InvalidRequest):
            cal.translate(P(relations=[]))
        with self.assertRaises(cal.InvalidRequest):
            cal.translate(P(relations=[{"from": "A", "to": "B", "a": "0", "b": "0"}]))
        with self.assertRaises(cal.InvalidRequest):
            cal.translate(P(source="Z"))
        with self.assertRaises(cal.InvalidRequest):
            cal.translate(P(readings=[]))
        with self.assertRaises(cal.InvalidRequest):
            cal.translate(P(relations=[{"from": "A", "to": "A", "a": "1", "b": "0"}]))

    def test_too_many_or_too_few_instruments(self):
        rels = [{"from": f"I{i}", "to": f"I{i+1}", "a": "1", "b": "0"} for i in range(50)]
        with self.assertRaises(cal.InvalidRequest):  # 51 instruments
            cal.translate(P(relations=rels, source="I0", target="I50"))
        with self.assertRaises(cal.InvalidRequest):  # 1 instrument
            cal.translate(P(relations=[{"from": "A", "to": "A", "a": "1", "b": "0"}]))

    def test_results_reduced_fractions(self):
        r = cal.translate(P(readings=["2"]))
        self.assertEqual(r.readings_out, [Fraction(17, 6)])
        self.assertEqual(r.to_json()["results"], ["17/6"])


def timed_payload(**over):
    payload = {
        "source": "A",
        "target": "C",
        "relations": [
            # Era 1 (Jan 1st): A->B = 2x+1, hence A->C = x + 5/6
            {
                "from": "A", "to": "B", "a": "2", "b": "1",
                "validFrom": "2026-01-01T00:00:00Z",
                "validUntil": "2026-01-02T00:00:00Z",
            },
            # Era 2 (from Jan 2nd, unbounded): A->B = 3x, hence A->C = 3/2 x + 1/3
            {
                "from": "A", "to": "B", "a": "3", "b": "0",
                "validFrom": "2026-01-02T00:00:00Z",
            },
            # B->C always in force.
            {"from": "B", "to": "C", "a": "1/2", "b": "1/3"},
        ],
        "timedReadings": [
            {"value": "1", "observedAt": "2026-01-01T00:00:00Z"},
            {"value": "1", "observedAt": "2026-01-02T00:00:00Z"},
        ],
    }
    payload.update(over)
    return payload


class TimedTranslationTests(unittest.TestCase):
    def test_uses_relations_valid_at_each_instant(self):
        r = cal.translate(timed_payload())
        body = r.to_json()
        self.assertEqual(
            body["timedReadings"],
            [
                {
                    "value": "1",
                    "observedAt": "2026-01-01T00:00:00Z",
                    "coefficient": "1",
                    "result": "11/6",
                },
                {
                    "value": "1",
                    "observedAt": "2026-01-02T00:00:00Z",
                    "coefficient": "3/2",
                    "result": "11/6",
                },
            ],
        )
        # No legacy fields leak into a timed response.
        self.assertNotIn("coefficients", body)
        self.assertNotIn("results", body)
        self.assertNotIn("readings", body)

    def test_half_open_interval_boundaries(self):
        # validUntil is exclusive: exactly at 2026-01-02T00:00:00Z era 1 is
        # already gone; validFrom is inclusive, so era 2 starts then.
        payload = timed_payload(
            timedReadings=[
                {"value": "0", "observedAt": "2026-01-01T23:59:59.999999Z"},
                {"value": "0", "observedAt": "2026-01-02T00:00:00Z"},
            ]
        )
        body = cal.translate(payload).to_json()
        self.assertEqual(body["timedReadings"][0]["coefficient"], "1")
        self.assertEqual(body["timedReadings"][1]["coefficient"], "3/2")

    def test_order_preserved_and_offset_normalised_to_utc(self):
        payload = timed_payload(
            timedReadings=[
                {"value": "2", "observedAt": "2026-01-02T08:00:00+08:00"},
                {"value": "2", "observedAt": "2026-01-01T01:00:00+01:00"},
            ]
        )
        body = cal.translate(payload).to_json()
        self.assertEqual(
            [item["observedAt"] for item in body["timedReadings"]],
            ["2026-01-02T00:00:00Z", "2026-01-01T00:00:00Z"],
        )
        self.assertEqual(
            [item["result"] for item in body["timedReadings"]],
            ["10/3", "17/6"],
        )

    def test_unbounded_validity_ends(self):
        # Relation only bounded at one end each.
        payload = {
            "source": "A", "target": "B",
            "relations": [
                {"from": "A", "to": "B", "a": "2", "b": "0",
                 "validUntil": "2026-01-01T00:00:00Z"},
                {"from": "A", "to": "B", "a": "3", "b": "0",
                 "validFrom": "2026-01-01T00:00:00Z"},
            ],
            "timedReadings": [
                {"value": "1", "observedAt": "2025-12-31T23:59:59Z"},
                {"value": "1", "observedAt": "2026-01-01T00:00:00Z"},
            ],
        }
        body = cal.translate(payload).to_json()
        self.assertEqual(
            [item["result"] for item in body["timedReadings"]], ["2", "3"]
        )

    def test_no_valid_path_at_instant_is_item_error(self):
        payload = timed_payload(
            timedReadings=[
                {"value": "1", "observedAt": "2026-01-01T00:00:00Z"},  # fine
                {"value": "1", "observedAt": "2025-12-31T23:59:59Z"},  # no A->B yet
            ]
        )
        with self.assertRaises(cal.TimedUnreachable) as ctx:
            cal.translate(payload)
        self.assertEqual(ctx.exception.code, "unreachable")
        self.assertEqual(ctx.exception.status, 404)
        self.assertEqual(ctx.exception.index, 1)
        self.assertEqual(ctx.exception.observed_at, "2025-12-31T23:59:59Z")

    def test_conflict_valid_only_at_instant_is_item_error(self):
        relations = [
            {"from": "A", "to": "B", "a": "2", "b": "1",
             "validFrom": "2026-01-01T00:00:00Z"},
            {"from": "B", "to": "C", "a": "3", "b": "0",
             "validFrom": "2026-01-01T00:00:00Z"},
            # Contradicts the path (would require b = -1/2) only on Jan 2nd.
            {"from": "C", "to": "A", "a": "1/6", "b": "0",
             "validFrom": "2026-01-02T00:00:00Z",
             "validUntil": "2026-01-03T00:00:00Z"},
        ]
        payload = timed_payload(
            relations=relations,
            timedReadings=[
                {"value": "7/3", "observedAt": "2026-01-01T00:00:00Z"},
                {"value": "7/3", "observedAt": "2026-01-02T12:00:00Z"},
                {"value": "7/3", "observedAt": "2026-01-03T00:00:00Z"},
            ],
        )
        with self.assertRaises(cal.TimedConflict) as ctx:
            cal.translate(payload)
        self.assertEqual(ctx.exception.code, "conflict")
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(ctx.exception.index, 1)
        self.assertEqual(ctx.exception.observed_at, "2026-01-02T12:00:00Z")
        self.assertEqual(set(ctx.exception.cycle), {"A", "B", "C"})

        # Outside the contradictory edge's window the graph is fine.
        payload["timedReadings"] = [
            {"value": "7/3", "observedAt": "2026-01-01T00:00:00Z"},
            {"value": "7/3", "observedAt": "2026-01-03T00:00:00Z"},
        ]
        body = cal.translate(payload).to_json()
        self.assertEqual(
            [item["result"] for item in body["timedReadings"]], ["17", "17"]
        )

    def test_batch_is_atomic_no_partial_results(self):
        payload = timed_payload(
            timedReadings=[
                {"value": "1", "observedAt": "2026-01-01T00:00:00Z"},
                {"value": "1", "observedAt": "2026-01-05T00:00:00Z"},
                {"value": "1", "observedAt": "2025-01-01T00:00:00Z"},
            ]
        )
        with self.assertRaises(cal.TimedItemError):
            cal.translate(payload)

    def test_mutually_exclusive_with_readings(self):
        with self.assertRaises(cal.InvalidRequest):
            cal.translate(timed_payload(readings=["1"]))

    def test_timed_batch_limits_and_shape(self):
        ok_item = {"value": "0", "observedAt": "2026-01-01T00:00:00Z"}
        cal.translate(timed_payload(timedReadings=[ok_item] * 100))
        with self.assertRaises(cal.InvalidRequest):
            cal.translate(timed_payload(timedReadings=[ok_item] * 101))
        with self.assertRaises(cal.InvalidRequest):
            cal.translate(timed_payload(timedReadings=[]))
        with self.assertRaises(cal.InvalidRequest):
            cal.translate(timed_payload(timedReadings=[{"observedAt": "2026-01-01T00:00:00Z"}]))
        with self.assertRaises(cal.InvalidRequest):
            cal.translate(timed_payload(timedReadings=[{"value": "1"}]))
        with self.assertRaises(cal.InvalidRequest):
            cal.translate(timed_payload(timedReadings=[{"value": "1", "observedAt": "nope"}]))
        with self.assertRaises(cal.InvalidRequest):
            cal.translate(
                timed_payload(timedReadings=[{"value": 1.5, "observedAt": "2026-01-01T00:00:00Z"}])
            )

    def test_timestamps_must_be_rfc3339_with_offset(self):
        for bad in [
            "2026-01-01T00:00:00",
            "2026-01-01 00:00:00Z",
            "01-01-2026T00:00:00Z",
            "2026-13-01T00:00:00Z",
            "2026-01-01T00:00:00.1234567Z",
        ]:
            with self.assertRaises(cal.InvalidRequest):
                cal.parse_utc(bad, "t")

    def test_validity_window_must_be_ordered(self):
        rel = {
            "from": "A", "to": "B", "a": "1", "b": "0",
            "validFrom": "2026-01-02T00:00:00Z",
            "validUntil": "2026-01-01T00:00:00Z",
        }
        with self.assertRaises(cal.InvalidRequest):
            cal.translate(timed_payload(relations=[rel]))
        rel["validFrom"] = "2026-01-01T00:00:00Z"  # equal ends also rejected
        with self.assertRaises(cal.InvalidRequest):
            cal.translate(timed_payload(relations=[rel]))

    def test_legacy_request_ignores_validity_windows(self):
        # A relation only valid in the future must still be used by a
        # classic readings request exactly as before.
        payload = {
            "source": "A", "target": "B",
            "relations": [
                {"from": "A", "to": "B", "a": "2", "b": "1",
                 "validFrom": "2030-01-01T00:00:00Z"}
            ],
            "readings": ["1"],
        }
        body = cal.translate(payload).to_json()
        self.assertEqual(body["coefficients"], {"a": "2", "b": "1"})
        self.assertEqual(body["results"], ["3"])


class TimedHttpSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), CalibrationHandler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def _post(self, payload):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/calibrations/translate",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_timed_translation_over_http(self):
        status, body = self._post(timed_payload())
        self.assertEqual(status, 200)
        self.assertEqual(
            body["timedReadings"],
            [
                {"value": "1", "observedAt": "2026-01-01T00:00:00Z",
                 "coefficient": "1", "result": "11/6"},
                {"value": "1", "observedAt": "2026-01-02T00:00:00Z",
                 "coefficient": "3/2", "result": "11/6"},
            ],
        )

    def test_timed_unreachable_reports_index_and_observed_at(self):
        status, body = self._post(
            timed_payload(
                timedReadings=[
                    {"value": "1", "observedAt": "2026-01-01T00:00:00Z"},
                    {"value": "1", "observedAt": "2024-01-01T00:00:00Z"},
                ]
            )
        )
        self.assertEqual(status, 404)
        error = body["error"]
        self.assertEqual(error["code"], "unreachable")
        self.assertEqual(error["index"], 1)
        self.assertEqual(error["observedAt"], "2024-01-01T00:00:00Z")
        self.assertNotIn("timedReadings", body)

    def test_timed_conflict_reports_index_and_observed_at(self):
        relations = [
            {"from": "A", "to": "B", "a": "2", "b": "1",
             "validFrom": "2026-01-01T00:00:00Z"},
            {"from": "B", "to": "C", "a": "3", "b": "0",
             "validFrom": "2026-01-01T00:00:00Z"},
            {"from": "C", "to": "A", "a": "1/6", "b": "0",
             "validFrom": "2026-01-02T00:00:00Z",
             "validUntil": "2026-01-03T00:00:00Z"},
        ]
        status, body = self._post(
            timed_payload(
                relations=relations,
                timedReadings=[
                    {"value": "1", "observedAt": "2026-01-02T00:00:00Z"},
                ],
            )
        )
        self.assertEqual(status, 409)
        error = body["error"]
        self.assertEqual(error["code"], "conflict")
        self.assertEqual(error["index"], 0)
        self.assertEqual(error["observedAt"], "2026-01-02T00:00:00Z")
        self.assertEqual(set(error["cycle"]), {"A", "B", "C"})

    def test_both_readings_fields_rejected(self):
        payload = timed_payload()
        payload["readings"] = ["1"]
        status, body = self._post(payload)
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_request")


class HttpSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), CalibrationHandler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def _post(self, payload):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/calibrations/translate",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def _get(self, path):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=5) as resp:
            return resp.status, json.loads(resp.read())

    def test_health(self):
        status, body = self._get("/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")

    def test_consistent_chain_over_http(self):
        status, body = self._post(P())
        self.assertEqual(status, 200)
        self.assertEqual(body["coefficients"], {"a": "1", "b": "5/6"})
        self.assertEqual(body["results"], ["5/6", "11/6", "10/3"])

    def test_conflict_over_http(self):
        rels = [
            {"from": "A", "to": "B", "a": "2", "b": "1"},
            {"from": "B", "to": "C", "a": "3", "b": "0"},
            {"from": "C", "to": "A", "a": "1/6", "b": "0"},
        ]
        status, body = self._post(P(relations=rels))
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "conflict")
        self.assertEqual(set(body["error"]["cycle"]), {"A", "B", "C"})
        self.assertNotIn("results", body)

    def test_unreachable_over_http(self):
        rels = [
            {"from": "A", "to": "B", "a": "2", "b": "1"},
            {"from": "D", "to": "E", "a": "1", "b": "0"},
        ]
        status, body = self._post(P(relations=rels, target="E"))
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "unreachable")


if __name__ == "__main__":
    unittest.main()
