"""Provider-neutral request and response contract tests."""

from __future__ import annotations

import unittest

from pydantic import ValidationError

from app.shared.models import (
    EmbeddingInput,
    EmbeddingObject,
    EmbeddingRequest,
    EmbeddingResponse,
    HealthResponse,
    Usage,
)


class EmbeddingRequestContractTests(unittest.TestCase):
    def test_plain_text_request_defaults_to_float_encoding(self) -> None:
        request = EmbeddingRequest(model="gte-multilingual-base", input="hello")

        self.assertEqual(request.model, "gte-multilingual-base")
        self.assertEqual(request.input, "hello")
        self.assertEqual(request.encoding_format, "float")

    def test_batch_and_structured_text_inputs_are_supported(self) -> None:
        request = EmbeddingRequest(
            model="gte-multilingual-base",
            input=[
                "plain",
                EmbeddingInput(type="text", text="structured"),
            ],
        )

        self.assertEqual(len(request.input), 2)
        self.assertEqual(request.input[0], "plain")
        self.assertEqual(request.input[1].text, "structured")

    def test_empty_batch_remains_accepted_for_compatibility(self) -> None:
        request = EmbeddingRequest(model="gte-multilingual-base", input=[])

        self.assertEqual(request.input, [])

    def test_batch_larger_than_2048_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValidationError, "Batch size must not exceed 2048"):
            EmbeddingRequest(
                model="gte-multilingual-base",
                input=["x"] * 2049,
            )

    def test_text_larger_than_100000_characters_is_rejected(self) -> None:
        with self.assertRaisesRegex(
            ValidationError,
            "Text input must not exceed 100,000 characters",
        ):
            EmbeddingRequest(
                model="gte-multilingual-base",
                input="x" * 100_001,
            )

    def test_base64_encoding_value_remains_accepted(self) -> None:
        request = EmbeddingRequest(
            model="gte-multilingual-base",
            input="hello",
            encoding_format="base64",
        )

        self.assertEqual(request.encoding_format, "base64")


class EmbeddingResponseContractTests(unittest.TestCase):
    def test_embedding_response_wire_shape(self) -> None:
        response = EmbeddingResponse(
            data=[EmbeddingObject(embedding=[0.25, -0.5], index=0)],
            model="gte-multilingual-base",
            usage=Usage(prompt_tokens=1, total_tokens=1),
        )

        self.assertEqual(
            response.model_dump(),
            {
                "object": "list",
                "data": [
                    {
                        "object": "embedding",
                        "embedding": [0.25, -0.5],
                        "index": 0,
                    }
                ],
                "model": "gte-multilingual-base",
                "usage": {"prompt_tokens": 1, "total_tokens": 1},
            },
        )

    def test_health_response_keeps_null_model_field(self) -> None:
        self.assertEqual(
            HealthResponse(status="ok").model_dump(),
            {"status": "ok", "model": None},
        )

    def test_legacy_model_module_reexports_shared_contract(self) -> None:
        from app.models import EmbeddingRequest as LegacyEmbeddingRequest

        self.assertIs(LegacyEmbeddingRequest, EmbeddingRequest)


if __name__ == "__main__":
    unittest.main()
