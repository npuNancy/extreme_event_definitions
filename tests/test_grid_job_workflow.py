"""Job-pack determinism and distributed baseline/signals completion contracts."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "infos/scnet_patchify_grid"))
import create_jobs as jobs
import run_job as runner


def config(tmp_path, *extra):
    return jobs.parser().parse_args([
        "--models", "CANESM5", "--scenarios", "ssp126", "--techs", "solar",
        "--patches", "R03C09", "--code-sha", "a" * 40,
        "--jobs-dir", str(tmp_path / "jobs"), *extra])


def test_inventory_and_dependencies(tmp_path):
    manifest, scripts = jobs.build(config(tmp_path))
    assert manifest["counts"] == {"baseline": 1, "signals": 10, "audit": 1}
    baseline, *_, audit = manifest["jobs"]
    assert len(audit["depends_on"]) == 11
    assert all(r["depends_on"] == [baseline["unit_id"]]
               for r in manifest["jobs"] if r["stage"] == "signals")
    assert manifest["jobs"][-2]["years"] == "2060-2060"
    full = jobs.parser().parse_args(["--models", *jobs.MODELS, "--code-sha", "a"*40,
                                     "--jobs-dir", str(tmp_path / "all")])
    m, s = jobs.build(full)
    assert m["counts"] == {"baseline":1128,"signals":11280,"audit":1128}
    assert len(s) == 13536


def test_identical_scripts_syntax_and_roles(tmp_path):
    args = config(tmp_path)
    m1, s1 = jobs.build(args)
    args.jobs_dir = str(tmp_path / "other-account")
    m2, s2 = jobs.build(args)
    assert (m1,s1) == (m2,s2)
    for script in s1.values():
        subprocess.run(["bash", "-n"], input=script, text=True, check=True)
        assert "#SBATCH --output=logs/%x-%j.out" in script
        assert "acp6varuz3" not in script
        assert '/work/home/aclym5felp' not in script
        assert script.count("/work/home/") == 1  # existing shared climate environment
    for row in m1["jobs"]:
        assert row["script_sha256"] == hashlib.sha256(s1[row["script"]].encode()).hexdigest()


def test_dry_run_and_collision_are_non_mutating(tmp_path):
    argv = ["--models","CANESM5","--scenarios","ssp126","--patches","R03C09",
            "--code-sha","a"*40,"--jobs-dir",str(tmp_path/"pack")]
    jobs.main([*argv,"--dry-run"])
    assert not (tmp_path/"pack").exists()
    jobs.main(argv)
    before = {p: p.read_bytes() for p in (tmp_path/"pack").rglob("*") if p.is_file()}
    with pytest.raises(SystemExit):
        jobs.main(argv)
    assert before == {p:p.read_bytes() for p in before}


@pytest.mark.parametrize("extra", [
    ["--models","CANESM5,CANESM5"], ["--baseline-years","2010-2024"],
    ["--signals-cpus","2"], ["--partition","wzhctest\n#SBATCH --exclusive"],
    ["--baseline-time","00:00:00"], ["--years-per-file","0"]])
def test_bad_pack_configuration(tmp_path, extra):
    with pytest.raises(ValueError):
        jobs.build(config(tmp_path,*extra))


def test_symlink_cannot_disguise_v1_as_v2(tmp_path):
    v1 = tmp_path/"v1"; v1.mkdir()
    v2 = tmp_path/"v2"; v2.mkdir()
    (v1/"data.nc").touch()
    (v2/"data.nc").symlink_to(v1/"data.nc")
    with pytest.raises(ValueError,match="escapes"):
        runner.within(v2/"data.nc",v2)


def test_compute_command_uses_local_output_external_baseline(tmp_path,monkeypatch):
    baseline = tmp_path/"worker-a"/"baseline.nc"
    baseline.parent.mkdir(); baseline.touch()
    monkeypatch.setenv("EXTREME_BASELINE_FILE",str(baseline))
    a = SimpleNamespace(stage="signals",model="CANESM5",scenario="ssp126",patch="R03C09",
        tech="solar",analysis_years="2015-2060",baseline_years="2015-2024",tile_shape=(32,32),
        time_chunk=240,complevel=1,processes=4,years="2015-2019",years_per_file=5)
    runtime = {"run_root":tmp_path/"worker-b","bcsd":"input","patch_manifest":"patch.json","land_plan":"land.nc"}
    cmd = runner.compute_command(a,runtime)
    assert cmd[cmd.index("--baseline-file")+1] == str(baseline)
    assert cmd[cmd.index("--output-root")+1] == str(tmp_path/"worker-b/outputs")
    assert cmd[cmd.index("--parts-root")+1] == str(tmp_path/"worker-b/parts")


def test_distributed_audit_and_failed_dependencies(tmp_path,monkeypatch):
    from tests.test_grid_signals import build_grid, args
    from grid_extreme_signals import grid_compute as compute, grid_io as io
    build_grid(tmp_path)
    compute.run(args(tmp_path,"baseline"),"baseline")
    baseline = tmp_path/"out/M/ssp126/P1/solar/baseline_2024-2024.nc"
    runtime = {"run_id":"test","release_sha256":"frozen-release","run_root":tmp_path/"auditor"}
    a = SimpleNamespace(model="M",scenario="ssp126",patch="P1",tech="solar",
        baseline_years="2024-2024",analysis_years="2024-2025",years_per_file=1,
        code_sha=io.complete(baseline)["context"]["baseline_contract"]["implementation"]["code_sha"])
    def row(path,stage,period,job):
        uid = jobs.unit_id(stage,"M","ssp126","solar","P1",period)
        receipt = tmp_path/f"receipt-{job}.json"
        receipt.write_text(json.dumps({"status":"COMPLETED","run_id":"test","unit_id":uid,
            "slurm_job_id":str(job),"code_sha":a.code_sha,"output":str(path),"artifact":io.file_identity(path),
            "input_release_sha256":"frozen-release"}))
        return {"unit_id":uid,"slurm_state":"COMPLETED","exit_code":"0:0",
                "job_id":str(job),"receipt":str(receipt),"output":str(path),"years":period}
    index = {"run_id":"test","model":"M","scenario":"ssp126","patch":"P1","tech":"solar",
             "baseline":row(baseline,"baseline","2024-2024",1),"signals":[]}
    for job,year in enumerate((2024,2025),2):
        period = f"{year}-{year}"
        compute.run(args(tmp_path,"signals",output=f"worker-{job}",years=period,years_per_file=1),"signals")
        path = tmp_path/f"worker-{job}/M/ssp126/P1/solar/signals_{period}.nc"
        index["signals"].append(row(path,"signals",period,job))
    source = tmp_path/"index.json"
    source.write_text(json.dumps(index))
    monkeypatch.setenv("EXTREME_COMBINATION_INDEX",str(source))
    output = runner.audit(a,runtime)
    report = json.loads(output.read_text())
    assert report["time_count"] == 730*8
    assert len(report["signals"]) == 2
    index["signals"][0]["slurm_state"] = "FAILED"
    source.write_text(json.dumps(index))
    with pytest.raises(ValueError,match="not successful"):
        runner.audit(a,runtime)
    index["signals"] = index["signals"][1:]
    source.write_text(json.dumps(index))
    with pytest.raises(ValueError,match="exactly once"):
        runner.audit(a,runtime)
