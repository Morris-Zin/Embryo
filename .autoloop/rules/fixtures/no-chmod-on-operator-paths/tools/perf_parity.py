import os


def prepare_sidecar_oracle_cache_dir(host_cache):
    os.chmod(host_cache, 0o777)
