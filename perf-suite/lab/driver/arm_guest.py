"""Trusted guest-side operations called only over the pinned QEMU SSH connection."""
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import tarfile
import time


BENCH = "/home/frappe/frappe-bench"
SITE = "bench-loadtest.localhost"
SERVICES = ("backend", "scheduler", "queue", "websocket", "frontend")
APP = BENCH + "/apps/khitmee_ops"
CONFIG = Path("/srv/khitmee-reference/perf-suite/docker")


def require(test, message):
    if not test:
        raise ValueError(message)


def command(*argv, data=None, timeout=120):
    p = subprocess.run(list(map(str, argv)), input=data, stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE, timeout=timeout)
    if p.returncode:
        # Docker and Compose errors can include rendered credentials.
        raise RuntimeError(f"{argv[0]} {argv[1]} failed with status {p.returncode}")
    return p.stdout


def inspect(name):
    return json.loads(command("docker", "inspect", name))[0]


def current_target():
    mem = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())
    return dict(hostname=socket.gethostname(), virtualization=command("systemd-detect-virt").decode().strip(),
                root_source=command("findmnt", "-no", "SOURCE", "/").decode().strip(),
                boot_id=Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
                machine_id_sha256=hashlib.sha256(Path("/etc/machine-id").read_bytes()).hexdigest(),
                vcpus=len(os.sched_getaffinity(0)), memory_bytes=int(mem["MemTotal"].split()[0])*1024,
                swap_bytes=int(mem["SwapTotal"].split()[0])*1024)


def availability(project):
    data = command("docker", "ps", "--format", '{{.Label "com.docker.compose.project"}}').decode()
    return sorted(set(data.splitlines()) - {project})


def readiness(project):
    backend = project + "-backend-1"
    info = inspect(backend)
    require(info["Config"]["Labels"]["com.docker.compose.project"] == project, "wrong backend")
    seed = json.loads(command("docker", "exec", backend, "cat", f"{BENCH}/sites/{SITE}/private/files/seed_manifest.json"))
    # Inspect installed source without importing an application module or authenticating.
    probe = """from pathlib import Path
import ast, json
base = Path('/home/frappe/frappe-bench/apps/khitmee_ops/khitmee_ops')
def functions(name):
    p=base/(name+'.py')
    return [n.name for n in ast.parse(p.read_bytes()).body if isinstance(n, ast.FunctionDef)] if p.exists() else []
print(json.dumps({k: functions(k) for k in ('api/vouchers','profit_summary/export_service','api/profit_export')}))
"""
    methods = json.loads(command("docker", "exec", "-i", backend, BENCH+"/env/bin/python", "-IB", "-", data=probe.encode()))
    return dict(project=project, target=current_target(), site=seed["site_name"], fixture_scale=seed["scale"],
                http_port=18086,
                catalog_items=seed["item_summary"]["count"], report_window=seed["report_window"],
                app_methods={name: names for name,names in methods.items() if name != "api/vouchers"},
                voucher_methods=[s for s in methods["api/vouchers"] if s.startswith("submit_")],
                full_year_export=all(k in methods["profit_summary/export_service"] for k in
                                     ("enqueue_profit_csv_export","download_profit_csv_export")))


def db_counts(project):
    sites = Path("/var/lib/docker/volumes") / (project+"-sites") / "_data"
    db = json.loads((sites/SITE/"site_config.json").read_bytes())["db_name"]
    require(re.fullmatch("[_a-zA-Z0-9]+",db), "unexpected site database name")
    tables = ("Sales Invoice","Stock Entry","GL Entry","Stock Ledger Entry","Payment Ledger Entry","Khitmee Idempotency Log")
    query = "SELECT " + ",".join(f"(SELECT COUNT(*) FROM `tab{t}`)" for t in tables)
    result = command("docker","exec",project+"-db-1","sh","-c",
                     'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mariadb -uroot -NB "$1" -e "$2"',
                     "counts",db,query)
    return dict(zip(tables,map(int,result.split()),strict=True))


def release_volumes(project, state):
    """Validate the entire remaining set before reclaiming this disposable arm's bytes."""
    expected = {key: f"{project}-{key}" for key in ("db", "sites", "redis-queue")}
    require(state["volumes"] == expected, "unexpected volume set during cleanup")
    existing = set(command("docker", "volume", "ls", "-q").decode().splitlines())
    remaining = [name for name in expected.values() if name in existing]
    for name in remaining:
        volume = json.loads(command("docker", "volume", "inspect", name))[0]
        labels = volume.get("Labels") or {}
        require(labels.get("khitmee.lab.owner") == "khitmee-lab-adapter" and
                labels.get("khitmee.lab.namespace") == project, "refusing to release foreign volume")
        users = command("docker", "ps", "-aq", "--filter", "volume=" + name).strip()
        require(not users, "volume still has a container user")
    for name in remaining:
        command("docker", "volume", "rm", name)
    return remaining


def deploy(state, project, directory, message):
    require(not availability(project), "another guest project is running; exclusive window required")
    require(not command("docker","ps","-aq","--filter",f"label=com.docker.compose.project={project}").strip(),
            "existing containers for arm")
    source = base64.b64decode(message["source"], validate=True)
    require(hashlib.sha256(source).hexdigest()==message["source_sha256"], "application transfer digest mismatch")
    app = directory/"app"
    app.mkdir(mode=0o755)
    with tarfile.open(fileobj=io.BytesIO(source)) as archive:
        for member in archive:
            parts = Path(member.name).parts
            require(parts and "/" not in parts and ".." not in parts and parts[:2]==("apps","khitmee_ops")
                    or member.isdir() and parts==("apps",), "invalid source path")
            path=app.joinpath(*parts[2:])
            if member.isdir():
                path.mkdir(parents=True,exist_ok=True)
            else:
                require(member.isfile(), "source archive contains nonregular file")
                path.parent.mkdir(parents=True,exist_ok=True)
                with path.open("xb") as out:
                    out.write(archive.extractfile(member).read())
                path.chmod(0o755 if member.mode & 0o111 else 0o644)
    for folder in [app,*[p for p in app.rglob("*") if p.is_dir()]]:
        folder.chmod(0o755)
    volumes={}
    for logical,vol in state["volumes"].items():
        require(vol==project+"-"+logical,"unexpected volume")
        entry=json.loads(command("docker","volume","inspect",vol))[0]
        labels=entry["Labels"]
        require(labels.get("khitmee.lab.namespace")==project and labels.get("khitmee.lab.owner")=="khitmee-lab-adapter",
                "volume lacks adapter ownership")
        key={"db":"perf-db-data","sites":"perf-sites","redis-queue":"perf-redis-queue-data"}[logical]
        volumes[key]={"external":True,"name":vol}
        if logical=="sites":
            root=Path(entry["Mountpoint"])
            require(root==Path("/var/lib/docker/volumes")/vol/"_data", "unexpected mount root")
            os.chown(root,1000,0)
            root.chmod(0o775)
            link=root/"assets"
            if not link.is_symlink():
                require(not link.exists(),"unexpected existing asset directory")
                link.symlink_to(BENCH+"/assets")
            require(os.readlink(link)==BENCH+"/assets","unexpected asset target")
    compose=["docker","compose","--project-name",project,"--project-directory",str(CONFIG),
             "--env-file",str(directory/"benchmark.env")]
    for name in ("compose.base.yaml","profile-2gb.yaml","khitmee-ops-2gb.yaml"):
        compose.extend(["-f",str(CONFIG/name)])
    config = json.loads(command(*compose,"config","--format","json"))
    db_command=config["services"]["db"]["command"]
    require(db_command.count("--performance-schema=OFF")==1,"unexpected DB instrumentation baseline")
    db_command[db_command.index("--performance-schema=OFF")]="--performance-schema=ON"
    backend_command=list(config["services"]["backend"]["command"])
    memory_profile=message.get("memory_profile")
    if memory_profile=="pr6-realized-2gb":
        require("--innodb-buffer-pool-size=448M" in db_command,"unexpected pre-PR6 DB memory baseline")
        db_command[db_command.index("--innodb-buffer-pool-size=448M")]="--innodb-buffer-pool-size=320M"
        require("--threads=2" in backend_command and "--workers=2" in backend_command,"unexpected pre-PR6 backend worker baseline")
        backend_command[backend_command.index("--threads=2")]="--threads=4"
        backend_command[backend_command.index("--workers=2")]="--workers=1"
    elif memory_profile:
        raise ValueError("unsupported memory profile")
    services={s:{"volumes":[{"type":"bind","source":str(app),"target":APP,"read_only":True}]}
              for s in (*SERVICES,"configurator")}
    services["db"]={"command":db_command}
    services["backend"]["command"]=backend_command
    override=directory/"driver-compose.json"
    override.write_text(json.dumps({"volumes":volumes,"services":services}))
    compose+=["-f",str(override)]
    model=json.loads(command(*compose,"config","--format","json"))
    for svc in model["services"].values():
        for mount in svc.get("volumes",[]):
            if mount["type"]=="bind":
                require(mount.get("read_only") and (mount["source"]==str(app) or Path(mount["source"]).parent==CONFIG),
                        "unexpected host bind")
            else:
                require(mount["type"]=="volume" and mount["source"] in volumes,"unexpected data volume")
    (directory/"driver-compose.argv").write_text(json.dumps(compose))
    command(*compose,"up","-d","--pull","never",timeout=240)
    for i in range(60):
        ping=subprocess.run(["curl","-fsS","--max-time","2","-H","Host: "+SITE,
                             "http://127.0.0.1:18086/api/method/ping"],capture_output=True)
        if ping.returncode==0 and json.loads(ping.stdout).get("message")=="pong":
            break
        time.sleep(1)
    else:
        raise RuntimeError("deployed site ping failed")
    instances={}
    for s in SERVICES:
        info=inspect(f"{project}-{s}-1")
        mounts=[m for m in info["Mounts"] if m["Destination"]==APP]
        require(len(mounts)==1 and mounts[0]["Source"]==str(app) and not mounts[0]["RW"],
                "deployed service app mount mismatch")
        instances[s]=dict(id=info["Id"], image=info["Image"],restart=info["RestartCount"])
    return {"source_sha256":message["source_sha256"],"instances":instances,"http_ping":"pong"}


def main(message):
    os.umask(0o077)
    os.environ["DOCKER_HOST"]="unix:///var/run/docker.sock"
    os.environ.pop("DOCKER_CONTEXT",None)
    current=current_target()
    require(current["hostname"]=="khitmee-lab" and current["virtualization"]=="kvm" and current["vcpus"]==2
            and current["swap_bytes"]==0 and command("docker","info","--format","{{.Name}}").strip()==b"khitmee-lab",
            "dedicated guest identity mismatch")
    if message["op"]=="readiness":
        print(json.dumps(readiness("khitmee-refcheck")))
        return
    project=message["namespace"]
    require(re.fullmatch(r"khitmee-arm-[0-9a-f]{16}",project),"invalid arm")
    require(all(current[k]==message["target"][k] for k in current),"guest identity changed")
    directory=Path("/var/lib/khitmee-lab-arms")/project
    state=json.loads((directory/"state.json").read_bytes())
    require(state["namespace"]==project and state["fixture_sha256"]==message["fixture_sha256"]
            and state["restored_from_immutable_fixture"] is True, "invalid restore marker")
    action=message["op"]
    if action=="deploy":
        result=deploy(state,project,directory,message)
    elif action=="cleanup":
        argv=directory/"driver-compose.argv"
        if argv.exists():
            compose=json.loads(argv.read_bytes())
            command(*compose,"stop","--timeout","3",*SERVICES,timeout=45)
            command(*compose,"down","--timeout","30",timeout=90)
        result={"stopped":True, "released_volumes":release_volumes(project,state)}
    elif action=="counts":
        result={"documents":db_counts(project), "services":{}}
        for s in (*SERVICES,"db","redis-cache","redis-queue"):
            i=inspect(project+"-"+s+"-1")
            result["services"][s]=[i["Id"],i["State"]["StartedAt"],i["RestartCount"],i["State"]["OOMKilled"]]
    elif action=="capabilities":
        result=readiness(project)
    else:
        raise ValueError("unexpected guest operation")
    print(json.dumps(result))
