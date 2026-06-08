"""
Custom Benchmarks -- Add your own benchmarks to the routing system.

This is designed for connectivity with external benchmark-creation tools.
You can define custom benchmarks with representative queries, and TryAii-DRE
will generate centroids and include them in routing decisions.
"""

from tryaii import Router

router = Router()

# ---- Add a custom benchmark via the Router API ----

router.add_benchmark(
    name="CustomerSupportQA",
    description="Customer support query handling quality",
    queries=[
        "How do I reset my password?",
        "I want to cancel my subscription",
        "Where is my order?",
        "I was charged twice for the same item",
        "How do I update my billing information?",
        "The app keeps crashing on my phone",
        "Can I get a refund for my purchase?",
        "How do I contact a human agent?",
        "My promo code isn't working",
        "I need to change my delivery address",
    ],
    min_score=0,
    max_score=100,
)

# Now add model scores for this benchmark
router.add_model(
    "gpt-4o",
    provider="OpenAI",
    benchmarks={"CustomerSupportQA": 88.0},  # Scores well on support
)

# Route a support-like query -- it will now factor in CustomerSupportQA
result = router.route("I need help with my account billing issue")
print(f"Best model for support: {result.best_model}")

# ---- Load benchmarks from a JSON file (external tool output) ----

# The expected format is:
# {
#     "benchmarks": [
#         {
#             "name": "MyBenchmark",
#             "description": "...",
#             "training_queries": ["...", "..."],
#             "normalization": {"min_score": 0, "max_score": 100}
#         }
#     ]
# }

# router.benchmarks.load_from_file("my_benchmarks.json")

# ---- Export your benchmark definitions ----

# router.benchmarks.export_to_file("exported_benchmarks.json")
