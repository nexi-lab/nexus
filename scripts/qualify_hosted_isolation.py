"""Opt-in live Docker isolation evidence for one exact reviewed hosted artifact.

Runs its real entrypoint until its first model request, then probes the same
container as its unprivileged user. No model is called and no level is granted.
This is one input to qualification, not an acceptance authority. The closed
attempt marker is retained for normal runtime maintenance.
"""

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
from uuid import uuid4

from nexus.bricks.sandbox import hosted_agent
from nexus.bricks.sandbox.hosted_agent import HostedDockerRunner, HostedRuntimeError
from nexus.contracts.agent_hosted import HostedArtifact

# The probe never returns file contents, environment values, or model inputs.
PROBE = r"""
import errno,json,os,pathlib,socket
checks={}
checks['unprivileged_uid']=os.getuid()==65532 and os.getgid()==65532
status=dict(line.split(':',1) for line in pathlib.Path('/proc/self/status').read_text().splitlines() if ':' in line)
checks['zero_effective_capabilities']=int(status['CapEff'].strip(),16)==0
checks['no_new_privileges']=status['NoNewPrivs'].strip()=='1'
checks['seccomp_filter']=status['Seccomp'].strip()=='2'
try:
    with open('/sudo-qualification-write','x') as f: f.write('synthetic probe')
    checks['root_write_denied']=False
except OSError as e: checks['root_write_denied']=e.errno in (errno.EROFS,errno.EACCES,errno.EPERM)
checks['host_data_absent']=not pathlib.Path('/host').exists()
checks['docker_socket_absent']=not pathlib.Path('/var/run/docker.sock').exists()
checks['no_active_external_interface']=all(name=='lo' or not (int(pathlib.Path('/sys/class/net',name,'flags').read_text().strip(),16)&1) for _,name in socket.if_nameindex())
sock=socket.socket();sock.settimeout(1)
try:
    sock.connect(('192.0.2.1',443));checks['network_bypass_denied']=False
except OSError as e: checks['network_bypass_denied']=e.errno in (errno.ENETUNREACH,errno.EHOSTUNREACH,errno.EPERM,errno.EACCES)
finally: sock.close()
checks['credential_env_absent']=not any(any(x in k.upper() for x in ('TOKEN','SECRET','PASSWORD','API_KEY','CREDENTIAL')) for k in os.environ)
with open('/tmp/sudo-qualification-write','x') as f: f.write('synthetic probe')
os.unlink('/tmp/sudo-qualification-write');checks['bounded_scratch_writable']=True
print(json.dumps(checks))
"""


def config_checks(info):
    host = info["HostConfig"]
    tmp = host.get("Tmpfs") or {}
    return {
        "running_exact_image": info["State"]["Running"],
        "no_network": host["NetworkMode"] == "none",
        "read_only_root": host["ReadonlyRootfs"],
        "not_privileged": not host["Privileged"],
        "all_capabilities_dropped": {c.upper() for c in host["CapDrop"]} == {"ALL"},
        "no_added_capabilities": not host.get("CapAdd"),
        "no_host_binds_or_volumes": not host.get("Binds") and not info["Mounts"],
        "private_pid_ipc": host["PidMode"] != "host" and host["IpcMode"] != "host",
        "bounded_resources": 0 < host["Memory"] <= 512 * 1024 * 1024
        and host["MemorySwap"] == host["Memory"]
        and 0 < host["NanoCpus"] <= 1000000000
        and 0 < host["PidsLimit"] <= 256,
        "bounded_nonexec_scratch": set(tmp) == {"/tmp"}
        and {"size=10m", "noexec", "nosuid", "nodev"}.issubset(tmp["/tmp"].split(",")),
    }


async def qualify(artifact, docker):
    runner = HostedDockerRunner(docker)
    execution_id = uuid4().hex
    report = {
        "kind": "HostedIsolationEvidence",
        "asset_id": artifact.asset_id,
        "artifact_id": artifact.id,
        "metadata_digest": artifact.metadata_digest,
        "spec_digest": artifact.spec_digest,
        "image_id": artifact.image.image_id,
        "runtime_authority_id": await runner.identity(),
        "runtime_code_digest": "sha256:"
        + hashlib.sha256(Path(hosted_agent.__file__).read_bytes()).hexdigest(),
        "probe_digest": "sha256:" + hashlib.sha256(PROBE.encode()).hexdigest(),
        "execution_id": execution_id,
        "checks": {},
        "model_calls": 0,
        "scope": "synthetic text; isolation only, not full L2 acceptance",
    }

    async def trace(_frame):
        pass

    async def model(_input):
        info = await runner.execution_container(artifact.image, execution_id)
        if info is None:
            raise HostedRuntimeError("QUALIFICATION_CONTAINER_MISSING")
        report["checks"].update(config_checks(info))
        probe = json.loads(await runner._command("exec", info["Id"], "python", "-c", PROBE))
        if not probe or any(type(value) is not bool for value in probe.values()):
            raise HostedRuntimeError("QUALIFICATION_PROBE_INVALID")
        report["checks"].update(probe)
        # Stop before the first model request; do not fabricate a model result.
        raise HostedRuntimeError("QUALIFICATION_STOP_BEFORE_MODEL")

    try:
        await runner.run(
            artifact.image,
            "6 * 7",
            model,
            trace,
            execution_id=execution_id,
            timeout_seconds=30,
            max_model_calls=1,
        )
        raise HostedRuntimeError("QUALIFICATION_MODEL_PATH_NOT_REACHED")
    except HostedRuntimeError as error:
        if str(error) != "QUALIFICATION_STOP_BEFORE_MODEL":
            raise
    closed = await runner.execution_container(artifact.image, execution_id)
    report["checks"]["cleanup_fenced"] = (
        closed is not None
        and closed["State"]["Status"] == "created"
        and closed["Config"]["Labels"]["dev.sudo.agent.closed"] == "true"
    )
    report["passed"] = bool(report["checks"]) and all(report["checks"].values())
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--docker", default="/usr/bin/docker")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    artifact = HostedArtifact.model_validate_json(args.artifact.read_bytes())
    # Refuse accidental replacement of prior evidence before performing side effects.
    with args.output.open("x") as output:
        try:
            report = asyncio.run(qualify(artifact, args.docker))
        except Exception as error:
            output.write(json.dumps({"passed": False, "error_type": type(error).__name__}))
            raise
        output.write(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"passed": report["passed"], "checks": report["checks"], "model_calls": 0}))
    raise SystemExit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
