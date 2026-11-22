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
