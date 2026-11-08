"""One current interim, writer lock and rollback marker for multi-file replace."""
import csv
import json
import os
from pathlib import Path
import shutil
import uuid
from datetime import datetime,timezone
import portalocker
from ..storage.files import read_json,write_json,write_csv,write_jsonl,sha256,digest
from ..storage.paths import safe_output
from .features import INTERIM_FIELDS
from ..storage.relocate import resolve_artifact


def _csv(path):
    if not path.exists():return []
    with path.open(encoding='utf-8-sig',newline='') as stream:return list(csv.DictReader(stream))

def natural_key(row):
    instant=datetime.fromisoformat(row['scheduled_at']).astimezone(timezone.utc).isoformat()
    return (row['study_id'],row['registry_version'],row['segment_id'],row['direction_id'],instant)

def _paths(config):
    root=config.output('interim');root.mkdir(parents=True,exist_ok=True)
    work=config.output('processed/publish');work.mkdir(parents=True,exist_ok=True)
    return root,work

def _recover(root,work):
    marker=root/'.publish_recovery.json'
    if not marker.exists():return
    record=read_json(marker);txn=Path(record['transaction']).resolve()
    if txn.parent!=work.resolve() or not txn.name.startswith('txn_'):
        raise ValueError('Unsafe publish recovery transaction')
    for entry in reversed(record['entries']):
        target=(root/entry['name']).resolve();backup=txn/'backup'/entry['name'];staged=txn/'new'/entry['name']
        safe_output(target,root,())
        if backup.exists():
            if target.exists():
                displaced=txn/'discard'/entry['name'];displaced.parent.mkdir(parents=True,exist_ok=True);os.replace(target,displaced)
            target.parent.mkdir(parents=True,exist_ok=True);os.replace(backup,target)
        elif not entry['existed'] and not staged.exists() and target.exists():
            displaced=txn/'discard'/entry['name'];displaced.parent.mkdir(parents=True,exist_ok=True);os.replace(target,displaced)
    marker.unlink()
    # Retain interrupted transaction evidence outside the checkout.

def _check_generation(root):
    if not (root/'manifest.json').exists():return
    m=read_json(root/'manifest.json')
    for name,checksum in m['table_hashes'].items():
        path=root/name
        if not path.exists() or sha256(path)!=checksum:raise ValueError('INTERIM_GENERATION_HASH_MISMATCH')

def read_current(config):
    """Recover/verify on open, including a restart that has no new snapshot."""
    root,work=_paths(config)
    with portalocker.Lock(root/'.publish.lock',timeout=30):
        _recover(root,work);_check_generation(root)
        return _csv(root/'observations_full.csv')

def publish_rows(config,full,interim,sidecars,*,day,registry,source,audit_files=None,replace_partition=False,replace_file=None):
    """Upsert natural keys, or replace exactly one day/study/registry partition.

    CSV renames are individually atomic. The marker allows rollback after a crash;
    manifest is replaced last. Readers must verify its generation hashes.
    """
    replace_file=replace_file or os.replace
    root,work=_paths(config)
    with portalocker.Lock(root/'.publish.lock',timeout=30):
        _recover(root,work);_check_generation(root)
        if not full or len(full)!=len(interim):raise ValueError('Empty or mismatched publication')
        study=config.study.study_id;partition='|'.join((str(day),study,registry));partition_id=digest(partition)[:16]
        byid={r['observation_id']:r for r in interim}
        if len(byid)!=len(interim):raise ValueError('Duplicate interim IDs')
        new={}
        for row in full:
            row=dict(row,study_id=study)
            if row.get('collection_kind')!='scheduled' or row['local_date']!=str(day) or row['registry_version']!=registry:
                raise ValueError('Publication scope must be scheduled day/registry')
            key=natural_key(row)
            if key in new:raise ValueError('Duplicate publication natural key')
            if row['observation_id'] not in byid:raise ValueError('Table key mismatch')
            new[key]=row
        existing=_csv(root/'observations_full.csv');short=_csv(root/'observations.csv')
        oldshort={r['observation_id']:r for r in short}
        merged={natural_key(r):r for r in existing}
        def belongs(r):return r['local_date']==str(day) and r['study_id']==study and r['registry_version']==registry
        if replace_partition:merged={k:r for k,r in merged.items() if not belongs(r)}
        merged.update(new);ordered=[merged[k] for k in sorted(merged)]
        shorts=[]
        for r in ordered:
            origin=byid.get(r['observation_id']) if natural_key(r) in new else oldshort.get(r['observation_id'])
            if origin is None:raise ValueError('Missing matching interim row')
            shorts.append({f:origin.get(f,'') for f in INTERIM_FIELDS})
        m=read_json(root/'manifest.json') if (root/'manifest.json').exists() else {'schema_version':'interim_current_v1','partitions':{}}
        oldpart=m['partitions'].get(partition,{})
        audit_name=f'audit/{day}/{partition_id}'
        txn=work/('txn_'+uuid.uuid4().hex);stage=txn/'new';stage.mkdir(parents=True)
        day_stage=stage/audit_name
        if not replace_partition and (root/audit_name).exists():shutil.copytree(root/audit_name,day_stage)
        else:day_stage.mkdir(parents=True)
        old_sidecars=[]
        sidepath=day_stage/'sidecar.jsonl'
        if sidepath.exists():old_sidecars=[json.loads(s) for s in sidepath.read_text(encoding='utf-8').splitlines()]
        replaced_ids={r['observation_id'] for r in existing if belongs(r) and natural_key(r) in new}
        kept={r['observation_id']:r for r in old_sidecars if r['observation_id'] not in replaced_ids}
        kept.update({r['observation_id']:r for r in sidecars})
        day_ids={r['observation_id'] for r in ordered if belongs(r)}
        write_jsonl(sidepath,[kept[i] for i in sorted(kept) if i in day_ids])
        source_key=source['source_run_id']
        if Path(source_key).name!=source_key:raise ValueError('Invalid source run ID')
        for name,path in (audit_files or {}).items():
            if Path(name).name!=name:raise ValueError('Invalid audit name')
            dest=day_stage/(source_key+'_'+name);shutil.copy2(path,dest)
        sources={} if replace_partition else dict(oldpart.get('sources',{}))
        for r in new.values():sources[r['scheduled_at']]=source
        # Retain only sources still used by the current day partition.
        used={s['source_run_id'] for s in sources.values()}
        for p in day_stage.iterdir():
            if p.name!='sidecar.jsonl' and not any(p.name.startswith(s+'_') for s in used):
                archive=txn/'superseded_audit'/p.name;archive.parent.mkdir(parents=True,exist_ok=True);os.replace(p,archive)
        fields=list(dict.fromkeys(k for r in ordered for k in r))
        write_csv(stage/'observations_full.csv',[{k:r.get(k,'') for k in fields} for r in ordered],fields)
        write_csv(stage/'observations.csv',shorts,INTERIM_FIELDS)
        assert [r['observation_id'] for r in ordered]==[r['observation_id'] for r in shorts]
        m['generation']=uuid.uuid4().hex;m['rows']=len(ordered)
        m['partitions'][partition]={'date':str(day),'study':study,'registry':registry,'rows':sum(belongs(r) for r in ordered),'audit_path':audit_name,'sources':sources}
        m['table_hashes']={n:sha256(stage/n) for n in ('observations.csv','observations_full.csv')}
        write_json(stage/'manifest.json',m)
        names=[audit_name,'observations.csv','observations_full.csv','manifest.json']
        entries=[{'name':n,'existed':(root/n).exists()} for n in names]
        write_json(root/'.publish_recovery.json',{'transaction':str(txn),'entries':entries})
        try:
            for entry in entries:
                target=root/entry['name'];target.parent.mkdir(parents=True,exist_ok=True)
                if target.exists():
                    backup=txn/'backup'/entry['name'];backup.parent.mkdir(parents=True,exist_ok=True);replace_file(target,backup)
                replace_file(stage/entry['name'],target)
        except Exception:
            _recover(root,work)
            raise
        (root/'.publish_recovery.json').unlink()
        archive_root=config.study.archive_root or config.study.project_files_root/'interim_history'
        archive_root.mkdir(parents=True,exist_ok=True)
        destination=archive_root/txn.name
        safe_output(destination,archive_root,())
        # An archive of the previous generation, not a second current dataset.
        shutil.move(str(txn),str(destination))
        return {'manifest':str(root/'manifest.json'),'rows':len(ordered),'partition_rows':m['partitions'][partition]['rows'],'generation':m['generation']}

def publish_run(config,run,*,replace_partition=False):
    run=resolve_artifact(run,config.study.relocation_file);m=read_json(run/'manifest.json')
    if m['status']!='passed' or not m['validation']['valid']:raise ValueError('Run did not pass invariants')
    tables=m['feature_tables'];original_paths=[Path(tables[k]) for k in ('full','interim','sidecar')]
    full_path,interim_path,sidecar=[resolve_artifact(p,config.study.relocation_file) for p in original_paths]
    for original,path in zip(original_paths,(full_path,interim_path,sidecar)):
        if not path.is_file() or sha256(path)!=tables['sha256'][str(original)]:raise ValueError('Selected publication artifact hash mismatch')
    full=_csv(full_path);short=_csv(interim_path)
    if not full:raise ValueError('No scheduled rows to publish')
    days={r['local_date'] for r in full};registries={r['registry_version'] for r in full}
    if len(days)!=1 or len(registries)!=1:raise ValueError('Publish one day/registry at a time')
    sidecars=[json.loads(s) for s in sidecar.read_text(encoding='utf-8').splitlines() if s]
    source={'source_run_id':m['run_id'],'source_run_path':str(run),'source_fingerprint':m['source_fingerprint'],'policy_sha256':m.get('policy_sha256'),'config_hash':m['config_hash'],'manifest_sha256':sha256(run/'manifest.json'),'methods':{k:m.get(k) for k in ('footprint_method','profile_method','interval_method')},'input_hashes':m['input_hashes']}
    audits={p.name:p for p in (run/'audit').iterdir() if p.is_file()}
    audits['manifest.json']=run/'manifest.json'
    return publish_rows(config,full,short,sidecars,day=next(iter(days)),registry=next(iter(registries)),source=source,audit_files=audits,replace_partition=replace_partition)
