"""The Stage memory material generator (scripts/stage_memory_materials.py). Offline, deterministic.

Every output goes under a temporary ``data`` directory passed to the tool, never the real one.
"""

from __future__ import annotations

import base64
import datetime
import hashlib
import importlib.util
import os
import stat
from pathlib import Path

import pytest
from charlie_memory_helpers import ROOT
from cryptography import x509
from cryptography.hazmat.primitives import serialization

from viharbot import service_auth as sa

spec = importlib.util.spec_from_file_location("stage_memory_materials", ROOT / "scripts" / "stage_memory_materials.py")
gen = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gen)


@pytest.fixture
def data_root(tmp_path):
    root = tmp_path / "data"
    root.mkdir()
    return root


@pytest.fixture
def out(data_root):
    return data_root / "stage"


def run(capsys, data_root, *args):
    code = gen.main(list(args), data_root=data_root)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def read(out, name) -> str:
    return (out / name).read_text()


def secrets_in(out) -> list[str]:
    """Every secret the tool wrote, in every encoding it wrote it."""
    values = []
    for name in ("memory.key.pem", "memory.key.b64", "charlie-signing.key",
                 "memory-migrator-password.txt", "memory-service-password.txt"):
        text = read(out, name).strip()
        values.append(text)
        values += [line for line in text.splitlines() if len(line) > 8 and "-----" not in line]
    return values


def test_everything_is_written_with_private_modes_and_nothing_secret_is_printed(capsys, data_root, out):
    code, stdout, stderr = run(capsys, data_root, "--out", str(out))
    assert code == 0
    assert stat.S_IMODE(out.stat().st_mode) == 0o700
    names = sorted(p.name for p in out.iterdir())
    assert names == sorted(gen.MATERIAL_FILES + gen.PASSWORD_FILES)  # and no leftover temporary file
    for path in out.iterdir():
        assert stat.S_IMODE(path.stat().st_mode) == 0o600, path.name
    for text in (stdout, stderr):
        for secret in secrets_in(out):
            assert secret not in text
    assert stderr == ""
    # Only non-secret facts: fingerprint, expiry, names of files and of variables.
    cert = x509.load_pem_x509_certificate((out / "memory.crt.pem").read_bytes())
    fingerprint = hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()
    assert fingerprint in stdout and "expires" in stdout
    for variable in ("VIHARBOT_MEMORY_CA_PEM", "MEMORY_TLS_CERT_PEM", "MEMORY_TLS_KEY_PEM", "VIHARBOT_SIGNING_KEY",
                     "VIHARBOT_KEY_REGISTRY_B64", "MEMORY_DB_MIGRATOR_PASSWORD", "MEMORY_DB_SERVICE_PASSWORD"):
        assert variable in stdout


def test_help_explains_each_variable(capsys):
    with pytest.raises(SystemExit) as exit_info:
        gen.main(["--help"])
    assert exit_info.value.code == 0
    text = capsys.readouterr().out
    for variable in ("VIHARBOT_MEMORY_CA_PEM", "MEMORY_TLS_CERT_PEM", "MEMORY_TLS_KEY_PEM", "VIHARBOT_SIGNING_KEY",
                     "VIHARBOT_KEY_REGISTRY_B64", "MEMORY_DB_MIGRATOR_PASSWORD", "MEMORY_DB_SERVICE_PASSWORD",
                     "--passwords-only", "--force", "--rotate-passwords"):
        assert variable in text


def test_an_output_path_outside_data_is_refused_and_nothing_is_created(capsys, tmp_path, data_root):
    inside_repo_elsewhere = tmp_path / "scripts-like"
    for target in (tmp_path / "elsewhere", inside_repo_elsewhere, data_root, data_root / ".." / "escape",
                   Path("/tmp") / "stage-materials-refused"):
        code, stdout, stderr = run(capsys, data_root, "--out", str(target))
        assert code == 2 and stdout == "" and "refusing" in stderr
        assert not target.resolve().exists() or target.resolve() == data_root.resolve()
    assert list(data_root.iterdir()) == []


def test_a_symlink_that_leads_out_of_data_is_refused(capsys, tmp_path, data_root):
    target = tmp_path / "real"
    target.mkdir()
    (data_root / "link").symlink_to(target, target_is_directory=True)
    code, _, stderr = run(capsys, data_root, "--out", str(data_root / "link"))
    assert code == 2 and "refusing" in stderr and list(target.iterdir()) == []


def test_the_real_repository_rule_refuses_paths_outside_data(capsys):
    for bad in (ROOT / "scripts" / "x", ROOT / "docs" / "stage", ROOT / "stage-materials"):
        code = gen.main(["--out", str(bad)])
        assert code == 2 and not bad.exists()
    assert "refusing" in capsys.readouterr().err
    assert gen.DEFAULT_OUTPUT == ROOT / "data" / "stage-memory"


def test_the_registry_grants_only_the_two_memory_capabilities_to_charlie(capsys, data_root, out):
    run(capsys, data_root, "--out", str(out))
    entries = sa.parse_registry(read(out, "memory-registry.toml"))
    assert list(entries) == ["charlie-stage-1"]
    entry = entries["charlie-stage-1"]
    assert entry.sub == "charlie" and entry.capabilities == {"memory.read", "memory.write"}
    assert base64.b64decode(read(out, "memory-registry.b64")).decode() == read(out, "memory-registry.toml")
    public = read(out, "charlie-signing.pub").strip()
    assert sa.b64url_encode(entry.key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)) == public
    assert read(out, "charlie-signing.key").strip() not in read(out, "memory-registry.toml")


def test_the_custom_key_id_is_used_and_a_bad_one_is_refused(capsys, data_root, out):
    run(capsys, data_root, "--out", str(out), "--kid", "charlie-window-2")
    assert list(sa.parse_registry(read(out, "memory-registry.toml"))) == ["charlie-window-2"]
    with pytest.raises(ValueError):
        gen.build_registry("AAAA", 'bad"kid')


def test_the_signing_key_signs_an_assertion_the_registry_verifies(capsys, data_root, out):
    run(capsys, data_root, "--out", str(out))
    registry = out / "memory-registry.toml"
    authenticator = sa.ServiceAuthenticator(audience="memory", registry=_loaded(registry))

    def token(capability="memory.read"):
        return sa.mint_assertion(
            kid="charlie-stage-1", sub="charlie", audience="memory", capability=capability,
            engagement="e", classification="internal", method="GET",
            target="/memory/agent/charlie/records?limit=5", key_path=str(out / "charlie-signing.key"))

    caller = authenticator.verify(token(), method="GET", raw_path=b"/memory/agent/charlie/records",
                                  query=b"limit=5", body=b"", required_capability="memory.read")
    assert caller.sub == "charlie" and caller.kid == "charlie-stage-1"
    # The environment-variable path the Stage service uses loads the same file's content.
    os.environ["VIHARBOT_SIGNING_KEY"] = read(out, "charlie-signing.key").strip()
    try:
        assert sa.load_signing_key().public_key().public_bytes_raw() == sa.load_signing_key(
            str(out / "charlie-signing.key")).public_key().public_bytes_raw()
    finally:
        del os.environ["VIHARBOT_SIGNING_KEY"]
    with pytest.raises(sa.AuthError):  # a capability the registry does not grant is refused
        authenticator.verify(token("research.run"), method="GET", raw_path=b"/memory/agent/charlie/records",
                             query=b"limit=5", body=b"")


def _loaded(path):
    registry = sa.KeyRegistry(path)
    registry.load_initial()
    return registry


def test_the_certificate_is_one_p256_server_certificate_with_the_requested_names_and_expiry(capsys, data_root, out):
    run(capsys, data_root, "--out", str(out))
    cert = x509.load_pem_x509_certificate((out / "memory.crt.pem").read_bytes())
    sans = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert [n.value for n in sans] == ["memory"]
    assert cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca is False
    assert cert.public_key().curve.name == "secp256r1"
    lifetime = cert.not_valid_after_utc - cert.not_valid_before_utc
    assert datetime.timedelta(days=365) <= lifetime <= datetime.timedelta(days=365, minutes=10)
    assert base64.b64decode(read(out, "memory.crt.b64")) == (out / "memory.crt.pem").read_bytes()
    assert base64.b64decode(read(out, "memory.key.b64")) == (out / "memory.key.pem").read_bytes()
    assert "\n" not in read(out, "memory.crt.b64").strip() and "\n" not in read(out, "memory.key.b64").strip()


def test_extra_names_and_a_custom_validity_are_honored(capsys, data_root, out):
    code, stdout, _ = run(capsys, data_root, "--out", str(out), "--san", "memory.internal,10.0.0.5", "--days", "30")
    assert code == 0
    cert = x509.load_pem_x509_certificate((out / "memory.crt.pem").read_bytes())
    sans = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert [str(n.value) for n in sans] == ["memory", "memory.internal", "10.0.0.5"]
    lifetime = cert.not_valid_after_utc - cert.not_valid_before_utc
    assert datetime.timedelta(days=30) <= lifetime <= datetime.timedelta(days=30, minutes=10)
    assert "memory.internal" in stdout
    for bad_days in ("0", "-3", "99999"):
        code, _, stderr = run(capsys, data_root, "--out", str(data_root / "other"), "--days", bad_days)
        assert code == 2 and "--days" in stderr


def test_the_two_passwords_are_random_distinct_and_long(capsys, data_root, out):
    run(capsys, data_root, "--out", str(out))
    migrator, service = (read(out, n).strip() for n in gen.PASSWORD_FILES)
    assert migrator != service and len(migrator) >= 32 and len(service) >= 32
    assert all(set(p) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_") for p in (migrator, service))
    again = data_root / "again"
    run(capsys, data_root, "--out", str(again))
    assert {migrator, service}.isdisjoint({read(again, n).strip() for n in gen.PASSWORD_FILES})


def snapshot(out):
    return {p.name: p.read_bytes() for p in out.iterdir()}


def test_nothing_is_overwritten_without_force(capsys, data_root, out):
    run(capsys, data_root, "--out", str(out))
    before = snapshot(out)
    code, stdout, stderr = run(capsys, data_root, "--out", str(out))
    assert code == 2 and "already exists" in stderr and "--force" in stderr and stdout == ""
    assert snapshot(out) == before
    code, _, _ = run(capsys, data_root, "--out", str(out), "--rotate-passwords")  # not enough on its own
    assert code == 2 and snapshot(out) == before


def test_force_replaces_certificate_key_and_registry_but_not_the_passwords(capsys, data_root, out):
    run(capsys, data_root, "--out", str(out))
    before = snapshot(out)
    code, stdout, _ = run(capsys, data_root, "--out", str(out), "--force")
    assert code == 0
    after = snapshot(out)
    for name in gen.MATERIAL_FILES:
        assert after[name] != before[name], name
    for name in gen.PASSWORD_FILES:
        assert after[name] == before[name], name
        assert f"{name}: kept unchanged" in stdout
    for path in out.iterdir():
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_force_with_rotate_passwords_replaces_everything(capsys, data_root, out):
    run(capsys, data_root, "--out", str(out))
    before = snapshot(out)
    code, _, _ = run(capsys, data_root, "--out", str(out), "--force", "--rotate-passwords")
    assert code == 0
    after = snapshot(out)
    assert all(after[name] != before[name] for name in before)


def test_force_creates_missing_passwords_when_only_the_material_exists(capsys, data_root, out):
    run(capsys, data_root, "--out", str(out))
    for name in gen.PASSWORD_FILES:
        (out / name).unlink()
    assert run(capsys, data_root, "--out", str(out), "--force")[0] == 0
    assert all((out / name).is_file() for name in gen.PASSWORD_FILES)


def test_passwords_only_changes_just_the_passwords_and_prints_no_certificate_facts(capsys, data_root, out):
    run(capsys, data_root, "--out", str(out))
    before = snapshot(out)
    code, stdout, stderr = run(capsys, data_root, "--out", str(out), "--passwords-only")
    assert code == 0 and stderr == ""
    after = snapshot(out)
    for name in gen.MATERIAL_FILES:
        assert after[name] == before[name], name
    for name in gen.PASSWORD_FILES:
        assert after[name] != before[name], name
        assert stat.S_IMODE((out / name).stat().st_mode) == 0o600
    assert "fingerprint" not in stdout
    for name in gen.PASSWORD_FILES:
        assert after[name].decode().strip() not in stdout
    assert set(after) == set(before)  # no temporary file left behind


def test_passwords_only_works_in_an_empty_directory_and_creates_it_privately(capsys, data_root, out):
    code, _, _ = run(capsys, data_root, "--out", str(out), "--passwords-only")
    assert code == 0 and sorted(p.name for p in out.iterdir()) == sorted(gen.PASSWORD_FILES)
    assert stat.S_IMODE(out.stat().st_mode) == 0o700


def test_a_loose_existing_directory_is_tightened(capsys, data_root, out):
    out.mkdir(mode=0o755)
    os.chmod(out, 0o755)
    run(capsys, data_root, "--out", str(out))
    assert stat.S_IMODE(out.stat().st_mode) == 0o700


def test_the_tool_has_no_secret_flag_and_prints_nothing_when_it_fails(capsys, data_root):
    with pytest.raises(SystemExit):
        gen.main(["--password", "x"], data_root=data_root)
    assert "unrecognized" in capsys.readouterr().err
