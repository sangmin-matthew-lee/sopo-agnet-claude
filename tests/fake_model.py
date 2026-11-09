"""Scripted stand-in for an OpenAI model so the Agents SDK loop can be tested offline."""

import json
from typing import Any

from agents.items import ModelResponse
from agents.models.interface import Model
from agents.usage import Usage
from openai.types.responses import ResponseFunctionToolCall, ResponseOutputMessage, ResponseOutputText


class ScriptedModel(Model):
    """Each script step is either ("tool", name, args_dict) or ("text", str)."""

    def __init__(self, script: list[tuple]):
        self.script = list(script)
        self.seen_inputs: list[Any] = []
        self.calls = 0

    async def get_response(self, system_instructions, input, model_settings, tools, output_schema,
                           handoffs, tracing, *, previous_response_id=None, conversation_id=None, prompt=None):
        self.seen_inputs.append(json.dumps(input, default=str) if not isinstance(input, str) else input)
        step = self.script.pop(0)
        self.calls += 1
        if step[0] == "tool":
            out = ResponseFunctionToolCall(
                type="function_call", call_id=f"call_{self.calls}", name=step[1],
                arguments=json.dumps(step[2]), id=f"fc_{self.calls}", status="completed")
        else:
            out = ResponseOutputMessage(
                id=f"msg_{self.calls}", type="message", role="assistant", status="completed",
                content=[ResponseOutputText(type="output_text", text=step[1], annotations=[])])
        return ModelResponse(output=[out], usage=Usage(), response_id=None)

    def stream_response(self, *a, **kw):
        raise NotImplementedError
