"""Covers Phase 2's engine.py fixes: the eval() RCE is gone, record_bags/
BAG_DIR reach XP_Database correctly through the constructor (not as
post-construction attributes that arrived too late to matter), and
REST_API_TOKEN is actually routed to the handler that already existed for
it in database.py but was never reachable.
"""
from unittest.mock import MagicMock, patch

import pytest


def _make_engine(**kwargs):
    from xparo.engine import Engine
    kwargs.setdefault("connection_type", "offline")
    return Engine("secret", "proj-engine-test", **kwargs)


def test_eval_key_is_not_specially_handled():
    """The old `elif k=="eval": return eval(val)` branch is gone -- an
    "eval" key now falls through to call_message like any other unknown
    key, and critically is never passed to Python's eval().
    """
    engine = _make_engine()
    received = []
    engine.call_message = lambda message, **kwargs: received.append(message)

    # If eval() were still wired up, this would execute os.system and blow
    # up the test run -- the point of the test is that it doesn't.
    engine.on_ws_message('ws', {"eval": "__import__('os').system('true')"})

    assert received == [{"eval": "__import__('os').system('true')"}]


def test_record_bags_true_reaches_orchestrator_construction():
    with patch('xparo.database.BlackboxOrchestrator') as mock_orchestrator_cls, \
         patch('xparo.database.signal.signal'), \
         patch('xparo.database.Thread'):
        engine = _make_engine(record_bags=True, BAG_DIR='/tmp/custom-bag-dir')
        assert engine.local_database.orchestrator is mock_orchestrator_cls.return_value
        # BAG_DIR must be the constructor-supplied one, not the Engine
        # default -- this is the exact bug the constructor-ordering fix
        # closes (BAG_DIR used to only take effect if set *before*
        # XP_Database/BlackboxOrchestrator were constructed).
        mock_orchestrator_cls.assert_called_once()
        assert mock_orchestrator_cls.call_args.args[2] == '/tmp/custom-bag-dir'


def test_record_bags_false_does_not_construct_orchestrator():
    with patch('xparo.database.BlackboxOrchestrator') as mock_orchestrator_cls:
        engine = _make_engine(record_bags=False)
        assert engine.local_database.orchestrator is None
        mock_orchestrator_cls.assert_not_called()


def test_rest_api_token_reaches_dashboard_receive_handler():
    """database.py's dashboard_receive already had a correct REST_API_TOKEN
    handler; on_ws_message's dispatch loop just never routed the key to it.
    """
    engine = _make_engine()
    engine.local_database.orchestrator = MagicMock()

    engine.on_ws_message('ws', {"REST_API_TOKEN": "  tok-123  "})

    assert engine.local_database.orchestrator.API_TOKEN == "tok-123"
    engine.local_database.orchestrator._process_uploads.assert_called_once()


def test_rest_api_token_is_a_noop_without_an_orchestrator():
    engine = _make_engine()
    assert engine.local_database.orchestrator is None
    # Must not raise even though there's nothing to arm.
    engine.on_ws_message('ws', {"REST_API_TOKEN": "tok-123"})


# ------------------------------------------------------------------
# Phase 4: remote_ops.py wiring into on_ws_message's dispatch table.
# remote_ops.py's own tests (test_remote_ops.py) cover the handler bodies
# in isolation; these cover that engine.py actually calls them with the
# right arguments and adapts send_response (dict) <-> private_send (JSON
# string) correctly.
# ------------------------------------------------------------------
def test_default_transport_is_django_ws():
    from xparo.transports.django_ws import DjangoWsTransport
    engine = _make_engine()
    assert isinstance(engine.transport, DjangoWsTransport)


def test_xparo_transport_tethered_tcp_selects_that_transport():
    from xparo.transports.tethered_tcp import TetheredTcpTransport
    # No Django to talk to over this transport (that's the whole reason it
    # exists) -- must not crash XP_Database's construction, which is what
    # this is really testing: getattr(self.transport, 'website_base_url',
    # None) has to tolerate a transport that doesn't define that attribute
    # at all (unlike DjangoWsTransport).
    engine = _make_engine(xparo_transport="tethered_tcp")
    assert isinstance(engine.transport, TetheredTcpTransport)
    assert not hasattr(engine.transport, 'website_base_url')


def test_run_command_dispatches_to_remote_ops(tmp_path):
    engine = _make_engine()
    sent = []
    engine.transport.send = lambda message, command_for=None: sent.append(message)

    engine.on_ws_message('ws', {"RUN_COMMAND": {"command": "echo hi", "request_id": "r1", "timeout": 5}})
    # Runs in its own thread (matches the original -- must never block the
    # dispatch loop) -- give it a moment to finish and reply.
    import time
    for _ in range(50):
        if sent:
            break
        time.sleep(0.05)

    assert len(sent) == 1
    import json
    result = json.loads(sent[0])["COMMAND_RESULT"]
    assert result["request_id"] == "r1"
    assert result["success"] is True
    assert "hi" in result["output"]


def test_run_command_empty_command_replies_immediately_no_thread():
    engine = _make_engine()
    sent = []
    engine.transport.send = lambda message, command_for=None: sent.append(message)

    engine.on_ws_message('ws', {"RUN_COMMAND": {"command": "   ", "request_id": "r2"}})

    import json
    assert len(sent) == 1
    assert json.loads(sent[0])["COMMAND_RESULT"]["output"] == "(empty command)"


def test_teleop_dispatches_to_remote_ops_and_publishes_joy():
    engine = _make_engine()
    joy_calls = []
    engine.joy_publish = lambda axes, buttons: joy_calls.append((axes, buttons))
    sent = []
    engine.transport.send = lambda message, command_for=None: sent.append(message)

    engine.on_ws_message('ws', {"TELEOP": {"axes": [1.0], "buttons": []}})

    assert len(joy_calls) == 1
    axes, buttons = joy_calls[0]
    assert len(axes) == 4 and len(buttons) == 3  # padded, see remote_ops.MIN_JOY_*
    import json
    assert json.loads(sent[0]) == {"TELEOP_ACK": {"success": True}}


def test_list_files_dispatches_against_engine_transfer_dir(tmp_path):
    engine = _make_engine()
    engine.transfer_dir = str(tmp_path)
    engine.file_transfer = __import__('xparo.remote_ops', fromlist=['FileTransferSession']).FileTransferSession(str(tmp_path))
    (tmp_path / "readme.txt").write_text("hi")
    sent = []
    engine.transport.send = lambda message, command_for=None: sent.append(message)

    engine.on_ws_message('ws', {"LIST_FILES": {}})

    import json
    tree = json.loads(sent[0])["FILE_LIST"]["tree"]
    assert tree[0]["name"] == "readme.txt"


def test_delete_file_dispatches_against_engine_transfer_dir(tmp_path):
    engine = _make_engine()
    engine.transfer_dir = str(tmp_path)
    (tmp_path / "doomed.txt").write_text("bye")
    sent = []
    engine.transport.send = lambda message, command_for=None: sent.append(message)

    engine.on_ws_message('ws', {"DELETE_FILE": {"path": "doomed.txt"}})

    import json
    ack = json.loads(sent[0])["DELETE_ACK"]
    assert ack["success"] is True
    assert not (tmp_path / "doomed.txt").exists()


def test_file_transfer_upload_round_trip_through_engine(tmp_path):
    import base64
    import json
    engine = _make_engine()
    engine.transfer_dir = str(tmp_path)
    from xparo import remote_ops
    engine.file_transfer = remote_ops.FileTransferSession(str(tmp_path))
    sent = []
    engine.transport.send = lambda message, command_for=None: sent.append(message)

    engine.on_ws_message('ws', {"FILE_REQ": {"filename": "up.bin", "direction": "upload", "size": 5}})
    engine.on_ws_message('ws', {"FILE_CHUNK": {"data": base64.b64encode(b"hello").decode()}})
    engine.on_ws_message('ws', {"FILE_COMPLETE": {}})

    assert (tmp_path / "up.bin").read_bytes() == b"hello"
    last = json.loads(sent[-1])
    assert last["FILE_COMPLETE"]["received"] == 5


def test_persisted_credential_is_scoped_to_project_id(tmp_path):
    """Found via a real local-testing session: credential.json used to
    store just {"value": ...}, with no notion of which project it was
    issued for -- so a credential persisted while testing against project
    A kept getting silently used (and silently overriding a freshly
    supplied xparo_secret_key) when later pointed at unrelated project B,
    producing a confusing 403 with no indication the new secret was never
    actually tried. A credential must only be reused for the exact
    project_id it was issued under.
    """
    engine = _make_engine()
    engine.xparo_credential_path = str(tmp_path / "credential.json")
    engine.project_id = "project-a"

    engine._persist_credential("secret-for-project-a")
    assert engine._load_persisted_credential() == "secret-for-project-a"

    engine.project_id = "project-b"
    assert engine._load_persisted_credential() is None

    engine.project_id = "project-a"
    assert engine._load_persisted_credential() == "secret-for-project-a"


def test_persist_credential_writes_the_project_id_alongside_the_value(tmp_path):
    import json as json_module
    engine = _make_engine()
    engine.xparo_credential_path = str(tmp_path / "credential.json")
    engine.project_id = "proj-engine-test"

    engine._persist_credential("some-secret")

    with open(engine.xparo_credential_path) as f:
        stored = json_module.load(f)
    assert stored == {"value": "some-secret", "project_id": "proj-engine-test"}
