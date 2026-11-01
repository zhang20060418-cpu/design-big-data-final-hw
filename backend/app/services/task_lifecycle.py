"""Process ownership and conservative startup recovery; never replay API calls."""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
import uuid


def process_identity(pid: int) -> tuple[str, str | None]:
    """Read-only liveness and creation identity, including PID reuse protection."""
    if not isinstance(pid,int) or pid<=0:return 'dead',None
    if os.name=='nt':
        import ctypes
        from ctypes import wintypes
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
        kernel.OpenProcess.restype=wintypes.HANDLE
        kernel.CloseHandle.argtypes=[wintypes.HANDLE]
        kernel.GetExitCodeProcess.argtypes=[wintypes.HANDLE,ctypes.POINTER(wintypes.DWORD)]
        kernel.GetProcessTimes.argtypes=[wintypes.HANDLE]+[ctypes.POINTER(wintypes.FILETIME)]*4
        handle=kernel.OpenProcess(0x1000,False,pid)
        if not handle:return ('unknown',None) if ctypes.get_last_error()==5 else ('dead',None)
        try:
            exit_code=wintypes.DWORD()
            if not kernel.GetExitCodeProcess(handle,ctypes.byref(exit_code)):return 'unknown',None
            if exit_code.value!=259:return 'dead',None
            times=[wintypes.FILETIME() for _ in range(4)]
            token=None
            if kernel.GetProcessTimes(handle,*[ctypes.byref(t) for t in times]):
                token=str((times[0].dwHighDateTime<<32)|times[0].dwLowDateTime)
            return 'alive',token
        finally:kernel.CloseHandle(handle)
    try:
        os.kill(pid,0)
    except ProcessLookupError:return 'dead',None
    except PermissionError:return 'unknown',None
    try:
        # Linux /proc: field 22 is process start time; handle spaces in comm.
        fields=Path(f'/proc/{pid}/stat').read_text().rsplit(')',1)[1].split()
        return 'alive',fields[19]
    except (OSError,IndexError):return 'alive',None


def write_owner(directory: Path, *, phase: str) -> None:
    try:
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        payload = {
            'schema_version':'task-owner-1',
            "pid": os.getpid(),
            'process_start_token':process_identity(os.getpid())[1],
            "phase": phase,
            "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
        path=directory/'owner.json'
        temp=directory/f'.owner.{uuid.uuid4().hex}.tmp'
        try:
            temp.write_text(json.dumps(payload,ensure_ascii=False),encoding='utf8')
            temp.replace(path)
        finally:temp.unlink(missing_ok=True)
    except OSError:
        # A read-only or vanished task directory must not fail the pipeline.
        return


def reconcile_active_tasks(stores=None, *, data_dir: Path | None=None) -> dict:
    """Expose abandoned work as interrupted; live/unknown owners remain untouched.

    No gate reset, auto-submit, source modification or provider request occurs.
    The user may explicitly continue in a separate attempt after this check.
    """
    from ..storage import real_store,demo_store,now_iso
    from ..config import DATA_DIR
    stores=[real_store,demo_store] if stores is None else stores
    base=Path(data_dir or DATA_DIR)
    result={'interrupted_task_ids':[],'live_owners_preserved':0,'unknown_owners_preserved':0}
    for store in stores:
        for task in store.list(100000,0):
            if task['status'] not in ('running','queued','preparing'):continue
            root=base/('demo_tasks' if store.mode=='demo' else 'tasks')/task['id']
            owner_path=root/'owner.json'
            try:owner=json.loads(owner_path.read_text(encoding='utf8'))
            except (OSError,ValueError):owner={}
            if not isinstance(owner,dict):owner={}
            state,token=process_identity(owner.get('pid'))
            if state=='unknown':
                result['unknown_owners_preserved']+=1;continue
            if state=='alive' and (not owner.get('process_start_token') or not token or owner['process_start_token']==token):
                result['live_owners_preserved']+=1;continue
            latest=store.get(task['id'])
            if latest is None or latest['status'] not in ('running','queued','preparing'):continue
            message='服务重启后确认原执行进程已中断；未自动重新发送请求，可选择继续审查或重新审查。'
            gate=root/'analysis/review_gate.json'
            try:
                if json.loads(gate.read_text(encoding='utf8')).get('state')=='executing':
                    message+=' 上次API请求结果未知，继续可能产生新的调用消耗。'
            except (OSError,ValueError):pass
            stages=latest.get('stages',[])
            target=next((s for s in stages if s.get('status')=='running'),None)
            if target is None:target=next((s for s in stages if s.get('status')=='pending'),None)
            if target is not None:target.update(status='failed',message=message,finished_at=now_iso())
            store.update(task['id'],status='failed',error='interrupted: '+message,
                         stages_json=stages,finished_at=now_iso())
            result['interrupted_task_ids'].append(task['id'])
    return result
