import json
from abc import ABC, abstractmethod
from typing import (
    Any,
    AsyncGenerator,
    Callable,
    Dict,
    List,
    Optional,
    Type,
    Union,
)

from pydantic import BaseModel

from ..providers.llm import CompletionProvider, GenerationConfig
from ..providers.prompt import PromptProvider
from .completion import MessageType
from .llm import LLMChatCompletion


class Tool(BaseModel):
    name: str
    description: str
    results_function: Callable
    llm_format_function: Callable
    stream_function: Optional[Callable] = None
    parameters: Optional[Dict[str, Any]] = None

    class Config:
        arbitrary_types_allowed = True


class ToolResult(BaseModel):
    raw_result: Any
    llm_formatted_result: str
    stream_result: Optional[str] = None


class Message(BaseModel):
    role: MessageType
    content: Optional[str] = None
    name: Optional[str] = None
    function_call: Optional[Dict[str, Any]] = None
    tool_calls: Optional[List[Dict[str, Any]]] = None


class Conversation:
    def __init__(self):
        self.messages: List[Message] = []

    def create_and_add_message(
        self,
        role: MessageType,
        content: Optional[str] = None,
        name: Optional[str] = None,
        function_call: Optional[Dict[str, Any]] = None,
        tool_calls: Optional[List[Dict[str, Any]]] = None,
    ):
        message = Message(
            role=role,
            content=content,
            name=name,
            function_call=function_call,
            tool_calls=tool_calls,
        )
        self.add_message(message)

    def add_message(self, message):
        self.messages.append(message)

    def get_messages(self) -> List[Dict[str, Any]]:
        return [msg.dict(exclude_none=True) for msg in self.messages]


# TODO - Move agents to provider pattern
class AgentConfig(BaseModel):
    system_instruction_name: str = "rag_agent"
    tool_names: list[str] = ["search"]
    generation_config: GenerationConfig = GenerationConfig()
    stream: bool = False

    @classmethod
    def create(cls: Type["AgentConfig"], **kwargs: Any) -> "AgentConfig":
        base_args = cls.__fields__.keys()
        filtered_kwargs = {
            k: v if v != "None" else None
            for k, v in kwargs.items()
            if k in base_args
        }
        return cls(**filtered_kwargs)


class Agent(ABC):
    def __init__(
        self,
        llm_provider: CompletionProvider,
        prompt_provider: PromptProvider,
        config: AgentConfig,
    ):
        self.llm_provider = llm_provider
        self.prompt_provider = prompt_provider
        self.config = config
        self.conversation = []
        self._completed = False
        self._tools = []
        self._register_tools()

    @abstractmethod
    def _register_tools(self):
        pass

    def _setup(self, system_instruction: Optional[str] = None):
        self.conversation = [
            Message(
                role=MessageType.SYSTEM,
                content=system_instruction
                or self.prompt_provider.get_prompt(
                    self.config.system_instruction_name
                ),
            )
        ]

    @property
    def tools(self) -> list[Tool]:
        return self._tools

    @tools.setter
    def tools(self, tools: list[Tool]):
        self._tools = tools

    @abstractmethod
    async def arun(
        self,
        system_instruction: Optional[str] = None,
        messages: Optional[list[Message]] = None,
        *args,
        **kwargs,
    ) -> Union[
        list[LLMChatCompletion], AsyncGenerator[LLMChatCompletion, None]
    ]:
        pass

    @abstractmethod
    async def process_llm_response(
        self,
        response: Union[Dict[str, Any], AsyncGenerator[Dict[str, Any], None]],
        *args,
        **kwargs,
    ) -> Union[str, AsyncGenerator[str, None]]:
        pass

    async def execute_tool(self, tool_name: str, *args, **kwargs) -> str:
        if tool := next((t for t in self.tools if t.name == tool_name), None):
            return await tool.function(*args, **kwargs)
        else:
            return f"Error: Tool {tool_name} not found."

    def get_generation_config(
        self, last_message: Message, stream: bool = False
    ) -> GenerationConfig:
        if (
            last_message.role in ["tool", "function"]
            and last_message.content != ""
        ):
            return GenerationConfig(
                **self.config.generation_config.dict(
                    exclude={"functions", "tools", "stream"}
                ),
                stream=stream,
            )
        return GenerationConfig(
            **self.config.generation_config.dict(
                exclude={"functions", "tools", "stream"}
            ),
            # FIXME: Use tools instead of functions
            # TODO - Investigate why `tools` fails with OpenAI+LiteLLM
            # tools=[
            #     {
            #         "function":{
            #             "name": tool.name,
            #             "description": tool.description,
            #             "parameters": tool.parameters,
            #         },
            #         "type": "function"
            #     }
            #     for tool in self.tools
            # ],
            functions=[
                {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                }
                for tool in self.tools
            ],
            stream=stream,
        )

    async def handle_function_or_tool_call(
        self,
        function_name: str,
        function_arguments: str,
        tool_id: Optional[str] = None,
        *args,
        **kwargs,
    ) -> Union[str, AsyncGenerator[str, None]]:
        (
            self.conversation.append(
                Message(
                    role=MessageType.ASSISTANT,
                    tool_calls=[
                        {
                            "id": tool_id,
                            "function": {
                                "name": function_name,
                                "arguments": function_arguments,
                            },
                        }
                    ],
                )
            )
            if tool_id
            else self.conversation.append(
                Message(
                    role=MessageType.ASSISTANT,
                    function_call={
                        "name": function_name,
                        "arguments": function_arguments,
                    },
                )
            )
        )

        # TODO - We always use tools, not functions
        # Think of ways to make this clearer

        tool = next((t for t in self.tools if t.name == function_name), None)
        if tool:
            raw_result = await tool.results_function(
                *args, **kwargs, **json.loads(function_arguments)
            )
            llm_formatted_result = tool.llm_format_function(raw_result)

            tool_result = ToolResult(
                raw_result=raw_result,
                llm_formatted_result=llm_formatted_result,
            )

            if tool.stream_function:
                tool_result.stream_result = tool.stream_function(raw_result)

            (
                self.conversation.append(
                    Message(
                        tool_call_id=tool_id,
                        role=MessageType.TOOL,
                        content=str(tool_result.llm_formatted_result),
                        name=function_name,
                    )
                )
                if tool_id
                else self.conversation.append(
                    Message(
                        role=MessageType.FUNCTION,
                        content=str(tool_result.llm_formatted_result),
                        name=function_name,
                    )
                )
            )

            return tool_result
        else:
            raise ValueError(f"Tool {function_name} not found")
