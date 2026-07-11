"""
Tests for P1-4: systemd worker minimum-privilege hardening.

These tests parse the systemd service unit file and the deployment setup
script as plain text/INI — no systemd installation or root privileges
are required.  They verify that the service no longer runs as root and
that the recommended security directives from the code review are
present.
"""

from __future__ import annotations

import configparser
import os
import re
from pathlib import Path

import pytest

# ── Path helpers ─────────────────────────────────────────────────

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SERVICE_FILE = PROJECT_ROOT / "deploy" / "stock-watchlist-report-worker.service"
SETUP_SCRIPT = PROJECT_ROOT / "deploy" / "setup-worker-user.sh"


# ── Fixtures ─────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def service_text() -> str:
    """Raw text of the systemd unit file."""
    assert SERVICE_FILE.is_file(), f"Service file not found: {SERVICE_FILE}"
    return SERVICE_FILE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def service_lines(service_text: str) -> list[str]:
    """Non-comment, non-blank lines of the unit file."""
    lines = []
    for raw in service_text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith(";"):
            continue
        lines.append(stripped)
    return lines


def _parse_directives(lines: list[str]) -> dict[str, list[str]]:
    """Parse *lines* into {section: {key: [values]}}.

    Handles duplicate keys (e.g. multiple ``SystemCallFilter`` lines)
    by collecting all values into a list.
    """
    sections: dict[str, dict[str, list[str]]] = {}
    current_section = None
    for line in lines:
        if line.startswith("[") and line.endswith("]"):
            current_section = line[1:-1]
            sections.setdefault(current_section, {})
            continue
        if current_section is None or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        sections.setdefault(current_section, {}).setdefault(key, []).append(value)
    return sections


@pytest.fixture(scope="module")
def service_cfg(service_lines: list[str]) -> dict[str, dict[str, list[str]]]:
    """Structured representation of the unit file."""
    return _parse_directives(service_lines)


@pytest.fixture(scope="module")
def service_section(service_cfg: dict) -> dict[str, list[str]]:
    """The [Service] section."""
    assert "Service" in service_cfg, "Missing [Service] section"
    return service_cfg["Service"]


@pytest.fixture(scope="module")
def setup_text() -> str:
    """Raw text of the deployment setup script."""
    assert SETUP_SCRIPT.is_file(), f"Setup script not found: {SETUP_SCRIPT}"
    return SETUP_SCRIPT.read_text(encoding="utf-8")


# ── Helpers ──────────────────────────────────────────────────────

def _get(section: dict[str, list[str]], key: str) -> str | None:
    """Return the first value for *key*, or None."""
    values = section.get(key)
    return values[0] if values else None


def _get_all(section: dict[str, list[str]], key: str) -> list[str]:
    """Return all values for *key* (may be empty)."""
    return section.get(key, [])


def _is_true(section: dict[str, list[str]], key: str) -> bool:
    """Check that a directive is set to 'true'."""
    val = _get(section, key)
    return val is not None and val.lower() == "true"


# ── Tests: non-root user ─────────────────────────────────────────

class TestNonRootUser:
    """P1-4 core requirement: the worker must not run as root."""

    def test_user_is_not_root(self, service_section):
        user = _get(service_section, "User")
        assert user is not None, "User= directive is missing"
        assert user != "root", "Service still runs as root"

    def test_user_is_dedicated(self, service_section):
        user = _get(service_section, "User")
        assert user == "stockwatch", (
            f"Expected dedicated user 'stockwatch', got '{user}'"
        )

    def test_group_is_set(self, service_section):
        group = _get(service_section, "Group")
        assert group is not None, "Group= directive is missing"
        assert group != "root", "Group must not be root"

    def test_group_is_dedicated(self, service_section):
        group = _get(service_section, "Group")
        assert group == "stockwatch", (
            f"Expected dedicated group 'stockwatch', got '{group}'"
        )


# ── Tests: privilege escalation prevention ──────────────────────

class TestPrivilegeEscalation:
    """Verify that the service cannot gain new privileges."""

    def test_no_new_privileges(self, service_section):
        assert _is_true(service_section, "NoNewPrivileges"), (
            "NoNewPrivileges=true is required to prevent privilege escalation"
        )

    def test_restrict_suid_sgid(self, service_section):
        assert _is_true(service_section, "RestrictSUIDSGID"), (
            "RestrictSUIDSGID=true prevents SUID/SGID bit misuse"
        )

    def test_capability_bounding_set_empty(self, service_section):
        """No Linux capabilities should be granted to the worker."""
        caps = _get(service_section, "CapabilityBoundingSet")
        assert caps is not None, "CapabilityBoundingSet= is missing"
        assert caps.strip() == "", (
            "CapabilityBoundingSet must be empty (no capabilities)"
        )

    def test_ambient_capabilities_empty(self, service_section):
        caps = _get(service_section, "AmbientCapabilities")
        if caps is not None:
            assert caps.strip() == "", (
                "AmbientCapabilities must be empty"
            )


# ── Tests: filesystem hardening ─────────────────────────────────

class TestFilesystemHardening:
    """Verify filesystem isolation directives."""

    def test_private_tmp(self, service_section):
        assert _is_true(service_section, "PrivateTmp"), (
            "PrivateTmp=true isolates /tmp and /var/tmp"
        )

    def test_private_devices(self, service_section):
        assert _is_true(service_section, "PrivateDevices"), (
            "PrivateDevices=true restricts access to physical devices"
        )

    def test_protect_system_strict(self, service_section):
        val = _get(service_section, "ProtectSystem")
        assert val is not None, "ProtectSystem= is missing"
        assert val == "strict", (
            f"ProtectSystem must be 'strict', got '{val}'"
        )

    def test_protect_home(self, service_section):
        assert _is_true(service_section, "ProtectHome"), (
            "ProtectHome=true hides /home, /root, /run/user"
        )

    def test_read_write_paths_set(self, service_section):
        rwp = _get(service_section, "ReadWritePaths")
        assert rwp is not None, "ReadWritePaths= is missing"
        assert rwp.strip() != "", "ReadWritePaths must not be empty"

    def test_read_write_paths_not_root(self, service_section):
        rwp = _get(service_section, "ReadWritePaths")
        assert "/" not in rwp.split() or all(
            p != "/" for p in rwp.split()
        ), "ReadWritePaths must not include '/'"

    def test_read_write_paths_includes_data(self, service_section):
        rwp = _get(service_section, "ReadWritePaths")
        paths = rwp.split()
        assert any("data" in p for p in paths), (
            "ReadWritePaths must include a data/ directory"
        )

    def test_read_write_paths_includes_runs(self, service_section):
        rwp = _get(service_section, "ReadWritePaths")
        paths = rwp.split()
        assert any("runs" in p for p in paths), (
            "ReadWritePaths must include the runs/ directory for temp report files"
        )

    def test_read_write_paths_not_entire_app(self, service_section):
        """ReadWritePaths should not grant write to the entire application dir."""
        rwp = _get(service_section, "ReadWritePaths")
        paths = rwp.split()
        for p in paths:
            # The path should be a subdirectory, not the app root itself
            assert not p.rstrip("/").endswith("Stock_watch_list"), (
                f"ReadWritePaths includes the entire app dir: {p} — "
                "use specific subdirectories instead"
            )

    def test_umask_restrictive(self, service_section):
        umask = _get(service_section, "UMask")
        assert umask is not None, "UMask= is missing"
        # 0077 means only the owner can read/write files
        assert umask in ("0077", "007"), (
            f"UMask should be 0077 for strict file permissions, got '{umask}'"
        )

    def test_remove_ipc(self, service_section):
        assert _is_true(service_section, "RemoveIPC"), (
            "RemoveIPC=true cleans up IPC objects on service stop"
        )


# ── Tests: kernel protections ───────────────────────────────────

class TestKernelProtections:
    """Verify kernel-level protection directives."""

    def test_protect_kernel_tunables(self, service_section):
        assert _is_true(service_section, "ProtectKernelTunables"), (
            "ProtectKernelTunables=true is required"
        )

    def test_protect_kernel_modules(self, service_section):
        assert _is_true(service_section, "ProtectKernelModules"), (
            "ProtectKernelModules=true is required"
        )

    def test_protect_kernel_logs(self, service_section):
        assert _is_true(service_section, "ProtectKernelLogs"), (
            "ProtectKernelLogs=true is required"
        )

    def test_protect_control_groups(self, service_section):
        assert _is_true(service_section, "ProtectControlGroups"), (
            "ProtectControlGroups=true is required"
        )

    def test_protect_clock(self, service_section):
        assert _is_true(service_section, "ProtectClock"), (
            "ProtectClock=true prevents clock modifications"
        )

    def test_protect_hostname(self, service_section):
        assert _is_true(service_section, "ProtectHostname"), (
            "ProtectHostname=true prevents hostname changes"
        )

    def test_restrict_realtime(self, service_section):
        assert _is_true(service_section, "RestrictRealtime"), (
            "RestrictRealtime=true prevents realtime scheduling"
        )

    def test_lock_personality(self, service_section):
        assert _is_true(service_section, "LockPersonality"), (
            "LockPersonality=true prevents personality changes"
        )

    def test_restrict_namespaces(self, service_section):
        assert _is_true(service_section, "RestrictNamespaces"), (
            "RestrictNamespaces=true prevents namespace creation"
        )

    def test_protect_proc(self, service_section):
        val = _get(service_section, "ProtectProc")
        assert val is not None, "ProtectProc= is recommended"
        assert val in ("invisible", "ptraceable", "noaccess"), (
            f"ProtectProc should restrict process visibility, got '{val}'"
        )


# ── Tests: syscall filtering ────────────────────────────────────

class TestSyscallFilter:
    """Verify syscall restriction directives."""

    def test_system_call_filter_present(self, service_section):
        filters = _get_all(service_section, "SystemCallFilter")
        assert len(filters) > 0, "SystemCallFilter= is missing"

    def test_system_call_filter_allows_system_service(self, service_section):
        filters = _get_all(service_section, "SystemCallFilter")
        allow_filters = [f for f in filters if not f.startswith("~")]
        assert any("@system-service" in f for f in allow_filters), (
            "SystemCallFilter must include @system-service group"
        )

    def test_system_call_filter_denies_privileged(self, service_section):
        filters = _get_all(service_section, "SystemCallFilter")
        deny_filters = [f for f in filters if f.startswith("~")]
        assert any("@privileged" in f for f in deny_filters), (
            "SystemCallFilter must deny @privileged syscalls"
        )

    def test_system_call_filter_denies_resources(self, service_section):
        filters = _get_all(service_section, "SystemCallFilter")
        deny_filters = [f for f in filters if f.startswith("~")]
        assert any("@resources" in f for f in deny_filters), (
            "SystemCallFilter must deny @resources syscalls"
        )

    def test_system_call_error_number(self, service_section):
        val = _get(service_section, "SystemCallErrorNumber")
        assert val is not None, "SystemCallErrorNumber= is recommended"
        assert val.upper() in ("EPERM", "ENOSYS"), (
            f"SystemCallErrorNumber should be EPERM or ENOSYS, got '{val}'"
        )


# ── Tests: network restrictions ─────────────────────────────────

class TestNetworkRestrictions:
    """Verify network address family restrictions."""

    def test_restrict_address_families_present(self, service_section):
        raf = _get(service_section, "RestrictAddressFamilies")
        assert raf is not None, "RestrictAddressFamilies= is missing"

    def test_restrict_address_families_includes_inet(self, service_section):
        raf = _get(service_section, "RestrictAddressFamilies")
        assert "AF_INET" in raf, (
            "AF_INET must be allowed for outbound TCP (SMTP, HTTPS)"
        )

    def test_restrict_address_families_includes_unix(self, service_section):
        raf = _get(service_section, "RestrictAddressFamilies")
        assert "AF_UNIX" in raf, (
            "AF_UNIX must be allowed for local sockets"
        )

    def test_restrict_address_families_not_wildcard(self, service_section):
        raf = _get(service_section, "RestrictAddressFamilies")
        # Should not include AF_NETLINK, AF_PACKET, etc.
        forbidden = {"AF_NETLINK", "AF_PACKET", "AF_RAW"}
        families = set(raf.split())
        assert not (families & forbidden), (
            f"RestrictAddressFamilies includes forbidden families: {families & forbidden}"
        )


# ── Tests: resource limits ──────────────────────────────────────

class TestResourceLimits:
    """Verify resource limit directives are present."""

    def test_memory_max_set(self, service_section):
        val = _get(service_section, "MemoryMax")
        assert val is not None, "MemoryMax= is recommended to limit memory usage"
        assert val.lower() not in ("infinity", "0"), (
            "MemoryMax should be a finite value"
        )

    def test_tasks_max_set(self, service_section):
        val = _get(service_section, "TasksMax")
        assert val is not None, "TasksMax= is recommended to limit process count"

    def test_limit_nofile_set(self, service_section):
        val = _get(service_section, "LimitNOFILE")
        assert val is not None, "LimitNOFILE= is recommended to limit file descriptors"
        assert int(val) > 0, "LimitNOFILE must be a positive integer"


# ── Tests: environment configuration ────────────────────────────

class TestEnvironmentConfig:
    """Verify environment loading via EnvironmentFile."""

    def test_environment_file_set(self, service_section):
        env_file = _get(service_section, "EnvironmentFile")
        assert env_file is not None, "EnvironmentFile= is missing"
        assert env_file.endswith(".env"), (
            f"EnvironmentFile should point to .env, got '{env_file}'"
        )

    def test_pythonunbuffered_preserved(self, service_section):
        envs = _get_all(service_section, "Environment")
        assert any("PYTHONUNBUFFERED=1" in e for e in envs), (
            "PYTHONUNBUFFERED=1 must be preserved"
        )

    def test_report_job_db_in_data_dir(self, service_section):
        """The job database should live in a dedicated data/ directory."""
        envs = _get_all(service_section, "Environment")
        db_env = [e for e in envs if e.startswith("REPORT_JOB_DB=")]
        assert len(db_env) == 1, "REPORT_JOB_DB must be set exactly once"
        assert "data/" in db_env[0], (
            f"REPORT_JOB_DB should point to data/ subdirectory, got '{db_env[0]}'"
        )

    def test_home_set_to_data_dir(self, service_section):
        """HOME should point to a writable directory, not /root or /home."""
        envs = _get_all(service_section, "Environment")
        home_env = [e for e in envs if e.startswith("HOME=")]
        assert len(home_env) == 1, "HOME must be set for cache/temp paths"
        assert "data" in home_env[0], (
            f"HOME should point to data/ directory, got '{home_env[0]}'"
        )


# ── Tests: service structure preserved ──────────────────────────

class TestServiceStructure:
    """Verify that essential service directives are still correct."""

    def test_exec_start_preserved(self, service_section):
        exec_start = _get(service_section, "ExecStart")
        assert exec_start is not None, "ExecStart= is missing"
        assert "daily_report.worker" in exec_start, (
            "ExecStart must run the daily_report.worker module"
        )

    def test_working_directory_preserved(self, service_section):
        wd = _get(service_section, "WorkingDirectory")
        assert wd is not None, "WorkingDirectory= is missing"
        assert "Stock_watch_list" in wd, (
            f"WorkingDirectory must point to the app directory, got '{wd}'"
        )

    def test_restart_always(self, service_section):
        restart = _get(service_section, "Restart")
        assert restart == "always", (
            f"Restart should be 'always', got '{restart}'"
        )

    def test_type_simple(self, service_section):
        svc_type = _get(service_section, "Type")
        assert svc_type == "simple", (
            f"Type should be 'simple', got '{svc_type}'"
        )

    def test_timeout_stop_sec(self, service_section):
        val = _get(service_section, "TimeoutStopSec")
        assert val is not None, "TimeoutStopSec= is missing"
        assert int(val) >= 10, "TimeoutStopSec should allow graceful shutdown"

    def test_kill_mode_control_group(self, service_section):
        val = _get(service_section, "KillMode")
        assert val == "control-group", (
            f"KillMode should be 'control-group', got '{val}'"
        )

    def test_wants_network_online(self, service_cfg):
        unit = service_cfg.get("Unit", {})
        wants = _get(unit, "Wants")
        assert wants is not None and "network-online.target" in wants, (
            "Unit must depend on network-online.target"
        )

    def test_after_network_online(self, service_cfg):
        unit = service_cfg.get("Unit", {})
        after = _get(unit, "After")
        assert after is not None and "network-online.target" in after, (
            "Unit must start after network-online.target"
        )

    def test_install_multi_user(self, service_cfg):
        install = service_cfg.get("Install", {})
        wanted_by = _get(install, "WantedBy")
        assert wanted_by == "multi-user.target", (
            f"WantedBy should be 'multi-user.target', got '{wanted_by}'"
        )

    def test_runtime_directory_set(self, service_section):
        rtd = _get(service_section, "RuntimeDirectory")
        assert rtd is not None, "RuntimeDirectory= is recommended for /run isolation"
        assert rtd.strip() != "", "RuntimeDirectory must not be empty"

    def test_runtime_directory_mode(self, service_section):
        mode = _get(service_section, "RuntimeDirectoryMode")
        assert mode is not None, "RuntimeDirectoryMode= is recommended"
        assert mode in ("0700", "0750"), (
            f"RuntimeDirectoryMode should be 0700 or 0750, got '{mode}'"
        )


# ── Tests: deployment setup script ──────────────────────────────

class TestSetupScript:
    """Verify the deployment setup script creates the user correctly."""

    def test_script_exists(self):
        assert SETUP_SCRIPT.is_file(), "setup-worker-user.sh must exist"

    def test_script_has_shebang(self, setup_text):
        first_line = setup_text.splitlines()[0]
        assert first_line.startswith("#!"), "Script must have a shebang line"
        assert "bash" in first_line, "Script should use bash"

    def test_script_creates_user(self, setup_text):
        assert "useradd" in setup_text, "Script must create the stockwatch user"
        assert "stockwatch" in setup_text, "Script must reference stockwatch user"

    def test_script_creates_group(self, setup_text):
        assert "groupadd" in setup_text, "Script must create the stockwatch group"

    def test_script_system_account(self, setup_text):
        assert "--system" in setup_text, (
            "User should be created as a system account (--system)"
        )

    def test_script_nologin_shell(self, setup_text):
        assert "nologin" in setup_text, (
            "User should have /usr/sbin/nologin or /sbin/nologin shell"
        )

    def test_script_creates_data_dir(self, setup_text):
        assert "data" in setup_text.lower(), (
            "Script must create the data/ directory"
        )
        assert "mkdir" in setup_text, "Script must create directories"

    def test_script_migrates_db(self, setup_text):
        assert "daily_report_jobs.db" in setup_text, (
            "Script must handle the daily_report_jobs.db migration"
        )
        assert "mv " in setup_text, "Script must move the DB file"

    def test_script_sets_ownership(self, setup_text):
        assert "chown" in setup_text, "Script must set file ownership"
        assert "stockwatch" in setup_text, (
            "Ownership must be set to stockwatch user"
        )

    def test_script_sets_permissions(self, setup_text):
        assert "chmod" in setup_text, "Script must set file permissions"

    def test_script_secures_env_file(self, setup_text):
        assert ".env" in setup_text, "Script must secure the .env file"

    def test_script_creates_runs_dir(self, setup_text):
        assert "runs" in setup_text, (
            "Script must ensure daily_report/runs/ exists"
        )

    def test_script_set_eu_options(self, setup_text):
        assert "set -euo pipefail" in setup_text or "set -e" in setup_text, (
            "Script should use 'set -euo pipefail' for safe error handling"
        )

    def test_script_checks_root(self, setup_text):
        assert "id -u" in setup_text, (
            "Script should verify it is running as root"
        )

    def test_script_prints_next_steps(self, setup_text):
        assert "systemctl" in setup_text, (
            "Script should print systemd installation instructions"
        )
