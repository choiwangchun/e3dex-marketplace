"""Explicit, per-job project/DB/root selection. Never a process-global active target."""
from contextvars import ContextVar
from functools import wraps
import json
import re
from .errors import InputError

_TARGET = ContextVar('e3d_work_target', default=None)
PREFIX = '-- E3D_WORK_TARGET='
LEGACY = dict(project='AYF', mdb=None, work_db='PIPETEAM/PIPE', root='/AYF-SPARE',
              root_ref=None, module='Design', session_id=None)


def fail(message):
    raise InputError('INVALID_WORK_TARGET', message)


def token(value, pattern, label):
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        fail('Invalid '+label)
    return value


def project_name(value):
    return token(value, r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', 'project name')


def db_name(value):
    value=token(value, r'[A-Za-z0-9_][A-Za-z0-9_./-]{0,127}', 'work DB name')
    if '//' in value or '..' in value: fail('Invalid work DB name')
    return value


def validate(value):
    keys={'session_id','project','mdb','work_db','root','root_ref','module'}
    if not isinstance(value,dict) or set(value)!=keys:
        fail('target requires exact session_id, project, mdb, work_db, root, root_ref and module')
    result=dict(value)
    token(value['session_id'],r'(?:des|draw)-[0-9]+-[0-9]+','session ID')
    project_name(value['project'])
    result['mdb']=token(value['mdb'],r'/?[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}','MDB').lstrip('/')
    db_name(value['work_db'])
    token(value['root'],r'/[A-Za-z0-9_./:#()-]{1,49}','SITE/DEPT name')
    if '//' in value['root'] or '..' in value['root']: fail('Invalid root name')
    token(value['root_ref'],r'=\d+/\d+','root REFNO')
    if value['module'] not in ('Design','Draw'): fail('Design or Draw module required')
    if not value['session_id'].startswith('des-' if value['module']=='Design' else 'draw-'):
        fail('Session ID and module disagree')
    return result


def current():
    return _TARGET.get() or LEGACY


def explicit():
    return _TARGET.get() is not None


def header():
    return PREFIX+json.dumps(current(),ensure_ascii=True,separators=(',',':'))+'\n' if explicit() else ''


def check_macro_target(path, args):
    """Bind generated macros to the selected session even when DB REF numbers coincide."""
    first=path.read_text(encoding='utf-8-sig').splitlines()[0:1]
    if not first or not first[0].startswith(PREFIX): return
    try: target=validate(json.loads(first[0][len(PREFIX):]))
    except (ValueError,TypeError) as exc: fail('Invalid macro work-target header: '+str(exc))
    for key in ('session_id','project','mdb'):
        actual=args.get(key)
        if key=='mdb' and isinstance(actual,str): actual=actual.lstrip('/')
        if actual!=target[key]:
            raise InputError('TARGET_CHANGED','Prepared macro belongs to a different '+key+'; select and prepare again')


def scoped_job(fn):
    """Thread-safe target context for nested pure compilers; legacy jobs stay AYF-bound."""
    @wraps(fn)
    def wrapped(job,*args,**kwargs):
        if not isinstance(job,dict): return fn(job,*args,**kwargs)
        selected=job.get('target')
        if selected is None and isinstance(job.get('before'),dict): selected=job['before'].get('target')
        previous=_TARGET.get()
        reset=None
        if selected is not None:
            selected=validate(selected)
            if previous is not None and previous!=selected: fail('Nested jobs cannot change work target')
            reset=_TARGET.set(selected)
        clean={k:v for k,v in job.items() if k!='target'}
        try:
            result=fn(clean,*args,**kwargs)
            if explicit() and isinstance(result,tuple) and len(result)==2 and isinstance(result[0],str) and isinstance(result[1],dict):
                text,details=result
                if previous is None: text=header()+text
                details=dict(details,target=dict(current()))
                if isinstance(details.get('updated_verify_job'),dict):
                    details['updated_verify_job']['target']=dict(current())
                return text,details
            if explicit() and isinstance(result,dict) and result.get('status')=='PREPARED_NOT_EXECUTED':
                result['target']=dict(current())
            return result
        finally:
            if reset is not None: _TARGET.reset(reset)
    return wrapped


def require_module(module):
    if explicit() and current()['module']!=module: fail('This job requires a '+module+' target')


def child_zone(zone, max_child=None):
    require_module('Design')
    suffix=zone.removeprefix(current()['root']+'/')
    if suffix==zone or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]*',suffix):
        fail('New ZONE must be directly under selected SITE namespace')
    if max_child and len(suffix)>max_child: fail('ZONE suffix exceeds supported length')
    return zone


def session_guard(prefix):
    t=current()
    code=f"""!{prefix}Project = CURRENT PROJECT
IF (!{prefix}Project.code().upcase() NEQ '{t['project'].upper()}') THEN
  RETURN ERROR 1 'Selected project changed'
ENDIF
"""
    if t['mdb']:
        code+=f"""!{prefix}Session = CURRENT SESSION
!{prefix}Mdb = !{prefix}Session.mdb().name
IF (!{prefix}Mdb NEQ '{t['mdb']}' AND !{prefix}Mdb NEQ '/{t['mdb']}') THEN
  RETURN ERROR 1 'Selected MDB changed'
ENDIF
"""
    return code


def root_guard(prefix, variable, root_ref=None, module='Design', writable=False):
    require_module(module)
    t=current()
    expected=root_ref or t['root_ref']
    token(expected,r'=\d+/\d+','root REFNO')
    if explicit() and expected!=t['root_ref']: fail('site_ref differs from selected root_ref')
    kind='SITE' if module=='Design' else 'DEPT'
    code=session_guard(prefix)+f"""!{variable} = object DBREF('{t['root']}')
IF (!{variable}.type NEQ '{kind}' OR !{variable}.refno.string() NEQ '{expected}') THEN
  RETURN ERROR 1 'Selected root identity changed'
ENDIF
VAR !{prefix}DB DBNAME OF $!{variable}
IF (!{prefix}DB NEQ '{t['work_db']}') THEN
  RETURN ERROR 1 'Selected work database changed'
ENDIF
"""
    if writable:
        code+=f"""VAR !{prefix}Write DBWRITE OF $!{variable}
IF (!{prefix}Write.upcase() NEQ 'TRUE' OR !{variable}.lock) THEN
  RETURN ERROR 1 'Selected root is read-only or locked'
ENDIF
"""
    return code


def owner_guard(prefix, variable, root_variable):
    kind='SITE' if current()['module']=='Design' else 'DEPT'
    return f"""!{prefix}Ancestor = !{variable}
DO
  IF (!{prefix}Ancestor.type EQ '{kind}') THEN
    BREAK
  ENDIF
  IF (!{prefix}Ancestor.type EQ 'WORL') THEN
    RETURN ERROR 1 'Object is outside selected root'
  ENDIF
  !{prefix}Ancestor = !{prefix}Ancestor.owner
ENDDO
IF (!{prefix}Ancestor NEQ !{root_variable}) THEN
  RETURN ERROR 1 'Object belongs to another selected root'
ENDIF
"""
