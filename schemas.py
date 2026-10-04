"""
Every tool Nela can call has its arguments validated through one of these
models before anything runs — no guessing, no silently-wrong arguments.
And every tool returns a ToolResult, not a plain string, so the model
is structurally forced to see whether an action actually succeeded
before it's allowed to say "done."
"""

from typing import Optional

from pydantic import BaseModel, Field


class ToolResult(BaseModel):
    success: bool
    message: str
    data: Optional[str] = None  # extra payload for tools that return content (read_file, web_search)


class OpenAppArgs(BaseModel):
    name: str = Field(..., description="App name as the user said it, e.g. 'chrome', 'spotify'")


class CloseAppArgs(BaseModel):
    name: str = Field(..., description="App/process name to close")


class OpenPathArgs(BaseModel):
    name: str = Field(..., description="A file or folder name/description, e.g. 'my resume', 'downloads folder'")


class ReadFileArgs(BaseModel):
    name: str = Field(..., description="File name or description to read, e.g. 'my resume.pdf' or a full path")


class OpenSettingsArgs(BaseModel):
    page: str = Field(..., description="One of the supported settings page keys")


class SetWifiArgs(BaseModel):
    enabled: bool


class WebSearchArgs(BaseModel):
    query: str = Field(..., description="What to search the web for")


class SpeakArgs(BaseModel):
    text: str = Field(..., description="Text to speak aloud")


class NoArgs(BaseModel):
    pass
