"""Magasin par sujet (famille « magasin ») : format et droits inchangés (AD-nettoyage-2)."""
import json
import os
import stat

import pytest

from rosetta.addons import _common, quotas, withings


def _ancien_write(data_dir, path, record):
    """Le code d'avant (git show origin/epic/rosetta-qualite), recopié."""
    os.makedirs(os.path.join(data_dir, "users"), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        json.dump(record, f)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


RECORD = {"refresh_token": "r€fresh", "userid": 42, "nested": {"a": [1, 2.5, None]}, "é": "ü"}


@pytest.fixture(params=["quotas", "withings"])
def mod(request, tmp_path, monkeypatch):
    monkeypatch.setenv("ROSETTA_QUOTAS_DATA", str(tmp_path))
    monkeypatch.setenv("ROSETTA_WITHINGS_DATA", str(tmp_path))
    return {"quotas": quotas, "withings": withings}[request.param]


def test_octets_et_droits_identiques_a_l_ancien(mod, tmp_path):
    mod._write_user("Léa", RECORD)
    path = mod._user_file("Léa")
    ref = tmp_path / "ref.json"
    _ancien_write(str(tmp_path), str(ref), RECORD)
    assert open(path, "rb").read() == ref.read_bytes()
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert not os.path.exists(path + ".tmp")


def test_ecrase_un_fichier_existant_en_0600(mod, tmp_path):
    mod._write_user("bob", {"a": 1})
    mod._write_user("bob", {"a": 2})
    assert mod._read_user("bob") == {"a": 2}
    assert stat.S_IMODE(os.stat(mod._user_file("bob")).st_mode) == 0o600


def test_lecture_d_un_fichier_ecrit_par_l_ancien(mod, tmp_path):
    os.makedirs(tmp_path / "users", exist_ok=True)
    _ancien_write(str(tmp_path), mod._user_file("bob"), RECORD)
    assert mod._read_user("bob") == RECORD


def test_defauts_distincts_absent_et_corrompu(tmp_path):
    p = str(tmp_path / "x.json")
    assert _common.read_user(p, {}) == {}
    assert _common.read_user(p) is None
    open(p, "w").write("{pas du json")
    assert _common.read_user(p, {}) == {}
    assert _common.read_user(p, None) is None
    open(p, "w").write("")
    assert _common.read_user(p, "d") == "d"


def test_defauts_par_addon(tmp_path, monkeypatch):
    monkeypatch.setenv("ROSETTA_QUOTAS_DATA", str(tmp_path))
    monkeypatch.setenv("ROSETTA_WITHINGS_DATA", str(tmp_path))
    assert quotas._read_user("nul") == {}
    assert withings._read_user("nul") is None
    os.makedirs(tmp_path / "users")
    open(quotas._user_file("c"), "w").write("{")
    open(withings._user_file("c"), "w").write("{")
    assert quotas._read_user("c") == {}
    assert withings._read_user("c") is None


def test_write_user_cree_le_dossier_parent(tmp_path):
    p = str(tmp_path / "a" / "b" / "u.json")
    _common.write_user(p, {"k": "v"})
    assert json.load(open(p)) == {"k": "v"}


def test_plus_de_os_replace_dans_quotas_withings():
    for m in (quotas, withings):
        assert "os.replace" not in open(m.__file__).read()
