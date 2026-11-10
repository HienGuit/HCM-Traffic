"""Explicit online day workflow and single-process offline ETL queue."""
from concurrent.futures import ProcessPoolExecutor
from datetime import date,datetime,timezone
from pathlib import Path
import threading
import uuid
import portalocker
from .config import ResolvedConfig
from .storage.files import read_json,write_json,digest
from .storage.state import State
from .domain.reference import RegistryPath
from .domain.capture import CapturePlan
from .capture.scheduler import Clock,scheduled_slots,run_schedule
from .capture.budget import BudgetController
from .capture.transport import Transport
from .capture.recovery import reconcile
from .capture.preflight import preflight
from .capture.contract import plan_matches_config
from .progress import Progress
from .processing.publish import publish_run,read_current
from .provenance import source_fingerprint
from .storage.relocate import resolve_artifact

_WORKER_GRAPH=None

def offline_task(record,config,paths,job_key):
    """Runs in a separate process: network_off monkeypatch cannot affect capture."""
    global _WORKER_GRAPH
    from .reference.graph import load_graph
    from .processing.pipeline import process_inventory
    from .processing.inputs import network_off
    with network_off():
        Progress(config.output(f'logs/{config.study.dates[0]}.log'))('Transform started',scheduled_at=record['scheduled_at'])
        if _WORKER_GRAPH is None:_WORKER_GRAPH=load_graph(config.study.graph_cache)
        inventory=config.output('processed/inputs/'+job_key+'.json')
        clean={k:record.get(k) for k in ('scheduled_at','snapshot','snapshot_id','status')}
        if clean.get('snapshot'):
            clean['snapshot_id']=read_json(Path(clean['snapshot'])/'manifest.json')['snapshot_id']
        payload={'collection_kind':'scheduled','snapshots':[clean],'config_hash':config.hash}
        if inventory.exists():
            if read_json(inventory)!=payload:raise ValueError('ETL input identity changed')
        else:write_json(inventory,payload,immutable=True)
        instant=datetime.fromisoformat(record['scheduled_at']).astimezone(timezone.utc).isoformat()
        result=process_inventory(inventory,paths,_WORKER_GRAPH,config,force=False,update_pointer=False,scheduled_scope={instant})
        if result.get('status')=='failed':raise ValueError('Transform invariants failed')
        return result

def existing_result(path):return {'run':path}


class ETLQueue:
    def __init__(self,config,paths,state,progress,executor=None,task=offline_task,publisher=publish_run):
        self.config=config;self.paths=paths;self.state=state;self.progress=progress
        self.pool=executor or ProcessPoolExecutor(max_workers=1)
        self.task=task;self.publisher=publisher;self.futures=[];self.errors=[];self.results=[]
        self.lock=threading.Lock()
        self.enqueued=set()
        self.version=digest([source_fingerprint(config.study.project_root),config.policy.model_dump(mode='json'),[p.model_dump(mode='json') for p in paths]])

    def enqueue(self,record):
        if record['status']=='missed':record=dict(record,snapshot=None,snapshot_id=None)
        elif not record.get('snapshot') or not (Path(record['snapshot'])/'manifest.json').is_file():
            pending=digest([record,self.version])
            self.state.set_job(pending,self.config.study.study_id,self.paths[0].registry_version,record['scheduled_at'],record.get('snapshot_id'),'processing_failed',error='RAW_NOT_FINALIZED')
            self.errors.append('Finalized raw pending: '+record['scheduled_at'])
            self.progress('Processing pending',scheduled_at=record['scheduled_at'],reason='RAW_NOT_FINALIZED');return
        key=digest([self.config.study.study_id,self.paths[0].registry_version,record['scheduled_at'],record.get('snapshot') or 'missed',self.version])
        old=self.state.job(key)
        if old and old.get('result_path'):
            old=dict(old,result_path=str(resolve_artifact(old['result_path'],self.config.study.relocation_file)))
        if key in self.enqueued:return
        self.enqueued.add(key)
        if old and old['status']=='published' and old['result_path'] and Path(old['result_path']).is_dir():
            self.progress('Transform already published',scheduled_at=record['scheduled_at']);return
        args=(key,self.config.study.study_id,self.paths[0].registry_version,record['scheduled_at'],record.get('snapshot'))
        self.state.set_job(*args,'processing')
        self.progress('Transform queued',scheduled_at=record['scheduled_at'])
        try:
            if old and old['status'] in ('processed','publish_failed') and old['result_path'] and Path(old['result_path']).is_dir():
                future=self.pool.submit(existing_result,old['result_path'])
            else:future=self.pool.submit(self.task,record,self.config,self.paths,key)
        except Exception as exc:
            self.state.set_job(*args,'processing_failed',error=type(exc).__name__);self.errors.append(type(exc).__name__);self.progress.failure('Transform enqueue failed',exc);return
        self.futures.append(future)
        def completed(f):
            result=None
            try:
                result=f.result()
                self.state.set_job(*args,'processed',result_path=result['run'])
                self.progress('Transform done',scheduled_at=record['scheduled_at'],run=Path(result['run']).name)
                published=self.publisher(self.config,result['run'])
                self.state.set_job(*args,'published',result_path=result['run'])
                self.results.append(published)
                self.progress('Interim updated',rows=published['rows'],day_rows=published['partition_rows'])
                self.progress('Today',**day_counts(self.config,record['scheduled_at'][:10]))
            except Exception as exc:
                status='publish_failed' if result else 'processing_failed'
                self.state.set_job(*args,status,result_path=result.get('run') if result else None,error=type(exc).__name__)
                self.errors.append(status+': '+type(exc).__name__);self.progress.failure(status,exc)
        future.add_done_callback(completed)

    def finish(self):
        self.pool.shutdown(wait=True)
        return self.errors


def day_counts(config,day):
    rows=[r for r in read_current(config) if r['local_date']==str(day) and r['study_id']==config.study.study_id]
    return {'processed_rows':len(rows),'speed_nonnull':sum(r.get('current_speed_kmh') not in (None,'') for r in rows),'ci_nonnull':sum(r.get('ci') not in (None,'') for r in rows),'weather_complete':sum(r.get('precipitation_mm') not in (None,'') and r.get('weather_code') not in (None,'') for r in rows)}

def _day_config(config,day):
    return ResolvedConfig(config.study.model_copy(update={'dates':[day]}),config.policy,config.sources,config.budget)

def _paths(config):
    if config.study.selected_paths_file is None or config.study.graph_cache is None:raise ValueError('Configure selected_paths_file and graph_cache')
    return [RegistryPath.model_validate(p) for p in read_json(config.study.selected_paths_file)]

def run_day(config,day,*,allow_online=False,clock=None,state=None,budget=None,transport=None,queue_factory=ETLQueue,progress=None):
    day=date.fromisoformat(str(day))
    if config.study.dates and day not in config.study.dates:return {'ready':False,'reasons':['STUDY_DATE_NOT_CONFIGURED'],'network_calls':0}
    configured=config
    config=_day_config(config,day);clock=clock or Clock()
    progress=progress or Progress(config.output(f'logs/{day}.log'),clock,config.study.timezone)
    clock.progress=progress
    paths=_paths(config);plan=CapturePlan.model_validate(read_json(config.study.capture_plan_file))
    # Plan binds the configured base setup, independent of a CLI day override.
    base_config=ResolvedConfig(config.study.model_copy(update={'dates':[]}),config.policy,config.sources,config.budget)
    plan_config=next((c for c in (configured,config,base_config) if plan_matches_config(plan,c)),configured)
    state=state or State(config.output('state/capture.sqlite'))
    with portalocker.Lock(config.output('state/capture.lock'),timeout=0):
        read_current(config)
        check=preflight(plan,plan_config,allow_online,state=state,day=day,clock=clock)
        progress('Preflight',**check)
        if not check['ready']:return {'ready':False,**check}
        progress('Ready',date=day,timezone=config.study.timezone,segment_directions=len(paths),slots=config.study.slots,
            traffic_resources=sum(r.provider=='traffic' for r in plan.resources),weather_resources=sum(r.provider=='weather' for r in plan.resources),
            weather_daily_cap=config.budget.weather_daily_attempt_cap,traffic_daily_cap=config.budget.traffic_daily_attempt_cap,raw=config.output('raw'),interim=config.output('interim'))
        reconcile(state,config)
        queue=queue_factory(config,paths,state,progress)
        transport=transport or Transport(config.sources,clock);budget=budget or BudgetController(state,config.budget,clock)
        current=[];session=uuid.uuid4().hex;interrupted=False
        def on_record(record):
            current.append(record)
            try:
                write_json(config.output(f'state/workflow_{day}_{plan.registry_version}.json'),{'collection_kind':'scheduled','snapshots':current,'config_hash':config.hash})
                queue.enqueue(record)
            except Exception as exc:
                queue.errors.append('ETL queue/state: '+type(exc).__name__)
                progress.failure('ETL queue/state failed; continuing capture',exc)
        try:
            run_schedule(day,plan,config,clock,state,budget,transport,on_record=on_record,progress=progress)
        except KeyboardInterrupt:
            interrupted=True;progress('Interrupted; preserving raw and pending ETL',level='WARNING')
        finally:
            queue.finish();transport.close()
        inventory=config.output(f'raw/{config.study.study_id}/{day}/inventory_{session}.json')
        write_json(inventory,{'collection_kind':'scheduled','snapshots':current,'config_hash':config.hash,'interrupted':interrupted},immutable=True)
        statuses={s:sum(r['status']==s for r in current) for s in ('complete','partial','missed','interrupted')}
        result={'status':'interrupted' if interrupted else 'failed' if queue.errors or statuses['interrupted'] or len(current)!=len(config.study.slots) else 'complete','inventory':str(inventory),'slots':len(current),'raw_status_counts':statuses,**day_counts(config,day),'errors':queue.errors,'interim':str(config.output('interim/observations.csv'))}
        progress('Day finished',**result);return result


def transform_day(config,day,*,queue_factory=ETLQueue,progress=None):
    """Recovery enumerates explicit raw date, never calls any transport."""
    day=date.fromisoformat(str(day));config=_day_config(config,day)
    progress=progress or Progress(config.output(f'logs/{day}.log'),timezone=config.study.timezone)
    paths=_paths(config);state=State(config.output('state/capture.sqlite'))
    with portalocker.Lock(config.output('state/capture.lock'),timeout=0):
        read_current(config)
        records={}
        for manifest in config.output(f'raw/{config.study.study_id}/{day}').glob('*/manifest.json'):
            m=read_json(manifest)
            if m['collection_kind']!='scheduled' or m['registry_version']!=paths[0].registry_version:continue
            instant=datetime.fromisoformat(m['scheduled_at']).astimezone(timezone.utc).isoformat()
            if instant in records:raise ValueError('Multiple finalized raw snapshots for one natural slot')
            records[instant]={'scheduled_at':instant,'snapshot':str(manifest.parent),'status':m['status']}
        with state.connect() as db:
            claimed={r['scheduled']:dict(r) for r in db.execute('SELECT * FROM slots WHERE study=? AND registry=?',(config.study.study_id,paths[0].registry_version))}
        for instant in scheduled_slots(day,config):
            key=instant.isoformat();row=claimed.get(key)
            if key not in records and row and row['status']=='missed':records[key]={'scheduled_at':key,'status':'missed'}
        if not records:return {'status':'failed','errors':['NO_FINALIZED_OR_MISSED_SLOTS'],'network':'off'}
        queue=queue_factory(config,paths,state,progress)
        for key in sorted(records):queue.enqueue(records[key])
        queue.finish()
        result={'status':'failed' if queue.errors else 'complete',**day_counts(config,day),'errors':queue.errors,'network':'off'}
        progress('Offline recovery finished',**result);return result
