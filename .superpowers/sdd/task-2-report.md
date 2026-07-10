# Task 2 Report: Aurora widgets and persistent multiline input

## Scope

- Created `src/paicli/render/history.py`
- Modified `src/paicli/render/textual_widgets.py`
- Modified `tests/test_tui.py`

No other source files were changed.

## TDD record

### RED

Command:

`D:\project\PaiCLI-Python\.venv\Scripts\python.exe -m pytest tests/test_tui.py -q --basetemp .\testing\pytest-basetemp`

Observed failure:

```text
ImportError while importing test module 'D:\project\PaiCLI-Python\.worktrees\textual-ui-repair\tests\test_tui.py'.
tests\test_tui.py:9: in <module>
    from paicli.render.history import PromptHistory
E   ModuleNotFoundError: No module named 'paicli.render.history'
```

This was the expected initial RED state for Task 2.

### GREEN

Implemented:

- `PromptHistory(path: Path, limit: int = 200)` with UTF-8 persistent storage, bounded history, and cursor navigation.
- `CommandInput(TextArea)` support for:
  - `MessageSubmitted(value: str)` on Enter
  - Shift+Enter newline insertion
  - Up/Down prompt history only for empty or single-line text
  - Tab completion for slash commands
- `ChatLog.add_info()` Rich-markup parsing via `Text.from_markup(...)` with plain-text fallback on markup errors.
- Aurora palette updates for `ToolCard`, `ChatLog`, `StatusBar`, and `InputBar`.

## Test coverage added

Added focused Task 2 coverage in `tests/test_tui.py` for:

- UTF-8 prompt history round-trip
- Rich markup rendering without literal tag leakage
- `MessageSubmitted` posting on Enter
- Shift+Enter newline preservation
- Up/Down history behavior for empty and single-line input only
- Tab completion for `/help`

## Verification

### Focused TUI command

Command:

`D:\project\PaiCLI-Python\.venv\Scripts\python.exe -m pytest tests/test_tui.py -q --basetemp .\testing\pytest-basetemp`

Result:

```text
.......                                                                  [100%]
```

### Commit-gate command

Command:

`D:\project\PaiCLI-Python\.venv\Scripts\python.exe -m pytest tests/test_tui.py tests/test_render.py tests/test_help.py -q --basetemp .\testing\pytest-basetemp`

Result:

```text
..........................................                            [100%]
```

## Notes / concerns

- `CommandInput` currently defaults its slash completion list to `["/help"]` unless a caller injects a broader list. This is enough for Task 2 and keeps command execution/routing out of scope, but fuller command vocabulary wiring belongs in a later task.
- Prompt history persistence uses one JSON string per line so multiline entries round-trip safely while staying UTF-8 text on disk.

## Commit intent

Suggested commit message used for this task:

`feat: add textual history and aurora widgets`
