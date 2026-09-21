# Task Router Agent

Takes a natural-language query, asks a model which registered task it means,
runs that task locally, says the result back in plain English, and speaks it.

```
POST /active_window  {"content": "what app am i working on?"}

  PromptIngestor    build a routing prompt from the task catalog
  ModelGate.route   ask the model  ->  {"task_id":"get-active-app","args":[]}
  Consumer          decode the reply
  TaskValidator     resolve task_id against the registry
  TaskRunner        run the script, capture stdout

  PromptIngestor    build a speech prompt from the execution envelope
  ModelGate.speak   rephrase the raw result as one spoken line
  ModelGate.synth.  render that line to a .wav in src/audio/

{"ok": true, "task_id": "get-active-app", "output": "{\"app\":\"Code.exe\"}",
 "spoken_text": "You're working in Visual Studio Code.",
 "audio_path": "src/audio/20260916-184259-a1b2c3d4e5f6.wav", ...}
```

## Running

```
pip install -r requirements.txt
python serve.py            # http://localhost:5000
```

`.env` needs `MODEL_NAME`, `AUDIO_MODEL_NAME`, and `HF_TOKEN`. Optional:
`ROLE`, `TEMPERATURE`, `MAX_TOKENS`, `SPEECH_TEMPERATURE`, `SPEECH_MAX_TOKENS`,
`AUDIO_ENABLED`, `REQUEST_TIMEOUT`, `MAX_RETRIES`, `HOST`, `PORT` (see
`src/config.py`).

Synthesis needs a CUDA GPU and the TTS weights, so `AUDIO_ENABLED` is **off by
default**. The reply still carries `spoken_text`; only the `.wav` is skipped.
Set `AUDIO_ENABLED=true` once a GPU is available.

## Running the model locally

Set `MODEL_BASE_URL` to any OpenAI-compatible endpoint and Hugging Face is
bypassed: no token, no quota, and task output stops leaving the machine — which
matters, because that output includes your calendar.

```
ollama pull qwen2.5:3b-instruct
```

```
MODEL_BASE_URL=http://127.0.0.1:11434/v1
MODEL_ID=qwen2.5:3b-instruct
```

Two things to know:

- **The colon is part of an Ollama model name**, not a provider suffix.
  `model_repo` only splits on `:` when `MODEL_BASE_URL` is unset, otherwise
  `qwen2.5:3b-instruct` would be read as model `qwen2.5` from provider
  `3b-instruct`.
- **CPU inference is slow.** On four cores, budget ~8s to route and ~6s to
  phrase the reply, so ~14s per question. `OLLAMA_KEEP_ALIVE=60m` avoids paying
  a ~15s model load on the first question after an idle spell. `OLLAMA_MODELS`
  moves the model store off the system drive.

A smaller model also holds the prompts to a higher standard: a 3B will copy
details out of a prompt's examples if you let it, which is why the speech
prompt says in as many words that the examples are tone only.

It binds `127.0.0.1` by default. The service runs local scripts on this
machine, so set `HOST=0.0.0.0` only if you actually want that reachable from
the network — and put authentication in front of it first.

| Endpoint | Purpose |
| --- | --- |
| `POST /active_window` | Route a query and run the task |
| `GET /tasks` | What the agent can currently do |
| `GET /health` | Liveness |

## Calendar access

`get-calendar` answers questions about meetings and calls — "do I have a call in
the next three hours", "what's my next meeting", "am I free right now".

Teams' calendar *is* the Exchange calendar, and the script reads it from
**Microsoft Graph** — Exchange Online's authoritative state.

Sign in once; after that the refresh token is used silently:

```
powershell -NoProfile -File src/scripts/get-calendar.ps1 -Login
```

That runs the OAuth device-code flow against the documented "Microsoft Graph
Command Line Tools" public client, so **no Azure app registration is needed**.
Tokens are cached under `%LOCALAPPDATA%\Deputy`, encrypted with DPAPI so only
the signing-in Windows user on that machine can read them. `-Logout` deletes
them. To use your own app registration instead, set `DEPUTY_GRAPH_CLIENT_ID`
and `DEPUTY_GRAPH_TENANT`.

Those two must be real environment variables, not `.env` entries: the script
runs as a subprocess and inherits the OS environment, while `.env` is only read
into `Setting` inside the Python process.

### Why not Outlook COM

The first version of this script used Outlook COM, which needed no auth at all.
It was abandoned because it is **silently wrong**. COM cold-starts a headless
Outlook and reads its local `.ost` cache, which is only as fresh as the last
time Outlook actually ran and finished syncing. On a machine where classic
Outlook is installed but unused, that cache had not been updated in six months:
it missed a meeting created that day and reported a cancelled meeting as live.
A calendar assistant that is confidently wrong is worse than one that fails, and
COM gives no reliable way to know how stale the cache is. Graph has no cache.

### Deliberate choices

- **Cancelled meetings are excluded**, and only counted. Graph states
  `isCancelled` outright, so this is now a fact rather than the
  `MeetingStatus`-guessing the COM version needed.
- **Teams detection is authoritative** — `isOnlineMeeting` plus
  `onlineMeetingProvider == teamsForBusiness`, not a regex on the location text.
- **Every event carries `startsInMinutes`.** The model is told never to do time
  arithmetic — it has no reliable idea what time it is — so the script computes
  the relative figure and the model only reads it out.
- **Recurring series are expanded server-side** by `calendarView`, so there is
  no recurrence handling in the script.

What it sends to the model is deliberately minimal: subject, times, organizer,
a Teams flag, and an attendee *count*. Event bodies, join URLs, and attendee
names are left out, because task output goes to a third-party inference
provider. `-IncludeAttendees` opts back in locally, and is deliberately absent
from `args_allowlist` so the router can never choose it.

Exit codes: `1` Graph request failed, `2` unreadable response, `4` not signed in
(run `-Login`). All surface as a normal `ok: false` rather than a crash.

## Adding a task

Two steps, no code changes.

1. Drop the script in `src/scripts/`.
2. Add an entry to `src/data/tasks.json`.

```json
{
  "id": "get-battery",
  "type": "powershell",
  "executor": "get-battery.ps1",
  "description": "Battery level and charging state. Use for questions about power, charge, or how long the laptop will last.",
  "allow_args": true,
  "args_allowlist": ["-Format", "json"]
}
```

The registry is read fresh whenever `tasks.json` changes on disk, so the router
prompt, `/tasks`, and the executor all pick the entry up without a restart.

Notes on the fields:

- **`description` is the routing logic.** It is the only thing the model sees
  about your task, so write it the way a user would ask for it. Vague
  descriptions are the usual cause of a query landing on the wrong task.
- **`type`** is `powershell`, `python`, `builtin`, or `noop`. Add a new kind of
  executor by adding a handler to `TaskRunner._HANDLERS`.
- **Arguments are opt-in and allowlisted.** Without `allow_args` a task always
  runs bare. With it, `args_allowlist` pins the exact strings the model may
  send; anything else is rejected before the script is spawned. Flags that take
  a free-form value can't be expressed this way — give the script a fixed set
  of modes instead, or leave the value out of the allowlist.
- **Never allowlist a flag that doesn't terminate.** `get-active-app.ps1` has a
  `-Watch` mode that loops forever; it is deliberately absent from the
  allowlist, because one routed request could otherwise hold a subprocess until
  the timeout fires.
- **A task can be registered before it exists.** Script-backed tasks whose file
  is missing are hidden from the router and listed under `unavailable` in
  `/tasks`, so the model is never offered something that can only fail. This is
  why `collect-logs` is in the catalog but not yet routable.

## Script conventions

Scripts are spawned directly with no shell, with `cwd` set to `src/scripts/`
and stdin closed. A script should:

- print its result to **stdout**, and diagnostics to **stderr**
- exit **0** on success, non-zero on failure
- terminate on its own; the default timeout is 60s (`timeout` in the payload,
  up to 900s)

`output` in the response is stdout trimmed of surrounding whitespace; `stdout`
keeps it verbatim.

## Design notes

**The model picks an id, never a filename.** It is given `task_id`,
`description`, and `allowed_args` — no executor paths. The registry alone maps
an id to something runnable, so a hallucinated or injected filename has nothing
to attach to.

**The model's reply is untrusted input.** It is parsed leniently (fences and
stray prose are recovered by `extract_json_object`) and then validated
strictly: unknown ids, type mismatches, and non-allowlisted args are all
rejected before anything is spawned.

**A failed task is a 200, not a 500.** `ok: false` with an `error` means the
pipeline worked and the task didn't. 5xx is reserved for the agent itself
breaking. `routing_reply` always carries the model's raw reply so a bad route
can be diagnosed afterwards.

**`retryable` distinguishes environment from intent.** `timeout`,
`spawn_failed`, and `powershell_not_available` are transient; `validation_failed`,
`script_not_found`, and `task_failed` will fail identically forever.

**Speech is presentation, not the answer.** By the time the rephrase runs, the
task has already executed. A failed rephrase falls back to `SPEECH_FALLBACK`
and a failed synthesis returns `audio_path: null` — neither discards a result
the caller can still use, so neither turns a working task into a 5xx.

**Task stdout is data to read aloud, never instructions.** The speech prompt
says so explicitly, and `ingest_speech` substitutes the query and the output
last, so a `{{TOKEN}}` appearing in either stays literal. Output is capped at
`MAX_SPOKEN_INPUT_CHARS` before it reaches the model.

**`request_id` is generated server-side.** It has to exist even when routing
fails, it must be unique per request, and it names the audio file — so it is
minted in `serve.py` and passed to `Consumer.consume`, which ignores any id the
model put in its reply. `audio_output_path` strips anything path-shaped from it
regardless.

**The TTS model loads on first use.** `ModelHelpers.audio_model` caches it, and
`qwen_tts`/`soundfile` are imported inside the functions that need them, so the
router runs on a machine with no GPU and no TTS stack installed.

## Tests

```
python -m pytest tests -q
```

PowerShell-dependent tests skip automatically where no shell is present. No
test makes a live inference call or loads the TTS model: `tests/conftest.py`
sets `AUDIO_ENABLED=false`, and the speech tests stub the model and the
synthesiser.
