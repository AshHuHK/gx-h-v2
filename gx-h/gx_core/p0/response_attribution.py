"""Attribution from observed Codex response boundaries and executed tool calls."""
from collections import defaultdict
from dataclasses import dataclass, field
import logging
import sqlite3

from ..domain import clock

RULE_VERSION = 'gx-response-class/1'
TOKEN_FIELDS = ('input_tokens', 'cached_input_tokens', 'output_tokens', 'reasoning_tokens', 'total_tokens')
READ = {'gx_task_context', 'gx_read_artifact', 'gx_grid_measurement', 'gx_capability_inspect', 'gx_project_state'}
CONTROL = {'gx_job_submit', 'gx_job_cancel', 'gx_job_status', 'gx_wait_job', 'gx_propose_intent'}
TERMINAL = {'ACCEPTED', 'TERMINAL_REJECTED', 'SCHEMA_INVALID_LIMIT'}
CALL_TYPES = {'function_call', 'custom_tool_call', 'local_shell_call', 'tool_search_call',
              'web_search_call', 'image_generation_call'}


def is_tool_item(item):
    return item.get('type') in CALL_TYPES or item.get('type') in ('custom_tool_call_output', 'function_call_output')


def tokens(params):
    usage = params.get('usage') or {}

    def count(key, *, positive=False):
        value = usage.get(key) if isinstance(usage, dict) else None
        return value if type(value) is int and value >= int(positive) else None

    # The pinned Responses adapter normalizes omitted detail buckets to zero.
    # Only positive detail counts distinguish an upstream value from that default.
    return (count('inputTokens'), count('cachedInputTokens', positive=True), count('outputTokens'),
            count('reasoningOutputTokens', positive=True), count('totalTokens'))


@dataclass
class _Turn:
    ordinal: int | None = None
    pending: dict = field(default_factory=dict)
    seen_calls: set = field(default_factory=set)
    wrappers: set = field(default_factory=set)
    uncertain_wrapper: bool = False


class Attribution:
    """One native connection; reject cross-connection guesses on a resumed turn."""

    def __init__(self, conn, run_id):
        self.conn = conn
        self.run_id = run_id
        self.turns = {}

    def start(self, thread, turn):
        if (thread, turn) in self.turns:
            return
        # A freshly attached connection can miss responses in an existing turn.
        # Only a live turn/started and no prior receipts assert a known first request.
        prior = self.conn.execute('SELECT 1 FROM p0_meter_responses WHERE thread_id=? AND turn_id=? LIMIT 1',
                                  (thread, turn)).fetchone()
        self.turns[thread, turn] = _Turn(ordinal=None if prior else 0, uncertain_wrapper=bool(prior))

    def end(self, thread, turn):
        self.turns.pop((thread, turn), None)

    def item(self, thread, turn, item):
        kind = item.get('type')
        if kind not in CALL_TYPES and kind not in ('custom_tool_call_output', 'function_call_output'):
            return
        call_id = item.get('call_id') or item.get('id')
        if not isinstance(call_id, str) or not call_id:
            return
        state = self.turns.setdefault((thread, turn), _Turn(uncertain_wrapper=True))
        if kind in CALL_TYPES:
            if call_id in state.seen_calls:
                return
            state.seen_calls.add(call_id)
            name, namespace = item.get('name') or kind, item.get('namespace')
            wrapper = kind == 'custom_tool_call' and name == 'exec' and namespace in (None, 'functions')
            state.pending[call_id] = (name, namespace, wrapper, None)
            if wrapper:
                state.wrappers.add(call_id)
        elif call_id in state.wrappers:
            # These status headers are written by Codex, ahead of the tool's content.
            # A yielded cell may continue to issue callbacks on a later request.
            output = item.get('output')
            if isinstance(output, list) and output and isinstance(output[0], dict):
                output = output[0].get('text')
            status = output.partition('\n')[0] if isinstance(output, str) else None
            if status not in ('Script completed', 'Script failed', 'Script terminated'):
                state.uncertain_wrapper = True
            else:
                self.model_outcome(thread, turn, call_id, status)
            state.wrappers.discard(call_id)

    def model_outcome(self, thread, turn, call_id, outcome):
        state = self.turns.get((thread, turn))
        if not state or call_id not in state.seen_calls:
            return
        if call_id in state.pending:
            name, namespace, wrapper, _ = state.pending[call_id]
            state.pending[call_id] = (name, namespace, wrapper, outcome)
        else:
            self.conn.execute("UPDATE p0_response_tools SET outcome=coalesce(outcome,?) WHERE thread_id=? "
                              "AND turn_id=? AND source='MODEL' AND call_id=?", (outcome, thread, turn, call_id))

    def completed(self, root, thread, turn, params):
        rid = params.get('responseId')
        state = self.turns.setdefault((thread, turn), _Turn(uncertain_wrapper=True))
        if not isinstance(rid, str) or not rid:
            state.pending.clear()
            state.ordinal = None
            return
        usage = tokens(params)
        ordinal = None if state.ordinal is None else state.ordinal + 1
        inserted = self.conn.execute(
            'INSERT OR IGNORE INTO p0_response_attribution(run_id,root_id,thread_id,turn_id,response_id,'
            'request_ordinal,observed_at,input_tokens,cached_input_tokens,output_tokens,reasoning_tokens,total_tokens) '
            'VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
            (self.run_id, root, thread, turn, rid, ordinal, clock.now_iso(), *usage))
        if not inserted.rowcount:
            old = self.conn.execute('SELECT * FROM p0_response_attribution WHERE thread_id=? AND response_id=?',
                                    (thread, rid)).fetchone()
            if old['turn_id'] != turn or tuple(old[k] for k in TOKEN_FIELDS) != usage:
                self.conn.execute('UPDATE p0_response_attribution SET conflicting=1 WHERE thread_id=? AND response_id=?',
                                  (thread, rid))
            # Do not give a delayed duplicate any pending calls from the next response.
            return
        state.ordinal = ordinal
        self.conn.executemany(
            'INSERT OR IGNORE INTO p0_response_tools(run_id,thread_id,turn_id,call_id,source,tool,namespace,'
            'model_call_id,response_id,link,outcome) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
            [(self.run_id, thread, turn, call, 'MODEL', name, ns, call, rid,
              'code_mode' if wrapper else 'raw_item', outcome)
             for call, (name, ns, wrapper, outcome) in state.pending.items()])
        state.pending.clear()

    def outcome(self, params, disposition):
        thread, turn, call, name = (params.get(k) for k in ('threadId', 'turnId', 'callId', 'tool'))
        if not all(isinstance(v, str) and v for v in (thread, turn, call, name)):
            return
        state = self.turns.get((thread, turn))
        if state is None:
            return  # belongs to a different or never observed native turn
        # A direct call joins by its actual ID even if its completion marker is delayed.
        # Code mode generates unrelated exec-UUIDs for nested calls. Associate only
        # while exactly one observed wrapper is active and no prior cell could be running.
        parent, relation = call, 'call_id'
        if (call.startswith('exec-') and call not in state.seen_calls
                and not state.uncertain_wrapper and len(state.wrappers) == 1):
            parent, relation = next(iter(state.wrappers)), 'single_code_mode_interval'
        try:
            self.conn.execute(
                'INSERT OR IGNORE INTO p0_response_tools(run_id,thread_id,turn_id,call_id,source,tool,namespace,'
                'model_call_id,link,outcome) VALUES(?,?,?,?,?,?,?,?,?,?)',
                (self.run_id, thread, turn, call, 'GX', name, params.get('namespace'), parent, relation, disposition))
        except sqlite3.Error as exc:
            logging.getLogger(__name__).warning('GX callback attribution unavailable (%s)', type(exc).__name__)


def _tool_class(tool):
    name, namespace = tool['tool'], tool.get('namespace')
    # science_grid@3 exposes its terminal submission directly to the model.
    if (namespace, name) == ('gx', 'gx_submit_product'):
        return 'PRODUCE'
    if namespace in (None, 'functions'):
        if name in READ:
            return 'READ'
        if name == 'gx_submit_product':
            return 'PRODUCE'
        if name in CONTROL:
            return 'OTHER_CONTROL'
    if namespace == 'collaboration' and name in {'spawn_agent', 'send_message', 'followup_task', 'wait_agent',
                                                 'list_agents', 'interrupt_agent'}:
        return 'OTHER_CONTROL'
    return 'UNCLASSIFIED'


def classify(rows):
    """Annotate ordered response rows; only tool names and committed dispositions enter the rules."""
    phases = {}
    for row in rows:
        key = row['thread_id']
        phase = phases.get(key, 'OPEN' if row['request_ordinal'] is not None else 'UNKNOWN')
        calls = row['tool_calls']
        parents = {call['model_call_id'] for call in calls if call['source'] == 'GX'}
        classes = set()
        dispositions = set()
        for call in calls:
            call['tool_class'] = _tool_class(call)
            if call['source'] == 'MODEL' and call['call_id'] in parents:
                continue  # wrapper and its executed calls describe the same operation
            classes.add(call['tool_class'])
            dispositions.add(call['outcome'])
        if phase in ('POST_SUBMIT', 'REPAIR'):
            category = phase
        elif phase == 'UNKNOWN' or (phase == 'SUBMITTED' and 'PRODUCE' in classes):
            category = 'UNCLASSIFIED'
        elif len(classes - {'UNCLASSIFIED'}) > 1:
            category = 'MIXED'
        elif classes and 'UNCLASSIFIED' not in classes:
            category = next(iter(classes))
        else:
            category = 'UNCLASSIFIED'
        row.update(workflow_phase=phase, request_class=category, classification_version=RULE_VERSION)
        if dispositions & TERMINAL:
            phases[key] = 'POST_SUBMIT'
        elif 'SCHEMA_INVALID' in dispositions and phase != 'POST_SUBMIT':
            phases[key] = 'REPAIR'
        elif 'PRODUCE' in classes and phase == 'OPEN':
            phases[key] = 'SUBMITTED'
    return rows


def summarize(rows):
    groups = defaultdict(list)
    for row in rows:
        groups[row['request_class']].append(row)
    result = {}
    for category, group in sorted(groups.items()):
        totals = {}
        for key in TOKEN_FIELDS:
            values = [r[key] for r in group if not r['conflicting'] and r[key] is not None]
            totals[key] = {'observed_count': len(values), 'observed_sum': sum(values) if values else None}
        result[category] = {'responses': len(group), 'usage': totals}
    return result


def report(conn, run_id):
    """Read-only projection. No billing and no subtraction from cumulative accounts."""
    rows = [dict(row) for row in conn.execute(
        'SELECT a.*,r.root_key,r.task_id,r.attempt,r.role, '
        'CASE WHEN a.thread_id=r.thread_id THEN r.model END AS requested_model, '
        'CASE WHEN a.thread_id=r.thread_id THEN r.provider END AS requested_provider,NULL AS model,NULL AS provider, '
        'CASE WHEN a.thread_id=r.thread_id THEN 1 ELSE c.metadata_verified END AS ownership_verified '
        'FROM p0_response_attribution a LEFT JOIN p0_roots r ON r.thread_id=a.root_id AND r.run_id=a.run_id '
        'LEFT JOIN p0_native_child_meters c ON c.child_id=a.thread_id AND c.run_id=a.run_id '
        'WHERE a.run_id=? ORDER BY a.rowid', (run_id,))]
    by_response = {(r['thread_id'], r['turn_id'], r['response_id']): r for r in rows}
    for row in rows:
        row['tool_calls'] = []
    calls = [dict(row) for row in conn.execute(
        'SELECT t.thread_id,t.turn_id,t.call_id,t.source,t.tool,t.namespace,t.model_call_id,t.link,t.outcome,'
        'm.response_id FROM p0_response_tools t LEFT JOIN p0_response_tools m ON m.thread_id=t.thread_id '
        "AND m.turn_id=t.turn_id AND m.source='MODEL' AND m.call_id=t.model_call_id WHERE t.run_id=? ORDER BY t.rowid",
        (run_id,))]
    unlinked = []
    for call in calls:
        row = by_response.get((call['thread_id'], call['turn_id'], call['response_id']))
        (row['tool_calls'] if row else unlinked).append(call)
    classify(rows)
    first = [{'thread_id': r['thread_id'], 'turn_id': r['turn_id'], 'response_id': r['response_id'],
              'input_tokens': None if r['conflicting'] else r['input_tokens']} for r in rows if r['request_ordinal'] == 1]
    return {'classification_version': RULE_VERSION, 'response_count': len(rows), 'by_class': summarize(rows),
            'first_request_input_tokens': first, 'responses': rows, 'unlinked_tools': unlinked}


def main():
    """Explicit export keeps ordinary budget reports bounded."""
    import argparse
    from contextlib import closing
    import json
    from pathlib import Path

    parser = argparse.ArgumentParser(description='Export observed native responses and their tool attribution.')
    parser.add_argument('database', type=Path, help='GX SQLite database')
    parser.add_argument('run_id', help='GX Run ID')
    args = parser.parse_args()
    with closing(sqlite3.connect(args.database.resolve().as_uri() + '?mode=ro', uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        print(json.dumps(report(conn, args.run_id), separators=(',', ':')))


if __name__ == '__main__':
    main()
