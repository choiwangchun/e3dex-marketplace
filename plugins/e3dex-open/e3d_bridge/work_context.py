"""Read open-session databases/roots and bind an explicit human-selected work target."""
import json
import os
from pathlib import Path
import uuid
from .errors import BridgeError, InputError
from .service import ROOT
from . import target


def data_dir():
    return Path(os.environ.get('E3D_BRIDGE_DATA_DIR',str(ROOT/'components/harness')))/'work-context'


def inspection_macro(project, module):
    project=target.project_name(project)
    kind='SITE' if module=='Design' else 'DEPT'
    return f"""-- Read-only session metadata and root list; never navigate or modify CE.
!wcProject = CURRENT PROJECT
IF (!wcProject.code().upcase() NEQ '{project.upper()}') THEN
  RETURN ERROR 1 'Selected project changed'
ENDIF
!wcSession = CURRENT SESSION
!wcDbs = !wcSession.current()
!wcModes = !wcSession.mode()
IF (!wcDbs.size() NEQ !wcModes.size()) THEN
  RETURN ERROR 1 'Database mode list is inconsistent'
ENDIF
DO !wcIndex INDICES !wcDbs
  IF (!wcIndex GT 500) THEN
    !!E3DHarnessData.append('databases_truncated=true')
    BREAK
  ENDIF
  !wcDb = !wcDbs[!wcIndex]
  !!E3DHarnessData.append('work_database=' & !wcDb.name & '|' & !wcDb.type & '|' & !wcModes[!wcIndex])
ENDDO
VAR !wcRoots COLLECT ALL {kind}
DO !wcIndex INDICES !wcRoots
  IF (!wcIndex GT 500) THEN
    !!E3DHarnessData.append('roots_truncated=true')
    BREAK
  ENDIF
  !wcRoot = object DBREF(!wcRoots[!wcIndex])
  VAR !wcDbName DBNAME OF $!wcRoot
  VAR !wcWrite DBWRITE OF $!wcRoot
  !!E3DHarnessData.append('work_root=' & !wcRoot.name & '|' & !wcRoot.refno.string() & '|' & !wcDbName & '|' & !wcWrite & '|' & !wcRoot.lock.string())
ENDDO
!!E3DHarnessData.append('work_context_inspect=pass')
"""


def operation(args, service):
    sid=args['session_id']
    response=service.call({'operation':'e3d_session_context','arguments':{'session_id':sid}})
    if not response['ok']:
        raise BridgeError(response['error']['code'],response['error']['message'],response['error']['details'])
    context=response['data']['context']
    project=target.project_name(context['project']['name'])
    mdb=context['mdb']['name'].lstrip('/')
    module=context['module']
    if module not in ('Design','Draw'): raise InputError('INVALID_CONTEXT','Design or Draw required')
    if args.get('project',project)!=project or args.get('mdb',mdb).lstrip('/')!=mdb:
        raise InputError('TARGET_CHANGED','Requested project/MDB differs from the selected open session')
    if args['action']=='select':
        required={'project','mdb','work_db','root','root_ref'}
        if required-set(args) or args.get('confirm') is not True:
            raise InputError('SELECTION_REQUIRED','Select project, MDB, work DB and root from inspection, then confirm=true after user choice')
        target.validate({k:args[k] for k in ('session_id','project','mdb','work_db','root','root_ref')}|{'module':module})
    folder=data_dir()/uuid.uuid4().hex
    folder.mkdir(parents=True)
    macro=folder/'inspect.pmlmac'
    macro.write_text(inspection_macro(project,module),encoding='utf-8')
    result=service.call({'operation':'e3d_run_macro','arguments':dict(session_id=sid,project=project,mdb=mdb,
        ref=context['currentElement']['ref'],macro=str(macro),effect='read-only',confirm=True,expect=['work_context_inspect=pass'])})
    if not result['ok']:
        raise BridgeError(result['error']['code'],result['error']['message'],{'result':result,'inspect_before_retry':True})
    path=Path(result['data']['report']['report']).with_suffix('.json')
    report=json.loads(path.read_text(encoding='utf-8-sig'))
    ref=context['currentElement']['ref']
    if report.get('status')!='OK' or report.get('issues') or report['receipt']['START_REF']!=[ref] or report['receipt']['END_REF']!=[ref]:
        raise BridgeError('WORK_CONTEXT_CHECK_FAILED','Read-only inspection did not preserve context',{'report':str(path)})
    fresh=service.call({'operation':'e3d_session_context','arguments':{'session_id':sid}})
    if not fresh['ok']:
        raise BridgeError('TARGET_CHANGED','Session became unavailable after inspection',{'result':fresh})
    after=fresh['data']['context']
    if (after['project']['name'],after['mdb']['name'].lstrip('/'),after['module'],after['currentElement']['ref'])!=(project,mdb,module,ref):
        raise InputError('TARGET_CHANGED','Session context changed during inspection; inspect again')
    databases=[]; roots=[]
    rows=report['receipt']['DATA']
    for line in rows:
        if line.startswith('work_database='):
            name,kind,mode=line.split('=',1)[1].split('|')
            databases.append(dict(name=name,type=kind,mode=mode,eligible=kind==('DESI' if module=='Design' else 'PADD') and mode=='RW'))
        elif line.startswith('work_root='):
            name,reference,db,write,locked=line.split('=',1)[1].split('|')
            roots.append(dict(name=name,ref=reference,work_db=db,type='SITE' if module=='Design' else 'DEPT',
                              writable=write.upper()=='TRUE',unlocked=locked.upper()=='FALSE'))
    dbmap={d['name']:d for d in databases}
    for root in roots: root['eligible']=root['writable'] and root['unlocked'] and dbmap.get(root['work_db'],{}).get('eligible',False)
    info=dict(status='NEEDS_TARGET_SELECTION',session_id=sid,project=project,mdb=mdb,module=module,
        databases=databases,roots=roots,databases_truncated='databases_truncated=true' in rows,
        roots_truncated='roots_truncated=true' in rows,report=str(path),
        next_action='Show the open project/MDB and eligible DB/root choices. Ask where to apply unless the user already specified this exact target. Do not pick the first or only candidate silently.')
    if args['action']=='select':
        matches=[r for r in roots if r['name']==args['root'] and r['ref']==args['root_ref'] and r['work_db']==args['work_db'] and r['eligible']]
        if len(matches)!=1:
            raise InputError('WORK_TARGET_UNAVAILABLE','Selected root/DB is not an eligible writable member of this session',info)
        chosen=target.validate(dict(session_id=sid,project=project,mdb=mdb,module=module,work_db=args['work_db'],root=args['root'],root_ref=args['root_ref']))
        target_file=folder/'target.json'
        target_file.write_text(json.dumps(chosen,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        info.update(status='SELECTED',target=chosen,target_file=str(target_file),
            next_action='Copy target into each job. Reuse only for the user-selected task; prepare again after session/project/MDB changes. This does not change the E3D MDB or save the model.')
    (folder/'result.json').write_text(json.dumps(info,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    return info
