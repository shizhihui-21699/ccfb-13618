"""沙箱命令构建：把声明式隔离 profile 翻译成 nsjail 命令行。

对应 architecture.md §6 的 L1/L2。L3（gVisor/Firecracker）由部署层
（K8s RuntimeClass）提供，不在本模块内。
"""
from __future__ import annotations

import os
import shlex
from dataclasses import dataclass, field

# 渲染进程只允许拿到这些环境变量，其余全部清空
ENV_ALLOWLIST = ("PATH", "LANG", "LC_ALL", "TESSDATA_PREFIX")

# seccomp 默认拒绝策略文件由镜像内固定路径提供（部署时构建，哈希校验）
SECCOMP_POLICY = "/etc/rebuild/seccomp/renderer.seccomp"

# 精简 rootfs：distroless 基础 + 渲染器，不含 shell/包管理器
DEFAULT_ROOTFS = "/var/lib/rebuild/rootfs"


@dataclass(frozen=True)
class SandboxProfile:
    job_id: str
    input_path: str           # 编排层预取到 spool 的单个输入文件（只读挂载）
    output_dir: str           # 宿主上的 tmpfs 目录，挂进沙箱可写
    worker_module: str        # 沙箱内执行的 worker，如 rebuild.workers.pdf_render
    cpu_seconds: int = 120
    memory_bytes: int = 2 * 1024**3
    max_procs: int = 256
    max_fds: int = 1024
    max_output_bytes: int = 4 * 1024**3
    extra_args: tuple[str, ...] = field(default_factory=tuple)


def build_nsjail_argv(profile: SandboxProfile) -> list[str]:
    """构造一次性（mode once）nsjail 调用。

    安全要点：
    - 网络命名空间独立且不接 veth，沙箱内无网络可用；
    - 输入目录只 bind 当前 job 的一个文件，其他租户路径在 mount 层面不可见；
    - no_new_privs + 自定义 seccomp 策略；
    - 子进程以非特权 uid 运行，-I 让 Python 忽略 PYTHONPATH/cwd 注入。
    """
    jail_id = f"rebuild-{profile.job_id}"
    argv = [
        "nsjail",
        "--mode", "once",
        "--time_limit", str(profile.cpu_seconds),
        "--rlimit_as", str(profile.memory_bytes),
        "--rlimit_nproc", str(profile.max_procs),
        "--rlimit_nofile", str(profile.max_fds),
        "--rlimit_fsize", str(profile.max_output_bytes),
        "--chroot", DEFAULT_ROOTFS,
        "--user", "65534",   # nobody
        "--group", "65534",
        "--no_new_privs",
        "--seccomp_string", f"POLICY FILE {SECCOMP_POLICY}",
        # L2 命名空间：pid/ipc/uts 全部独立
        "--disable_clone_newuser",
        "--enable_clone_newnet",   # 独立 netns，不配置任何接口 => 无网络
        "--enable_clone_newpid",
        "--enable_clone_newipc",
        "--enable_clone_newuts",
        "--hostname", jail_id,
        # 输入：只读、绑定单个文件，noexec/no-suid
        "--bindmount_ro", f"{profile.input_path}:/in/document:noexec,nosuid",
        # 输出：tmpfs，容量受限
        "--tmpfs_size", str(profile.max_output_bytes),
        "--bindmount", f"{profile.output_dir}:/out",
        # rootfs 本身只读
        "--mount_proc_ro",
    ]
    for key in ENV_ALLOWLIST:
        if key in os.environ:
            argv += ["--env", f"{key}={os.environ[key]}"]

    argv += [
        "/usr/bin/python3", "-I", "-m", profile.worker_module,
        "--input", "/in/document",
        "--output", "/out",
        *profile.extra_args,
    ]
    return argv


def describe(profile: SandboxProfile) -> str:
    """审计日志用：只记录结构，不记录文档内容。"""
    return " ".join(shlex.quote(a) for a in build_nsjail_argv(profile))


# ---------------------------------------------------------------------------
# 本地开发替代方案（生产必须用 nsjail/gVisor，不能拿这个上线）：
#
# Linux 开发机：bubblewrap
#   bwrap --ro-bind /usr /usr --ro-bind rootfs/app /app \
#         --ro-bind input /in/document --bind tmpfs /out \
#         --unshare-all --share-net ... python -I -m <worker>
#
# macOS 开发机：sandbox-exec，profile 中 (deny network*)、
#   (allow file-read* (subpath "/in"))、(allow file-write* (subpath "/out"))
# ---------------------------------------------------------------------------
