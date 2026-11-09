"""Scripted stand-in for an OpenAI model so the Agents SDK loop can be tested offline."""

import json
from typing import Any

from agents.items import ModelResponse
from agents.models.interface import Model
from agents.usage import Usage
from openai.types.responses import (
    Response,
    ResponseCompletedEvent,
    ResponseFunctionCallArgumentsDeltaEvent,
    ResponseFunctionToolCall,
    ResponseOutputItemAddedEvent,
    ResponseOutputMessage,
    ResponseOutputText,
    ResponseTextDeltaEvent,
)

CHUNK = 7  # characters per streamed delta


class ScriptedModel(Model):
    """Each script step is either ("tool", name, args_dict) or ("text", str)."""

    def __init__(self, script: list[tuple]):
        self.script = list(script)
        self.seen_inputs: list[Any] = []
        self.calls = 0

    def _next_item(self, input):
        self.seen_inputs.append(json.dumps(input, default=str) if not isinstance(input, str) else input)
        step = self.script.pop(0)
        self.calls += 1
        if step[0] == "tool":
            return ResponseFunctionToolCall(
                type="function_call", call_id=f"call_{self.calls}", name=step[1],
                arguments=json.dumps(step[2]), id=f"fc_{self.calls}", status="completed")
        return ResponseOutputMessage(
            id=f"msg_{self.calls}", type="message", role="assistant", status="completed",
            content=[ResponseOutputText(type="output_text", text=step[1], annotations=[])])

    async def get_response(self, system_instructions, input, model_settings, tools, output_schema,
                           handoffs, tracing, *, previous_response_id=None, conversation_id=None, prompt=None):
        return ModelResponse(output=[self._next_item(input)], usage=Usage(), response_id=None)

    async def stream_response(self, system_instructions, input, model_settings, tools, output_schema,
                              handoffs, tracing, *, previous_response_id=None, conversation_id=None, prompt=None):
        """Emit the item in small deltas like the real Responses API, then response.completed."""
        item = self._next_item(input)
        seq = 0

        def nxt():
            nonlocal seq
            seq += 1
            return seq

        yield ResponseOutputItemAddedEvent.model_construct(
            type="response.output_item.added", item=item, output_index=0, sequence_number=nxt())
        if isinstance(item, ResponseFunctionToolCall):
            text = item.arguments
            for i in range(0, len(text), CHUNK):
                yield ResponseFunctionCallArgumentsDeltaEvent.model_construct(
                    type="response.function_call_arguments.delta", delta=text[i:i + CHUNK],
                    item_id=item.id, output_index=0, sequence_number=nxt())
        else:
            text = item.content[0].text
            for i in range(0, len(text), CHUNK):
                yield ResponseTextDeltaEvent.model_construct(
                    type="response.output_text.delta", delta=text[i:i + CHUNK], item_id=item.id,
                    output_index=0, content_index=0, logprobs=[], sequence_number=nxt())
        response = Response.model_construct(
            id=f"resp_{self.calls}", object="response", created_at=0, model="scripted",
            output=[item], usage=None, parallel_tool_calls=False, tool_choice="auto", tools=[],
            status="completed")
        yield ResponseCompletedEvent.model_construct(
            type="response.completed", response=response, sequence_number=nxt())
