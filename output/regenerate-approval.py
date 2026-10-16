from __future__ import annotations
import asyncio
import json
import sqlite3
import sys
from pathlib import Path
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(r'C:\Users\yuanchuan\Desktop\git\low_code\write')
RUNTIME = Path(r'C:\Users\yuanchuan\AppData\Local\GxpLowcodeEditor')
PLUGIN = Path(r'C:\Users\yuanchuan\.codex\plugins\cache\bianpan-local\gxp-lowcode-editor\0.1.15+codex.local-20261016-binding-fix')
SESSION_ID = '0ef61f517bfe4e4cb325e3bd1350ea19'
ORIGINAL_PLAN_ID = 'c6b187f144c84f5f81b7801dfb154ab4'
ORIGINAL_OWNER = '479668bbb4c94ba1876a33eff152ea7b'
RESULT = ROOT / 'output' / ('approval-regeneration-' + SESSION_ID + '.json')


def local_plan():
    with sqlite3.connect((RUNTIME / 'state' / 'editor.sqlite3').as_uri() + '?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        row = db.execute('SELECT id,session_id,artifact_path,status FROM plans WHERE id=? AND session_id=?', (ORIGINAL_PLAN_ID, SESSION_ID)).fetchone()
        if row is None:
            raise RuntimeError('Original plan does not match the supplied session')
        session = db.execute('SELECT conversation_id,state FROM sessions WHERE id=?', (SESSION_ID,)).fetchone()
    path = Path(row['artifact_path']).resolve()
    if not path.is_relative_to((RUNTIME / 'plans').resolve()):
        raise RuntimeError('Unexpected original plan artifact location')
    original = json.loads(path.read_text(encoding='utf-8'))
    if original.get('session_id') != SESSION_ID or not isinstance(original.get('operations'), list):
        raise RuntimeError('Invalid original plan identity or operations')
    return original, dict(session)


async def main():
    phase = sys.argv[1]
    saved = json.loads(RESULT.read_text(encoding='utf-8')) if RESULT.exists() else {}
    original, original_session = local_plan()
    if not saved and original_session['conversation_id'] != ORIGINAL_OWNER:
        raise RuntimeError('Session ownership has changed since the supplied markers')
    server = StdioServerParameters(command=sys.executable, args=['-B', str(PLUGIN / 'mcp' / 'server.py')])
    async with stdio_client(server) as (read, write):
        async with ClientSession(read, write) as client:
            await client.initialize()

            async def call(name, args):
                response = await client.call_tool(name, args)
                if response.isError:
                    raise RuntimeError(name + ': ' + '\n'.join(c.text for c in response.content if hasattr(c, 'text')))
                data = response.structuredContent
                if data is None:
                    data = json.loads(next(c.text for c in response.content if hasattr(c, 'text')))
                return data

            def record(key, value):
                saved[key] = value
                RESULT.write_text(json.dumps(saved, ensure_ascii=False, indent=2), encoding='utf-8')

            if not saved.get('conversation_id'):
                created = await call('create_conversation', {'label':'Repair compiler binding validation and regenerate supplied approval plan'})
                record('conversation_id', created['conversation_id'])
            owner = saved['conversation_id']
            common = {'conversation_id':owner}
            if not saved.get('attached'):
                attached = await call('attach_edit_session', {**common, 'session_id':SESSION_ID})
                record('attached', attached)
                print(json.dumps({'attached':attached}, ensure_ascii=False), flush=True)
            if phase == 'inspect':
                status = await call('editor_status', common)
                summary = {key:status.get(key) for key in ('planning_ready','write_ready','active_session')}
                record('status_summary', summary)
                print(json.dumps({'status':summary,'operation_count':len(original['operations'])}, ensure_ascii=False), flush=True)
            elif phase == 'plan':
                if saved.get('new_plan'):
                    raise RuntimeError('A regenerated plan already exists; use prepare instead of replanning twice')
                plan = await call('plan_canvas_patch', {**common, 'session_id':SESSION_ID, 'operations':original['operations']})
                record('new_plan', plan)
                print(json.dumps({'plan_id':plan.get('plan_id'),'operation_count':plan.get('operation_count'), 'validation':plan.get('validation'),'write_executed':plan.get('write_executed')}, ensure_ascii=False), flush=True)
            elif phase == 'reopen':
                approval = await call('reopen_approval', {**common, 'transaction_id':'7eedb72b86814438a66ed59381a2f79f'})
                record('reopened_approval', approval)
                print(json.dumps(approval, ensure_ascii=False), flush=True)
                # Keep the MCP transport alive while its approval child holds the window lock.
                # stdio_client tears down its child process tree on exit.
                await asyncio.sleep(5)
                sys.path.insert(0, str(PLUGIN / 'mcp'))
                from gxp_editor.execution_lock import approval_window_lock, ApprovalWindowBusyError
                with sqlite3.connect((RUNTIME / 'state' / 'editor.sqlite3').as_uri() + '?mode=ro', uri=True) as db:
                    row = db.execute('SELECT artifact_path FROM transactions WHERE id=? AND session_id=?', ('7eedb72b86814438a66ed59381a2f79f', SESSION_ID)).fetchone()
                if row is None:
                    raise RuntimeError('Approval transaction disappeared')
                while True:
                    try:
                        with approval_window_lock(row[0]):
                            break
                    except ApprovalWindowBusyError:
                        await asyncio.sleep(3)
            elif phase == 'verify':
                verified = await call('verify_saved_draft', {**common, 'session_id':SESSION_ID})
                record('saved_verification', verified)
                print(json.dumps({'verification':verified}, ensure_ascii=False), flush=True)
                finished = await call('finish_edit_session', {**common, 'session_id':SESSION_ID})
                record('finished', finished)
                print(json.dumps({'finished':finished}, ensure_ascii=False), flush=True)
            elif phase == 'prepare':
                if not saved.get('new_plan',{}).get('plan_id'):
                    raise RuntimeError('No regenerated plan exists')
                if saved.get('approval'):
                    raise RuntimeError('Approval was already prepared; do not open another window')
                approval = await call('prepare_write', {**common, 'plan_id':saved['new_plan']['plan_id']})
                record('approval', approval)
                print(json.dumps(approval, ensure_ascii=False), flush=True)
            else:
                raise RuntimeError('Unknown phase')

try:
    asyncio.run(main())
except Exception as exc:
    (ROOT / 'output' / 'approval-reopen-error.json').write_text(json.dumps({'error':str(exc)}, ensure_ascii=False), encoding='utf-8')
    raise
