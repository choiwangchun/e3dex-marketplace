"""Transport-independent validation, dispatch and error normalization."""
import importlib.util
import json
from pathlib import Path
import sys
import uuid
from .contract import VERSION, REQUEST_SCHEMA, OPERATIONS, validate
from .errors import BridgeError, InputError

# Core generation runs on the approval server; this package contains no core code.
REMOTE_OPERATIONS = {'e3d_structures_harness', 'e3d_equipment_harness', 'e3d_pipe_harness', 'e3d_plan_export', 'e3d_plan_profile'}

ROOT = Path(__file__).resolve().parents[1]


def load_backend():
    name = '_e3d_session_backend'
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, ROOT / 'components/harness/bridge/multi/sessions.py')
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


def envelope(request=None, *, data=None, error=None):
    request = request if isinstance(request, dict) else {}
    args = request.get('arguments')
    args = args if isinstance(args, dict) else {}
    rid = request.get('request_id')
    return {'schema_version': VERSION,
            'request_id': rid if isinstance(rid, str) and 0 < len(rid) <= 128 else uuid.uuid4().hex,
            'operation': request.get('operation') if isinstance(request.get('operation'), str) else None,
            'session_id': args.get('session_id') if isinstance(args.get('session_id'), str) else None,
            'ok': error is None, 'data': data,
            'error': None if error is None else {'code': error.code, 'message': str(error),
                                                'details': error.details, 'retryable': False}}


def parse_json(raw):
    def reject_constant(value):
        raise ValueError('Invalid JSON constant: ' + value)

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('Duplicate JSON key: ' + key)
            result[key] = value
        return result
    try:
        return json.loads(raw, object_pairs_hook=pairs, parse_constant=reject_constant)
    except (ValueError, UnicodeError) as exc:
        raise InputError('INVALID_JSON', 'Request must be valid UTF-8 JSON: ' + str(exc)) from exc


class Service:
    def __init__(self, backend=None):
        self.backend = backend or load_backend()

    def call(self, request):
        try:
            validate(request, REQUEST_SCHEMA)
            operation = request['operation']
            if operation not in OPERATIONS:
                raise InputError('UNKNOWN_OPERATION', 'Unknown operation: ' + operation)
            args = request['arguments']
            validate(args, OPERATIONS[operation]['inputSchema'], '$.arguments')
            if operation in REMOTE_OPERATIONS:
                from e3d_loader.remote import core_call
                return core_call(request)
            if operation == 'e3d_work_context':
                from .work_context import operation as work_operation
                data = work_operation(args, self)
            elif operation == 'e3d_structures_harness':
                from .structures import operation as structures_operation
                data = structures_operation(args)
            elif operation == 'e3d_equipment_harness':
                from .equipment import operation as equipment_operation
                data = equipment_operation(args)
            elif operation == 'e3d_pipe_harness':
                from .pipe import operation as pipe_operation
                data = pipe_operation(args)
            elif operation == 'e3d_plan_export':
                from .plan import read
                from .plan_export import normalize, read_style_ids
                data = normalize(args['native_file'], args['output_file'],
                                 read(args['profile_file']), read_style_ids(read(args['receipt_file'])))
            elif operation == 'e3d_plan_profile':
                from .plan import operation as plan_operation
                data = plan_operation(args)
            elif operation == 'e3d_setup':
                from .setup import setup
                data = setup(args, self)
            elif operation == 'e3d_list_sessions':
                data = self.backend.list_sessions(**args)
            elif operation == 'e3d_session_context':
                data = self.backend.session_context(args['session_id'])
            else:
                data = self.backend.run_macro(args)
                if data.get('ok') is False:
                    status = {2: 'PML_ERROR', 3: 'CHECK_FAILED', 4: 'NOT_RUN', 5: 'INDETERMINATE'}.get(data.get('exit_code'), 'EXECUTION_FAILED')
                    return envelope(request, data=data, error=BridgeError(status, 'Harness execution: ' + status,
                        {'inspect_report_before_retry': True}))
            return envelope(request, data=data)
        except BridgeError as exc:
            return envelope(request, error=exc)
        except Exception as exc:
            return envelope(request, error=BridgeError('INTERNAL_ERROR', str(exc)))
