"""
Standard benchmark definitions -- the 12 benchmarks shipped with TryAii-DRE.
"""

from tryaii.benchmarks.registry import BenchmarkDefinition
from tryaii.scoring.benchmarks import NormalizationRange

STANDARD_BENCHMARKS: list[BenchmarkDefinition] = [
    BenchmarkDefinition(
        name="MMLU",
        description="Academic knowledge across 57 subjects",
        training_queries=[],  # Loaded from training_queries.json at runtime
        normalization=NormalizationRange(25, 95),
        broad_category="EDUCATIONAL",
        subcategories=["ACADEMIC_INSTRUCTION", "RESEARCH_METHODOLOGY"],
    ),
    BenchmarkDefinition(
        name="HellaSwag",
        description="Commonsense reasoning about everyday situations",
        training_queries=[],
        normalization=NormalizationRange(50, 98),
        broad_category="CONVERSATIONAL",
        subcategories=["PERSONAL_ADVICE"],
    ),
    BenchmarkDefinition(
        name="HumanEval",
        description="Code generation and programming tasks",
        training_queries=[],
        normalization=NormalizationRange(20, 95),
        broad_category="TECHNICAL",
        subcategories=["CODE_TECHNICAL"],
    ),
    BenchmarkDefinition(
        name="SWE-bench",
        description="Real-world software engineering and debugging",
        training_queries=[],
        normalization=NormalizationRange(5, 85),
        broad_category="TECHNICAL",
        subcategories=["CODE_TECHNICAL", "DATA_SCIENCE"],
    ),
    BenchmarkDefinition(
        name="TruthfulQA",
        description="Truthful and accurate question answering",
        training_queries=[],
        normalization=NormalizationRange(20, 85),
        broad_category="CONVERSATIONAL",
        subcategories=["PERSONAL_ADVICE"],
    ),
    BenchmarkDefinition(
        name="ARC",
        description="Science exam questions requiring reasoning",
        training_queries=[],
        normalization=NormalizationRange(0, 95),
        broad_category="EDUCATIONAL",
        subcategories=["ACADEMIC_INSTRUCTION", "STUDY_ASSISTANCE"],
    ),
    BenchmarkDefinition(
        name="GSM8K",
        description="Grade school math word problems",
        training_queries=[],
        normalization=NormalizationRange(20, 98),
        broad_category="TECHNICAL",
        subcategories=["MATHEMATICAL_SCIENTIFIC"],
    ),
    BenchmarkDefinition(
        name="DROP",
        description="Reading comprehension requiring arithmetic and reasoning",
        training_queries=[],
        normalization=NormalizationRange(30, 90),
        broad_category="TECHNICAL",
        subcategories=["MATHEMATICAL_SCIENTIFIC", "DATA_SCIENCE"],
    ),
    BenchmarkDefinition(
        name="SuperGLUE",
        description="Natural language understanding tasks",
        training_queries=[],
        normalization=NormalizationRange(40, 95),
        broad_category="BUSINESS",
        subcategories=["PROFESSIONAL_COMMUNICATION"],
    ),
    BenchmarkDefinition(
        name="Chatbot Arena (LMSys)",
        description="Human-rated conversational quality",
        training_queries=[],
        normalization=NormalizationRange(1000, 1550),
        broad_category="CONVERSATIONAL",
        subcategories=["PERSONAL_ADVICE", "RECOMMENDATIONS"],
    ),
    BenchmarkDefinition(
        name="MT-Bench",
        description="Multi-turn conversation and instruction following",
        training_queries=[],
        normalization=NormalizationRange(5, 10),
        broad_category="CREATIVE",
        subcategories=["WRITING_LITERARY"],
    ),
    BenchmarkDefinition(
        name="LiveBench",
        description="Fresh, contamination-resistant evaluation tasks",
        training_queries=[],
        normalization=NormalizationRange(0, 100),
        broad_category="TECHNICAL",
        subcategories=["CODE_TECHNICAL", "MATHEMATICAL_SCIENTIFIC"],
    ),
]
