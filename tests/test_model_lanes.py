#!/usr/bin/env python3
"""Hermetic tests for provider-neutral model-lane configuration (H8)."""

from __future__ import annotations

import http.client
import io
import json
import os
from pathlib import Path
from typing import Mapping
import stat
import tempfile
import unittest
from unittest import mock
from urllib import request as urlrequest

from hermes_helmet import authority
from hermes_helmet import cli as helmet_cli
from hermes_helmet import model_lanes as ml


ROOT = Path(__file__).resolve().parents[1]
EXAMPLECO = ROOT / "config" / "fixtures" / "exampleco" / "policy.json"
EXAMPLE_POLICY = ROOT / "config" / "policy.example.json"
MODEL_LANES_DOCS = ROOT / "docs" / "model-lanes.md"
MODEL_LANES_FIXTURE = (
    ROOT / "config" / "fixtures" / "exampleco" / "model-lanes" / "company.template.json"
)

FORBIDDEN_PUBLIC_MARKERS = (
    "time" + "left--",
    "yia-" + "mw-agent",
    "wisdom" + "helm-builder",
    "MachineWisdomAI/",
    "xai-" + "oauth",
    "grok-4" + ".5",
)


def _policy_with_lanes(directory: Path, lanes: dict[str, object] | None) -> Path:
    raw = json.loads(EXAMPLECO.read_text(encoding="utf-8"))
    if lanes is not None:
        raw["model_lanes"] = lanes
    path = directory / "policy.json"
    path.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
    return path


def _chat_lane(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "kind": "chat_completions",
        "provider": "openai-compatible",
        "model": "example-chat-model",
        "base_url": "http://127.0.0.1:11434/v1",
        "timeout_seconds": 30,
        "structured_output": True,
    }
    payload.update(overrides)
    return payload


def _embedding_lane(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "kind": "embeddings",
        "provider": "openai-compatible",
        "model": "example-embed-model",
        "base_url": "http://127.0.0.1:11434/v1",
        "timeout_seconds": 30,
        "bind": "loopback",
        "dimensions": 768,
        "quantization": "q4_k_m",
    }
    payload.update(overrides)
    return payload


class ModelLanePolicyTests(unittest.TestCase):
    def test_exampleco_loads_without_optional_lanes(self) -> None:
        policy = authority.load_authority(EXAMPLECO)
        self.assertEqual(policy.inference_provider, "openai")
        self.assertEqual(policy.inference_model, "gpt-4.1")
        lanes = policy.model_lanes
        self.assertEqual(lanes.hermes_executor.provider, "openai")
        self.assertEqual(lanes.hermes_executor.model, "gpt-4.1")
        self.assertEqual(lanes.hermes_executor.kind, "chat_completions")
        self.assertIsNone(lanes.fava_generation)
        self.assertIsNone(lanes.openviking_semantic_generation)
        self.assertIsNone(lanes.embeddings)
        self.assertFalse(lanes.optional_lanes_selected)

    def test_optional_lanes_are_independent_of_hermes_executor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = _policy_with_lanes(
                Path(directory),
                {
                    "fava_generation": _chat_lane(model="fava-only-model"),
                    "openviking_semantic_generation": _chat_lane(
                        model="ov-semantic-only",
                        base_url="http://host.docker.internal:11434/v1",
                        bind="container-host",
                    ),
                    "embeddings": _embedding_lane(),
                },
            )
            policy = authority.load_authority(path)
            self.assertEqual(policy.inference_model, "gpt-4.1")
            self.assertEqual(policy.model_lanes.hermes_executor.model, "gpt-4.1")
            self.assertEqual(policy.model_lanes.fava_generation.model, "fava-only-model")
            self.assertEqual(
                policy.model_lanes.openviking_semantic_generation.model,
                "ov-semantic-only",
            )
            self.assertEqual(policy.model_lanes.embeddings.dimensions, 768)
            self.assertNotEqual(
                policy.model_lanes.hermes_executor.model,
                policy.model_lanes.fava_generation.model,
            )

    def test_hermes_executor_override_must_match_inference_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = _policy_with_lanes(
                Path(directory),
                {
                    "hermes_executor": _chat_lane(
                        provider="openai",
                        model="not-the-policy-model",
                        base_url="https://api.openai.com/v1",
                    )
                },
            )
            with self.assertRaisesRegex(authority.AuthorityError, "hermes_executor"):
                authority.load_authority(path)

    def test_matching_hermes_executor_lane_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = _policy_with_lanes(
                Path(directory),
                {
                    "hermes_executor": _chat_lane(
                        provider="openai",
                        model="gpt-4.1",
                        base_url="https://api.openai.com/v1",
                    )
                },
            )
            policy = authority.load_authority(path)
            self.assertEqual(policy.model_lanes.hermes_executor.base_url, "https://api.openai.com/v1")

    def test_policy_rejects_secret_shaped_lane_keys(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = _policy_with_lanes(
                Path(directory),
                {"fava_generation": _chat_lane(api_key="sk-examplecredential123456")},
            )
            with self.assertRaisesRegex(authority.AuthorityError, "secrets"):
                authority.load_authority(path)

    def test_embeddings_require_fixed_positive_dimensions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = _policy_with_lanes(
                Path(directory),
                {"embeddings": _embedding_lane(dimensions=0)},
            )
            with self.assertRaisesRegex(authority.AuthorityError, "dimensions"):
                authority.load_authority(path)

    def test_chat_lanes_reject_embedding_dimensions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = _policy_with_lanes(
                Path(directory),
                {"fava_generation": _chat_lane(dimensions=768)},
            )
            with self.assertRaisesRegex(authority.AuthorityError, "dimensions"):
                authority.load_authority(path)

    def test_embeddings_reject_chat_kind(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = _policy_with_lanes(
                Path(directory),
                {"embeddings": _embedding_lane(kind="chat_completions")},
            )
            with self.assertRaisesRegex(authority.AuthorityError, "kind"):
                authority.load_authority(path)


class ModelLaneProbeTests(unittest.TestCase):
    def test_chat_probe_uses_chat_completions_contract(self) -> None:
        lane = ml.lane_from_mapping("fava_generation", _chat_lane())
        transport = ml.FakeOpenAITransport(chat_content='{"ok": true}')
        result = ml.probe_chat_completions(lane, transport=transport)
        self.assertTrue(result.ok)
        self.assertEqual(result.lane_id, "fava_generation")
        self.assertEqual(result.contract, "chat_completions")
        self.assertTrue(transport.chat_called)
        self.assertFalse(transport.embeddings_called)
        self.assertTrue(str(transport.last_url).endswith("/chat/completions"))

    def test_structured_output_probe_requires_json_content(self) -> None:
        lane = ml.lane_from_mapping("fava_generation", _chat_lane(structured_output=True))
        transport = ml.FakeOpenAITransport(chat_content="not-json")
        result = ml.probe_chat_completions(lane, transport=transport)
        self.assertFalse(result.ok)
        self.assertIn("structured", result.message.casefold())

    def test_embedding_probe_verifies_declared_dimensions(self) -> None:
        lane = ml.lane_from_mapping("embeddings", _embedding_lane(dimensions=4))
        transport = ml.FakeOpenAITransport(embedding=[0.1, 0.2, 0.3, 0.4])
        result = ml.probe_embeddings(lane, transport=transport)
        self.assertTrue(result.ok)
        self.assertEqual(result.observed_dimensions, 4)
        self.assertTrue(transport.embeddings_called)
        self.assertFalse(transport.chat_called)
        self.assertTrue(str(transport.last_url).endswith("/embeddings"))

    def test_embedding_probe_fails_on_dimension_mismatch(self) -> None:
        lane = ml.lane_from_mapping("embeddings", _embedding_lane(dimensions=8))
        transport = ml.FakeOpenAITransport(embedding=[0.1, 0.2])
        result = ml.probe_embeddings(lane, transport=transport)
        self.assertFalse(result.ok)
        self.assertIn("dimension", result.message.casefold())

    def test_generation_and_embedding_are_separate_contracts(self) -> None:
        chat = ml.lane_from_mapping("fava_generation", _chat_lane())
        embed = ml.lane_from_mapping("embeddings", _embedding_lane(dimensions=2))
        transport = ml.FakeOpenAITransport(embedding=[0.2, 0.1], chat_content="{}")
        chat_result = ml.probe_chat_completions(chat, transport=transport)
        embed_result = ml.probe_embeddings(embed, transport=transport)
        self.assertTrue(chat_result.ok)
        self.assertTrue(embed_result.ok)
        self.assertNotEqual(chat_result.contract, embed_result.contract)

    def test_disposable_index_retrieval_does_not_touch_production(self) -> None:
        lane = ml.lane_from_mapping("embeddings", _embedding_lane(dimensions=3))
        transport = ml.FakeOpenAITransport(
            embeddings_by_text={
                "alpha marker": [1.0, 0.0, 0.0],
                "unrelated noise": [0.0, 1.0, 0.0],
                "alpha query": [0.9, 0.1, 0.0],
            }
        )
        production = {"touched": False}

        def guard(_method: str, url: str, **_kwargs: object) -> None:
            if "production" in url or "persist" in url:
                production["touched"] = True

        transport.before_request = guard
        result = ml.probe_disposable_retrieval(lane, transport=transport)
        self.assertTrue(result.ok)
        self.assertEqual(result.contract, "disposable_retrieval")
        self.assertFalse(production["touched"])
        self.assertTrue(result.discarded_index)

    def test_setup_does_not_report_success_until_selected_lanes_probe(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = _policy_with_lanes(
                Path(directory),
                {
                    "fava_generation": _chat_lane(),
                    "embeddings": _embedding_lane(dimensions=2),
                },
            )
            policy = authority.load_authority(path)
            report = ml.setup_model_lanes(policy, transport=None, probe=False)
            self.assertFalse(report.ok)
            self.assertFalse(report.setup_success)
            transport = ml.FakeOpenAITransport(
                chat_content="{}",
                embedding=[0.1, 0.2],
            )
            probed = ml.setup_model_lanes(policy, transport=transport, probe=True)
            self.assertTrue(probed.ok)
            self.assertTrue(probed.setup_success)
            selected = {item.lane_id for item in probed.probes}
            self.assertIn("hermes_executor", selected)
            self.assertIn("fava_generation", selected)
            self.assertIn("embeddings", selected)

    def test_declining_optional_lanes_keeps_minimum_runtime(self) -> None:
        policy = authority.load_authority(EXAMPLECO)
        report = ml.doctor_model_lanes(policy, require_live=False)
        self.assertTrue(report.ok)
        self.assertTrue(report.skipped_optional)
        self.assertEqual(report.hermes_executor.model, "gpt-4.1")

    def test_one_artifact_is_not_claimed_for_multiple_lanes(self) -> None:
        chat = ml.lane_from_mapping("fava_generation", _chat_lane(model="shared-name"))
        embed = ml.lane_from_mapping(
            "embeddings",
            _embedding_lane(model="shared-name", dimensions=2),
        )
        with self.assertRaisesRegex(ml.ModelLaneError, "multiple model lanes"):
            ml.claim_shared_artifact([chat, embed], evidence=None)
        probes = (
            ml.ProbeResult(
                lane_id="fava_generation",
                contract="chat_completions",
                ok=True,
                message="chat probed",
            ),
            ml.ProbeResult(
                lane_id="embeddings",
                contract="embeddings",
                ok=True,
                message="embed probed",
                observed_dimensions=2,
            ),
        )
        ml.assert_independent_lane_probes((chat, embed), probes)

    def test_embedding_change_requires_reindex_and_keeps_rollback(self) -> None:
        previous = ml.lane_from_mapping("embeddings", _embedding_lane(dimensions=768))
        candidate = ml.lane_from_mapping(
            "embeddings",
            _embedding_lane(dimensions=1024, model="other-embed", quantization="q8_0"),
        )
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "embedding-index-state.json"
            ml.write_embedding_index_state(state, previous)
            with self.assertRaisesRegex(ml.ModelLaneError, "re-index"):
                ml.apply_embedding_lane_change(state, candidate, reindex_confirmed=False)
            rollback = ml.embedding_rollback_plan(state, candidate)
            self.assertEqual(rollback.previous_model, previous.model)
            self.assertEqual(rollback.previous_dimensions, 768)
            self.assertTrue(rollback.preserves_production_data)
            applied = ml.apply_embedding_lane_change(
                state, candidate, reindex_confirmed=True
            )
            self.assertEqual(applied.model, "other-embed")
            self.assertEqual(applied.dimensions, 1024)
            restored = ml.rollback_embedding_lane(state, index_restore_confirmed=True)
            self.assertEqual(restored.model, previous.model)
            self.assertEqual(restored.dimensions, 768)


class ModelLaneRuntimeConfigTests(unittest.TestCase):
    def test_owner_only_runtime_config_omits_secret_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "lanes" / "fava_generation.json"
            lane = ml.lane_from_mapping("fava_generation", _chat_lane())
            written = ml.write_lane_runtime_config(
                output,
                lane,
                credential_env="FAVA_GENERATION_TOKEN",
            )
            mode = stat.S_IMODE(output.stat().st_mode)
            self.assertEqual(mode, 0o600)
            text = output.read_text(encoding="utf-8")
            self.assertNotIn("sk-", text)
            self.assertEqual(written.credential_env, "FAVA_GENERATION_TOKEN")
            public = json.loads(text)
            self.assertNotIn("credential", public)
            self.assertNotIn("api_key", public)
            self.assertEqual(public["credential_env"], "FAVA_GENERATION_TOKEN")

    def test_secret_free_examples_cover_required_contracts(self) -> None:
        examples = ml.secret_free_contract_examples()
        fava = examples["fava_generation"]
        ov = examples["openviking_semantic_generation"]
        embed = examples["embeddings"]
        self.assertTrue(str(fava["path"]).endswith("/chat/completions"))
        self.assertIn("semantic", str(ov["note"]).casefold())
        self.assertTrue(str(embed["path"]).endswith("/embeddings"))
        blob = json.dumps(examples)
        for marker in FORBIDDEN_PUBLIC_MARKERS:
            self.assertNotIn(marker, blob)
        self.assertNotIn("sk-", blob)


class ModelLaneDocsAndCliTests(unittest.TestCase):
    def test_docs_present_local_mac_options_equally(self) -> None:
        text = MODEL_LANES_DOCS.read_text(encoding="utf-8")
        for required in (
            "Ollama",
            "Unsloth Studio",
            "OrbStack",
            "loopback",
            "authentication",
            "host.docker.internal",
            "timeout",
            "structured-output",
            "re-index",
            "rollback",
            "production",
            "/v1/chat/completions",
            "/v1/embeddings",
        ):
            self.assertIn(required, text)
        ollama = text.index("Ollama")
        unsloth = text.index("Unsloth Studio")
        orbstack = text.index("OrbStack")
        self.assertLess(abs(ollama - unsloth), 800)
        self.assertLess(abs(unsloth - orbstack), 800)
        self.assertNotIn("prefer Ollama", text.casefold())
        self.assertNotIn("prefer unsloth", text.casefold())
        self.assertNotIn("prefer orbstack", text.casefold())
        for marker in FORBIDDEN_PUBLIC_MARKERS:
            self.assertNotIn(marker, text)

    def test_fixture_template_is_secret_free(self) -> None:
        payload = json.loads(MODEL_LANES_FIXTURE.read_text(encoding="utf-8"))
        blob = json.dumps(payload)
        self.assertIn("/v1/chat/completions", blob)
        self.assertIn("/v1/embeddings", blob)
        self.assertIn("semantic", blob.casefold())
        for marker in FORBIDDEN_PUBLIC_MARKERS:
            self.assertNotIn(marker, blob)

    def test_cli_models_setup_skip_path(self) -> None:
        stdout = io.StringIO()
        with mock.patch("sys.stdout", stdout):
            code = helmet_cli.main(
                ["models", "setup", "--config", str(EXAMPLE_POLICY), "--json"]
            )
        self.assertEqual(code, 0)
        report = json.loads(stdout.getvalue())
        self.assertTrue(report["ok"])
        self.assertTrue(report["skipped_optional"])

    def test_cli_models_setup_probe_passes_http_transport(self) -> None:
        fake_report = ml.SetupReport(
            ok=True,
            setup_success=True,
            skipped_optional=False,
            hermes_executor=ml.default_hermes_executor("openai", "gpt-4.1"),
        )
        stdout = io.StringIO()
        with mock.patch("sys.stdout", stdout), mock.patch(
            "hermes_helmet.cli.setup_model_lanes", return_value=fake_report
        ) as setup:
            code = helmet_cli.main(
                [
                    "models",
                    "setup",
                    "--config",
                    str(EXAMPLE_POLICY),
                    "--probe",
                    "--json",
                ]
            )
        self.assertEqual(code, 0)
        kwargs = setup.call_args.kwargs
        self.assertTrue(kwargs["probe"])
        self.assertIsInstance(kwargs["transport"], ml.HttpOpenAITransport)

    def test_doctor_includes_model_lanes_section(self) -> None:
        stdout = io.StringIO()
        with mock.patch("sys.stdout", stdout):
            code = helmet_cli.main(["doctor", "--config", str(EXAMPLE_POLICY), "--json"])
        self.assertEqual(code, 0)
        report = json.loads(stdout.getvalue())
        self.assertTrue(report["ok"])
        lanes = report["model_lanes"]
        self.assertTrue(lanes["ok"])
        self.assertTrue(lanes["skipped_optional"])

    def test_cli_models_setup_skip_path_does_not_claim_setup_success(self) -> None:
        stdout = io.StringIO()
        with mock.patch("sys.stdout", stdout):
            code = helmet_cli.main(
                ["models", "setup", "--config", str(EXAMPLE_POLICY), "--json"]
            )
        self.assertEqual(code, 0)
        report = json.loads(stdout.getvalue())
        self.assertTrue(report["ok"])
        self.assertTrue(report["skipped_optional"])
        self.assertFalse(report["setup_success"])

    def test_cli_authenticated_probe_uses_runtime_credentials(self) -> None:
        secrets = {
            "hermes_executor": "secret-hermes-token",
            "fava_generation": "secret-fava-token",
            "embeddings": "secret-embed-token",
        }
        captured: list[tuple[str, str]] = []

        def fake_request(
            self,
            method: str,
            url: str,
            *,
            headers: Mapping[str, str] | None = None,
            body: object = None,
            timeout: float | None = None,
        ) -> dict[str, object]:
            captured.append((url, str((headers or {}).get("Authorization", ""))))
            if "/embeddings" in url:
                vector = {"embedding": [0.1, 0.2]}
                return {"data": [vector, vector, vector]}
            return {
                "choices": [{"message": {"role": "assistant", "content": "lane-ok"}}]
            }

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy_path = _policy_with_lanes(
                root,
                {
                    "fava_generation": _chat_lane(structured_output=False),
                    "embeddings": _embedding_lane(dimensions=2),
                },
            )
            runtime = root / "runtime"
            runtime.mkdir()
            env_names = {
                "hermes_executor": "HERMES_EXECUTOR_TOKEN",
                "fava_generation": "FAVA_GENERATION_TOKEN",
                "embeddings": "EMBEDDINGS_TOKEN",
            }
            for lane_id, env_name in env_names.items():
                path = runtime / f"{lane_id}.json"
                path.write_text(
                    json.dumps({"lane": lane_id, "credential_env": env_name}) + "\n",
                    encoding="utf-8",
                )
                os.chmod(path, 0o600)
            env = {
                env_names[lane_id]: secret for lane_id, secret in secrets.items()
            }
            stdout = io.StringIO()
            stderr = io.StringIO()
            with mock.patch("sys.stdout", stdout), mock.patch(
                "sys.stderr", stderr
            ), mock.patch.object(
                ml.HttpOpenAITransport, "request", fake_request
            ), mock.patch.dict(os.environ, env, clear=False):
                code = helmet_cli.main(
                    [
                        "models",
                        "setup",
                        "--config",
                        str(policy_path),
                        "--probe",
                        "--runtime-dir",
                        str(runtime),
                        "--embedding-index-state",
                        str(root / "embedding-index-state.json"),
                        "--json",
                    ]
                )
            public = stdout.getvalue() + stderr.getvalue()
            for secret in secrets.values():
                self.assertNotIn(secret, public)
            self.assertEqual(code, 0)
            report = json.loads(stdout.getvalue())
            self.assertTrue(report["setup_success"])
            urls = [url for url, _header in captured]
            self.assertTrue(any("/chat/completions" in url for url in urls))
            self.assertTrue(any("/embeddings" in url for url in urls))
            for url, header in captured:
                if "/embeddings" in url:
                    self.assertEqual(header, "Bearer secret-embed-token")
                elif "127.0.0.1" in url:
                    self.assertEqual(header, "Bearer secret-fava-token")
                else:
                    self.assertEqual(header, "Bearer secret-hermes-token")

    def test_cli_unconfirmed_embedding_change_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            previous = ml.lane_from_mapping("embeddings", _embedding_lane(dimensions=768))
            policy_path = _policy_with_lanes(
                root,
                {
                    "embeddings": _embedding_lane(
                        dimensions=1024, model="other-embed", quantization="q8_0"
                    )
                },
            )
            state = root / "embedding-index-state.json"
            ml.write_embedding_index_state(state, previous)
            stdout = io.StringIO()
            stderr = io.StringIO()
            with mock.patch("sys.stdout", stdout), mock.patch("sys.stderr", stderr):
                code = helmet_cli.main(
                    [
                        "models",
                        "setup",
                        "--config",
                        str(policy_path),
                        "--probe",
                        "--embedding-index-state",
                        str(state),
                        "--json",
                    ]
                )
            self.assertNotEqual(code, 0)
            blob = stdout.getvalue() + stderr.getvalue()
            self.assertIn("re-index", blob.casefold())
            self.assertNotIn("Traceback", blob)


class ModelLaneReviewRepairTests(unittest.TestCase):
    def test_chat_probe_rejects_empty_object_payload(self) -> None:
        lane = ml.lane_from_mapping(
            "fava_generation", _chat_lane(structured_output=False)
        )

        class EmptyPayload:
            def request(self, *args: object, **kwargs: object) -> dict[str, object]:
                return {}

        result = ml.probe_chat_completions(lane, transport=EmptyPayload())
        self.assertFalse(result.ok)
        self.assertIn("assistant", result.message.casefold())

    def test_chat_probe_rejects_error_object_payload(self) -> None:
        lane = ml.lane_from_mapping(
            "hermes_executor",
            _chat_lane(
                structured_output=False,
                provider="openai",
                model="gpt-4.1",
                base_url="https://api.openai.com/v1",
            ),
        )

        class ErrorPayload:
            def request(self, *args: object, **kwargs: object) -> dict[str, object]:
                return {"error": "invalid_request"}

        result = ml.probe_chat_completions(lane, transport=ErrorPayload())
        self.assertFalse(result.ok)

    def test_chat_probe_requires_usable_assistant_choice(self) -> None:
        lane = ml.lane_from_mapping(
            "fava_generation", _chat_lane(structured_output=False)
        )

        class MissingMessage:
            def request(self, *args: object, **kwargs: object) -> dict[str, object]:
                return {"choices": [{"index": 0}]}

        result = ml.probe_chat_completions(lane, transport=MissingMessage())
        self.assertFalse(result.ok)

    def test_http_transport_blocks_credential_bearing_cross_origin_redirect(
        self,
    ) -> None:
        handler = ml._CredentialRedirectHandler()
        req = urlrequest.Request(
            "http://127.0.0.1:11434/v1/chat/completions",
            headers={"Authorization": "Bearer synthetic-live-key"},
        )
        with self.assertRaisesRegex(ml.ModelLaneError, "cross-origin redirect"):
            handler.redirect_request(
                req,
                fp=io.BytesIO(b""),
                code=302,
                msg="Found",
                headers=urlrequest.Request("http://127.0.0.1/").headers,
                newurl="http://10.0.0.9/v1/chat/completions",
            )
        same_origin = handler.redirect_request(
            req,
            fp=io.BytesIO(b""),
            code=302,
            msg="Found",
            headers=urlrequest.Request("http://127.0.0.1/").headers,
            newurl="http://127.0.0.1:11434/v1/other",
        )
        self.assertIsNotNone(same_origin)

    def test_embedding_probe_malformed_values_stay_inside_error_boundary(self) -> None:
        lane = ml.lane_from_mapping("embeddings", _embedding_lane(dimensions=2))

        class BadVector:
            def request(self, *args: object, **kwargs: object) -> dict[str, object]:
                return {"data": [{"embedding": ["not-a-number", object()]}]}

        try:
            result = ml.probe_embeddings(lane, transport=BadVector())
        except (TypeError, ValueError) as exc:
            self.fail(f"probe escaped ModelLaneError boundary: {exc}")
        self.assertFalse(result.ok)
        self.assertIn("malformed", result.message.casefold())

    def test_disposable_retrieval_malformed_values_stay_inside_error_boundary(
        self,
    ) -> None:
        lane = ml.lane_from_mapping("embeddings", _embedding_lane(dimensions=2))

        class BadVector:
            def request(self, *args: object, **kwargs: object) -> dict[str, object]:
                return {
                    "data": [
                        {"embedding": [0.1, 0.2]},
                        {"embedding": [float("nan"), 0.2]},
                        {"embedding": [0.3, 0.4]},
                    ]
                }

        try:
            result = ml.probe_disposable_retrieval(lane, transport=BadVector())
        except (TypeError, ValueError) as exc:
            self.fail(f"probe escaped ModelLaneError boundary: {exc}")
        self.assertFalse(result.ok)
        self.assertIn("malformed", result.message.casefold())

    def test_setup_without_probe_does_not_validate_executor(self) -> None:
        policy = authority.load_authority(EXAMPLECO)
        report = ml.setup_model_lanes(policy, transport=None, probe=False)
        self.assertTrue(report.ok)
        self.assertTrue(report.skipped_optional)
        self.assertFalse(report.setup_success)

    def test_authenticated_probes_forward_credentials_including_retrieval(self) -> None:
        chat = ml.lane_from_mapping(
            "fava_generation", _chat_lane(structured_output=False)
        )
        embed = ml.lane_from_mapping("embeddings", _embedding_lane(dimensions=2))
        transport = ml.FakeOpenAITransport(
            chat_content="ok",
            embedding=[0.1, 0.2],
            embeddings_by_text={
                "alpha marker": [1.0, 0.0],
                "unrelated noise": [0.0, 1.0],
                "alpha query": [0.9, 0.1],
            },
        )
        seen: list[str] = []

        def capture(_method: str, url: str, **kwargs: object) -> None:
            headers = dict(kwargs.get("headers") or {})
            seen.append(headers.get("Authorization", ""))

        transport.before_request = capture
        chat_result = ml.probe_chat_completions(
            chat, transport=transport, credential="chat-secret"
        )
        embed_result = ml.probe_embeddings(
            embed, transport=transport, credential="embed-secret"
        )
        retrieval = ml.probe_disposable_retrieval(
            embed, transport=transport, credential="embed-secret"
        )
        self.assertTrue(chat_result.ok)
        self.assertTrue(embed_result.ok)
        self.assertTrue(retrieval.ok)
        self.assertIn("Bearer chat-secret", seen)
        self.assertGreaterEqual(seen.count("Bearer embed-secret"), 2)


def _index_current(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    current = payload.get("current")
    if not isinstance(current, dict):
        raise AssertionError("embedding index state missing current")
    return current


class ModelLaneFollowUpReviewTests(unittest.TestCase):
    def test_failed_confirmed_embedding_change_does_not_persist_candidate(self) -> None:
        previous = ml.lane_from_mapping("embeddings", _embedding_lane(dimensions=2))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy_path = _policy_with_lanes(
                root,
                {
                    "embeddings": _embedding_lane(
                        dimensions=4, model="other-embed", quantization="q8_0"
                    )
                },
            )
            policy = authority.load_authority(policy_path)
            state = root / "embedding-index-state.json"
            ml.write_embedding_index_state(state, previous)
            transport = ml.FakeOpenAITransport(
                chat_content="ok",
                embedding=[0.1, 0.2],
            )
            report = ml.setup_model_lanes(
                policy,
                transport=transport,
                probe=True,
                embedding_index_state=state,
                reindex_confirmed=True,
            )
            self.assertFalse(report.ok)
            self.assertFalse(report.setup_success)
            current = _index_current(state)
            self.assertEqual(current["model"], previous.model)
            self.assertEqual(current["dimensions"], 2)
            self.assertEqual(current["quantization"], "q4_k_m")

    def test_unprobed_setup_leaves_embedding_state_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy_path = _policy_with_lanes(
                root, {"embeddings": _embedding_lane(dimensions=2)}
            )
            policy = authority.load_authority(policy_path)
            missing = root / "missing-embedding-index-state.json"
            report = ml.setup_model_lanes(
                policy,
                transport=None,
                probe=False,
                embedding_index_state=missing,
            )
            self.assertFalse(report.setup_success)
            self.assertFalse(missing.exists())
            previous = ml.lane_from_mapping("embeddings", _embedding_lane(dimensions=2))
            existing = root / "embedding-index-state.json"
            ml.write_embedding_index_state(existing, previous)
            before = existing.read_text(encoding="utf-8")
            again = ml.setup_model_lanes(
                policy,
                transport=None,
                probe=False,
                embedding_index_state=existing,
            )
            self.assertFalse(again.setup_success)
            self.assertEqual(existing.read_text(encoding="utf-8"), before)

    def test_runtime_credentials_reject_permissive_mode(self) -> None:
        policy = authority.load_authority(EXAMPLECO)
        lanes = ml.lanes_from_policy(policy)
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory)
            path = runtime / "hermes_executor.json"
            path.write_text(
                json.dumps({"credential_env": "HERMES_EXECUTOR_TOKEN"}) + "\n",
                encoding="utf-8",
            )
            os.chmod(path, 0o644)
            with self.assertRaisesRegex(ml.ModelLaneError, "owner-only"):
                ml.resolve_runtime_credentials(
                    lanes, runtime, env={"HERMES_EXECUTOR_TOKEN": "secret-token"}
                )

    def test_runtime_credentials_reject_symlink(self) -> None:
        policy = authority.load_authority(EXAMPLECO)
        lanes = ml.lanes_from_policy(policy)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "secret.json"
            target.write_text(
                json.dumps({"credential_env": "HERMES_EXECUTOR_TOKEN"}) + "\n",
                encoding="utf-8",
            )
            os.chmod(target, 0o600)
            runtime = root / "runtime"
            runtime.mkdir()
            link = runtime / "hermes_executor.json"
            link.symlink_to(target)
            with self.assertRaisesRegex(ml.ModelLaneError, "symlink"):
                ml.resolve_runtime_credentials(
                    lanes, runtime, env={"HERMES_EXECUTOR_TOKEN": "secret-token"}
                )

    def test_live_doctor_uses_runtime_credentials(self) -> None:
        secrets = {
            "hermes_executor": "secret-hermes-token",
            "fava_generation": "secret-fava-token",
        }
        captured: list[tuple[str, str]] = []

        def fake_request(
            self,
            method: str,
            url: str,
            *,
            headers: Mapping[str, str] | None = None,
            body: object = None,
            timeout: float | None = None,
        ) -> dict[str, object]:
            captured.append((url, str((headers or {}).get("Authorization", ""))))
            return {
                "choices": [{"message": {"role": "assistant", "content": "lane-ok"}}]
            }

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy_path = _policy_with_lanes(
                root,
                {"fava_generation": _chat_lane(structured_output=False)},
            )
            runtime = root / "runtime"
            runtime.mkdir()
            env_names = {
                "hermes_executor": "HERMES_EXECUTOR_TOKEN",
                "fava_generation": "FAVA_GENERATION_TOKEN",
            }
            for lane_id, env_name in env_names.items():
                path = runtime / f"{lane_id}.json"
                path.write_text(
                    json.dumps({"credential_env": env_name}) + "\n",
                    encoding="utf-8",
                )
                os.chmod(path, 0o600)
            env = {
                env_names[lane_id]: secret for lane_id, secret in secrets.items()
            }
            stdout = io.StringIO()
            stderr = io.StringIO()
            with mock.patch("sys.stdout", stdout), mock.patch(
                "sys.stderr", stderr
            ), mock.patch.object(
                ml.HttpOpenAITransport, "request", fake_request
            ), mock.patch.dict(os.environ, env, clear=False):
                code = helmet_cli.main(
                    [
                        "doctor",
                        "--config",
                        str(policy_path),
                        "--live",
                        "--runtime-dir",
                        str(runtime),
                        "--json",
                    ]
                )
            public = stdout.getvalue() + stderr.getvalue()
            for secret in secrets.values():
                self.assertNotIn(secret, public)
            self.assertEqual(code, 0)
            report = json.loads(stdout.getvalue())
            self.assertTrue(report["model_lanes"]["ok"])
            self.assertTrue(any("/chat/completions" in url for url, _header in captured))
            headers = {url: header for url, header in captured}
            for url, header in captured:
                if "127.0.0.1" in url:
                    self.assertEqual(header, "Bearer secret-fava-token")
                else:
                    self.assertEqual(header, "Bearer secret-hermes-token")
            self.assertGreaterEqual(len(headers), 1)

    def test_embedding_probe_overflow_stays_inside_error_boundary(self) -> None:
        lane = ml.lane_from_mapping("embeddings", _embedding_lane(dimensions=2))

        class OverflowVector:
            def request(self, *args: object, **kwargs: object) -> dict[str, object]:
                return {"data": [{"embedding": [10**10000, 0.1]}]}

        try:
            result = ml.probe_embeddings(lane, transport=OverflowVector())
        except OverflowError as exc:
            self.fail(f"probe escaped ModelLaneError boundary: {exc}")
        self.assertFalse(result.ok)
        self.assertIn("malformed", result.message.casefold())

    def test_legacy_live_doctor_defers_hermes_probe_when_optional_lanes_are_declined(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            raw = json.loads(EXAMPLECO.read_text(encoding="utf-8"))
            raw["inference_provider"] = "provider-native"
            raw["inference_model"] = "example-native-model"
            policy_path = Path(directory) / "policy.json"
            policy_path.write_text(json.dumps(raw), encoding="utf-8")
            stdout = io.StringIO()
            with mock.patch("sys.stdout", stdout), mock.patch.object(
                ml.HttpOpenAITransport, "request"
            ) as request:
                code = helmet_cli.main(
                    ["doctor", "--config", str(policy_path), "--live", "--json"]
                )
            report = json.loads(stdout.getvalue())
        request.assert_not_called()
        self.assertEqual(code, 0)
        self.assertTrue(report["model_lanes"]["ok"])
        self.assertTrue(report["model_lanes"]["skipped_optional"])

    def test_shared_live_doctor_fails_closed_for_provider_native_executor_without_base_url(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            raw = json.loads(EXAMPLECO.read_text(encoding="utf-8"))
            raw["inference_provider"] = "provider-native"
            raw["inference_model"] = "example-native-model"
            policy_path = Path(directory) / "policy.json"
            policy_path.write_text(json.dumps(raw), encoding="utf-8")
            policy = authority.load_authority(policy_path)
            with self.assertRaisesRegex(ml.ModelLaneError, "base_url"):
                ml.doctor_model_lanes(policy, require_live=True)

    def test_rollback_without_index_restore_confirmation_fails_closed(self) -> None:
        previous = ml.lane_from_mapping("embeddings", _embedding_lane(dimensions=768))
        candidate = ml.lane_from_mapping(
            "embeddings",
            _embedding_lane(dimensions=1024, model="other-embed", quantization="q8_0"),
        )
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "embedding-index-state.json"
            ml.write_embedding_index_state(state, previous)
            ml.apply_embedding_lane_change(state, candidate, reindex_confirmed=True)
            before = state.read_text(encoding="utf-8")
            with self.assertRaisesRegex(ml.ModelLaneError, "restore|re-index"):
                ml.rollback_embedding_lane(state)
            self.assertEqual(state.read_text(encoding="utf-8"), before)
            restored = ml.rollback_embedding_lane(state, index_restore_confirmed=True)
            self.assertEqual(restored.model, previous.model)
            self.assertEqual(restored.dimensions, 768)

    def test_invalid_embedding_state_json_is_model_lane_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "embedding-index-state.json"
            state.write_text("{not-json", encoding="utf-8")
            policy_path = _policy_with_lanes(
                root, {"embeddings": _embedding_lane(dimensions=2)}
            )
            with self.assertRaises(ml.ModelLaneError):
                ml._load_index_state(state)
            stdout = io.StringIO()
            stderr = io.StringIO()
            with mock.patch("sys.stdout", stdout), mock.patch("sys.stderr", stderr):
                code = helmet_cli.main(
                    [
                        "models",
                        "setup",
                        "--config",
                        str(policy_path),
                        "--embedding-index-state",
                        str(state),
                        "--json",
                    ]
                )
            blob = stdout.getvalue() + stderr.getvalue()
            self.assertNotEqual(code, 0)
            self.assertNotIn("Traceback", blob)
            self.assertNotIn("JSONDecodeError", blob)

    def test_non_finite_stored_dimension_is_model_lane_error(self) -> None:
        previous = ml.lane_from_mapping("embeddings", _embedding_lane(dimensions=2))
        candidate = ml.lane_from_mapping(
            "embeddings", _embedding_lane(dimensions=4, model="other-embed")
        )
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "embedding-index-state.json"
            ml.write_embedding_index_state(state, previous)
            state.write_text(
                json.dumps(
                    {
                        "current": {"model": "example-embed-model", "dimensions": 1e309},
                        "previous": None,
                    }
                ).replace("Infinity", "1e309")
                + "\n",
                encoding="utf-8",
            )
            if "Infinity" in state.read_text(encoding="utf-8"):
                state.write_text(
                    '{"current": {"model": "example-embed-model", "dimensions": 1e309},'
                    ' "previous": null}\n',
                    encoding="utf-8",
                )
            with self.assertRaises(ml.ModelLaneError):
                ml.apply_embedding_lane_change(state, candidate, reindex_confirmed=True)

    def test_http_invalid_utf8_stays_inside_probe_boundary(self) -> None:
        class FakeResponse:
            def __enter__(self) -> "FakeResponse":
                return self

            def __exit__(self, *args: object) -> bool:
                return False

            def read(self) -> bytes:
                return b"\xff\xfe"

        class FakeOpener:
            def open(self, *args: object, **kwargs: object) -> FakeResponse:
                return FakeResponse()

        transport = ml.HttpOpenAITransport()
        with mock.patch.object(ml.urlrequest, "build_opener", return_value=FakeOpener()):
            try:
                transport.request("POST", "http://127.0.0.1:9/v1/embeddings")
            except UnicodeDecodeError as exc:
                self.fail(f"UTF-8 decode escaped probe boundary: {exc}")
            except ml.ModelLaneError:
                return
            self.fail("invalid UTF-8 did not fail closed")

    def test_http_overlimit_integer_json_stays_inside_probe_boundary(self) -> None:
        class FakeResponse:
            def __enter__(self) -> "FakeResponse":
                return self

            def __exit__(self, *args: object) -> bool:
                return False

            def read(self) -> bytes:
                return b'{"n": ' + b"1" + b"0" * 5000 + b"}"

        class FakeOpener:
            def open(self, *args: object, **kwargs: object) -> FakeResponse:
                return FakeResponse()

        transport = ml.HttpOpenAITransport()
        with mock.patch.object(ml.urlrequest, "build_opener", return_value=FakeOpener()):
            try:
                transport.request("POST", "http://127.0.0.1:9/v1/embeddings")
            except ValueError as exc:
                self.fail(f"over-limit JSON integer escaped probe boundary: {exc}")
            except ml.ModelLaneError:
                return
            self.fail("over-limit JSON integer did not fail closed")

    def test_live_doctor_minimum_runtime_probes_hermes_credential_and_fails(self) -> None:
        secret = "secret-hermes-token"
        captured: list[tuple[str, str]] = []

        def fake_request(
            self,
            method: str,
            url: str,
            *,
            headers: Mapping[str, str] | None = None,
            body: object = None,
            timeout: float | None = None,
        ) -> dict[str, object]:
            captured.append((url, str((headers or {}).get("Authorization", ""))))
            raise ml.ModelLaneError("hermes probe failed")

        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory) / "runtime"
            runtime.mkdir()
            path = runtime / "hermes_executor.json"
            path.write_text(
                json.dumps({"credential_env": "HERMES_EXECUTOR_TOKEN"}) + "\n",
                encoding="utf-8",
            )
            os.chmod(path, 0o600)
            stdout = io.StringIO()
            stderr = io.StringIO()
            with mock.patch("sys.stdout", stdout), mock.patch(
                "sys.stderr", stderr
            ), mock.patch.object(
                ml.HttpOpenAITransport, "request", fake_request
            ), mock.patch.dict(os.environ, {"HERMES_EXECUTOR_TOKEN": secret}, clear=False):
                code = helmet_cli.main(
                    [
                        "doctor",
                        "--config",
                        str(EXAMPLE_POLICY),
                        "--live",
                        "--runtime-dir",
                        str(runtime),
                        "--json",
                    ]
                )
            public = stdout.getvalue() + stderr.getvalue()
            self.assertNotIn(secret, public)
            self.assertNotIn("Traceback", public)
            self.assertNotEqual(code, 0)
            report = json.loads(stdout.getvalue())
            self.assertFalse(report["model_lanes"]["ok"])
            self.assertTrue(report["model_lanes"]["skipped_optional"])
            self.assertTrue(any("/chat/completions" in url for url, _header in captured))
            self.assertTrue(
                any(header == f"Bearer {secret}" for _url, header in captured)
            )

    def test_cli_embedding_rollback_requires_index_restore_confirmation(self) -> None:
        previous = ml.lane_from_mapping("embeddings", _embedding_lane(dimensions=768))
        candidate = ml.lane_from_mapping(
            "embeddings",
            _embedding_lane(dimensions=1024, model="other-embed", quantization="q8_0"),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "embedding-index-state.json"
            production = root / "production-index.bin"
            production.write_bytes(b"live-corpus")
            ml.write_embedding_index_state(state, previous)
            ml.apply_embedding_lane_change(state, candidate, reindex_confirmed=True)
            stdout = io.StringIO()
            stderr = io.StringIO()
            with mock.patch("sys.stdout", stdout), mock.patch("sys.stderr", stderr):
                denied = helmet_cli.main(
                    [
                        "models",
                        "rollback-embedding",
                        "--embedding-index-state",
                        str(state),
                        "--json",
                    ]
                )
            self.assertNotEqual(denied, 0)
            blob = stdout.getvalue() + stderr.getvalue()
            self.assertNotIn("Traceback", blob)
            current = _index_current(state)
            self.assertEqual(current["model"], candidate.model)
            stdout = io.StringIO()
            stderr = io.StringIO()
            with mock.patch("sys.stdout", stdout), mock.patch("sys.stderr", stderr):
                code = helmet_cli.main(
                    [
                        "models",
                        "rollback-embedding",
                        "--embedding-index-state",
                        str(state),
                        "--index-restore-confirmed",
                        "--json",
                    ]
                )
            self.assertEqual(code, 0)
            blob = stdout.getvalue() + stderr.getvalue()
            self.assertNotIn("Traceback", blob)
            report = json.loads(stdout.getvalue())
            self.assertEqual(report["model"], previous.model)
            self.assertEqual(report["dimensions"], 768)
            current = _index_current(state)
            self.assertEqual(current["model"], previous.model)
            self.assertEqual(current["dimensions"], 768)
            self.assertEqual(production.read_bytes(), b"live-corpus")
            docs = MODEL_LANES_DOCS.read_text(encoding="utf-8")
            self.assertIn("rollback-embedding", docs)
            self.assertIn("index-restore-confirmed", docs)

    def test_confirmed_rollback_validates_previous_before_write(self) -> None:
        current = {"model": "new-embed", "dimensions": 1024, "quantization": "q8_0"}
        cases = (
            ("malformed", {"model": "old-embed", "dimensions": float("inf")}),
            ("fractional", {"model": "old-embed", "dimensions": 1.9}),
            ("non-positive", {"model": "old-embed", "dimensions": 0}),
            ("negative", {"model": "old-embed", "dimensions": -3}),
            ("missing-model", {"dimensions": 768, "quantization": "q4_k_m"}),
            ("empty-model", {"model": "  ", "dimensions": 768}),
        )
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "embedding-index-state.json"
            for label, previous in cases:
                with self.subTest(label):
                    state.write_text(
                        json.dumps({"current": current, "previous": previous}) + "\n",
                        encoding="utf-8",
                    )
                    before = state.read_bytes()
                    with self.assertRaises(ml.ModelLaneError):
                        ml.rollback_embedding_lane(state, index_restore_confirmed=True)
                    self.assertEqual(state.read_bytes(), before)

    def test_rollback_writes_normalized_fingerprint_reusable_by_setup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "embedding-index-state.json"
            state.write_text(
                json.dumps(
                    {
                        "current": {
                            "model": "new-embed",
                            "dimensions": 1024.0,
                            "quantization": "q8_0",
                        },
                        "previous": {
                            "model": "example-embed-model",
                            "dimensions": 768.0,
                            "quantization": "q4_k_m",
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            restored = ml.rollback_embedding_lane(state, index_restore_confirmed=True)
            self.assertEqual(restored.model, "example-embed-model")
            self.assertEqual(restored.dimensions, 768)
            persisted = json.loads(state.read_text(encoding="utf-8"))
            self.assertEqual(persisted["current"]["dimensions"], 768)
            self.assertIsInstance(persisted["current"]["dimensions"], int)
            self.assertEqual(persisted["previous"]["dimensions"], 1024)
            self.assertIsInstance(persisted["previous"]["dimensions"], int)
            policy = authority.load_authority(
                _policy_with_lanes(
                    Path(directory),
                    {"embeddings": _embedding_lane(dimensions=768)},
                )
            )
            report = ml.setup_model_lanes(
                policy,
                transport=None,
                probe=False,
                embedding_index_state=state,
                reindex_confirmed=False,
            )
            self.assertFalse(report.setup_success)
            self.assertIn("not probed", report.message)

    def test_apply_rejects_malformed_existing_state_without_mutation(self) -> None:
        candidate = ml.lane_from_mapping(
            "embeddings",
            _embedding_lane(dimensions=1024, model="other-embed", quantization="q8_0"),
        )
        cases = (
            ("missing-model", {"dimensions": 768, "quantization": "q4_k_m"}),
            ("empty-model", {"model": "  ", "dimensions": 768}),
            ("non-string-model", {"model": 123, "dimensions": 768}),
            (
                "non-string-quantization",
                {"model": "old-embed", "dimensions": 768, "quantization": 8},
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "embedding-index-state.json"
            for label, current in cases:
                with self.subTest(label):
                    state.write_text(
                        json.dumps({"current": current, "previous": None}) + "\n",
                        encoding="utf-8",
                    )
                    before = state.read_bytes()
                    with self.assertRaises(ml.ModelLaneError):
                        ml.apply_embedding_lane_change(
                            state, candidate, reindex_confirmed=True
                        )
                    self.assertEqual(state.read_bytes(), before)

    def test_apply_normalizes_existing_fingerprint_and_keeps_rollbackable(self) -> None:
        candidate = ml.lane_from_mapping(
            "embeddings",
            _embedding_lane(dimensions=1024, model="other-embed", quantization="q8_0"),
        )
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "embedding-index-state.json"
            state.write_text(
                json.dumps(
                    {
                        "current": {
                            "model": "example-embed-model",
                            "dimensions": 768.0,
                            "quantization": "q4_k_m",
                        },
                        "previous": None,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            applied = ml.apply_embedding_lane_change(
                state, candidate, reindex_confirmed=True
            )
            self.assertEqual(applied.model, "other-embed")
            persisted = json.loads(state.read_text(encoding="utf-8"))
            self.assertEqual(persisted["previous"]["model"], "example-embed-model")
            self.assertEqual(persisted["previous"]["dimensions"], 768)
            self.assertIsInstance(persisted["previous"]["dimensions"], int)
            self.assertEqual(persisted["previous"]["quantization"], "q4_k_m")
            restored = ml.rollback_embedding_lane(state, index_restore_confirmed=True)
            self.assertEqual(restored.model, "example-embed-model")
            self.assertEqual(restored.dimensions, 768)
            self.assertEqual(restored.quantization, "q4_k_m")

    def test_unchanged_apply_preserves_previous_for_rollback(self) -> None:
        previous = ml.lane_from_mapping("embeddings", _embedding_lane(dimensions=768))
        candidate = ml.lane_from_mapping(
            "embeddings",
            _embedding_lane(dimensions=1024, model="other-embed", quantization="q8_0"),
        )
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "embedding-index-state.json"
            ml.write_embedding_index_state(state, previous)
            ml.apply_embedding_lane_change(state, candidate, reindex_confirmed=True)
            after_change = json.loads(state.read_text(encoding="utf-8"))
            self.assertEqual(after_change["current"]["model"], "other-embed")
            self.assertEqual(after_change["previous"]["model"], previous.model)
            self.assertEqual(after_change["previous"]["dimensions"], 768)
            before_repeat = state.read_bytes()
            repeated = ml.apply_embedding_lane_change(
                state, candidate, reindex_confirmed=False
            )
            self.assertEqual(repeated.model, candidate.model)
            self.assertEqual(repeated.dimensions, candidate.dimensions)
            self.assertEqual(state.read_bytes(), before_repeat)
            persisted = json.loads(state.read_text(encoding="utf-8"))
            self.assertEqual(persisted["previous"]["model"], previous.model)
            self.assertEqual(persisted["previous"]["dimensions"], 768)
            self.assertEqual(persisted["previous"]["quantization"], previous.quantization)
            restored = ml.rollback_embedding_lane(state, index_restore_confirmed=True)
            self.assertEqual(restored.model, previous.model)
            self.assertEqual(restored.dimensions, 768)
            self.assertEqual(restored.quantization, previous.quantization)

    def test_http_response_read_timeout_stays_inside_probe_boundary(self) -> None:
        class FakeResponse:
            def __enter__(self) -> "FakeResponse":
                return self

            def __exit__(self, *args: object) -> bool:
                return False

            def read(self) -> bytes:
                raise TimeoutError("timed out")

        class FakeOpener:
            def open(self, *args: object, **kwargs: object) -> FakeResponse:
                return FakeResponse()

        transport = ml.HttpOpenAITransport()
        with mock.patch.object(ml.urlrequest, "build_opener", return_value=FakeOpener()):
            try:
                transport.request("POST", "http://127.0.0.1:9/v1/chat/completions")
            except TimeoutError as exc:
                self.fail(f"response read timeout escaped probe boundary: {exc}")
            except ml.ModelLaneError:
                return
            self.fail("response read timeout did not fail closed")

    def test_http_incomplete_read_stays_inside_probe_boundary(self) -> None:
        class FakeResponse:
            def __enter__(self) -> "FakeResponse":
                return self

            def __exit__(self, *args: object) -> bool:
                return False

            def read(self) -> bytes:
                raise http.client.IncompleteRead(b'{"partial":')

        class FakeOpener:
            def open(self, *args: object, **kwargs: object) -> FakeResponse:
                return FakeResponse()

        transport = ml.HttpOpenAITransport()
        with mock.patch.object(ml.urlrequest, "build_opener", return_value=FakeOpener()):
            try:
                transport.request("POST", "http://127.0.0.1:9/v1/chat/completions")
            except http.client.IncompleteRead as exc:
                self.fail(f"incomplete HTTP read escaped probe boundary: {exc}")
            except ml.ModelLaneError:
                return
            self.fail("incomplete HTTP read did not fail closed")

    def test_live_doctor_human_output_shows_failed_hermes_probe(self) -> None:
        secret = "secret-hermes-token"

        def fake_request(
            self,
            method: str,
            url: str,
            *,
            headers: Mapping[str, str] | None = None,
            body: object = None,
            timeout: float | None = None,
        ) -> dict[str, object]:
            raise ml.ModelLaneError("hermes probe failed")

        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory) / "runtime"
            runtime.mkdir()
            path = runtime / "hermes_executor.json"
            path.write_text(
                json.dumps({"credential_env": "HERMES_EXECUTOR_TOKEN"}) + "\n",
                encoding="utf-8",
            )
            os.chmod(path, 0o600)
            stdout = io.StringIO()
            stderr = io.StringIO()
            with mock.patch("sys.stdout", stdout), mock.patch(
                "sys.stderr", stderr
            ), mock.patch.object(
                ml.HttpOpenAITransport, "request", fake_request
            ), mock.patch.dict(os.environ, {"HERMES_EXECUTOR_TOKEN": secret}, clear=False):
                code = helmet_cli.main(
                    [
                        "doctor",
                        "--config",
                        str(EXAMPLE_POLICY),
                        "--live",
                        "--runtime-dir",
                        str(runtime),
                    ]
                )
            public = stdout.getvalue() + stderr.getvalue()
            self.assertNotIn(secret, public)
            self.assertNotIn("Traceback", public)
            self.assertNotEqual(code, 0)
            self.assertIn("model_lanes: FAILED", public)
            self.assertNotIn("model_lanes: skipped optional", public)


if __name__ == "__main__":
    unittest.main()
