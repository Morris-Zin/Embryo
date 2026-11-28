"""Collect the report screen inside the isolated arm; source is bundled by the external driver."""
import threading


def resource_snapshot():
    with open("/proc/stat") as f:
        ticks = list(map(int, f.readline().split()[1:9]))
    mem = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        key, value = line.split(":", 1)
        if key in ("MemTotal", "MemAvailable"):
            mem[key] = int(value.split()[0]) * 1024
    disks = Path("/sys/class/block/vda/stat").read_text().split()
    return {"cpu_ticks":sum(ticks)-ticks[3]-ticks[4], "memory_bytes":mem["MemTotal"]-mem["MemAvailable"],
            "disk_bytes":(int(disks[2])+int(disks[6]))*512}


def screen_once(message, probe, implementation):
    project=message["namespace"]
    current=current_target()
    require(all(current[k]==message["target"][k] for k in current),"guest identity changed")
    require(re.fullmatch(r"khitmee-arm-[0-9a-f]{16}",project) and not availability(project),"exclusive arm required")
    require((Path("/var/lib/khitmee-lab-arms")/project/"state.json").is_file(),"missing arm state")
    guest_probe.PROJECT=project
    samples=[]
    stop=threading.Event()
    def sample():
        while not stop.wait(0.1):
            samples.append(resource_snapshot()["memory_bytes"])
    thread=threading.Thread(target=sample,daemon=True)
    start=resource_snapshot()
    clock=time.monotonic()
    thread.start()
    try:
        measured=guest_probe.collect(implementation,probe)
    finally:
        stop.set()
        thread.join()
    end=resource_snapshot()
    print(json.dumps({"observation":measured,"target":current,
                      "wall_seconds":time.monotonic()-clock,"resource_start":start,"resource_end":end,
                      "memory_samples":samples,"ticks_per_second":os.sysconf("SC_CLK_TCK")},allow_nan=False))
