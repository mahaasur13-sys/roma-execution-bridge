"""
ROMA Slurm Integration Plugin
Executes jobs on a Slurm cluster via SSH (paramiko) or REST API.
"""

import logging
import os
import re
import tempfile
from pathlib import Path

logger = logging.getLogger("roma.slurm")

SLURM_ENABLED = os.environ.get("SLURM_ENABLED", "false").lower() == "true"
SLURM_HEAD_NODE = os.environ.get("SLURM_HEAD_NODE", "")
SLURM_USER = os.environ.get("SLURM_USER", "root")
SLURM_SSH_KEY = os.environ.get("SLURM_SSH_KEY", "")
SLURM_REST_API = os.environ.get("SLURM_REST_API", "")

# Slurm job ids and derived file names must not contain shell metacharacters
# and must not start with '-' (or any flag-like prefix) so they can never be
# interpreted as CLI flags by squeue/sacct/scancel.
_SLURM_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class SlurmPlugin:
    """Execute jobs on a Slurm cluster via SSH."""

    def __init__(self):
        self.enabled = SLURM_ENABLED
        self.head_node = SLURM_HEAD_NODE
        self.user = SLURM_USER
        self.ssh_key = SLURM_SSH_KEY
        self.rest_api = SLURM_REST_API
        self._ssh_client = None

    def _ensure_ssh(self):
        """Lazy SSH connection."""
        if self._ssh_client is not None:
            return
        try:
            import paramiko

            self._ssh_client = paramiko.SSHClient()
            # Никакого AutoAddPolicy: непроверенный ключ хоста = MITM на кластер.
            # Доверяем только known_hosts (системный + ~/.ssh/known_hosts).
            self._ssh_client.load_system_host_keys()
            user_known_hosts = Path.home() / ".ssh" / "known_hosts"
            if user_known_hosts.exists():
                self._ssh_client.load_host_keys(str(user_known_hosts))
            self._ssh_client.set_missing_host_key_policy(paramiko.RejectPolicy())
            connect_kwargs = {"username": self.user, "timeout": 10}
            if self.ssh_key:
                key_path = Path(self.ssh_key).expanduser()
                if key_path.exists():
                    connect_kwargs["key_filename"] = str(key_path)
            self._ssh_client.connect(self.head_node, **connect_kwargs)
            logger.info("Slurm SSH connected to %s", self.head_node)
        except Exception as e:
            logger.error(
                "Failed to connect to Slurm head node %s: %s", self.head_node, e
            )
            raise RuntimeError(f"Slurm SSH unavailable: {e}")

    def _ssh_exec(self, cmd: str, timeout: int = 30) -> tuple[int, str, str]:
        """Execute command via SSH. Returns (exit_code, stdout, stderr)."""
        self._ensure_ssh()
        _, stdout, stderr = self._ssh_client.exec_command(cmd, timeout=timeout)
        exit_code = stdout.channel.recv_exit_status()
        return exit_code, stdout.read().decode().strip(), stderr.read().decode().strip()

    def _rest_call(
        self, endpoint: str, method: str = "GET", data: dict | None = None
    ) -> dict:
        """Call Slurm REST API if configured."""
        import requests as _r

        url = f"{self.rest_api.rstrip('/')}/{endpoint.lstrip('/')}"
        headers = {"X-SLURM-USER-NAME": self.user, "Content-Type": "application/json"}
        resp = _r.request(method, url, json=data, headers=headers, timeout=10)
        resp.raise_for_status()
        return resp.json()

    def _generate_sbatch_script(self, job: dict) -> str:
        """Generate a Slurm batch script from job parameters."""
        script = job.get("script", "echo 'ROMA job running'; hostname; date")
        gpu_count = job.get("gpu_count", 0)
        memory_mb = job.get("memory", 512)
        time_limit = job.get("time_limit", "01:00:00")
        job_name = f"roma-{job.get('job_id', 'unknown')[:8]}"
        working_dir = job.get("working_dir", "/tmp")

        header = f"""#!/bin/bash
#SBATCH --job-name={job_name}
#SBATCH --output=/tmp/roma-%j.out
#SBATCH --error=/tmp/roma-%j.err
#SBATCH --time={time_limit}
#SBATCH --mem={memory_mb}M
#SBATCH --nodes=1
#SBATCH --ntasks=1
"""
        if gpu_count > 0:
            header += f"#SBATCH --gres=gpu:{gpu_count}\n"

        return header + f"\ncd {working_dir}\n{script}\n"

    def execute(self, job: dict) -> dict:
        """Submit job to Slurm. Returns dict with slurm_job_id."""
        if not self.enabled:
            return {
                "slurm_job_id": None,
                "status": "local_emulation",
                "message": "Slurm disabled — running locally",
            }

        try:
            script = self._generate_sbatch_script(job)
            # Write script to temp file, scp to cluster, and sbatch
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".sh", delete=False, prefix="roma-"
            ) as f:
                f.write(script)
                f.flush()
                script_path = f.name

            sftp = self._ssh_client.open_sftp()
            safe_job_id = (
                re.sub(
                    r"[^A-Za-z0-9._-]", "_", str(job.get("job_id", "tmp"))[:8]
                ).lstrip("._-")
                or "job"
            )
            remote_path = f"/tmp/roma-{safe_job_id}.sh"
            sftp.put(script_path, remote_path)
            sftp.close()

            exit_code, stdout, stderr = self._ssh_exec(f"sbatch {remote_path}")
            os.unlink(script_path)

            if exit_code != 0:
                logger.error("sbatch failed: %s", stderr)
                return {"slurm_job_id": None, "status": "failure", "message": stderr}

            # Parse sbatch output: "Submitted batch job 12345"
            match = re.search(r"Submitted batch job (\d+)", stdout)
            slurm_job_id = match.group(1) if match else None

            logger.info("Slurm job submitted: %s → %s", job.get("job_id"), slurm_job_id)
            return {
                "slurm_job_id": slurm_job_id,
                "status": "submitted",
                "message": stdout,
            }

        except RuntimeError:
            raise
        except Exception as e:
            logger.error("Slurm execute error: %s", e)
            return {"slurm_job_id": None, "status": "failure", "message": str(e)}

    def get_status(self, slurm_job_id: str) -> dict:
        """Query job status from Slurm via sacct or squeue."""
        if not self.enabled:
            return {
                "slurm_job_id": slurm_job_id,
                "status": "unknown",
                "message": "Slurm disabled",
            }
        if not _SLURM_ID_RE.match(str(slurm_job_id)):
            return {
                "slurm_job_id": slurm_job_id,
                "status": "error",
                "message": "invalid slurm_job_id",
            }

        try:
            # Try squeue first (running/pending), then sacct (completed)
            _, stdout, _ = self._ssh_exec(
                f"squeue -j {slurm_job_id} -o '%T' --noheader", timeout=10
            )
            if stdout:
                return {
                    "slurm_job_id": slurm_job_id,
                    "status": stdout.strip().lower(),
                    "source": "squeue",
                }

            _, stdout, _ = self._ssh_exec(
                f"sacct -j {slurm_job_id} -o 'State' --noheader -P", timeout=10
            )
            if stdout:
                status = stdout.strip().split("\n")[0].lower()
                return {
                    "slurm_job_id": slurm_job_id,
                    "status": status,
                    "source": "sacct",
                }

            return {
                "slurm_job_id": slurm_job_id,
                "status": "not_found",
                "source": "none",
            }

        except Exception as e:
            logger.error("Slurm status error for %s: %s", slurm_job_id, e)
            return {"slurm_job_id": slurm_job_id, "status": "error", "message": str(e)}

    def cancel(self, slurm_job_id: str) -> dict:
        """Cancel a Slurm job via scancel."""
        if not self.enabled:
            return {
                "slurm_job_id": slurm_job_id,
                "status": "not_cancelled",
                "message": "Slurm disabled",
            }
        if not _SLURM_ID_RE.match(str(slurm_job_id)):
            return {
                "slurm_job_id": slurm_job_id,
                "status": "error",
                "message": "invalid slurm_job_id",
            }

        try:
            exit_code, stdout, stderr = self._ssh_exec(
                f"scancel {slurm_job_id}", timeout=10
            )
            if exit_code == 0:
                logger.info("Slurm job cancelled: %s", slurm_job_id)
                return {
                    "slurm_job_id": slurm_job_id,
                    "status": "cancelled",
                    "message": stdout,
                }
            return {"slurm_job_id": slurm_job_id, "status": "error", "message": stderr}
        except Exception as e:
            logger.error("Slurm cancel error for %s: %s", slurm_job_id, e)
            return {"slurm_job_id": slurm_job_id, "status": "error", "message": str(e)}

    def close(self):
        if self._ssh_client:
            self._ssh_client.close()
            self._ssh_client = None


# Global instance
slurm = SlurmPlugin()
