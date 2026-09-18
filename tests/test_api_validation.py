"""HTTP-level regression tests for the FastAPI application.

These exercise the request validation error envelope end to end with
``fastapi.testclient``. Unlike the standard-library-only engine tests they
need FastAPI/httpx installed; skip cleanly when those packages are absent.

Run with:  python3 -m unittest discover -s tests -v
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from fastapi.testclient import TestClient

    from app.main import app
except ImportError:  # pragma: no cover - exercised only without web deps
    TestClient = None
    app = None


@unittest.skipIf(TestClient is None, "fastapi/httpx not installed")
class EvaluationPreviewValidationTests(unittest.TestCase):
    """``POST /api/v1/evaluation-previews`` request-validation semantics."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "test.db")
        os.environ["FLAG_DB_PATH"] = self.db_path
        # The lifespan handler creates the schema; the context manager runs it.
        self.client_cm = TestClient(app)
        self.client = self.client_cm.__enter__()

        self.v1 = {
            "version": "v1",
            "flags": {"f": {"enabled": True, "default": "old", "rules": []}},
        }
        self.v2 = {
            "version": "v2",
            "flags": {"f": {"enabled": True, "default": "new", "rules": []}},
        }
        self.assertEqual(
            self.client.post("/api/v1/configs", json=self.v1).status_code, 201
        )
        self.assertEqual(
            self.client.post("/api/v1/configs", json=self.v2).status_code, 201
        )

    def tearDown(self):
        self.client_cm.__exit__(None, None, None)
        self.tmp.cleanup()
        os.environ.pop("FLAG_DB_PATH", None)

    def _payload(self, **overrides):
        payload = {
            "from_version": "v1",
            "to_version": "v2",
            "flags": ["f"],
            "subjects": [{"subject_id": "s1"}],
        }
        payload.update(overrides)
        return payload

    def test_duplicate_subject_id_returns_422_envelope_not_500(self):
        # Regression: the model validator's ValueError leaked into the error
        # ``ctx`` as a raw exception, which JSONResponse could not serialize,
        # turning the 422 into a 500.
        response = self.client.post(
            "/api/v1/evaluation-previews",
            json=self._payload(
                subjects=[{"subject_id": "dup"}, {"subject_id": "dup"}]
            ),
        )

        self.assertEqual(response.status_code, 422)
        body = response.json()
        self.assertEqual(
            body["error"]["code"],
            "invalid_request",
            f"unexpected envelope: {body}",
        )
        self.assertIn("message", body["error"])

        errors = body["error"]["errors"]
        self.assertEqual(len(errors), 1)
        entry = errors[0]
        # Every error item keeps the four standard pydantic fields.
        for key in ("loc", "type", "msg", "input"):
            self.assertIn(key, entry)
        self.assertEqual(entry["type"], "value_error")
        # The duplicate identifier must remain visible in the message.
        self.assertIn("dup", entry["msg"])
        self.assertIn("dup", entry["ctx"]["error"])
        # The embedded exception must be a stable, JSON-encoded string...
        self.assertIsInstance(entry["ctx"]["error"], str)
        # ...and the whole body must round-trip through JSON (it already did
        # via response.json(), assert the stringified ctx has no object repr).
        self.assertNotIn("object at 0x", entry["ctx"]["error"])
        # The offending request body is echoed back as the input.
        self.assertEqual(entry["input"]["subjects"][0]["subject_id"], "dup")

    def test_duplicate_subject_id_three_subjects_still_lists_duplicate(self):
        response = self.client.post(
            "/api/v1/evaluation-previews",
            json=self._payload(
                subjects=[
                    {"subject_id": "alpha"},
                    {"subject_id": "beta"},
                    {"subject_id": "alpha"},
                ]
            ),
        )
        self.assertEqual(response.status_code, 422)
        entry = response.json()["error"]["errors"][0]
        self.assertIn("alpha", entry["msg"])
        self.assertNotIn("beta", entry["msg"])

    def test_ordinary_field_errors_keep_envelope_and_locations(self):
        # Normal field-level validation must not regress: each item retains
        # loc/type/msg/input and the envelope stays invalid_request.
        response = self.client.post(
            "/api/v1/evaluation-previews",
            json={"from_version": "", "flags": [], "subjects": []},
        )
        self.assertEqual(response.status_code, 422)
        body = response.json()
        self.assertEqual(body["error"]["code"], "invalid_request")

        by_field = {}
        for entry in body["error"]["errors"]:
            for key in ("loc", "type", "msg", "input"):
                self.assertIn(key, entry)
            by_field[entry["loc"][-1]] = entry

        self.assertEqual(by_field["from_version"]["type"], "string_too_short")
        self.assertEqual(by_field["from_version"]["input"], "")
        self.assertIn(by_field["flags"]["type"], ("too_short",))
        self.assertEqual(by_field["flags"]["input"], [])
        self.assertIn(by_field["subjects"]["type"], ("too_short",))
        # Numeric ctx values remain numbers after sanitization.
        self.assertEqual(by_field["flags"]["ctx"]["min_length"], 1)

    def test_missing_required_field_is_reported_per_field(self):
        response = self.client.post(
            "/api/v1/evaluation-previews",
            json={"from_version": "v1", "to_version": "v2"},
        )
        self.assertEqual(response.status_code, 422)
        names = {entry["loc"][-1] for entry in response.json()["error"]["errors"]}
        self.assertIn("flags", names)
        self.assertIn("subjects", names)

    def test_unknown_field_uses_same_envelope(self):
        response = self.client.post(
            "/api/v1/evaluation-previews",
            json=self._payload(unexpected=123),
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["error"]["code"], "invalid_request")
        self.assertEqual(
            response.json()["error"]["errors"][0]["type"], "extra_forbidden"
        )

    def test_missing_versions_still_return_404(self):
        response = self.client.post(
            "/api/v1/evaluation-previews",
            json=self._payload(to_version="ghost"),
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"]["code"], "version_not_found")

    def test_valid_preview_still_succeeds(self):
        response = self.client.post(
            "/api/v1/evaluation-previews",
            json=self._payload(),
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["from_version"], "v1")
        self.assertEqual(body["to_version"], "v2")
        self.assertEqual(len(body["changed"]), 1)
        change = body["changed"][0]
        self.assertEqual(change["subject_id"], "s1")
        self.assertEqual(change["flag"], "f")
        self.assertEqual(change["from"]["value"], "old")
        self.assertEqual(change["to"]["value"], "new")
        self.assertEqual(
            body["summary"], [{"flag": "f", "changed": 1, "unchanged": 0}]
        )


@unittest.skipIf(TestClient is None, "fastapi/httpx not installed")
class ValidationErrorSerializationTests(unittest.TestCase):
    """The request-validation handler must never fail to serialize."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["FLAG_DB_PATH"] = os.path.join(self.tmp.name, "test.db")
        self.client_cm = TestClient(app)
        self.client = self.client_cm.__enter__()

    def tearDown(self):
        self.client_cm.__exit__(None, None, None)
        self.tmp.cleanup()
        os.environ.pop("FLAG_DB_PATH", None)

    def test_malformed_json_body_returns_serializable_422(self):
        response = self.client.post(
            "/api/v1/evaluation-previews",
            content=b"{not valid json",
            headers={"content-type": "application/json"},
        )
        self.assertEqual(response.status_code, 422)
        body = response.json()
        self.assertEqual(body["error"]["code"], "invalid_request")
        entry = body["error"]["errors"][0]
        for key in ("loc", "type", "msg", "input"):
            self.assertIn(key, entry)
        # The body must be plain JSON-serializable data end to end.
        json.dumps(body)

    def test_other_endpoints_share_the_safe_envelope(self):
        response = self.client.post("/api/v1/configs", json={"version": ""})
        self.assertEqual(response.status_code, 422)
        body = response.json()
        self.assertEqual(body["error"]["code"], "invalid_request")
        for entry in body["error"]["errors"]:
            for key in ("loc", "type", "msg", "input"):
                self.assertIn(key, entry)


@unittest.skipIf(TestClient is None, "fastapi/httpx not installed")
class EnvironmentHistoryApiTests(unittest.TestCase):
    """``GET /api/v1/environments/{name}/history`` audit-trail semantics."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["FLAG_DB_PATH"] = os.path.join(self.tmp.name, "test.db")
        self.client_cm = TestClient(app)
        self.client = self.client_cm.__enter__()

        for version in ("v1", "v2"):
            self.assertEqual(
                self.client.post(
                    "/api/v1/configs",
                    json={
                        "version": version,
                        "flags": {"f": {"enabled": True, "default": version}},
                    },
                ).status_code,
                201,
            )
        self.assertEqual(
            self.client.put(
                "/api/v1/environments/prod",
                json={"version": "v1", "expected_revision": 0},
            ).status_code,
            201,
        )
        self.assertEqual(
            self.client.put(
                "/api/v1/environments/prod",
                json={"version": "v2", "expected_revision": 1},
            ).status_code,
            200,
        )

    def tearDown(self):
        self.client_cm.__exit__(None, None, None)
        self.tmp.cleanup()
        os.environ.pop("FLAG_DB_PATH", None)

    def test_history_returns_records_in_ascending_revision_order(self):
        response = self.client.get("/api/v1/environments/prod/history")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["name"], "prod")
        self.assertEqual(len(body["history"]), 2)

        first, second = body["history"]
        self.assertEqual(first["revision"], 1)
        self.assertEqual(first["version"], "v1")
        self.assertEqual(first["operation"], "create")
        self.assertEqual(second["revision"], 2)
        self.assertEqual(second["version"], "v2")
        self.assertEqual(second["operation"], "update")
        for entry in (first, second):
            self.assertIn("created_at", entry)

        # The newest record's timestamp matches the pointer's updated_at.
        env = self.client.get("/api/v1/environments/prod").json()
        self.assertEqual(second["created_at"], env["updated_at"])

    def test_unknown_environment_returns_404(self):
        response = self.client.get("/api/v1/environments/ghost/history")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(
            response.json()["error"]["code"], "environment_not_found"
        )

    def test_from_revision_and_limit_filter_the_listing(self):
        response = self.client.get(
            "/api/v1/environments/prod/history", params={"from_revision": 2}
        )
        self.assertEqual(response.status_code, 200)
        history = response.json()["history"]
        self.assertEqual([entry["revision"] for entry in history], [2])

        response = self.client.get(
            "/api/v1/environments/prod/history", params={"limit": 1}
        )
        self.assertEqual(response.status_code, 200)
        history = response.json()["history"]
        self.assertEqual([entry["revision"] for entry in history], [1])

    def test_failed_writes_produce_no_history_records(self):
        # Stale update -> 409, unknown version -> 404; neither may append.
        stale = self.client.put(
            "/api/v1/environments/prod",
            json={"version": "v1", "expected_revision": 99},
        )
        self.assertEqual(stale.status_code, 409)
        missing = self.client.put(
            "/api/v1/environments/prod",
            json={"version": "ghost", "expected_revision": 2},
        )
        self.assertEqual(missing.status_code, 404)

        history = self.client.get("/api/v1/environments/prod/history").json()["history"]
        self.assertEqual([entry["revision"] for entry in history], [1, 2])

    def test_invalid_query_params_return_422_envelope(self):
        for params in ({"from_revision": -1}, {"limit": 0}, {"limit": 1001}):
            response = self.client.get(
                "/api/v1/environments/prod/history", params=params
            )
            self.assertEqual(response.status_code, 422, params)
            self.assertEqual(response.json()["error"]["code"], "invalid_request")


@unittest.skipIf(TestClient is None, "fastapi/httpx not installed")
class ChangeApprovalApiTests(unittest.TestCase):
    """``/api/v1/change-approvals`` review-then-execute flow."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["FLAG_DB_PATH"] = os.path.join(self.tmp.name, "test.db")
        self.client_cm = TestClient(app)
        self.client = self.client_cm.__enter__()

        for version in ("v1", "v2", "v3"):
            self.assertEqual(
                self.client.post(
                    "/api/v1/configs",
                    json={
                        "version": version,
                        "flags": {"f": {"enabled": True, "default": version}},
                    },
                ).status_code,
                201,
            )
        # Environment "prod" starts at v1, revision 1.
        self.assertEqual(
            self.client.put(
                "/api/v1/environments/prod",
                json={"version": "v1", "expected_revision": 0},
            ).status_code,
            201,
        )

    def tearDown(self):
        self.client_cm.__exit__(None, None, None)
        self.tmp.cleanup()
        os.environ.pop("FLAG_DB_PATH", None)

    def _create(self, **overrides):
        payload = {
            "environment": "prod",
            "target_version": "v2",
            "base_revision": 1,
            "applicant": "alice",
            "required_approvals": 2,
        }
        payload.update(overrides)
        return self.client.post("/api/v1/change-approvals", json=payload)

    def _vote(self, approval_id, approver):
        return self.client.post(
            f"/api/v1/change-approvals/{approval_id}/votes",
            json={"approver": approver},
        )

    def _execute(self, approval_id):
        return self.client.post(f"/api/v1/change-approvals/{approval_id}/execute")

    def test_full_flow_create_vote_execute_and_inspect(self):
        created = self._create()
        self.assertEqual(created.status_code, 201, created.text)
        approval = created.json()
        self.assertEqual(approval["status"], "pending")
        self.assertEqual(approval["votes"], 0)
        self.assertEqual(approval["approvers"], [])
        self.assertEqual(approval["environment"], "prod")
        self.assertEqual(approval["target_version"], "v2")
        self.assertEqual(approval["base_revision"], 1)
        self.assertEqual(approval["applicant"], "alice")
        self.assertEqual(approval["required_approvals"], 2)
        approval_id = approval["id"]

        first = self._vote(approval_id, "bob")
        self.assertEqual(first.status_code, 201, first.text)
        self.assertEqual(first.json()["votes"], 1)
        self.assertEqual(first.json()["approvers"], ["bob"])

        second = self._vote(approval_id, "carol")
        self.assertEqual(second.status_code, 201, second.text)
        self.assertEqual(second.json()["votes"], 2)

        executed = self._execute(approval_id)
        self.assertEqual(executed.status_code, 200, executed.text)
        body = executed.json()
        self.assertEqual(body["status"], "executed")
        self.assertEqual(body["executed_revision"], 2)
        self.assertIsNotNone(body["executed_at"])

        # The pointer moved and the immutable audit trail grew by one record.
        env = self.client.get("/api/v1/environments/prod").json()
        self.assertEqual((env["version"], env["revision"]), ("v2", 2))
        history = self.client.get("/api/v1/environments/prod/history").json()
        self.assertEqual(
            [(h["revision"], h["version"]) for h in history["history"]],
            [(1, "v1"), (2, "v2")],
        )

        # The detail query reports the final state and the vote tally.
        detail = self.client.get(f"/api/v1/change-approvals/{approval_id}")
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()["status"], "executed")
        self.assertEqual(detail.json()["votes"], 2)
        self.assertEqual(detail.json()["approvers"], ["bob", "carol"])

    def test_create_unknown_environment_returns_404(self):
        response = self._create(environment="ghost")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(
            response.json()["error"]["code"], "environment_not_found"
        )

    def test_create_unknown_target_version_returns_404(self):
        response = self._create(target_version="ghost")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"]["code"], "version_not_found")

    def test_create_stale_base_revision_returns_409(self):
        response = self._create(base_revision=99)
        self.assertEqual(response.status_code, 409)
        error = response.json()["error"]
        self.assertEqual(error["code"], "stale_revision")
        self.assertEqual(error["current_revision"], 1)

    def test_create_targeting_current_version_returns_409(self):
        response = self._create(target_version="v1")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "no_change")

    def test_required_approvals_bounds_are_validated(self):
        for value in (0, 6):
            response = self._create(required_approvals=value)
            self.assertEqual(response.status_code, 422, value)
            self.assertEqual(
                response.json()["error"]["code"], "invalid_request"
            )

    def test_duplicate_vote_returns_409(self):
        approval_id = self._create().json()["id"]
        self.assertEqual(self._vote(approval_id, "bob").status_code, 201)
        response = self._vote(approval_id, "bob")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "duplicate_vote")
        detail = self.client.get(f"/api/v1/change-approvals/{approval_id}").json()
        self.assertEqual(detail["votes"], 1)

    def test_applicant_cannot_self_approve(self):
        approval_id = self._create().json()["id"]
        response = self._vote(approval_id, "alice")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "self_approval")
        detail = self.client.get(f"/api/v1/change-approvals/{approval_id}").json()
        self.assertEqual(detail["votes"], 0)

    def test_execute_before_threshold_returns_409(self):
        approval_id = self._create().json()["id"]
        self._vote(approval_id, "bob")  # 1 of 2 required
        response = self._execute(approval_id)
        self.assertEqual(response.status_code, 409)
        error = response.json()["error"]
        self.assertEqual(error["code"], "approvals_not_met")
        self.assertEqual(error["votes"], 1)
        self.assertEqual(error["required_approvals"], 2)
        # The pointer did not move.
        env = self.client.get("/api/v1/environments/prod").json()
        self.assertEqual((env["version"], env["revision"]), ("v1", 1))

    def test_execute_after_pointer_moved_marks_approval_stale(self):
        approval_id = self._create().json()["id"]
        self._vote(approval_id, "bob")
        self._vote(approval_id, "carol")
        # The pointer moves behind the approval's back: revision 1 -> 2.
        self.assertEqual(
            self.client.put(
                "/api/v1/environments/prod",
                json={"version": "v3", "expected_revision": 1},
            ).status_code,
            200,
        )

        response = self._execute(approval_id)
        self.assertEqual(response.status_code, 409)
        error = response.json()["error"]
        self.assertEqual(error["code"], "stale_revision")
        self.assertEqual(error["current_revision"], 2)

        # The approval is now stale; the pointer and history are untouched.
        detail = self.client.get(f"/api/v1/change-approvals/{approval_id}").json()
        self.assertEqual(detail["status"], "stale")
        self.assertIsNone(detail["executed_revision"])
        env = self.client.get("/api/v1/environments/prod").json()
        self.assertEqual((env["version"], env["revision"]), ("v3", 2))
        history = self.client.get("/api/v1/environments/prod/history").json()
        self.assertEqual(
            [(h["revision"], h["version"]) for h in history["history"]],
            [(1, "v1"), (2, "v3")],
        )

    def test_closed_approval_rejects_votes_and_reexecution(self):
        approval_id = self._create().json()["id"]
        self._vote(approval_id, "bob")
        self._vote(approval_id, "carol")
        self.assertEqual(self._execute(approval_id).status_code, 200)

        for response in (
            self._vote(approval_id, "dave"),
            self._execute(approval_id),
        ):
            self.assertEqual(response.status_code, 409)
            error = response.json()["error"]
            self.assertEqual(error["code"], "approval_closed")
            self.assertEqual(error["status"], "executed")

    def test_unknown_approval_returns_404(self):
        for response in (
            self.client.get("/api/v1/change-approvals/999"),
            self._vote(999, "bob"),
            self._execute(999),
        ):
            self.assertEqual(response.status_code, 404)
            self.assertEqual(
                response.json()["error"]["code"], "approval_not_found"
            )


if __name__ == "__main__":
    unittest.main()
