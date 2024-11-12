from datetime import datetime
from typing import Any, Callable

from anthropic import AsyncAnthropic  # Import AsyncAnthropic client
from anthropic.types.beta.beta_message import BetaMessage
from beartype import beartype
from langchain_core.tools import BaseTool
from langchain_core.tools.convert import tool as tool_decorator
from litellm import ChatCompletionMessageToolCall, Function, Message
from litellm.types.utils import Choices, ModelResponse
from temporalio import activity
from temporalio.exceptions import ApplicationError
from pydantic import BaseModel

from ...autogen.Tools import (
    BraveIntegrationDef,
    BrowserbaseCompleteSessionIntegrationDef,
    BrowserbaseContextIntegrationDef,
    BrowserbaseCreateSessionIntegrationDef,
    BrowserbaseExtensionIntegrationDef,
    BrowserbaseGetSessionConnectUrlIntegrationDef,
    BrowserbaseGetSessionIntegrationDef,
    BrowserbaseGetSessionLiveUrlsIntegrationDef,
    BrowserbaseListSessionsIntegrationDef,
    DummyIntegrationDef,
    EmailIntegrationDef,
    RemoteBrowserIntegrationDef,
    SpiderIntegrationDef,
    Tool,
    WeatherIntegrationDef,
    WikipediaIntegrationDef,
    BaseIntegrationDef,
)
from ...clients import (
    litellm,  # We dont directly import `acompletion` so we can mock it
)
from ...common.protocol.tasks import StepContext, StepOutcome
from ...common.storage_handler import auto_blob_store
from ...common.utils.template import render_template
from ...env import anthropic_api_key, debug
from ..utils import get_handler_with_filtered_params
from .base_evaluate import base_evaluate

COMPUTER_USE_BETA_FLAG = "computer-use-2024-10-22"


def _get_integration_arguments(tool: Tool):
    providers_map = {
        "brave": BraveIntegrationDef,
        "dummy": DummyIntegrationDef,
        "email": EmailIntegrationDef,
        "spider": SpiderIntegrationDef,
        "wikipedia": WikipediaIntegrationDef,
        "weather": WeatherIntegrationDef,
        "browserbase": {
            "create_context": BrowserbaseContextIntegrationDef,
            "install_extension_from_github": BrowserbaseExtensionIntegrationDef,
            "list_sessions": BrowserbaseListSessionsIntegrationDef,
            "create_session": BrowserbaseCreateSessionIntegrationDef,
            "get_session": BrowserbaseGetSessionIntegrationDef,
            "complete_session": BrowserbaseCompleteSessionIntegrationDef,
            "get_live_urls": BrowserbaseGetSessionLiveUrlsIntegrationDef,
            "get_connect_url": BrowserbaseGetSessionConnectUrlIntegrationDef,
        },
        "remote_browser": RemoteBrowserIntegrationDef,
    }

    integration: BaseIntegrationDef | dict[str, BaseIntegrationDef] = providers_map.get(tool.integration.provider)
    if isinstance(integration, dict):
        integration: BaseIntegrationDef = integration.get(tool.integration.method)

    properties = {
        "type": "object",
        "properties": {},
        "required": [],
    }

    arguments: BaseModel | Any | None = integration.arguments
    if not arguments:
        return properties

    if isinstance(arguments, BaseModel):
        for fld_name, fld_annotation in arguments.model_fields.items():
            properties["properties"][fld_name] = {
                "type": fld_annotation.annotation,
                "description": fld_name,
            }
            if fld_annotation.is_required:
                properties["required"].append(fld_name)

    elif isinstance(arguments, dict):
        properties["properties"] = arguments

    return properties


def format_tool(tool: Tool) -> dict:
    if tool.type == "computer_20241022":
        return {
            "type": tool.type,
            "name": tool.name,
            "display_width_px": tool.computer_20241022
            and tool.computer_20241022.display_width_px,
            "display_height_px": tool.computer_20241022
            and tool.computer_20241022.display_height_px,
            "display_number": tool.computer_20241022
            and tool.computer_20241022.display_number,
        }

    if tool.type in ["bash_20241022", "text_editor_20241022"]:
        return tool.model_dump(include={"type", "name"})

    if tool.type == "function":
        return {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.function and tool.function.parameters,
            },
        }

    # For other tool types, we need to translate them to the OpenAI function tool format
    formatted = {
        "type": "function",
        "function": {"name": tool.name, "description": tool.description},
    }

    if tool.type == "system":
        handler: Callable = get_handler_with_filtered_params(tool.system)

        lc_tool: BaseTool = tool_decorator(handler)

        json_schema: dict = lc_tool.get_input_jsonschema()

        formatted["function"]["description"] = formatted["function"][
            "description"
        ] or json_schema.get("description")

        formatted["function"]["parameters"] = json_schema

    elif tool.type == "integration" and tool.integration:
        formatted["function"]["parameters"] = _get_integration_arguments(tool)

    elif tool.type == "api_call" and tool.api_call:
        formatted["function"]["parameters"] = tool.api_call.schema_

    return formatted


EVAL_PROMPT_PREFIX = "$_ "


@activity.defn
@auto_blob_store
@beartype
async def prompt_step(context: StepContext) -> StepOutcome:
    # Get context data
    prompt: str | list[dict] = context.current_step.model_dump()["prompt"]
    context_data: dict = context.prepare_for_step(include_remote=True)

    # If the prompt is a string and starts with $_ then we need to evaluate it
    should_evaluate_prompt = isinstance(prompt, str) and prompt.startswith(
        EVAL_PROMPT_PREFIX
    )

    if should_evaluate_prompt:
        prompt = await base_evaluate(
            prompt[len(EVAL_PROMPT_PREFIX) :].strip(), context_data
        )

        if not isinstance(prompt, (str, list)):
            raise ApplicationError(
                "Invalid prompt expression, expected a string or list"
            )

    # Wrap the prompt in a list if it is not already
    prompt = (
        prompt if isinstance(prompt, list) else [{"role": "user", "content": prompt}]
    )

    # Render template messages if we didn't evaluate the prompt
    if not should_evaluate_prompt:
        # Render template messages
        prompt = await render_template(
            prompt,
            context_data,
            skip_vars=["developer_id"],
        )

    # Get settings and run llm
    agent_default_settings: dict = (
        context.execution_input.agent.default_settings.model_dump()
        if context.execution_input.agent.default_settings
        else {}
    )

    agent_model: str = (
        context.execution_input.agent.model
        if context.execution_input.agent.model
        else "gpt-4o"
    )

    excluded_keys = [
        "prompt",
        "kind_",
        "label",
        "unwrap",
        "auto_run_tools",
        "disable_cache",
        "tools",
    ]

    # Get passed settings
    passed_settings: dict = context.current_step.model_dump(
        exclude=excluded_keys, exclude_unset=True
    )
    passed_settings.update(passed_settings.pop("settings", {}))

    if not passed_settings.get("tools"):
        passed_settings.pop("tool_choice", None)

    # Format tools for litellm
    formatted_tools = [format_tool(tool) for tool in context.tools]

    # Map tools to their original objects
    tools_mapping: dict[str, Tool] = {
        fmt_tool.get("name") or fmt_tool.get("function", {}).get("name"): orig_tool
        for fmt_tool, orig_tool in zip(formatted_tools, context.tools)
    }

    # Check if the model is Anthropic
    if agent_model.lower().startswith("claude-3.5") and any(
        tool["type"] in ["computer_20241022", "bash_20241022", "text_editor_20241022"]
        for tool in formatted_tools
    ):
        # Retrieve the API key from the environment variable
        betas = [COMPUTER_USE_BETA_FLAG]
        # Use Anthropic API directly
        client = AsyncAnthropic(api_key=anthropic_api_key)

        # Reformat the prompt for Anthropic
        # Anthropic expects a list of messages with role and content (and no name etc)
        prompt = [{"role": "user", "content": message["content"]} for message in prompt]

        # Filter tools for specific types
        filtered_tools = [
            tool
            for tool in formatted_tools
            if tool["type"]
            in ["computer_20241022", "bash_20241022", "text_editor_20241022"]
        ]

        # Claude Response
        claude_response: BetaMessage = await client.beta.messages.create(
            model="claude-3-5-sonnet-20241022",
            messages=prompt,
            tools=filtered_tools,
            max_tokens=1024,
            betas=betas,
        )

        # Claude returns [ToolUse | TextBlock]
        # We need to convert tool_use to tool_calls
        # And set content = TextBlock.text
        # But we need to ensure no more than one text block is returned
        if (
            len([block for block in claude_response.content if block.type == "text"])
            > 1
        ):
            raise ApplicationError("Claude should only return one message")

        text_block = next(
            (block for block in claude_response.content if block.type == "text"),
            None,
        )

        stop_reason = claude_response.stop_reason

        if stop_reason == "tool_use":
            choice = Choices(
                message=Message(
                    role="assistant",
                    content=text_block.text if text_block else None,
                    tool_calls=[
                        ChatCompletionMessageToolCall(
                            type="function",
                            function=Function(
                                name=block.name,
                                arguments=block.input,
                            ),
                        )
                        for block in claude_response.content
                        if block.type == "tool_use"
                    ],
                ),
                finish_reason="tool_calls",
            )
        else:
            assert (
                text_block
            ), "Claude should always return a text block for stop_reason=stop"

            choice = Choices(
                message=Message(
                    role="assistant",
                    content=text_block.text,
                ),
                finish_reason="stop",
            )

        response: ModelResponse = ModelResponse(
            id=claude_response.id,
            choices=[choice],
            created=int(datetime.now().timestamp()),
            model=claude_response.model,
            object="text_completion",
        )

    else:
        # FIXME: hardcoded tool to a None value as the tool calls are not implemented yet
        formatted_tools = None
        # Use litellm for other models
        completion_data: dict = {
            "model": agent_model,
            "tools": formatted_tools or None,
            "messages": prompt,
            **agent_default_settings,
            **passed_settings,
        }

        extra_body = {
            "cache": {"no-cache": debug or context.current_step.disable_cache},
        }

        response: ModelResponse = await litellm.acompletion(
            **completion_data,
            extra_body=extra_body,
        )

    if context.current_step.unwrap:
        if len(response.choices) != 1:
            raise ApplicationError("Only one choice is supported")

        choice = response.choices[0]
        if choice.finish_reason == "tool_calls":
            raise ApplicationError("Tool calls cannot be unwrapped")

        return StepOutcome(
            output=choice.message.content,
            next=None,
        )

    # Re-convert tool-calls to integration/system calls if needed
    response_as_dict = response.model_dump()

    for choice in response_as_dict["choices"]:
        if choice["finish_reason"] == "tool_calls":
            calls = choice["message"]["tool_calls"]

            for call in calls:
                call_name = call["function"]["name"]
                call_args = call["function"]["arguments"]

                original_tool = tools_mapping.get(call_name)
                if not original_tool:
                    raise ApplicationError(f"Tool {call_name} not found")

                if original_tool.type == "function":
                    continue

                call.pop("function")
                call["type"] = original_tool.type
                call[original_tool.type] = {
                    "name": call_name,
                    "arguments": call_args,
                }

    return StepOutcome(
        output=response_as_dict,
        next=None,
    )
