"""Shared data models for the OpenAI-compatible /v1/embeddings API."""

from pydantic import BaseModel, Field, field_validator


class ImageContent(BaseModel):
    """Image content for multimodal embedding models."""

    type: str = Field("image_url", description="Content type: 'image_url' or 'image_base64'.")
    image_url: str | None = Field(default=None, description="URL of the image.")
    image_base64: str | None = Field(default=None, description="Base64-encoded image data.")


class EmbeddingInput(BaseModel):
    """A single embedding input — either text or image."""

    type: str = Field("text", description="'text' or 'image'.")
    text: str | None = Field(default=None, description="Text content (when type='text').")
    image: ImageContent | None = Field(default=None, description="Image content (when type='image').")


# The input field accepts:
#   - a plain string (text)
#   - a list of strings (batch text)
#   - an EmbeddingInput object
#   - a list of EmbeddingInput objects
EmbeddingInputType = str | EmbeddingInput | list[str | EmbeddingInput]


class EmbeddingRequest(BaseModel):
    """OpenAI-compatible embedding request with image support."""

    input: EmbeddingInputType = Field(..., description="Input text(s) or image(s) to embed.")
    model: str = Field(..., description="Model identifier.")
    encoding_format: str = Field(
        default="float", description="Encoding format: 'float' or 'base64'."
    )

    @field_validator("input")
    @classmethod
    def validate_input_size(cls, v: "EmbeddingInputType") -> "EmbeddingInputType":
        items = v if isinstance(v, list) else [v]
        if len(items) > 2048:
            raise ValueError("Batch size must not exceed 2048 items")
        for item in items:
            if isinstance(item, str) and len(item) > 100_000:
                raise ValueError("Text input must not exceed 100,000 characters")
            elif isinstance(item, EmbeddingInput) and item.text and len(item.text) > 100_000:
                raise ValueError("Text input must not exceed 100,000 characters")
        return v


class EmbeddingObject(BaseModel):
    object: str = "embedding"
    embedding: list[float]
    index: int


class Usage(BaseModel):
    prompt_tokens: int
    total_tokens: int


class EmbeddingResponse(BaseModel):
    object: str = "list"
    data: list[EmbeddingObject]
    model: str
    usage: Usage


class HealthResponse(BaseModel):
    status: str = "ok"
    model: str | None = None


class ErrorResponse(BaseModel):
    error: dict
