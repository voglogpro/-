"""Linux Docker proof for the narrowly privileged rollback promotion step.

This is intentionally separate from unittest discovery.  GitHub Actions runs
it on an ephemeral Linux host with a real Docker daemon.  Volumes are uniquely
named and deliberately not removed, matching the production no-delete rule;
the disposable CI runner destroys them after the job.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import secrets
import subprocess

from scripts.sqlite_volume_rollback import PROMOTE_CODE, _common_docker_run


IMAGE = (
    "python:3.12.13-slim-bookworm@sha256:"
    "d50fb7611f86d04a3b0471b46d7557818d88983fc3136726336b2a4c657aa30b"
)
UID = 65532
GID = 65532
SETUP_CODE = """
from pathlib import Path
root=Path('/target/restored')
(root/'proof_photos').mkdir(parents=True)
(root/'bibitasks.db').write_bytes(b'sqlite-fixture')
(root/'proof_photos'/'proof.jpg').write_bytes(b'jpeg-fixture')
""".strip()
VERIFY_CODE = """
import json,os,stat
from pathlib import Path
root=Path('/target')
paths=[root,*root.rglob('*')]
result={
 'uid':os.getuid(),'gid':os.getgid(),
 'owners':all(p.stat().st_uid==os.getuid() and p.stat().st_gid==os.getgid() for p in paths),
 'modes':all(stat.S_IMODE(p.stat().st_mode)==(0o700 if p.is_dir() else 0o600) for p in paths),
 'content':(root/'proof_photos'/'proof.jpg').read_bytes()==b'jpeg-fixture',
}
print(json.dumps(result))
""".strip()


def command(args, *, check=True):
    result = subprocess.run(args, capture_output=True, text=True, timeout=180, check=False)
    if check and result.returncode != 0:
        raise RuntimeError(
            f"Docker integration command failed ({result.returncode}): "
            f"{' '.join(result.stderr.strip().split())[:500]}"
        )
    return result


def docker_common():
    values = _common_docker_run()
    values[0] = "/usr/bin/docker"
    return values


def fresh_volume(suffix):
    name = f"bibitasks_rollback_ci_{suffix}_{secrets.token_hex(8)}"
    command([
        "/usr/bin/docker", "volume", "create", "--label",
        "com.bibitasks.rollback.integration=true", name,
    ])
    return name


def stage(volume):
    command([
        *docker_common(), "--user", "0:0", "--mount",
        f"type=volume,src={volume},dst=/target",
        IMAGE, "python", "-c", SETUP_CODE,
    ])


def promote(volume, *, chown_cap):
    values = [*docker_common()]
    if chown_cap:
        values.extend(["--cap-add", "CHOWN"])
    values.extend([
        "--user", "0:0", "--env", f"TARGET_UID={UID}",
        "--env", f"TARGET_GID={GID}", "--mount",
        f"type=volume,src={volume},dst=/target",
        IMAGE, "python", "-c", PROMOTE_CODE,
    ])
    return command(values, check=False)


def main():
    if os.name != "posix" or not Path("/usr/bin/docker").is_file():
        raise SystemExit("Linux /usr/bin/docker is required")
    command(["/usr/bin/docker", "pull", IMAGE])

    denied_volume = fresh_volume("denied")
    stage(denied_volume)
    denied = promote(denied_volume, chown_cap=False)
    if denied.returncode == 0:
        raise AssertionError("promotion unexpectedly succeeded without CAP_CHOWN")

    allowed_volume = fresh_volume("allowed")
    stage(allowed_volume)
    allowed = promote(allowed_volume, chown_cap=True)
    if allowed.returncode != 0:
        raise AssertionError(
            "promotion with only CAP_CHOWN failed: "
            + " ".join(allowed.stderr.strip().split())[:500]
        )
    verified = command([
        *docker_common(), "--user", f"{UID}:{GID}", "--mount",
        f"type=volume,src={allowed_volume},dst=/target,readonly",
        IMAGE, "python", "-c", VERIFY_CODE,
    ])
    result = json.loads(verified.stdout)
    if result != {
        "uid": UID, "gid": GID, "owners": True, "modes": True, "content": True,
    }:
        raise AssertionError(f"unprivileged volume verification failed: {result}")
    print(json.dumps({
        "ok": True,
        "negative_without_cap_chown": True,
        "positive_with_only_cap_chown": True,
        "unprivileged_read": True,
    }, sort_keys=True))


if __name__ == "__main__":
    main()
