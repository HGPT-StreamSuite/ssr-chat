from __future__ import annotations

from pathlib import Path

from .analysis import QuestionAnalyzer, TriggerDetector
from .answer_context import AnswerContextBuilder
from .context import ConversationContext
from .input_validation import EventValidator
from .local_knowledge import LocalKnowledgeReader
from .model import FixtureModelClient
from .output import DryRunOutput
from .queries import (
    GameGuideQuery,
    GameInfoQuery,
    GameItemQuery,
    PersonQuery,
    WeatherQuery,
)
from .runtime import ChatRuntime
from .sop import FixedSop
from .stores import (
    ConversationEventStore,
    KnowledgeGapStore,
    RunTraceStore,
    SopEventStore,
)
from .weather_adapter import FixtureWeatherAdapter


def build_runtime(
    state_dir: str | Path,
    fixture_dir: str | Path | None = None,
    *,
    weather_adapter=None,
    analyzer=None,
    answer_context=None,
    model=None,
    output=None,
) -> ChatRuntime:
    state = Path(state_dir)
    fixtures = (
        Path(fixture_dir)
        if fixture_dir is not None
        else Path(__file__).resolve().parents[1] / "fixtures"
    )
    conversation_store = ConversationEventStore(state / "conversation_events.jsonl")
    run_traces = RunTraceStore(state / "run_traces.jsonl")
    sop_store = SopEventStore(state / "sop_events.jsonl")
    gap_store = KnowledgeGapStore(state / "knowledge_gaps.jsonl")

    local_reader = LocalKnowledgeReader(fixtures / "published_knowledge.json")
    weather = weather_adapter or FixtureWeatherAdapter(
        fixtures / "weather.json", fixtures / "tool_config.json"
    )
    detector = TriggerDetector()
    question_analyzer = analyzer or QuestionAnalyzer(detector)
    builder = answer_context or AnswerContextBuilder()
    local_model = model or FixtureModelClient()
    dry_run_output = output or DryRunOutput()

    queries = {
        "weather": WeatherQuery(weather),
        "person": PersonQuery(local_reader),
        "game_info": GameInfoQuery(local_reader),
        "game_guide": GameGuideQuery(local_reader),
        "game_item": GameItemQuery(local_reader),
    }
    context = ConversationContext(conversation_store)
    sop = FixedSop(sop_store)
    return ChatRuntime(
        validator=EventValidator(),
        trigger_detector=detector,
        conversation_store=conversation_store,
        context=context,
        analyzer=question_analyzer,
        queries=queries,
        answer_context=builder,
        model=local_model,
        output=dry_run_output,
        run_traces=run_traces,
        sop=sop,
        gaps=gap_store,
    )
