# Archive wait decision

On candidate `362157a`, the official Lead made six decision submissions in the first research turn of Run `gxr_01m8g0b9dfdm3empcn`. The sixth submission was accepted. The original requests, responses and scientific score remain with the benchmark. `first-turn.json` indexes the actual incoming tool schema, call IDs, stored request and response hashes, and the accepted event. This note concerns those six calls only.

| Submission | Submitted | Actual disposition |
| --- | --- | --- |
| 1 | `mode=amend` with `ops=[]` | Refused because the explicit mode and operations disagree. The author could intend to wait or could have omitted an operation. |
| 2 | One sentence in the watch array | Refused because each watch requires an object. |
| 3 | One object with `description` | Refused because the event and subject were unspecified. |
| 4 | `event=research.catalog.updated` and the Run ID | Refused because this event is outside the registered set. |
| 5 | `evidence_added` and the correct Run ID, with `disposition=MAINTAIN` | Refused because the author had not registered a comparison. This is a separate decision about scientific intent. |
| 6 | Same valid watch with the comparison fields omitted | The Run accepted the wait. |

The official schema already required `event` and `subject_ref` and enumerated the seven events. The first two watch errors did not tell the Lead that it could omit this optional field and keep its scientific condition in the rationale. Waiting for an event causes the Lead to reconsider the evidence. GX does not evaluate a free-text predicate about a future publication.

The patch adds guidance only to failed, current-policy Run Lead calls with these field errors. The mode refusal gives both actions and explains the default wait. A malformed watch reports the registered events and one complete event/subject example, with instructions for choosing the ordinary wait or a custom event. If the draft also supplies comparison disposition without a comparison ID, the reply explains when those fields apply. It preserves the scientific choice for the caller and records no new decisions, subscriptions or Jobs on failure. Successful inputs, original tool menus, old-policy Runs, and independent module inputs keep their existing behavior. Historical callback IDs replay their saved responses.

## Controlled verification

Run the two suites from the product root.

```sh
export GX_CODEX_OFFLINE=1 PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH=gx-h:.:gx-h/tests:acceptance/v62a
.venv/bin/python acceptance/harness_final/watch_diagnostics/test_wait_refusal.py -v
.venv/bin/python -m unittest -v acceptance.harness_refactor.closing.test_field_format acceptance.harness_refactor.probes.test_method_wake
```

`baseline.log` records the missing recovery guidance on the frozen candidate. `green.log` covers the original three malformed watches, their exact replay, the contradictory mode, explicit and default valid watches, the separate comparison refusal and the independent module reply. `compatibility.log` contains the existing closing and Job wake tests. These are controlled native callbacks on new ordinary Goals. The official benchmark was not rerun, and the number of real model submissions saved by the new replies is unknown.
