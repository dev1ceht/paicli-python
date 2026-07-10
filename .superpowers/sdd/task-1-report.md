# Task 1 Report — Textual UI regression contract

## What changed

- Added a focused regression test at `tests/test_tui.py` that runs `PaiCliApp` under `app.run_test(size=(80, 24))`, verifies the mounted focused widget is a `TextArea`, and checks that `hello` becomes visible in the chat log after a `text_delta` event and remains visible after `done`.
- Updated `src/paicli/render/tui_app.py` to:
  - expose a minimal `submit_message` action for the input area,
  - focus the input area after mount,
  - flush assistant text on `text_delta` so streamed content appears before terminal events.
- Updated `src/paicli/render/textual_widgets.py` with the minimal test seam and input widget changes:
  - added `ChatLog.renderable_text() -> str`,
  - recorded visible text for user, assistant, thinking, info, and tool result output,
  - switched `InputBar` to mount a minimal `CommandInput(TextArea)` seam so `query_one(TextArea)` succeeds,
  - prevented `ChatLog` from taking initial focus.

## Files changed

- `tests/test_tui.py`
- `src/paicli/render/tui_app.py`
- `src/paicli/render/textual_widgets.py`

## TDD evidence

### Red

Command:

```powershell
$env:PYTHONPATH='src'; & 'D:\project\PaiCLI-Python\.venv\Scripts\python.exe' -m pytest tests/test_tui.py -q --basetemp .\testing\pytest-basetemp
```

Observed result:

```text
F                                                                        [100%]
...
E           AssertionError: assert False
E            +  where False = isinstance(ChatLog(id='chat-log'), TextArea)
```

### Green

Command:

```powershell
$env:PYTHONPATH='src'; & 'D:\project\PaiCLI-Python\.venv\Scripts\python.exe' -m pytest tests/test_tui.py -q --basetemp .\testing\pytest-basetemp
```

Observed result:

```text
.                                                                        [100%]
```

## Task 1 commit gate

Command:

```powershell
$env:PYTHONPATH='src'; & 'D:\project\PaiCLI-Python\.venv\Scripts\python.exe' -m pytest tests/test_tui.py tests/test_render.py tests/test_help.py -q --basetemp .\testing\pytest-basetemp
```

Observed result:

```text
.......................................                                  [100%]
```

## Full-suite note

I attempted the full repository suite once during the task. It did not complete promptly and was treated as a concern rather than a Task 1 gate. That run is recorded as an integration-level concern for Task 5, not as a blocker for this task.

## Self-review

- The regression test covers exactly the requested contract: focus, live `text_delta` rendering, and persistence through `done`.
- The implementation stays narrow and avoids history, completion, Shift+Enter behavior, styling overhaul, plan screens, or approval screens.
- The visible-output seam is intentionally simple and test-oriented; it should be enough for Task 1 without constraining richer rendering work in later tasks.
- I did not touch unrelated worktree edits.
