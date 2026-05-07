"""Pydantic models for Vapi webhook payloads.

Real Vapi tool-call shape (verified 2026-05 via docs.vapi.ai/server-url):

Request:
  {"message": {
     "type": "tool-calls",
     "call": {"assistantId": "...", "customer": {"number": "+1..."}},
     "toolCallList": [{"id": "call_xxx", "name": "fn_name", "parameters": {...}}]
  }}

Response:
  {"results": [{"toolCallId": "call_xxx", "name": "fn_name", "result": "<json string>"}]}
"""
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class VapiCallCustomer(BaseModel):
    model_config = ConfigDict(extra="allow")
    number: str | None = None


class VapiCallObject(BaseModel):
    model_config = ConfigDict(extra="allow")
    assistantId: str | None = None
    customer: VapiCallCustomer = Field(default_factory=VapiCallCustomer)


class VapiFunctionCall(BaseModel):
    model_config = ConfigDict(extra="allow")
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class VapiToolCall(BaseModel):
    """One tool invocation. Vapi nests name/args under `function`, not top-level
    (despite what the doc examples show). Keep `function` as the source of truth."""
    model_config = ConfigDict(extra="allow")
    id: str
    type: str | None = None  # always "function" in practice
    function: VapiFunctionCall


class VapiToolCallMessage(BaseModel):
    model_config = ConfigDict(extra="allow")
    type: str
    call: VapiCallObject = Field(default_factory=VapiCallObject)
    toolCallList: list[VapiToolCall] = Field(default_factory=list)


class VapiToolCallRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    message: VapiToolCallMessage


class VapiToolCallResult(BaseModel):
    toolCallId: str
    name: str | None = None
    result: str  # Vapi requires this be a JSON-stringified value


class VapiToolCallResponse(BaseModel):
    results: list[VapiToolCallResult]
