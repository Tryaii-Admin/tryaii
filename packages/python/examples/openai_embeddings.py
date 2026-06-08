"""
OpenAI Embeddings example.

Setup:
    pip install tryaii[openai]
    export OPENAI_API_KEY=sk-...
"""

from tryaii import Router
from tryaii.embeddings import OpenAIEmbeddingProvider

router = Router(
    embedding_provider=OpenAIEmbeddingProvider(),
)

result = router.route("Summarize this architecture decision")
print(f"Best model: {result.best_model}")
print(f"Reasoning: {result.best_reasoning}")
