"""Resource observations from JobService's effective capability resources.

No scheduler, GPU ownership inference, cgroup, model or HTTP. Group RSS and whole
shared-device growth are sampled abort thresholds. They are not hard memory
quotas. No virtual-address cap is inferred from an RSS allowance.
"""
from __future__ import annotations
import ctypes
import hashlib
import json
import math
import os
from pathlib import Path
import resource
import signal
import time
from typing import Any,Mapping

SCHEMA='gx.job_resource_guard/1'
class ObservationRefused(ValueError):pass


def canonical_sha(value:Any)->str:
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()).hexdigest()


def source_sha()->str:
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def policy_for(capability_version:str,resources:Mapping[str,Any],*,expected_gpu:Mapping[str,Any]|None=None,
               instrument_selftest:bool=False,gpu_min_free_mib:int=4096)->dict:
    """Use declared/tightened bounds; admission and current limits belong to JobService.

    A missing optional host-memory allowance is observed without an RSS abort
    threshold. CPU-only jobs see no GPU.
    """
    if any(type(resources.get(k)) is not int or resources[k]<=0 for k in ('cpu_threads','wall_seconds')):
        raise ObservationRefused('positive CPU and wall resource bounds are required')
    memory=resources.get('memory_mib')
    if memory is not None and (type(memory) is not int or memory<=0):
        raise ObservationRefused('host memory allowance must be positive when declared')
    gpu=resources.get('gpu')
    required_env = {'CUDA_VISIBLE_DEVICES': ''} if gpu is None or instrument_selftest else {}
    if gpu is not None:
        if (not isinstance(gpu,Mapping) or type(gpu.get('memory_mib')) is not int or gpu['memory_mib']<=0
                or not isinstance(gpu.get('scope'),str) or not gpu['scope']):
            raise ObservationRefused('positive GPU allowance and scope are required')
        if type(gpu_min_free_mib) is not int or gpu_min_free_mib<0:
            raise ObservationRefused('GPU free-memory floor must be nonnegative')
        if expected_gpu is not None and (not isinstance(expected_gpu,Mapping)
                or set(expected_gpu)!={'uuid','name','total_mib'} or not isinstance(expected_gpu.get('uuid'),str)
                or not expected_gpu['uuid'].startswith('GPU-') or not isinstance(expected_gpu.get('name'),str)
                or type(expected_gpu.get('total_mib')) is not int or expected_gpu['total_mib']<gpu['memory_mib']):
            raise ObservationRefused('expected physical GPU identity is invalid')
    elif expected_gpu is not None:raise ObservationRefused('CPU resources cannot select a GPU')
    return {'schema':SCHEMA,'capability_version':capability_version,'implementation_sha256':source_sha(),
            'cpu_threads':resources['cpu_threads'],'cpu_seconds':resources['wall_seconds']*resources['cpu_threads'],
            'max_group_rss_bytes':memory*1024*1024 if memory is not None else None,'max_members':4096,
            'gpu_read_timeout_seconds':1.0,'gpu_min_free_mib':gpu_min_free_mib,
            'gpu_max_delta_mib':gpu['memory_mib'] if gpu else None,
            'expected_gpu':dict(expected_gpu) if expected_gpu else None,'cleanup_timeout_seconds':3.0,
            'instrument_selftest':instrument_selftest,
            'required_env':required_env,
            'semantics':'sampled own-process-group RSS and shared-device aggregate growth/free-floor abort; no hard RAM/VRAM quota'}


def before_exec(parent_pid:int,policy:Mapping[str,Any])->None:
    libc=ctypes.CDLL(None,use_errno=True)
    if libc.prctl(1,signal.SIGKILL,0,0,0)!=0 or os.getppid()!=parent_pid:os._exit(125)
    allowed=sorted(os.sched_getaffinity(0));threads=policy['cpu_threads']
    if len(allowed)<threads:os._exit(125)
    os.sched_setaffinity(0,set(allowed[:threads]))
    seconds=int(policy['cpu_seconds'])
    resource.setrlimit(resource.RLIMIT_CPU,(seconds,seconds+1))


def enable_subreaper()->None:
    if ctypes.CDLL(None,use_errno=True).prctl(36,1,0,0,0)!=0:
        raise ObservationRefused('job runner cannot adopt/reap its own descendants')


def parse_stat(text:str)->dict:
    tail=text.rsplit(') ',1)
    if len(tail)!=2:raise ObservationRefused('process identity is unreadable')
    fields=tail[1].split()
    if len(fields)<22:raise ObservationRefused('process identity is truncated')
    return {'state':fields[0],'pgid':int(fields[2]),'start_ticks':fields[19],'rss_pages':int(fields[21])}


def group_sample(pgid:int,*,maximum:int=4096,proc_root:Path=Path('/proc'))->dict:
    names=[p for p in proc_root.iterdir() if p.name.isdecimal()]
    if len(names)>65536:raise ObservationRefused('process census exceeds the bounded observation policy')
    members=[];zombies=[];rss=0;page=os.sysconf('SC_PAGE_SIZE')
    for item in names:
        try:
            with (item/'stat').open() as handle:text=handle.read(8193)
            if len(text)>8192:raise ObservationRefused('process stat exceeds its fixed bound')
            row=parse_stat(text)
        except (FileNotFoundError,ProcessLookupError):continue
        except PermissionError as exc:raise ObservationRefused('process census has an unreadable member; group absence cannot be proven') from exc
        if row['pgid']!=pgid:continue
        if row['state']=='Z':
            zombies.append({'pid':int(item.name),'start_ticks':row['start_ticks']});continue
        if row['rss_pages']<0:raise ObservationRefused('process RSS is invalid')
        members.append({'pid':int(item.name),'start_ticks':row['start_ticks']})
        rss+=row['rss_pages']*page
        if len(members)>maximum:raise ObservationRefused('owned process group exceeds its observed member bound')
    return {'members':members,'zombies':zombies,'rss_bytes':rss,'shared_pages_may_be_counted_more_than_once':True}


def gpu_problem(reading:Mapping[str,Any],baseline_mib:int,policy:Mapping[str,Any])->str|None:
    if reading.get('ok') is not True:return 'gpu_observation_unavailable'
    used=reading.get('memory_used_mib');free=reading.get('memory_free_mib');total=reading.get('memory_total_mib')
    if any(type(x) is not int for x in [used,free,total]) or used<0 or free<0 or total<=0 or used+free!=total:
        return 'gpu_observation_invalid'
    if policy['expected_gpu'] is not None and total!=policy['expected_gpu']['total_mib']:return 'gpu_physical_identity_changed'
    if used-baseline_mib>policy['gpu_max_delta_mib']:return 'shared_gpu_growth_limit'
    if free<policy['gpu_min_free_mib']:return 'shared_gpu_free_floor'
    return None


def wait_group_gone(pgid:int,policy:Mapping[str,Any])->dict:
    deadline=time.monotonic()+policy['cleanup_timeout_seconds'];reaped=[]
    while True:
        while True:
            try:pid,status=os.waitpid(-pgid,os.WNOHANG)
            except ChildProcessError:break
            if not pid:break
            reaped.append(pid)
        observed=group_sample(pgid,maximum=policy['max_members'])
        if not observed['members'] and not observed['zombies']:
            return {'proven_gone':True,'survivors':[],'reaped':reaped}
        if time.monotonic()>=deadline:return {'proven_gone':False,'survivors':observed['members'],'reaped':reaped}
        time.sleep(.05)


def finish_owned_group(pid:int,start_ticks:str,policy:Mapping[str,Any],*,terminate:bool,grace:float):
    """Retain the unreaped direct leader while signalling its original group.

    WNOWAIT pins the actual child's PID until the whole group has been signalled;
    no signal is sent on the strength of a possibly reused integer alone.
    """
    def identity():
        try:
            os.waitid(os.P_PID,pid,os.WEXITED|os.WNOHANG|os.WNOWAIT)
            actual=parse_stat(Path(f'/proc/{pid}/stat').read_text())
        except (OSError,ValueError) as exc:raise ObservationRefused('direct-child identity cannot be proven for cleanup') from exc
        if actual['start_ticks']!=start_ticks or actual['pgid']!=pid:
            raise ObservationRefused('direct-child group identity changed; refusing to signal another group')
    identity()
    observed=group_sample(pid,maximum=policy['max_members'])
    had_descendants=any(x['pid']!=pid for x in observed['members'])
    if terminate:
        os.killpg(pid,signal.SIGTERM);deadline=time.monotonic()+grace
        while time.monotonic()<deadline:
            if not group_sample(pid,maximum=policy['max_members'])['members']:break
            time.sleep(.05)
    identity()
    try:os.killpg(pid,signal.SIGKILL)
    except ProcessLookupError:pass
    _,status,usage=os.wait4(pid,0)
    cleanup=wait_group_gone(pid,policy);cleanup['had_descendants']=had_descendants
    return status,usage,cleanup


def child_start_ticks(pid:int)->str:
    # The caller owns an unreaped direct child; unlike the general liveness helper,
    # a just-exited zombie still supplies the exact reserved PID identity.
    return parse_stat(Path(f'/proc/{pid}/stat').read_text())['start_ticks']
