"""
TryAii-DRE CLI.

Commands:
    tryaii-dre route "your prompt here"     -- Route a prompt and show recommendations
    tryaii-dre setup                         -- Pre-generate centroids for faster first use
    tryaii-dre models                        -- List available models
    tryaii-dre benchmarks                    -- List available benchmarks
    tryaii-dre regenerate                    -- Regenerate centroids (after model change)
"""

from __future__ import annotations

import argparse
import json
import logging
import sys


def cmd_route(args):
    """Route a prompt and display results."""
    from tryaii_dre import Priorities, Router

    router = Router()

    priorities = Priorities(
        quality=args.quality,
        cost=args.cost,
        speed=args.speed,
    )

    result = router.route(args.prompt, priorities=priorities, top_k=args.top_k)

    print(f"\nPrompt: {args.prompt}")
    print(f"Category: {result.classification.broad_category} > {result.classification.subcategory}")
    print(f"Confidence: {result.classification.confidence:.3f}")
    print(f"Classifier: {result.classification.classifier_used}")
    print(f"\nTop {len(result.scores)} Recommendations:")
    print("-" * 70)

    for i, score in enumerate(result.scores, 1):
        model = router.models.get_model(score.model_id)
        provider = model.provider if model else "?"
        price = ""
        if model and model.pricing:
            price = f"${model.pricing.input_per_1k:.4f}/${model.pricing.output_per_1k:.4f} per 1k"

        print(f"  {i}. {score.model_id}")
        print(f"     Provider: {provider} | Score: {score.final_score:.3f}")
        print(f"     Quality: {score.quality_score:.3f} | Cost: {score.cost_score:.3f} | Speed: {score.speed_score:.3f}")
        if price:
            print(f"     Pricing: {price}")
        print(f"     Reason: {score.reasoning}")
        print()


def cmd_setup(args):
    """Pre-generate centroids."""
    from tryaii_dre import TryaiiDreConfig
    from tryaii_dre.centroids.loader import CentroidLoader
    from tryaii_dre.embeddings.local import LocalEmbeddingProvider

    config = TryaiiDreConfig()
    if args.model:
        config.embedding_model = args.model

    print(f"Setting up TryAii-DRE with embedding model: {config.embedding_model}")
    print("This will download the model and generate centroids (one-time operation)...\n")

    provider = LocalEmbeddingProvider(model_name=config.embedding_model)
    loader = CentroidLoader(config=config, embedding_provider=provider)
    centroids = loader.get_centroids()

    print(f"\nSetup complete! Generated {len(centroids)} benchmark centroids.")
    print(f"Saved to: {config.centroid_file}")


def cmd_models(args):
    """List available models."""
    from tryaii_dre import ModelRegistry

    registry = ModelRegistry.default()
    models = registry.all_models

    if args.provider:
        models = [m for m in models if m.provider.lower() == args.provider.lower()]

    if args.json:
        data = [m.to_dict() for m in models]
        print(json.dumps(data, indent=2))
        return

    print(f"\nAvailable Models ({len(models)}):")
    print("-" * 70)

    by_provider: dict[str, list] = {}
    for m in models:
        by_provider.setdefault(m.provider, []).append(m)

    for provider, provider_models in sorted(by_provider.items()):
        print(f"\n  {provider} ({len(provider_models)} models):")
        for m in provider_models:
            latency = m.latency or "?"
            price = ""
            if m.pricing:
                price = f" | ${m.pricing.input_per_1k:.4f}/{m.pricing.output_per_1k:.4f}"
            print(f"    - {m.model_id} [{latency}]{price}")


def cmd_benchmarks(args):
    """List available benchmarks."""
    from tryaii_dre import BenchmarkRegistry

    registry = BenchmarkRegistry.default()

    if args.json:
        data = [b.to_dict() for b in registry.all_benchmarks]
        print(json.dumps(data, indent=2))
        return

    print(f"\nAvailable Benchmarks ({len(registry)}):")
    print("-" * 60)

    for b in registry.all_benchmarks:
        norm = f"[{b.normalization.min_score}-{b.normalization.max_score}]"
        print(f"  {b.name:30s} {norm:15s} {b.description}")


def cmd_regenerate(args):
    """Regenerate centroids."""
    from tryaii_dre import TryaiiDreConfig
    from tryaii_dre.centroids.loader import CentroidLoader
    from tryaii_dre.embeddings.local import LocalEmbeddingProvider

    config = TryaiiDreConfig()
    if args.model:
        config.embedding_model = args.model

    print(f"Regenerating centroids for: {config.embedding_model}")

    provider = LocalEmbeddingProvider(model_name=config.embedding_model)
    loader = CentroidLoader(config=config, embedding_provider=provider)
    centroids = loader.regenerate()

    print(f"Done! Generated {len(centroids)} centroids at {config.centroid_file}")


def cli():
    """Main CLI entry point."""
    parser = argparse.ArgumentParser(
        prog="tryaii-dre",
        description="TryAii-DRE -- Embedding-based AI model router",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose logging")

    subparsers = parser.add_subparsers(dest="command")

    # route
    route_parser = subparsers.add_parser("route", help="Route a prompt to the best model")
    route_parser.add_argument("prompt", help="The prompt to route")
    route_parser.add_argument("--quality", type=int, default=3, help="Quality priority (1-5)")
    route_parser.add_argument("--cost", type=int, default=3, help="Cost priority (1-5)")
    route_parser.add_argument("--speed", type=int, default=3, help="Speed priority (1-5)")
    route_parser.add_argument("--top-k", type=int, default=5, help="Number of recommendations")

    # setup
    setup_parser = subparsers.add_parser("setup", help="Initialize centroids")
    setup_parser.add_argument("--model", help="Embedding model name")

    # models
    models_parser = subparsers.add_parser("models", help="List available models")
    models_parser.add_argument("--provider", help="Filter by provider")
    models_parser.add_argument("--json", action="store_true", help="Output as JSON")

    # benchmarks
    bench_parser = subparsers.add_parser("benchmarks", help="List available benchmarks")
    bench_parser.add_argument("--json", action="store_true", help="Output as JSON")

    # regenerate
    regen_parser = subparsers.add_parser("regenerate", help="Regenerate centroids")
    regen_parser.add_argument("--model", help="Embedding model name")

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    if args.command == "route":
        cmd_route(args)
    elif args.command == "setup":
        cmd_setup(args)
    elif args.command == "models":
        cmd_models(args)
    elif args.command == "benchmarks":
        cmd_benchmarks(args)
    elif args.command == "regenerate":
        cmd_regenerate(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    cli()
