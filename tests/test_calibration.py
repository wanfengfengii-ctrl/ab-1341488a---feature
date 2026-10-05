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


def TIMED(**over):
    """A->B relation is revoked and replaced at 2024-06-01T00:00:00Z."""
    payload = {
        "source": "A",
        "target": "C",
        "relations": [
            {"from": "A", "to": "B", "a": "2", "b": "1", "validUntil": "2024-06-01T00:00:00Z"},
            {"from": "A", "to": "B", "a": "3", "b": "0", "validFrom": "2024-06-01T00:00:00Z"},
            {"from": "B", "to": "C", "a": "1/2", "b": "1/3"},
        ],
        "timedReadings": [
            {"value": "0", "observedAt": "2024-05-31T23:59:59Z"},
            {"value": "0", "observedAt": "2024-06-01T00:00:00Z"},
        ],
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


class TimedTranslationTests(unittest.TestCase):
    def test_cross_boundary_uses_relation_in_force(self):
        r = cal.translate(TIMED())
        body = r.to_json()
        items = body["timedResults"]
        # before the boundary: A->C is x + 5/6; from the boundary on: 3/2 x + 1/3
        self.assertEqual([i["result"] for i in items], ["5/6", "1/3"])
        self.assertEqual(items[0]["coefficients"], {"a": "1", "b": "5/6"})
        self.assertEqual(items[1]["coefficients"], {"a": "3/2", "b": "1/3"})
        # input order, original values and moments are echoed per entry
        self.assertEqual([i["index"] for i in items], [0, 1])
        self.assertEqual([i["value"] for i in items], ["0", "0"])
        self.assertEqual(
            [i["observedAt"] for i in items],
            ["2024-05-31T23:59:59Z", "2024-06-01T00:00:00Z"],
        )
        self.assertNotIn("results", body)

    def test_valid_from_inclusive_valid_until_exclusive(self):
        # exactly at the boundary the revoked relation no longer applies
        r = cal.translate(
            TIMED(timedReadings=[{"value": "2", "observedAt": "2024-06-01T00:00:00Z"}])
        )
        self.assertEqual(r.items[0].transform, cal.Affine(Fraction(3, 2), Fraction(1, 3)))
        self.assertEqual(r.items[0].result, Fraction(10, 3))

    def test_unbounded_window_ends(self):
        # the first A->B relation has no validFrom: unbounded into the past
        r = cal.translate(
            TIMED(timedReadings=[{"value": "1", "observedAt": "1999-12-31T00:00:00Z"}])
        )
        self.assertEqual(r.items[0].result, Fraction(11, 6))
        # the second has no validUntil: unbounded into the future
        r = cal.translate(
            TIMED(timedReadings=[{"value": "1", "observedAt": "2999-01-01T00:00:00Z"}])
        )
        self.assertEqual(r.items[0].result, Fraction(11, 6))
        self.assertEqual(r.items[0].transform.a, Fraction(3, 2))

    def test_input_order_preserved(self):
        r = cal.translate(
            TIMED(
                timedReadings=[
                    {"value": "1", "observedAt": "2024-06-02T00:00:00Z"},
                    {"value": "1", "observedAt": "2024-05-02T00:00:00Z"},
                    {"value": "2", "observedAt": "2024-06-02T00:00:00Z"},
                ]
            )
        )
        body = r.to_json()
        self.assertEqual(
            [(i["observedAt"], i["result"]) for i in body["timedResults"]],
            [
                ("2024-06-02T00:00:00Z", "11/6"),
                ("2024-05-02T00:00:00Z", "11/6"),
                ("2024-06-02T00:00:00Z", "10/3"),
            ],
        )
        # same numeric result on both sides of the boundary, different evidence
        self.assertEqual(r.items[0].transform.a, Fraction(3, 2))
        self.assertEqual(r.items[1].transform.a, Fraction(1))

    def test_timed_and_readings_are_mutually_exclusive(self):
        with self.assertRaises(cal.InvalidRequest):
            cal.translate(TIMED(readings=["1"]))

    def test_at_most_100_timed_entries(self):
        entry = {"value": "1", "observedAt": "2024-06-02T00:00:00Z"}
        with self.assertRaises(cal.InvalidRequest):
            cal.translate(TIMED(timedReadings=[dict(entry) for _ in range(101)]))
        r = cal.translate(TIMED(timedReadings=[dict(entry) for _ in range(100)]))
        self.assertEqual(len(r.items), 100)

    def test_empty_or_malformed_timed_readings_rejected(self):
        with self.assertRaises(cal.InvalidRequest):
            cal.translate(TIMED(timedReadings=[]))
        with self.assertRaises(cal.InvalidRequest):
            cal.translate(TIMED(timedReadings="not-a-list"))
        with self.assertRaises(cal.InvalidRequest):
            cal.translate(TIMED(timedReadings=[{"value": "1"}]))
        with self.assertRaises(cal.InvalidRequest):
            cal.translate(TIMED(timedReadings=[{"observedAt": "2024-06-01T00:00:00Z"}]))
        with self.assertRaises(cal.InvalidRequest):
            cal.translate(TIMED(timedReadings=["just-a-string"]))

    def test_non_utc_or_malformed_timestamps_rejected(self):
        for bad in [
            "2024-06-01",
            "2024-06-01T00:00:00+08:00",
            "2024-06-01 00:00:00Z",
            "2024-13-01T00:00:00Z",
            "not-a-time",
            "",
            12345,
            None,
        ]:
            with self.assertRaises(cal.InvalidRequest, msg=repr(bad)):
                cal.translate(TIMED(timedReadings=[{"value": "1", "observedAt": bad}]))

    def test_rfc3339_utc_variants_accepted(self):
        for good in [
            "2024-06-01T00:00:00Z",
            "2024-06-01T00:00:00.123456Z",
            "2024-06-01T00:00:00+00:00",
            "2024-06-01t00:00:00z",
        ]:
            r = cal.translate(TIMED(timedReadings=[{"value": "0", "observedAt": good}]))
            self.assertEqual(len(r.items), 1, msg=good)

    def test_no_valid_path_reports_index_and_moment(self):
        rels = [
            {
                "from": "A", "to": "B", "a": "2", "b": "1",
                "validFrom": "2024-06-01T00:00:00Z",
                "validUntil": "2024-07-01T00:00:00Z",
            },
            {"from": "B", "to": "C", "a": "1/2", "b": "1/3"},
        ]
        with self.assertRaises(cal.Unreachable) as ctx:
            cal.translate(
                TIMED(
                    relations=rels,
                    timedReadings=[
                        {"value": "1", "observedAt": "2024-06-15T00:00:00Z"},
                        {"value": "2", "observedAt": "2024-08-01T00:00:00Z"},
                    ],
                )
            )
        self.assertEqual(ctx.exception.code, "unreachable")
        self.assertEqual(ctx.exception.index, 1)
        self.assertEqual(ctx.exception.observed_at, "2024-08-01T00:00:00Z")

    def test_conflict_at_moment_reports_index_and_fails_whole_batch(self):
        rels = [
            {"from": "A", "to": "B", "a": "2", "b": "1"},
            {
                "from": "A", "to": "B", "a": "3", "b": "0",
                "validFrom": "2024-06-01T00:00:00Z",
                "validUntil": "2024-07-01T00:00:00Z",
            },
            {"from": "B", "to": "C", "a": "1/2", "b": "1/3"},
        ]
        with self.assertRaises(cal.CalibrationConflict) as ctx:
            cal.translate(
                TIMED(
                    relations=rels,
                    timedReadings=[
                        # before the overlap this entry alone would succeed...
                        {"value": "1", "observedAt": "2024-05-01T00:00:00Z"},
                        # ...but the batch must not return partial results
                        {"value": "1", "observedAt": "2024-06-15T00:00:00Z"},
                    ],
                )
            )
        self.assertEqual(ctx.exception.code, "conflict")
        self.assertEqual(ctx.exception.index, 1)
        self.assertEqual(ctx.exception.observed_at, "2024-06-15T00:00:00Z")

    def test_invalid_validity_window_on_relation_rejected(self):
        rels = [
            {"from": "A", "to": "B", "a": "2", "b": "1", "validFrom": "soon"},
            {"from": "B", "to": "C", "a": "1/2", "b": "1/3"},
        ]
        with self.assertRaises(cal.InvalidRequest):
            cal.translate(TIMED(relations=rels))

    def test_legacy_readings_ignore_validity_windows(self):
        rels = [
            {"from": "A", "to": "B", "a": "2", "b": "1", "validUntil": "2024-06-01T00:00:00Z"},
            {"from": "B", "to": "C", "a": "1/2", "b": "1/3"},
        ]
        r = cal.translate(P(relations=rels))
        self.assertEqual(r.transform, cal.Affine(Fraction(1), Fraction(5, 6)))
        self.assertEqual(r.readings_out, [Fraction(5, 6), Fraction(11, 6), Fraction(10, 3)])

    def test_timed_value_formats(self):
        r = cal.translate(
            TIMED(
                timedReadings=[
                    {"value": 2, "observedAt": "2024-05-02T00:00:00Z"},
                    {"value": "5/2", "observedAt": "2024-05-02T00:00:00Z"},
                ]
            )
        )
        self.assertEqual([i.result for i in r.items], [Fraction(17, 6), Fraction(10, 3)])
        # original values are echoed verbatim
        self.assertEqual([i.value for i in r.items], [2, "5/2"])
        with self.assertRaises(cal.InvalidRequest):
            cal.translate(
                TIMED(timedReadings=[{"value": 1.5, "observedAt": "2024-05-02T00:00:00Z"}])
            )


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

    def test_timed_cross_boundary_over_http(self):
        status, body = self._post(TIMED())
        self.assertEqual(status, 200)
        items = body["timedResults"]
        self.assertEqual([i["result"] for i in items], ["5/6", "1/3"])
        self.assertEqual(items[0]["coefficients"], {"a": "1", "b": "5/6"})
        self.assertEqual(items[1]["coefficients"], {"a": "3/2", "b": "1/3"})

    def test_timed_conflict_over_http(self):
        rels = [
            {"from": "A", "to": "B", "a": "2", "b": "1"},
            {
                "from": "A", "to": "B", "a": "3", "b": "0",
                "validFrom": "2024-06-01T00:00:00Z",
                "validUntil": "2024-07-01T00:00:00Z",
            },
            {"from": "B", "to": "C", "a": "1/2", "b": "1/3"},
        ]
        status, body = self._post(
            TIMED(
                relations=rels,
                timedReadings=[
                    {"value": "1", "observedAt": "2024-05-01T00:00:00Z"},
                    {"value": "1", "observedAt": "2024-06-15T00:00:00Z"},
                ],
            )
        )
        self.assertEqual(status, 409)
        error = body["error"]
        self.assertEqual(error["code"], "conflict")
        self.assertEqual(error["index"], 1)
        self.assertEqual(error["observedAt"], "2024-06-15T00:00:00Z")
        self.assertNotIn("timedResults", body)

    def test_timed_unreachable_over_http(self):
        rels = [
            {
                "from": "A", "to": "B", "a": "2", "b": "1",
                "validFrom": "2024-06-01T00:00:00Z",
                "validUntil": "2024-07-01T00:00:00Z",
            },
            {"from": "B", "to": "C", "a": "1/2", "b": "1/3"},
        ]
        status, body = self._post(
            TIMED(relations=rels, timedReadings=[
                {"value": "1", "observedAt": "2024-08-01T00:00:00Z"},
            ])
        )
        self.assertEqual(status, 404)
        error = body["error"]
        self.assertEqual(error["code"], "unreachable")
        self.assertEqual(error["index"], 0)
        self.assertEqual(error["observedAt"], "2024-08-01T00:00:00Z")
        self.assertNotIn("timedResults", body)

    def test_readings_and_timed_readings_rejected_over_http(self):
        status, body = self._post(TIMED(readings=["1"]))
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_request")


if __name__ == "__main__":
    unittest.main()
