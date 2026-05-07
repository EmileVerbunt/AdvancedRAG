"""Versioned prompt loader using Jinja2 templates from config/prompts."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined


@dataclass(slots=True)
class RenderedPrompt:
    name: str
    version: str
    system: str
    user: str


class PromptRegistry:
    """Loads `<name>.<version>.j2` templates and renders them.

    Each template uses a top-level `SYSTEM:` and `USER:` separator so a single
    file holds both halves of the chat prompt.
    """

    def __init__(self, prompts_dir: Path) -> None:
        self._dir = prompts_dir
        self._env = Environment(
            loader=FileSystemLoader(str(prompts_dir)),
            undefined=StrictUndefined,
            keep_trailing_newline=True,
        )

    def render(self, name: str, version: str, **context: object) -> RenderedPrompt:
        template = self._env.get_template(f"{name}.{version}.j2")
        rendered = template.render(**context)
        system, user = self._split(rendered)
        return RenderedPrompt(name=name, version=version, system=system, user=user)

    @staticmethod
    def _split(text: str) -> tuple[str, str]:
        s_marker = "SYSTEM:"
        u_marker = "USER:"
        if s_marker not in text or u_marker not in text:
            return "", text.strip()
        _, rest = text.split(s_marker, 1)
        system, user = rest.split(u_marker, 1)
        return system.strip(), user.strip()
