"""Memory service security matrix, part 3 (Part of #40): nothing sensitive reaches an application log.

Runs the real service, the real client and the real Stage launcher through success AND error paths
while capturing every log record (formatted line and every raw attribute, including exception text)
plus stdout and stderr, then searches for: memory content, bearer tokens, private signing keys,
private TLS keys, CA text and database URLs or passwords. A static scan of the logging calls backs
the dynamic scan. Offline; sentinels are built at run time so no secret-looking literal is in source.
"""

from __future__ import annotations

import ast
import base64
import datetime as dt
import json
import logging
import re
import secrets
from pathlib import Path

import pytest
from charlie_memory_helpers import FakeMemoryHttp, FakeRepo, charlie, cleanup_module, memory_env, serve
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from memory_helpers import agent_path, build_world, create_body, new_key

from viharbot import charlie_memory
from viharbot import memory_client as mc
from viharbot.memory_service import stage_entry as se
from viharbot.memory_service.repository import MemoryStoreError
from viharbot.observability import JsonFormatter

SRC = Path(__file__).resolve().parents[1] / "src" / "viharbot"
NAMED = ("memory", "memory_audit", "memory_store", "memory_policy", "memory_client", "charlie_memory",
         "outbound_auth", "service_auth", "specialist", "uvicorn", "uvicorn.error", "uvicorn.access")


def sentinel(label: str) -> str:
    return "-".join(("LOGSCAN", label.upper(), secrets.token_hex(6)))


class Everything(logging.Handler):
    """Keeps each record's formatted line, raw attributes and formatted exception text."""

    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.setFormatter(JsonFormatter())
        self.parts: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        if record.name.startswith(("httpx", "httpcore", "urllib3", "asyncio")):
            return  # the TEST client's own logging, not the application's
        text = [self.format(record), json.dumps(record.__dict__, default=str), record.getMessage()]
        if record.exc_info:
            text.append("".join(logging.Formatter().formatException(record.exc_info)))
        self.parts.extend(text)

    def dump(self) -> str:
        return "\n".join(self.parts)


@pytest.fixture
def logs():
    handler = Everything()
    names = ("",) + NAMED
    saved = []
    for name in names:
        logger = logging.getLogger(name)
        saved.append((logger, logger.level))
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
    yield handler
    for logger, level in saved:
        logger.removeHandler(handler)
        logger.setLevel(level)
    cleanup_module()


def pem_body(pem: str) -> str:
    return re.sub(r"-----[^-]+-----|\s", "", pem)


def ec_pair():
    key = ec.generate_private_key(ec.SECP256R1())
    now = dt.datetime.now(dt.timezone.utc)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "memory")])
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - dt.timedelta(minutes=5))
            .not_valid_after(now + dt.timedelta(days=30))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("memory")]), critical=False)
            .sign(key, hashes.SHA256()))
    return (cert.public_bytes(serialization.Encoding.PEM).decode(),
            key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                              serialization.NoEncryption()).decode())


# --- the service: success and error paths ---------------------------------------------------------------


def test_service_logs_carry_no_content_token_or_signing_key_on_any_path(tmp_path, logs):
    content = sentinel("content")
    world = build_world(tmp_path, FakeRepo(), extra_subs=("charlie", "mallory"))
    # success paths
    created = world.call("charlie", "POST", agent_path("charlie"), create_body(content))
    assert created.status_code == 201
    memory_id = created.json()["memory_id"]
    body = create_body(content)
    assert world.call("charlie", "POST", agent_path("charlie"), body).status_code == 201
    assert world.call("charlie", "POST", agent_path("charlie"), body).status_code in (200, 201)  # replay
    assert content in world.call("charlie", "GET", agent_path("charlie", memory_id)).text
    assert world.call("charlie", "GET", agent_path("charlie") + "?limit=5").status_code == 200
    # error paths, each carrying the sentinel in a different place
    world.call("charlie", "POST", agent_path("charlie"), {**create_body(content), "owner": content})  # 422
    world.call("charlie", "POST", agent_path("charlie"), create_body(content + "x" * 20000))  # too big
    world.call("charlie", "POST", agent_path("charlie"), create_body(content, classification="restricted"))
    world.call("charlie", "POST", agent_path("nova"), create_body(content))  # cross-agent, 403
    world.call("mallory", "POST", agent_path("charlie"), create_body(content))  # unnamed principal
    world.call("charlie", "GET", agent_path("charlie") + f"?agent={content}")  # bad query
    world.call("charlie", "POST", agent_path("charlie"), raw_body=b'{"content": "' + content.encode() + b'"')  # bad JSON
    world.call("charlie", "GET", "/memory/agent/charlie/records/not-a-uuid")  # bad id in path
    world.call(None, "POST", agent_path("charlie"), create_body(content), token=content)  # garbage token
    world.call("charlie", "POST", agent_path("charlie"), create_body(content), claim_sub="nova")  # forged sub
    replayed = world.tokens[0]
    world.call(None, "POST", agent_path("charlie"), create_body(content), token=replayed)  # token replay
    dump = logs.dump()
    assert dump, "the capture saw nothing: the test would prove nothing"
    assert content not in dump
    for token in world.tokens:
        assert token not in dump and token.split(".")[2] not in dump
    for seed in world.secrets_seen:
        assert seed not in dump
    assert "ASSERTION" not in dump.upper().replace("X-VIHARBOT-ASSERTION", "")  # no header dump either


def test_the_authentication_log_reflects_a_caller_chosen_path_but_bounded_and_without_the_query(tmp_path, logs):
    """Recorded gap (LOW): service_auth logs the raw request path before the memory service parses
    it, so caller-chosen text appears in the log. Bounded to 200 characters, the query string is
    cut, and the JSON formatter escapes it. Real memory content is never in a path or query."""
    probe = sentinel("path")
    world = build_world(tmp_path, FakeRepo(), extra_subs=("charlie",))
    world.call("charlie", "GET", f"/memory/agent/charlie/records/{probe}?agent={probe}")
    world.call("charlie", "GET", "/memory/agent/charlie/records/" + "A" * 400)
    targets = []
    for part in logs.parts:
        if part.startswith("{") and '"service authentication decision"' in part and '"target"' in part:
            targets.append(json.loads(part).get("target"))
    assert any(probe in (t or "") for t in targets)  # reflected
    assert all(len(t or "") <= 200 for t in targets)  # bounded
    assert all("?" not in (t or "") for t in targets)  # query cut (and so the second probe is absent)
    assert logs.dump().count(probe) >= 1


def test_a_repository_that_raises_with_content_in_its_message_is_never_echoed_or_logged(tmp_path, logs):
    content = sentinel("exc")
    dsn_password = sentinel("dbpw")
    dsn = f"postgresql://memory_service:{dsn_password}@db.example.invalid:25060/memorydb?sslmode=verify-full"

    class Boom(FakeRepo):
        def create(self, new, ctx):
            raise RuntimeError(f"{content} {dsn}")

        def list(self, *a, **k):
            raise ValueError(f"{content} {dsn}")

    world = build_world(tmp_path, Boom(), extra_subs=("charlie",))
    created = world.call("charlie", "POST", agent_path("charlie"), create_body(content))
    listed = world.call("charlie", "GET", agent_path("charlie"))
    assert created.status_code == 500 and listed.status_code == 500
    for response in (created, listed):
        assert content not in response.text and dsn_password not in response.text
    dump = logs.dump()
    assert "unexpected memory service error" in dump  # it WAS logged, by class only
    assert content not in dump and dsn_password not in dump and "postgresql://" not in dump
    assert "RuntimeError" in dump and "ValueError" in dump


@pytest.mark.parametrize("make", [
    lambda text: MemoryStoreError(text),  # an adapter that (wrongly) puts content in the code
])
def test_a_store_error_whose_code_carries_content_is_not_logged_or_echoed(tmp_path, logs, make):
    content = sentinel("code")

    class Leaky(FakeRepo):
        def list(self, *a, **k):
            raise make(content)

    world = build_world(tmp_path, Leaky(), extra_subs=("charlie",))
    response = world.call("charlie", "GET", agent_path("charlie"))
    assert response.status_code >= 400
    assert content not in response.text
    assert content not in logs.dump()


# --- the client: success and error paths --------------------------------------------------------------------


def test_client_and_charlie_logs_carry_no_content_token_or_url_secret(tmp_path, logs, monkeypatch):
    marker = sentinel("remote")
    userinfo = sentinel("userinfo")
    body_with_marker = json.dumps({"kind": "memory_record_list", "not_instructions": True, "records": [],
                                   "echo": marker}).encode()
    cases = [
        (200, body_with_marker),
        (200, b'{"records": "' + marker.encode() + b'"}'),
        (200, b"not json " + marker.encode()),
        (500, marker.encode()),
        (403, marker.encode()),
        (307, marker.encode()),
    ]
    for index, (status, body) in enumerate(cases):
        with FakeMemoryHttp(status=status, body=body, headers={"Location": f"http://127.0.0.1:1/{marker}"}
                            if status == 307 else None) as server:
            world = build_world(tmp_path / f"c{index}", FakeRepo(), extra_subs=("charlie",)) \
                if (tmp_path / f"c{index}").mkdir() is None else None
            kid, key_file = world.keys["charlie"]
            monkeypatch.setenv("VIHARBOT_OUTBOUND_AUTH", "on")
            monkeypatch.setenv("VIHARBOT_SIGNING_KID", kid)
            monkeypatch.setenv("VIHARBOT_SIGNING_KEY_FILE", str(key_file))
            monkeypatch.setenv("VIHARBOT_SERVICE_SUBJECT", "charlie")
            client = mc.client_from_environment({mc.URL_ENV: server.url, mc.LOCAL_DEV_ENV: "on"})
            assert charlie_memory.prior_block(client, f"req-{index}", "synthesis") == ""
            result = type("R", (), {"confidence": "high", "key_points": [marker], "caveats": [marker]})()
            charlie_memory.store(client, {"research": {"claim": marker}}, result, f"req-{index}", "synthesis")
            sent = "\n".join(s.headers.get("X-Viharbot-Assertion", "") for s in server.seen)
            assert sent and sent not in logs.dump()  # a real token WAS sent, and is not in any log
    # a URL carrying credentials is refused without echoing them
    assert charlie_memory.load_client("synthesis", {mc.URL_ENV: f"https://user:{userinfo}@memory:8443"}) is None
    dump = logs.dump()
    assert marker not in dump and userinfo not in dump
    assert "bad_url" in dump


def test_what_charlie_stores_never_contains_the_question_or_any_response_text(monkeypatch, tmp_path, logs):
    question = sentinel("question")
    claim = sentinel("claim")
    repo = FakeRepo()
    world = build_world(tmp_path, repo, extra_subs=("charlie",))
    with serve(world.client.app) as url:
        env = memory_env(url, str(world.keys["charlie"][1]), kid=world.keys["charlie"][0])
        seen = charlie(monkeypatch, tmp_path, env)
        import time

        response = seen.ask(question, {"research": {"claim": claim}})
        deadline = time.time() + 5
        while not repo.records and time.time() < deadline:
            time.sleep(0.05)
    assert response.status_code == 200 and repo.records
    stored = json.dumps([r.content for r in repo.records])
    assert question not in stored and claim not in stored
    assert set(json.loads(repo.records[0].content)) == set(charlie_memory.STORED_KEYS)
    assert question not in logs.dump() and claim not in logs.dump()


# --- the Stage launcher: private keys, CA text, database URLs --------------------------------------------------


def test_launcher_output_has_no_private_key_ca_text_registry_or_database_url(capsys, logs):
    cert, key = ec_pair()
    ca, _ = ec_pair()
    password = sentinel("dbpass")
    raw_public = new_key()[1]
    registry = (f'[[keys]]\nkid = "k0"\nsub = "charlie"\npublic_key = "{raw_public}"\n'
                'capabilities = ["memory.read", "memory.write"]\n')
    b64 = lambda text: base64.b64encode(text.encode()).decode()  # noqa: E731
    env = {se.ROLE_ENV: "serve", se.CERT_ENV: b64(cert), se.KEY_ENV: b64(key), se.REGISTRY_ENV: b64(registry),
           se.DB_CA_ENV: b64(ca), "MEMORY_DB_HOST": "db.example.invalid", "MEMORY_DB_NAME": "memorydb",
           "MEMORY_DB_USER": "memory_service", "MEMORY_DB_PASSWORD": password, "PORT": "8443"}
    seen_conninfo = []

    def factory(app_env, conninfo):
        seen_conninfo.append(conninfo)
        return object()

    assert se.main(env, server=lambda *a: None, app_factory=factory) == 0
    assert password in seen_conninfo[0]  # the secret exists where it must, only in the connection string
    for name in (se.CERT_ENV, se.KEY_ENV, se.REGISTRY_ENV, se.DB_CA_ENV, "MEMORY_DB_PASSWORD", "MEMORY_DB_HOST"):
        broken = {**env, name: ("zz" + env[name][2:]) if name != "MEMORY_DB_HOST" else ""}
        se.main(broken, server=lambda *a: None, app_factory=factory)
    captured = capsys.readouterr()
    text = captured.out + captured.err + logs.dump()
    for secret in (password, pem_body(key)[:40], pem_body(cert)[:40], pem_body(ca)[:40], b64(key)[:30],
                   b64(ca)[:30], b64(registry)[:30], raw_public):
        assert secret not in text
    for fragment in ("BEGIN", "PRIVATE KEY", "postgresql://", "sslrootcert", "password=", "host=db."):
        assert fragment not in text, fragment


def test_a_ca_or_key_pasted_into_the_wrong_variable_is_not_echoed(capsys):
    cert, key = ec_pair()
    b64 = lambda text: base64.b64encode(text.encode()).decode()  # noqa: E731
    with pytest.raises(mc.MemoryClientError) as caught:
        mc.client_from_environment({mc.URL_ENV: "https://memory:8443", mc.CA_PEM_ENV: b64(key)})  # key as the CA
    assert pem_body(key)[:30] not in str(caught.value) and caught.value.reason == "bad_ca_pem"
    assert se.main({se.ROLE_ENV: "serve", se.CERT_ENV: b64(key), se.KEY_ENV: b64(cert)},
                   server=lambda *a: None) == se.EXIT_REFUSED
    captured = capsys.readouterr()
    assert pem_body(key)[:30] not in captured.err + captured.out


# --- static scan of every logging and printing call on the memory path --------------------------------------------

SCANNED = [*sorted((SRC / "memory_service").glob("*.py")), SRC / "memory_client.py", SRC / "charlie_memory.py",
           SRC / "outbound_auth.py", SRC / "service_auth.py"]
LOG_METHODS = {"debug", "info", "warning", "error", "critical", "exception", "log"}
FORBIDDEN_EXTRA_KEYS = re.compile(
    r"(content|token|password|passwd|secret|private|pem|body|prompt|dsn|conninfo|url|text|assertion|"
    r"authorization|seed|signing_key)", re.I)


def _calls(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            yield node


def test_logging_calls_use_constant_messages_and_never_exception_text_or_tracebacks():
    offenders = []
    for path in SCANNED:
        for call in _calls(path):
            func = call.func
            is_log = (isinstance(func, ast.Attribute) and func.attr in LOG_METHODS
                      and isinstance(func.value, ast.Name) and "log" in func.value.id.lower())
            if not is_log:
                continue
            if func.attr in ("exception",) or any(k.arg == "exc_info" for k in call.keywords):
                offenders.append(f"{path.name}:{call.lineno} traceback")
            first = call.args[0] if call.args else None
            if not (isinstance(first, ast.Constant) and isinstance(first.value, str)):
                offenders.append(f"{path.name}:{call.lineno} non-constant message")
            if len(call.args) > 1:
                offenders.append(f"{path.name}:{call.lineno} %-style arguments")
            for keyword in call.keywords:
                if keyword.arg == "extra" and isinstance(keyword.value, ast.Dict):
                    for key in keyword.value.keys:
                        if isinstance(key, ast.Constant) and FORBIDDEN_EXTRA_KEYS.search(str(key.value)):
                            offenders.append(f"{path.name}:{call.lineno} extra key {key.value}")
    assert offenders == []


def test_print_calls_in_the_memory_path_are_fixed_text_class_names_or_json_only():
    allowed = {"stage_entry.py": 3, "migrate.py": 2}
    if (SRC / "memory_service" / "stage_bootstrap.py").exists():  # present once the bootstrap job has landed
        allowed["stage_bootstrap.py"] = 1  # one `_log` print: step name, outcome, SQLSTATE, fixed flags
    found: dict[str, list[int]] = {}
    for path in SCANNED:
        for call in _calls(path):
            if isinstance(call.func, ast.Name) and call.func.id == "print":
                found.setdefault(path.name, []).append(call.lineno)
    assert {name: len(lines) for name, lines in found.items()} == allowed
    source = (SRC / "memory_service" / "stage_entry.py").read_text()
    assert "print(f\"memory-stage: refusing to start: {error}\"" in source  # StageConfigError text only
    assert "class StageConfigError" in source
    migrate = (SRC / "memory_service" / "migrate.py").read_text()
    assert "type(exc).__name__" in migrate  # the class, not the driver message


def test_the_only_exception_text_that_is_logged_is_policy_config_text_built_from_code_constants():
    source = (SRC / "memory_service" / "policy.py").read_text()
    for call in _calls(SRC / "memory_service" / "policy.py"):
        if isinstance(call.func, ast.Name) and call.func.id == "PolicyConfigError" and call.args:
            arg = call.args[0]
            if isinstance(arg, ast.JoinedStr):  # f-string: only the table's own key names, never values
                names = {v.value.id for v in arg.values if isinstance(v, ast.FormattedValue)
                         and isinstance(v.value, ast.Name)}
                assert names <= {"key", "low", "high"}, ast.dump(arg)
    assert 'extra={"agent": "memory", "reason": str(exc)}' in source


def test_the_stage_bootstrap_job_prints_only_step_outcome_sqlstate_and_fixed_fields(capsys, logs):
    bootstrap = pytest.importorskip("viharbot.memory_service.stage_bootstrap")
    admin_pw, svc_pw, mig_pw = sentinel("adminpw"), sentinel("svcpw"), sentinel("migpw")
    leak = sentinel("driver")
    env = {"MEMORY_ADMIN_DB_HOST": "127.0.0.1", "MEMORY_ADMIN_DB_NAME": "memorydb",
           "MEMORY_ADMIN_DB_USER": "doadmin", "MEMORY_ADMIN_DB_PASSWORD": admin_pw,
           "MEMORY_DB_MIGRATOR_PASSWORD": mig_pw, "MEMORY_DB_SERVICE_PASSWORD": svc_pw}

    class Session:
        def __init__(self, fail_on=None):
            self.fail_on = fail_on

        def execute(self, sql):
            if self.fail_on and self.fail_on in sql:
                raise bootstrap.DbError("42501")
            return [(1,)] if sql.startswith("SELECT 1") else [(bootstrap.SERVICE_ROLE,)]

        def encrypt_password(self, role, password):
            return None

        def close(self):
            pass

    class Gateway:
        def __init__(self, conninfo, fail_on=None, connect_fails=False):
            self.fail_on, self.connect_fails = fail_on, connect_fails

        def connect(self, *, user=None, password=None):
            if self.connect_fails:
                raise bootstrap.DbError("28P01")
            return Session(self.fail_on)

    def boom(conninfo):
        raise RuntimeError(f"{leak} {conninfo}")  # a driver-style message carrying connection details

    runs = [
        dict(gateway_factory=lambda c: Gateway(c), migrate_fn=lambda c: ["0001_init.sql"]),
        dict(gateway_factory=lambda c: Gateway(c, connect_fails=True), migrate_fn=lambda c: []),
        dict(gateway_factory=lambda c: Gateway(c, fail_on="GRANT CONNECT"), migrate_fn=lambda c: []),
        dict(gateway_factory=lambda c: Gateway(c), migrate_fn=boom),
    ]
    codes = [bootstrap.run_bootstrap(env, **kwargs) for kwargs in runs]
    assert codes == [0, 1, 1, 1]
    captured = capsys.readouterr()
    text = captured.out + captured.err + logs.dump()
    for secret in (admin_pw, svc_pw, mig_pw, leak, "postgresql://", "password=", "host=", "127.0.0.1"):
        assert secret not in text, secret
    allowed = {"timestamp", "level", "agent", "event", "step", "outcome", "sqlstate", "applied",
               "untested_on_digitalocean", "database_tls", "error_class"}
    lines = [json.loads(line) for line in captured.out.splitlines()]
    assert lines and all(set(line) <= allowed for line in lines)
    assert {line.get("error_class") for line in lines} - {None} == {"RuntimeError"}
