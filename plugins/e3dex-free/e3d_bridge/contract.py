"""One operation catalog and request/response schema for every transport."""
import re
from .errors import InputError

VERSION = '1.0'


def obj(properties, required=()):
    return {'type': 'object', 'properties': properties, 'required': list(required), 'additionalProperties': False}


def string(description='', **limits):
    return {'type': 'string', 'description': description, 'minLength': 1, 'maxLength': 32768, **limits}


SESSION = string('Exact session_id from e3d_list_sessions', pattern=r'^(des|draw)-[0-9]+-[0-9]+$')
OPERATIONS = {
    'e3d_work_context': {
        'description': 'Inspect current-session databases and SITE/DEPT roots read-only, then bind a user-selected project/MDB/work DB/root to exact session identity. First show open projects with e3d_list_sessions and ask where to apply unless already specified. Never automatically select the first or only candidate. select requires explicit user choice and confirm=true. Returns target for each modeling job; no global active target, login/MDB switch or SAVEWORK.',
        'inputSchema': obj({
            'action': {'type':'string','enum':['inspect','select']},
            'session_id': SESSION,
            'project': string('Exact project from the selected open session'),
            'mdb': string('Exact MDB from inspection'),
            'work_db': string('Exact writable DESI/PADD database chosen by the user'),
            'root': string('Selected SITE for Design or DEPT for Draw'),
            'root_ref': string('Exact SITE/DEPT REFNO from inspection'),
            'confirm': {'type':'boolean'}},['action','session_id'])},
    'e3d_structures_harness': {
        'description': 'Prepare E3D Structures macros (inspect, settings, create, verify) on the E3DEX server. Read e3d_guide first for the job format and rules, then run the returned macro with e3d_run_macro.',
        'inputSchema': obj({
            'action': {'type':'string','enum':['prepare_inspect','prepare_settings','prepare_create','prepare_verify']},
            'job_file':string('Absolute JSON job file; target from e3d_work_context, exact SITE REFNO and pinned SPEC/profile references')
        },['action','job_file'])},
    'e3d_equipment_harness': {
        'description': 'Prepare E3D equipment macros (inspect, create, verify, unname, rebase, nozzle naming) on the E3DEX server. Read e3d_guide first for the job format and rules, then run the returned macro with e3d_run_macro.',
        'inputSchema': obj({
            'action': {'type':'string','enum':['prepare_inspect','prepare_create','prepare_verify','prepare_unname','prepare_rebase','prepare_name_nozzles']},
            'job_file':string('Absolute JSON job file; target from e3d_work_context and exact SITE REFNO required')
        },['action','job_file'])},
    'e3d_pipe_harness': {
        'description': 'Prepare E3D PIPE macros (inspect, route, repair, network, clash) on the E3DEX server. Read e3d_guide first for the job format and rules, then run the returned macro with e3d_run_macro.',
        'inputSchema': obj({
            'action': {'type':'string','enum':['prepare_inspect','prepare_route','prepare_repair','prepare_network','prepare_clash']},
            'job_file':string('Absolute JSON job path; exact scope name and REFNO required')
        },['action','job_file'])},
    'e3d_plan_export': {
        'description': 'Finish a native PLAN DXF on the E3DEX server (line style normalization). Read e3d_guide first.',
        'inputSchema': obj({
            'native_file':string('Absolute native DXF path'),
            'output_file':string('Different absolute final DXF path'),
            'profile_file':string('Resolved profile JSON from the corresponding prepared job'),
            'receipt_file':string('Harness report.json containing plan_line_style readbacks')
        },['native_file','output_file','profile_file','receipt_file'])},
    'e3d_plan_profile': {
        'description': 'Resolve PLAN drawing profiles and prepare Draw macros on the E3DEX server. Read e3d_guide first for the job format and rules, then run the returned macros with e3d_run_macro.',
        'inputSchema': obj({
            'action': {'type':'string','enum':['resolve','prepare','prepare_update','save_user']},
            'user_profile': string('Explicit named user preferences; do not infer a user identity'),
            'job_file': string('Absolute JSON path: target from e3d_work_context, department, new registry/drawing names, bounds_world_mm, collected items/ref'),
            'current_profile_file': string('Required for prepare_update: latest successfully applied resolved profile. Preserves unmentioned previous customizations.'),
            'overrides': {'type':'array','maxItems':25,'items':obj({
                'layer':string('Exact DXF layer, e.g. EQUI or EQUIP CEN'),
                'color':{'type':'string','enum':['Red','Yellow','Green','Cyan','Blue','Magenta','White']},
                'line_style':{'type':'string','enum':['Solid','Dashed','Chained','DashDotDot','Dotted']}
            },['layer'])}},['action'])},
    'e3d_setup': {
        'description': 'Inspect or perform explicitly requested initial setup on this Windows PC, then verify an exact user-selected project/session. Bundled Python needs no separate install. Never close E3D or save work. apply/verify require confirm=true. READY requires a successful live read-only probe; NEEDS_* states require the stated user action.',
        'inputSchema': obj({
            'action': {'type': 'string', 'enum': ['inspect', 'apply', 'verify']},
            'module': {'type': 'string', 'enum': ['Design', 'Draw', 'Both']},
            'e3d_root': string('Optional exact E3D 2.1 installation directory; required if discovery is ambiguous'),
            'session_id': SESSION,
            'project': string('User-selected project; verify must not switch to another project'),
            'confirm': {'type': 'boolean'},
            'allow_elevation': {'type': 'boolean', 'description': 'Allow Windows UAC consent if installation access is denied; never bypass policy.'}}, ['action'])},
    'e3d_list_sessions': {
        'description': 'List all open local E3D project sessions. Show project, user, module and MDB, then ask where to apply unless the user already specified the target. Never select even a sole candidate silently or choose by port alone.',
        'inputSchema': obj({'project':string('Optional exact project filter; omit to discover all open projects')})},
    'e3d_session_context': {
        'description': 'Read fresh selected-session context after process identity and listener ownership checks.',
        'inputSchema': obj({'session_id': SESSION}, ['session_id'])},
    'e3d_run_macro': {
        'description': 'Run a reviewed PML file in an exact user-selected project/session with context checks and a cross-process lock. effect declares intent, not a sandbox. Never retry INDETERMINATE automatically or SAVEWORK without authorization.',
        'inputSchema': obj({
            'session_id': SESSION, 'macro': string('Existing absolute .pmlmac path'),
            'project': string('Expected exact user-selected project'), 'mdb': string('Expected MDB'),
            'ref': string('Expected current element REFNO'),
            'effect': {'type': 'string', 'enum': ['read-only', 'session', 'model']},
            'confirm': {'type': 'boolean'},
            'expect': {'type': 'array', 'items': string('Expected key=value'), 'maxItems': 100}},
            ['session_id', 'macro', 'project', 'mdb', 'ref', 'effect', 'confirm'])}
}
REQUEST_SCHEMA = obj({
    'operation': string('Operation name'), 'arguments': {'type': 'object'},
    'request_id': string('Correlation only; does not provide idempotency', maxLength=128)},
    ['operation', 'arguments'])
RESPONSE_SCHEMA = obj({
    'schema_version': {'type': 'string', 'const': VERSION},
    'request_id': {'type': 'string'}, 'operation': {'type': ['string', 'null']},
    'session_id': {'type': ['string', 'null']}, 'ok': {'type': 'boolean'},
    'data': {'type': ['object', 'null']},
    'error': {'anyOf': [{'type': 'null'}, obj({
        'code': {'type': 'string'}, 'message': {'type': 'string'},
        'details': {'type': 'object'}, 'retryable': {'type': 'boolean', 'const': False}},
        ['code', 'message', 'details', 'retryable'])]}},
    ['schema_version', 'request_id', 'operation', 'session_id', 'ok', 'data', 'error'])


def validate(value, spec, path='$'):
    """Validate the exact JSON Schema subset used by input contracts."""
    kind = spec.get('type')
    valid = {'object': isinstance(value, dict), 'string': isinstance(value, str),
             'boolean': type(value) is bool, 'array': isinstance(value, list)}
    if kind and not valid.get(kind, False):
        raise InputError('INVALID_REQUEST', f'{path} must be {kind}', {'path': path})
    if 'enum' in spec and value not in spec['enum']:
        raise InputError('INVALID_REQUEST', f'{path} must be one of {spec["enum"]}', {'path': path})
    if kind == 'object':
        properties = spec.get('properties', {})
        missing = [key for key in spec.get('required', []) if key not in value]
        if missing:
            raise InputError('INVALID_REQUEST', f'{path}: missing {", ".join(missing)}', {'path': path})
        if spec.get('additionalProperties') is False:
            unknown = set(value) - set(properties)
            if unknown:
                raise InputError('INVALID_REQUEST', f'{path}: unknown fields {", ".join(sorted(unknown))}', {'path': path})
        for key, child in properties.items():
            if key in value:
                validate(value[key], child, path + '.' + key)
    elif kind == 'string':
        if len(value) < spec.get('minLength', 0) or len(value) > spec.get('maxLength', 32768):
            raise InputError('INVALID_REQUEST', f'{path}: invalid length', {'path': path})
        if 'pattern' in spec and not re.fullmatch(spec['pattern'], value):
            raise InputError('INVALID_REQUEST', f'{path}: invalid format', {'path': path})
    elif kind == 'array':
        if len(value) > spec.get('maxItems', 1000):
            raise InputError('INVALID_REQUEST', f'{path}: too many items', {'path': path})
        for i, item in enumerate(value):
            validate(item, spec['items'], f'{path}[{i}]')


def catalog():
    return {'schema_version': VERSION, 'request_schema': REQUEST_SCHEMA, 'response_schema': RESPONSE_SCHEMA,
            'operations': OPERATIONS}


def tools():
    return [{'name': name, **spec, 'outputSchema': RESPONSE_SCHEMA} for name, spec in OPERATIONS.items()]
