"""Tool-error-key table derived at runtime from the harness retail toolkit source.

Every `raise ValueError(...)` in the retail tools becomes a (key, regex) row:
string literals map to exact patterns, f-string slots become capture groups.
Non-literal raise arguments abort table construction — the table must stay
complete. Because the table is rebuilt from tools.py on every load, it can
never drift from the harness source.
"""

from __future__ import annotations

import ast
import functools
import re
from dataclasses import dataclass
from pathlib import Path

_SLOT = "\x00"


@dataclass(frozen=True, slots=True)
class ErrorKeyTable:
    source: Path
    patterns: tuple[tuple[str, str], ...]
    raise_site_count: int

    def match(self, message: str) -> str:
        """Map a tool error message (with or without the "Error: " prefix) to its key.

        When several patterns match (a template like "(.+?) not found" overlaps
        the exact messages), the one with the most literal characters wins. Zero
        matches or a literal-length tie between distinct keys raises LookupError:
        the message matches no raise site in the source, so investigate the
        harness rather than patching around it.
        """
        text = message.removeprefix("Error: ")
        matches = sorted(
            (
                (len(pattern.replace("(.+?)", "")), key)
                for key, pattern in self.patterns
                if re.fullmatch(pattern, text)
            ),
            reverse=True,
        )
        if not matches or (len(matches) > 1 and matches[0][0] == matches[1][0]):
            raise LookupError(
                f"no unambiguous error-key match for {text!r} "
                f"(candidates: {matches!r}); the message matches no raise site "
                f"in {self.source}"
            )
        return matches[0][1]


@functools.cache
def _load_from_resolved(tools_path: Path) -> ErrorKeyTable:
    tree = ast.parse(tools_path.read_text(), filename=str(tools_path))
    templates: dict[str, int] = {}
    rejected: list[int] = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Raise)
            and isinstance(node.exc, ast.Call)
            and isinstance(node.exc.func, ast.Name)
            and node.exc.func.id == "ValueError"
        ):
            continue
        if len(node.exc.args) != 1:
            rejected.append(node.lineno)
            continue
        arg = node.exc.args[0]
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            template = arg.value
        elif isinstance(arg, ast.JoinedStr) and all(
            isinstance(value, ast.FormattedValue)
            or (isinstance(value, ast.Constant) and isinstance(value.value, str))
            for value in arg.values
        ):
            template = "".join(
                _SLOT if isinstance(value, ast.FormattedValue) else str(value.value)
                for value in arg.values
            )
        else:
            rejected.append(node.lineno)
            continue
        templates[template] = templates.get(template, 0) + 1
    if rejected:
        raise SystemExit(
            f"{tools_path}: non-literal ValueError messages at lines {rejected}; "
            "extend analysis/error_keys.py deliberately instead of skipping them"
        )
    patterns: list[tuple[str, str]] = []
    used_keys: dict[str, int] = {}
    for template in sorted(templates, key=lambda t: (_SLOT in t, t)):
        base = re.sub(
            r"_+",
            "_",
            re.sub(r"[^a-z0-9]+", "_", template.replace(_SLOT, " x ").lower()),
        ).strip("_")
        used_keys[base] = used_keys.get(base, 0) + 1
        key = base if used_keys[base] == 1 else f"{base}_{used_keys[base]}"
        pattern = "".join(
            "(.+?)" if chunk == _SLOT else re.escape(chunk)
            for chunk in re.split(f"({_SLOT})", template)
            if chunk
        )
        patterns.append((key, pattern))
    return ErrorKeyTable(
        source=tools_path,
        patterns=tuple(patterns),
        raise_site_count=sum(templates.values()),
    )


def load_error_keys(tools_path: Path) -> ErrorKeyTable:
    return _load_from_resolved(tools_path.resolve())
