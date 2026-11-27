# Local project bridge

`gx-web` binds to `127.0.0.1` and attaches a browser to one named project's state directory. It reads the same CLI status, durable feedback inbox and recorded task, Job and run transitions as the native project driver. The operator selects the project, state directory and optional Markdown commission on the command line.

```bash
bin/gx-web --project demo --state-dir ./var/demo \
  --prompt-file examples/module_collaboration/question.md --collaboration module-leads
```

Open `http://127.0.0.1:8080/`. **Start or resume** runs `bin/gx-project --background` and displays its startup receipt. A fresh project uses the selected Markdown. An existing project continues with its saved settings. Omit `--prompt-file` to attach to an existing CLI project's status, milestones and feedback. The page shows the most recent captured Root reply when the project receipt identifies it. Status updates and reading events consume no model turn.

| Method | Path | Result |
| --- | --- | --- |
| GET | `/api/science/conversations` | The configured project ID and launch availability |
| GET | `/api/science/conversations/demo` | Project status, progress and feedback delivery receipts |
| GET | `/api/science/conversations/demo/events` | SSE milestones from the project's GX event log |
| POST | `/api/science/conversations/demo/messages` | Start the configured commission or enqueue a user addition |

The `science` URL prefix comes from the original web bridge. Replace `demo` with the selected project ID. Send `{"kind":"launch"}` to start or resume. Send `{"kind":"feedback","client_message_id":"follow-up-01","text":"Compare both windows."}` to append instructions. Repeat the same message ID and body after a lost HTTP reply. SSE accepts GX event IDs in `Last-Event-ID` or the `after` query. Browser reconnections use the last delivered cursor. The server is intended for a local single-user session.

## Research conversations

From the release directory, start or resume the scientific study through its usual entry point. A second terminal can attach to its workspace while research continues.

```bash
gx-h/bin/gx-web --state-dir /path/to/study-state --project pcna-first-flight --port 8080
```

Open `http://127.0.0.1:8080/` and choose the Run. To open a particular Run by default, append `--run-id gxr_…`. The page shows the selected Goal, messages from both clients, Main replies, questions from the research team and recorded feedback outcomes. Open a feedback evidence link for the corresponding delivery and disposition. Select a route to attach a note to that route. For a closed Goal, **Start a follow-up study** sends the new question with the originals selected in the form.

Use the same Run from the CLI:

```bash
bin/gx-p0 conversation --state /path/to/study-state --run-id "$run_id"
bin/gx-p0 message --state /path/to/study-state --run-id "$run_id" \
    --message-id pi-window-question --text 'What evidence supports the choice of window?'
```

On the P0 route, GET accepts `?run_id=…`; the POST envelope requires `run_id`, `text` and `client_message_id`. Optional anchors are `conversation_id`, `goal_version`, `branch_id`, `question_id` and `reply_to`. An anchored inquiry keeps a blocking question open. To select a listed answer directly, send its exact text in `answer_option` with `question_id`. A 202 receipt means GX durably received the message. The subsequent timeline records Main’s actual reply and scientific outcomes. Reuse the exact envelope and ID after a lost response. A conflicting use returns 409. The project’s `/basis` endpoint reads the published decision record, optionally scoped by Goal version, branch, decision reference or event cursor. Add `message_id` and `feedback_id` for one targeted feedback record.
