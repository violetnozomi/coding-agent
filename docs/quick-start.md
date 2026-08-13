# NZ-Coder five-minute quick start

_Install, configure, validate, and start a repository coding session._

---

## 📦 Install

NZ-Coder metadata supports Python 3.9 or newer. The current release evidence is
Linux/Python 3.13; Python 3.9–3.12 and other operating systems still require the
documented release matrix. `pipx` is the recommended user install because it
keeps the application separate from project dependencies.

```bash
pipx install /path/to/nzcoder

# Equivalent alternatives
uv tool install /path/to/nzcoder
python -m pip install /path/to/nzcoder
```

The project is not yet published under a stable package index name, so these
commands assume you already have a source checkout or local release artifact.
The five-minute flow starts from that point.

For development, use `python -m pip install -e ".[dev]"` from the checkout.
The optional semantic experiment is deliberately excluded from the default
installation; install it only with `python -m pip install ".[semantic-experiment]"`.

## ⚙️ Configure

Open the trusted repository that NZ-Coder may inspect and modify:

```bash
cd /path/to/repository
nz-coder init
```

Edit the generated `.env` and set the endpoint, model, and one credential. A
minimal OpenAI-compatible example is:

```dotenv
API_KEY=replace-me
BASE_URL=https://provider.example/v1
MODEL_ID=provider/model-name
PERMISSION_MODE=default
```

`nz-coder init` creates `.env` with owner-only permissions and refuses to
overwrite an existing file. Shell environment variables take precedence.

## ✅ Validate

```bash
nz-coder doctor
nz-coder config show --sources
nz-coder platform
```

`FAIL` means a required capability is unavailable. `WARN` identifies optional
or experimental capabilities, such as an uninstalled language server; it does
not mean every coding task is blocked.

## 🚀 Start coding

```bash
nz-coder
```

At the prompt, try:

```text
Inspect this repository and explain its test strategy.
/mode default
/attach path/to/failure.log
Fix the smallest root cause and run the targeted tests.
/diff
/status
```

Use `Enter` to submit, `Alt+Enter` for a newline, `Ctrl+P` for the command
palette, and type `/` to see command completion. `Ctrl+C` cancels a draft or
active run; press it twice while idle to exit.

## 🔄 Continue later

```text
/sessions
/session
/timeline
/fork 3
/undo
/redo
```

For automation, use the same native runtime through headless JSONL:

```bash
nz-coder run --output jsonl "inspect the failure and propose a minimal fix"
```

See the [CLI reference](cli-reference.md), [daemon guide](remote-daemon.md), and
[troubleshooting guide](troubleshooting.md) for the next steps.
