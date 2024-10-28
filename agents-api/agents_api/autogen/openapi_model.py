# ruff: noqa: F401, F403, F405
import ast
from typing import (
    Annotated,
    Any,
    Generic,
    Literal,
    Self,
    Type,
    TypeVar,
    Union,
    get_args,
)
from uuid import UUID

import jinja2
from litellm.utils import _select_tokenizer as select_tokenizer
from litellm.utils import token_counter
from pydantic import (
    AwareDatetime,
    Field,
    ValidationError,
    computed_field,
    field_validator,
    model_validator,
)

from ..common.utils.datetime import utcnow
from .Agents import *
from .Chat import *
from .Common import *
from .Docs import *
from .Entries import *
from .Executions import *
from .Jobs import *
from .Sessions import *
from .Tasks import *
from .Tools import *
from .Users import *

# Generic models
# --------------

DataT = TypeVar("DataT", bound=BaseModel)


class ListResponse(BaseModel, Generic[DataT]):
    items: list[DataT]


# Aliases
# -------


class CreateToolRequest(UpdateToolRequest):
    pass


class CreateOrUpdateAgentRequest(UpdateAgentRequest):
    pass


class CreateOrUpdateUserRequest(UpdateUserRequest):
    pass


class CreateOrUpdateSessionRequest(CreateSessionRequest):
    pass


ChatResponse = ChunkChatResponse | MessageChatResponse


class MapReduceStep(Main):
    pass


class ChatMLTextContentPart(Content):
    pass


class ChatMLImageContentPart(ContentModel):
    pass


class InputChatMLMessage(Message):
    pass


# Patches
# -------


def type_property(self: BaseModel) -> str:
    return (
        "function"
        if self.function
        else "integration"
        if self.integration
        else "system"
        if self.system
        else "api_call"
        if self.api_call
        else None
    )


# Patch original Tool class to add 'type' property
TaskTool.type = computed_field(property(type_property))

# Patch original Tool class to add 'type' property
Tool.type = computed_field(property(type_property))

# Patch original UpdateToolRequest class to add 'type' property
UpdateToolRequest.type = computed_field(property(type_property))

# Patch original PatchToolRequest class to add 'type' property
PatchToolRequest.type = computed_field(property(type_property))


# Patch Task Workflow Steps
# -------------------------


def validate_python_expression(expr: str) -> tuple[bool, str]:
    try:
        ast.parse(expr)
        return True, ""
    except SyntaxError as e:
        return False, f"SyntaxError in '{expr}': {str(e)}"


def validate_jinja_template(template: str) -> tuple[bool, str]:
    env = jinja2.Environment()
    try:
        parsed_template = env.parse(template)
        for node in parsed_template.body:
            if isinstance(node, jinja2.nodes.Output):
                for child in node.nodes:
                    if isinstance(child, jinja2.nodes.Name):
                        # Check if the variable is a valid Python expression
                        is_valid, error = validate_python_expression(child.name)
                        if not is_valid:
                            return (
                                False,
                                f"Invalid Python expression in Jinja template '{template}': {error}",
                            )
        return True, ""
    except jinja2.exceptions.TemplateSyntaxError as e:
        return False, f"TemplateSyntaxError in '{template}': {str(e)}"


@field_validator("evaluate")
def validate_evaluate_expressions(cls, v):
    for key, expr in v.items():
        is_valid, error = validate_python_expression(expr)
        if not is_valid:
            raise ValueError(f"Invalid Python expression in key '{key}': {error}")
    return v


EvaluateStep.validate_evaluate_expressions = validate_evaluate_expressions


@field_validator("arguments")
def validate_arguments(cls, v):
    if isinstance(v, dict):
        for key, expr in v.items():
            if isinstance(expr, str):
                is_valid, error = validate_python_expression(expr)
                if not is_valid:
                    raise ValueError(
                        f"Invalid Python expression in arguments key '{key}': {error}"
                    )
    return v


ToolCallStep.validate_arguments = validate_arguments


# Add the new validator function
@field_validator("prompt")
def validate_prompt(cls, v):
    if isinstance(v, str):
        is_valid, error = validate_jinja_template(v)
        if not is_valid:
            raise ValueError(f"Invalid Jinja template in prompt: {error}")
    elif isinstance(v, list):
        for item in v:
            if "content" in item:
                is_valid, error = validate_jinja_template(item["content"])
                if not is_valid:
                    raise ValueError(
                        f"Invalid Jinja template in prompt content: {error}"
                    )
    return v


# Patch the original PromptStep class to add the new validator
PromptStep.validate_prompt = validate_prompt


@field_validator("set")
def validate_set_expressions(cls, v):
    for key, expr in v.items():
        is_valid, error = validate_python_expression(expr)
        if not is_valid:
            raise ValueError(f"Invalid Python expression in set key '{key}': {error}")
    return v


SetStep.validate_set_expressions = validate_set_expressions


@field_validator("log")
def validate_log_template(cls, v):
    is_valid, error = validate_jinja_template(v)
    if not is_valid:
        raise ValueError(f"Invalid Jinja template in log: {error}")
    return v


LogStep.validate_log_template = validate_log_template


@field_validator("return_")
def validate_return_expressions(cls, v):
    for key, expr in v.items():
        is_valid, error = validate_python_expression(expr)
        if not is_valid:
            raise ValueError(
                f"Invalid Python expression in return key '{key}': {error}"
            )
    return v


ReturnStep.validate_return_expressions = validate_return_expressions


@field_validator("arguments")
def validate_yield_arguments(cls, v):
    if isinstance(v, dict):
        for key, expr in v.items():
            is_valid, error = validate_python_expression(expr)
            if not is_valid:
                raise ValueError(
                    f"Invalid Python expression in yield arguments key '{key}': {error}"
                )
    return v


YieldStep.validate_yield_arguments = validate_yield_arguments


@field_validator("if_")
def validate_if_expression(cls, v):
    is_valid, error = validate_python_expression(v)
    if not is_valid:
        raise ValueError(f"Invalid Python expression in if condition: {error}")
    return v


IfElseWorkflowStep.validate_if_expression = validate_if_expression


@field_validator("over")
def validate_over_expression(cls, v):
    is_valid, error = validate_python_expression(v)
    if not is_valid:
        raise ValueError(f"Invalid Python expression in over: {error}")
    return v


@field_validator("reduce")
def validate_reduce_expression(cls, v):
    if v is not None:
        is_valid, error = validate_python_expression(v)
        if not is_valid:
            raise ValueError(f"Invalid Python expression in reduce: {error}")
    return v


MapReduceStep.validate_over_expression = validate_over_expression
MapReduceStep.validate_reduce_expression = validate_reduce_expression


# Patch workflow
# --------------

_CreateTaskRequest = CreateTaskRequest


class CreateTaskRequest(_CreateTaskRequest):
    @model_validator(mode="after")
    def validate_task_request(self) -> "CreateTaskRequest":
        workflows = {
            k: v
            for k, v in self.model_dump().items()
            if k not in _CreateTaskRequest.model_fields or k == "main"
        }

        for workflow_name, workflow_definition in workflows.items():
            try:
                print("VALIDATING WORKFLOW STEPS")
                validate_workflow_steps(workflow_definition)
                print("VALIDATED WORKFLOW STEPS")
            except ValueError as e:
                raise ValueError(f"Invalid workflow '{
                                 workflow_name}': {str(e)}")
        return self

    pass


def validate_workflow_steps(steps):
    for index, step in enumerate(steps):
        print("STARTED VALIDATING STEP")
        step_type, step_content = next(iter(step.items()))
        print(f"STEP TYPE: {step_type}")
        print(f"STEP CONTENT: {step_content}")
        print("STEP ITEMS ARE:")
        step_type = step.get("kind_")
        step_content = step[step_type]
        for k, v in step.items():
            print(f"{k}: {v}")
        try:
            print(f"VALIDATING STEP {step_type}")
            validate_step(step_type, step_content)
            print(f"VALIDATED STEP {step_type}")
        except ValueError as e:
            raise ValueError(f"Error in step {index}: {str(e)}")
        print("FINISHED VALIDATING STEP")


# Mapping between step types and their corresponding classes
step_type_to_class = {
    "evaluate": EvaluateStep,
    "tool_call": ToolCallStep,
    "prompt": PromptStep,
    "get": GetStep,
    "set": SetStep,
    "log": LogStep,
    "return": ReturnStep,
    "sleep": SleepStep,
    "error": ErrorWorkflowStep,
    "yield": YieldStep,
    "wait_for_input": WaitForInputStep,
    "if_else": IfElseWorkflowStep,
    "switch": SwitchStep,
    "foreach": ForeachStep,
    "parallel": ParallelStep,
    "map": MapReduceStep,
}


# Update the validate_step function to use this mapping
def validate_step(step_type, step_content):
    step_class = step_type_to_class.get(step_type)
    if step_class:
        try:
            # TODO: Add support for other fields (eg. map step needs additional 'over' field)
            step_class(**{step_type: step_content})
        except ValidationError as e:
            raise ValueError(f"Validation error for '{step_type}' step: {str(e)}")
    else:
        raise ValueError(f"Unknown step type '{step_type}'")


# Custom types (not generated correctly)
# --------------------------------------

ChatMLContent = (
    list[ChatMLTextContentPart | ChatMLImageContentPart]
    | Tool
    | ChosenToolCall
    | str
    | ToolResponse
    | list[
        list[ChatMLTextContentPart | ChatMLImageContentPart]
        | Tool
        | ChosenToolCall
        | str
        | ToolResponse
    ]
)

# Extract ChatMLRole
ChatMLRole = BaseEntry.model_fields["role"].annotation

# Extract ChatMLSource
ChatMLSource = BaseEntry.model_fields["source"].annotation

# Extract ExecutionStatus
ExecutionStatus = Execution.model_fields["status"].annotation

# Extract TransitionType
TransitionType = Transition.model_fields["type"].annotation

# Assertions to ensure consistency (optional, but recommended for runtime checks)
assert ChatMLRole == BaseEntry.model_fields["role"].annotation
assert ChatMLSource == BaseEntry.model_fields["source"].annotation
assert ExecutionStatus == Execution.model_fields["status"].annotation
assert TransitionType == Transition.model_fields["type"].annotation


# Create models
# -------------


class CreateTransitionRequest(Transition):
    # The following fields are optional in this

    id: UUID | None = None
    execution_id: UUID | None = None
    created_at: AwareDatetime | None = None
    updated_at: AwareDatetime | None = None
    metadata: dict[str, Any] | None = None
    task_token: str | None = None


class CreateEntryRequest(BaseEntry):
    timestamp: Annotated[
        float, Field(ge=0.0, default_factory=lambda: utcnow().timestamp())
    ]

    @classmethod
    def from_model_input(
        cls: Type[Self],
        model: str,
        *,
        role: ChatMLRole,
        content: ChatMLContent,
        name: str | None = None,
        source: ChatMLSource,
        **kwargs: dict,
    ) -> Self:
        tokenizer: dict = select_tokenizer(model=model)
        token_count = token_counter(
            model=model, messages=[{"role": role, "content": content, "name": name}]
        )

        return cls(
            role=role,
            content=content,
            name=name,
            source=source,
            tokenizer=tokenizer["type"],
            token_count=token_count,
            **kwargs,
        )


# Workflow related models
# -----------------------

WorkflowStep = (
    EvaluateStep
    | ToolCallStep
    | PromptStep
    | GetStep
    | SetStep
    | LogStep
    | ReturnStep
    | SleepStep
    | ErrorWorkflowStep
    | YieldStep
    | WaitForInputStep
    | IfElseWorkflowStep
    | SwitchStep
    | ForeachStep
    | ParallelStep
    | MapReduceStep
)


class Workflow(BaseModel):
    name: str
    steps: list[WorkflowStep]


# Task spec helper models
# ----------------------


class TaskToolDef(BaseModel):
    type: str
    name: str
    spec: dict
    inherited: bool = False


_Task = Task


class TaskSpec(_Task):
    model_config = ConfigDict(extra="ignore")

    workflows: list[Workflow]
    tools: list[TaskToolDef]

    # Remove main field from the model
    main: None = None


class TaskSpecDef(TaskSpec):
    id: UUID | None = None
    created_at: AwareDatetime | None = None
    updated_at: AwareDatetime | None = None


class PartialTaskSpecDef(TaskSpecDef):
    name: str | None = None


class Task(_Task):
    model_config = ConfigDict(
        **{
            **_Task.model_config,
            "extra": "allow",
        }
    )


# Patch some models to allow extra fields
# --------------------------------------

WorkflowType = RootModel[
    list[
        EvaluateStep
        | ToolCallStep
        | PromptStep
        | GetStep
        | SetStep
        | LogStep
        | ReturnStep
        | SleepStep
        | ErrorWorkflowStep
        | YieldStep
        | WaitForInputStep
        | IfElseWorkflowStep
        | SwitchStep
        | ForeachStep
        | ParallelStep
        | MapReduceStep
    ]
]


CreateOrUpdateTaskRequest = CreateTaskRequest

_PatchTaskRequest = PatchTaskRequest


class PatchTaskRequest(_PatchTaskRequest):
    model_config = ConfigDict(
        **{
            **_PatchTaskRequest.model_config,
            "extra": "allow",
        }
    )


_UpdateTaskRequest = UpdateTaskRequest


class UpdateTaskRequest(_UpdateTaskRequest):
    model_config = ConfigDict(
        **{
            **_UpdateTaskRequest.model_config,
            "extra": "allow",
        }
    )
