#!/usr/bin/python3 -I
#
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
"""Cluster cost collector for the Cluster Costs dashboard.

Runs as root from cluster-cost.timer every 60 seconds on the monitoring node
(the ParallelCluster head node or a PCS login node). Each run finds the
cluster's running instances, lists their EBS volumes, reads Slurm's nodes
and jobs, refreshes the cached prices that are due, prices every instance,
volume and PCS fee, splits the compute node cost among Slurm users and
accounts, adds the run to the accumulated totals, and writes the rates, the
totals and the health of the run to node_exporter's textfile directory:

    /var/lib/prometheus/node-exporter/cluster_cost.prom

Configuration comes from the environment (the unit's EnvironmentFile,
/etc/aws-parallelcluster-monitoring/cluster-cost.env): PLATFORM, CLUSTER_ID
and REGION. The accumulators (state.json), the price cache and the run
report live in the state directory, /var/lib/aws-parallelcluster-monitoring/cost.

    cluster-cost-collector            one run
    cluster-cost-collector --dry-run  one run that prints the exposition and
                                      writes nothing

Exit codes: 0 the textfile was written (degraded runs included), or another
run holds the lock; 1 the textfile or the state couldn't be written; 2 the
configuration is invalid. Standard library only, Python 3.7 or later.
"""

import argparse
import calendar
import datetime
import fcntl
import json
import logging
import math
import os
import re
import signal
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Optional

LOG = logging.getLogger("cluster_cost")

# --- Files -------------------------------------------------------------------
STATE_DIR_DEFAULT = "/var/lib/aws-parallelcluster-monitoring/cost"
TEXTFILE_DIR_DEFAULT = "/var/lib/prometheus/node-exporter"
TEXTFILE_NAME = "cluster_cost.prom"
TEXTFILE_TMP = ".cluster_cost.prom.tmp"   # doesn't end in .prom: node_exporter ignores it
PRICES_FILE = "prices.json"
STATE_FILE = "state.json"
STATE_BACKUP = "state.json.bak"
STATE_CORRUPT = "state.json.corrupt"
STATE_TMP = "state.json.tmp"
REPORT_FILE = "last-run.json"
LOCK_FILE = ".lock"
TEXTFILE_MODE = 0o644                     # node_exporter reads it as nobody
PRIVATE_FILE_MODE = 0o640

# --- Stages and codes --------------------------------------------------------
STAGES = ("discovery", "ebs", "slurm", "on_demand_prices", "spot_prices", "pcs", "state")

AWS_CODES = frozenset([
    "AwsCliNotFound", "Timeout", "EndpointUnreachable", "CredentialsError", "AccessDenied",
    "Throttled", "AwsApiError", "InvalidResponse", "AwsCliError",
])
# After one of these, the run makes no more calls to that source, and the
# source is backed off. The other AWS codes are specific to one request: only
# that entry waits.
SOURCE_CODES = frozenset([
    "AccessDenied", "Throttled", "CredentialsError", "EndpointUnreachable", "Timeout", "AwsCliNotFound",
])
STAGE_CODES = {
    "discovery": ("NoInstancesFound",),
    "slurm": ("SlurmNotFound", "SlurmCommandFailed", "Timeout", "SlurmJsonInvalid", "NodeMappingIncomplete"),
    "state": ("StateWriteFailed", "StateUnreadable"),
}
INTERNAL_ERROR = "InternalError"
ERROR_CODES = frozenset(AWS_CODES | {c for codes in STAGE_CODES.values() for c in codes} | {INTERNAL_ERROR})

# Credential sources that must never replace the instance profile (review
# finding 65). They are removed from the environment of every aws call.
CREDENTIAL_ENV_VARS = (
    "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_SECURITY_TOKEN",
    "AWS_PROFILE", "AWS_DEFAULT_PROFILE", "AWS_WEB_IDENTITY_TOKEN_FILE", "AWS_ROLE_ARN",
    "AWS_ROLE_SESSION_NAME", "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
    "AWS_CONTAINER_CREDENTIALS_FULL_URI", "AWS_CONTAINER_AUTHORIZATION_TOKEN",
    "AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE", "AWS_EC2_METADATA_DISABLED",
)

# --- Unpriced resources --------------------------------------------------------
KINDS = ("instance", "volume", "fee")
UNPRICED_REASONS = (
    "not_in_price_list", "ambiguous_price", "price_api_error", "price_lookup_pending",
    "spot_price_unavailable", "spot_platform_unsupported", "unknown_volume_type", "invalid_record",
    "capacity_block", "dedicated_host", "volume_list_unavailable",
)
# Not priced by design: they don't make the estimate incomplete.
NON_DEGRADING_REASONS = frozenset(["capacity_block", "dedicated_host"])
# last_error values of a successful lookup that found no usable price.
NOT_FOUND_REASONS = frozenset(["not_in_price_list", "ambiguous_price", "spot_price_unavailable"])

# --- Metric families, in the order of the file -------------------------------
# (name, value format, help)
FAMILIES = (
    ("cluster_cost_usd_per_hour", "usd", "Estimated cost rate per cost line, in USD per hour."),
    ("cluster_cost_user_usd_per_hour", "usd", "Compute node cost held by running jobs, per user and account, in USD per hour."),
    ("cluster_cost_unallocated_usd_per_hour", "usd", "Compute node cost that no job holds, in USD per hour."),
    ("cluster_cost_idle_nodes", "count", "Running, priced compute instances with no job."),
    ("cluster_cost_nodes", "count", "Running cluster instances, priced or not."),
    ("cluster_cost_accumulated_usd", "usd", "Accumulated cost per cost line, in USD."),
    ("cluster_cost_user_accumulated_usd", "usd", "Accumulated compute node cost per user and account, in USD."),
    ("cluster_cost_unallocated_accumulated_usd", "usd", "Accumulated idle and unallocated compute node cost, in USD."),
    ("cluster_cost_period_start_timestamp_seconds", "timestamp", "Start of the accumulation period."),
    ("cluster_cost_unaccounted_seconds", "seconds", "Seconds not integrated into the accumulated totals."),
    ("cluster_cost_unpriced_resources", "count", "Resources left out of the estimate, by kind and reason."),
    ("cluster_cost_unpriced_resource_seconds", "seconds", "Resource-seconds that the accumulated totals leave out because the resource was unpriced."),
    ("cluster_cost_collector_up", "count", "1 when the run produced a complete estimate, 0 otherwise."),
    ("cluster_cost_collector_stage_ok", "count", "1 or 0 for each stage that ran."),
    ("cluster_cost_collector_stage_error", "count", "1 for each failed stage, with its error code."),
    ("cluster_cost_collector_last_success_timestamp_seconds", "timestamp", "Last run with collector_up 1."),
    ("cluster_cost_collector_run_duration_seconds", "seconds", "Duration of this run."),
    ("cluster_cost_price_age_seconds", "seconds", "Age of the oldest cached value used in the run."),
    ("cluster_cost_collector_info", "count", "Collector configuration. Always 1."),
)
FAMILY_NAMES = tuple(f[0] for f in FAMILIES)
LABEL_MAX = 128

# --- Time budget (design "Time budget") ----------------------------------------
RUN_DEADLINE = 120.0        # every run ends within this
CALL_WINDOW = 100.0         # calls stop here; the last 20 s are the reserve for steps 8-11
REQUIRED_MIN_LEFT = 5.0     # a required call needs at least this much time left
OPTIONAL_MIN_LEFT = 30.0    # an optional call needs at least this much time left
CALL_TIMEOUT = 30.0         # subprocess timeout (required calls: min(30 s, time left))
PRICING_CALL_BUDGET = 20    # Pricing calls per run, failed ones included
AWS_TIMEOUT_ARGS = ("--cli-connect-timeout", "5", "--cli-read-timeout", "20")
PRICING_REGION = "us-east-1"

# --- Cache intervals (design "Price cache"), in seconds ------------------------
MINUTE = 60.0
HOUR = 3600.0
DAY = 86400.0
BACKOFF_SECONDS = {"pricing": 15 * MINUTE, "spot": 5 * MINUTE, "pcs": 10 * MINUTE}
# (due again after a success, after a not found, after a failed call)
OD_INTERVALS = (DAY, 6 * HOUR, 15 * MINUTE)
EBS_INTERVALS = (DAY, DAY, 15 * MINUTE)
PCS_FEE_INTERVALS = (DAY, 6 * HOUR, 15 * MINUTE)
PCS_CLUSTER_INTERVALS = (10 * MINUTE, 10 * MINUTE, 10 * MINUTE)
PCS_NODE_GROUPS_INTERVALS = (HOUR, HOUR, 10 * MINUTE)
PCS_NODE_GROUPS_UNKNOWN_ID = 5 * MINUTE
SPOT_REFRESH = 5 * MINUTE
SPOT_MAX_AGE = 6 * HOUR
PCS_CLUSTER_MAX_AGE = DAY
VOLUME_RECORD_MAX_AGE = HOUR
VOLUME_CHUNK = 100

# --- Slurm (design "Allocation (users, accounts, idle)") -------------------------
PC_SLURM_BIN = "/opt/slurm/bin"
PCS_SLURM_LINK = "/etc/aws/pcs/scheduler/slurm"
PCS_SCHEDULER_DIR = "/opt/aws/pcs/scheduler"
SLURM_COMMANDS = ("squeue", "scontrol")
SCONTROL_ARGS = ("show", "nodes", "--json")
SQUEUE_ARGS = ("--json", "--states=RUNNING,CONFIGURING,COMPLETING")
# Jobs in these states hold resources. For the mapping check (step 8), a node
# is busy when it holds a RUNNING job that is neither CONFIGURING nor
# COMPLETING, and isn't in one of the node states of booting or lost nodes.
HOLDING_JOB_STATES = frozenset(["RUNNING", "CONFIGURING", "COMPLETING"])
NOT_BUSY_JOB_STATES = frozenset(["CONFIGURING", "COMPLETING"])
NOT_BUSY_NODE_STATES = frozenset(["POWERING_UP", "POWERING_DOWN", "POWERED_DOWN", "DOWN", "NOT_RESPONDING"])
UNALLOCATED_REASONS = ("idle_node", "partial_node")
USER_KEY_CAP = 5000         # accumulated (username, account) keys per period
OTHER_USER = "(other)"

# --- Accumulation (design "Accumulation and state") ------------------------------
PERIODS = ("since_start", "month")
ACC_FAMILIES = ("lines", "alloc")
ACC_BUCKETS = ("lines", "users", "unallocated", "unpriced")
GAP_CAP = HOUR              # a longer gap adds one hour at the last rates; the rest is unaccounted

# --- Validation ----------------------------------------------------------------
CLUSTER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
REGION_RE = re.compile(r"^[a-z]{2,4}(-[a-z0-9]+)+-[0-9]+$")
INSTANCE_ID_RE = re.compile(r"^i-[0-9a-f]{8,17}$")
INSTANCE_TYPE_RE = re.compile(r"^[a-z0-9-]+\.[a-z0-9-]+$")
AZ_RE = re.compile(r"^[a-z0-9-]{1,64}$")
USAGE_OPERATION_RE = re.compile(r"^RunInstances(:[0-9a-z]{4})?$")
VOLUME_ID_RE = re.compile(r"^vol-[0-9a-f]+$")
VOLUME_TYPE_RE = re.compile(r"^[a-z0-9]{1,32}$")
NODE_GROUP_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
SCHEDULER_VERSION_RE = re.compile(r"^[0-9]{2}\.[0-9]{2}$")
ADVANCED_TIER_RE = re.compile(r"^(p|trn)[0-9]")
AWS_ERROR_RE = re.compile(r"An error occurred \(([A-Za-z0-9._-]+)\)")
SLURM_TREE_RE = re.compile(r"^slurm-[0-9]{2}\.[0-9]{2}$")
COUNT_RE = re.compile(r"^[0-9]+$")
MONTH_RE = re.compile(r"^[0-9]{4}-[0-9]{2}$")
LABEL_NAME_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")

CREDENTIALS_X = frozenset([
    "NoCredentials", "ExpiredToken", "ExpiredTokenException", "RequestExpired", "InvalidClientTokenId",
    "UnrecognizedClientException", "AuthFailure", "SignatureDoesNotMatch",
])
ACCESS_DENIED_X = frozenset(["AccessDenied", "AccessDeniedException", "UnauthorizedOperation", "UnauthorizedException"])
THROTTLED_X = frozenset([
    "Throttling", "ThrottlingException", "ThrottledException", "RequestLimitExceeded",
    "TooManyRequestsException", "RequestThrottled", "RequestThrottledException", "SlowDown",
])
ENDPOINT_UNREACHABLE_TEXT = (
    "Could not connect to the endpoint URL", "Connect timeout on endpoint URL",
    "Failed to connect to proxy URL", "Connection was closed before we received a valid response",
)
CREDENTIALS_TEXT = ("Unable to locate credentials", "Error when retrieving credentials")


# =============================================================================
# Configuration
# =============================================================================
class ConfigError(Exception):
    """Invalid configuration: the run exits with 2 and writes nothing."""


@dataclass
class Config:
    platform: str
    cluster_id: str
    region: str
    state_dir: str = STATE_DIR_DEFAULT
    textfile_dir: str = TEXTFILE_DIR_DEFAULT
    slurm_bin_dir: Optional[str] = None
    debug: bool = False


def load_config(environ):
    """Read and validate the configuration variables (design "Configuration file")."""
    platform = environ.get("PLATFORM", "")
    if platform not in ("parallelcluster", "pcs"):
        raise ConfigError("PLATFORM must be parallelcluster or pcs, not %r" % platform[:64])
    cluster_id = environ.get("CLUSTER_ID", "")
    if not CLUSTER_ID_RE.match(cluster_id):
        raise ConfigError("CLUSTER_ID %r is not a valid cluster name or ID" % cluster_id[:140])
    region = environ.get("REGION", "")
    if region.startswith("cn-"):
        raise ConfigError("AWS China regions are not supported: their price lists are in CNY")
    if not REGION_RE.match(region):
        raise ConfigError("REGION %r is not a valid region code" % region[:64])
    slurm_bin_dir = environ.get("CLUSTER_COST_SLURM_BIN_DIR") or None
    return Config(
        platform=platform,
        cluster_id=cluster_id,
        region=region,
        state_dir=environ.get("CLUSTER_COST_STATE_DIR") or STATE_DIR_DEFAULT,
        textfile_dir=environ.get("CLUSTER_COST_TEXTFILE_DIR") or TEXTFILE_DIR_DEFAULT,
        slurm_bin_dir=slurm_bin_dir,
        debug=environ.get("CLUSTER_COST_DEBUG") == "1",
    )


def check_private_dir(path, owner_uid):
    """Refuse a directory that others could use to change what we write.

    It must be an absolute path to an existing directory that isn't a
    symlink, is owned by owner_uid (root in production), and isn't writable
    by group or others.
    """
    if not os.path.isabs(path):
        raise ConfigError("%s is not an absolute path" % path)
    try:
        st = os.lstat(path)
    except OSError as e:
        raise ConfigError("%s: %s" % (path, e.strerror))
    if stat.S_ISLNK(st.st_mode):
        raise ConfigError("%s is a symlink" % path)
    if not stat.S_ISDIR(st.st_mode):
        raise ConfigError("%s is not a directory" % path)
    if st.st_uid != owner_uid:
        raise ConfigError("%s is owned by uid %d, not %d" % (path, st.st_uid, owner_uid))
    if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise ConfigError("%s is writable by group or others (mode %04o)" % (path, stat.S_IMODE(st.st_mode)))


# =============================================================================
# Time
# =============================================================================
class Clock:
    """Wall clock for timestamps, monotonic clock for the run deadline."""

    def wall(self):
        return time.time()

    def monotonic(self):
        return time.monotonic()


class Deadline:
    """The run's time budget. Calls can run until CALL_WINDOW seconds."""

    def __init__(self, clock):
        self.clock = clock
        self.start = clock.monotonic()

    def elapsed(self):
        return self.clock.monotonic() - self.start

    def time_left(self):
        return CALL_WINDOW - self.elapsed()

    def required_timeout(self):
        """The timeout of a required call, or None when it can't start."""
        left = self.time_left()
        if left < REQUIRED_MIN_LEFT:
            return None
        return min(CALL_TIMEOUT, left)

    def optional_allowed(self):
        return self.time_left() >= OPTIONAL_MIN_LEFT


def utc_iso(t):
    """Unix time as ISO 8601 UTC, to the second (2026-11-25T21:40:02Z)."""
    return datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso8601(text):
    """An ISO 8601 timestamp as Unix time, or None. Python 3.7 doesn't accept 'Z'."""
    if not isinstance(text, str) or not text:
        return None
    s = text.strip()
    if s.endswith("Z") or s.endswith("z"):
        s = s[:-1] + "+00:00"
    try:
        d = datetime.datetime.fromisoformat(s)
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=datetime.timezone.utc)
    return d.timestamp()


def hours_in_month(t):
    """H = 24 x the days of the UTC month that contains t."""
    d = datetime.datetime.fromtimestamp(t, datetime.timezone.utc)
    return 24.0 * calendar.monthrange(d.year, d.month)[1]


# =============================================================================
# Subprocesses
# =============================================================================
class CommandResult:
    """What a command did: exit code, output, and whether it ran at all."""

    def __init__(self, rc=0, stdout="", stderr="", timed_out=False, not_found=False, duration=0.0):
        self.rc = rc
        self.stdout = stdout
        self.stderr = stderr
        self.timed_out = timed_out
        self.not_found = not_found
        self.duration = duration

    def __repr__(self):
        return "CommandResult(rc=%r, timed_out=%r, not_found=%r)" % (self.rc, self.timed_out, self.not_found)


def _decode(data):
    return (data or b"").decode("utf-8", "replace")


def run_command(argv, timeout, env):
    """Run argv without a shell. On a timeout, kill its whole process group.

    The command starts in its own session, so that a child it starts (the
    AWS CLI's, or a stub's `sleep`) can't keep the pipes open after the
    timeout.
    """
    start = time.monotonic()
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, env=env, start_new_session=True,
                                close_fds=True)
    except OSError as e:
        return CommandResult(rc=127, stderr=str(e), not_found=True, duration=time.monotonic() - start)
    try:
        out, err = proc.communicate(timeout=timeout)
        return CommandResult(rc=proc.returncode, stdout=_decode(out), stderr=_decode(err),
                             duration=time.monotonic() - start)
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        try:
            out, err = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            # A descendant left the process group and still holds the pipes.
            for pipe in (proc.stdout, proc.stderr):
                try:
                    pipe.close()
                except OSError:
                    pass
            proc.wait()
            out, err = b"", b""
        return CommandResult(rc=proc.returncode, stdout=_decode(out), stderr=_decode(err),
                             timed_out=True, duration=time.monotonic() - start)


def _kill_group(proc):
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        try:
            proc.kill()
        except OSError:
            pass


# =============================================================================
# AWS calls
# =============================================================================
def aws_env(environ):
    """The environment of every aws call: the instance profile's credentials only.

    The caller's credential variables are removed (finding 65), and the CLI
    reads no config or credentials file, so neither root's ~/.aws nor a
    shell's exports can replace the instance profile. Everything else, such
    as AWS_ENDPOINT_URL*, the proxy variables, AWS_RETRY_MODE and
    AWS_MAX_ATTEMPTS, is kept.
    """
    env = {k: v for k, v in environ.items() if k not in CREDENTIAL_ENV_VARS}
    env["AWS_CONFIG_FILE"] = "/nonexistent"
    env["AWS_SHARED_CREDENTIALS_FILE"] = "/nonexistent"
    env["AWS_PAGER"] = ""
    return env


_NOT_JSON = object()


def _parse_json(text):
    try:
        return json.loads(text)
    except ValueError:
        return _NOT_JSON


def classify_aws_error(result, needed=None):
    """One code from the closed set of design "AWS error codes", or None.

    The table is checked in order. `needed(data)` says whether the parsed
    output of a successful call has what the call needs; when it hasn't,
    the code is InvalidResponse. No CLI text ever becomes a code.
    """
    if result.not_found:
        return "AwsCliNotFound"
    if result.timed_out:
        return "Timeout"
    err = result.stderr or ""
    if "Read timeout on endpoint URL" in err:
        return "Timeout"
    if any(s in err for s in ENDPOINT_UNREACHABLE_TEXT):
        return "EndpointUnreachable"
    m = AWS_ERROR_RE.search(err)
    x = m.group(1) if m else None
    if any(s in err for s in CREDENTIALS_TEXT) or x in CREDENTIALS_X:
        return "CredentialsError"
    if x in ACCESS_DENIED_X:
        return "AccessDenied"
    if x in THROTTLED_X:
        return "Throttled"
    if x:
        return "AwsApiError"
    if result.rc == 0:
        data = _parse_json(result.stdout)
        if data is _NOT_JSON or (needed is not None and not needed(data)):
            return "InvalidResponse"
        return None
    return "AwsCliError"


def _has_list(key):
    return lambda data: isinstance(data, dict) and isinstance(data.get(key), list)


def _is_list(data):
    return isinstance(data, list)


def _has_dict(key):
    return lambda data: isinstance(data, dict) and isinstance(data.get(key), dict)


class AwsResult:
    def __init__(self, data=None, code=None, message=""):
        self.data = data
        self.code = code
        self.message = message

    @property
    def ok(self):
        return self.code is None


# =============================================================================
# Stages and health
# =============================================================================
class Stage:
    """The result of one stage.

    Its code is the first failed call of this run, in call order. Without
    one, it is the code of the backoff that blocked a due entry, and
    otherwise the last_error of the first failed needed entry in key order
    (design "Deferred and blocked refreshes").
    """

    def __init__(self, name):
        self.name = name
        self.call_code = None
        self.call_message = ""
        self.blocked = None          # (source, code, until)
        self.stored = {}             # key -> code of a needed entry whose last attempt failed
        self.direct = None           # (code, message): a stage-specific failure
        self.internal = False

    def call_failed(self, code, message):
        if self.call_code is None:
            self.call_code, self.call_message = code, message

    def source_blocked(self, source, code, until):
        if self.blocked is None:
            self.blocked = (source, code, until)

    def stored_failure(self, key, code):
        self.stored[key] = code

    def fail(self, code, message):
        if self.direct is None:
            self.direct = (code, message)

    def internal_error(self):
        self.internal = True

    @property
    def code(self):
        if self.internal:
            return INTERNAL_ERROR
        if self.direct is not None:
            return self.direct[0]
        if self.call_code is not None:
            return self.call_code
        if self.blocked is not None:
            return self.blocked[1]
        if self.stored:
            return self.stored[min(self.stored)]
        return None

    @property
    def ok(self):
        return self.code is None

    def describe(self):
        """The stage's entry in last-run.json."""
        if self.ok:
            return {"ok": True}
        out = {"ok": False, "code": self.code}
        if self.internal:
            out["why"] = "internal error"
        elif self.direct is not None:
            out["why"], out["message"] = "stage failed", self.direct[1]
        elif self.call_code is not None:
            out["why"], out["message"] = "call failed", self.call_message
        elif self.blocked is not None:
            out["why"] = "source %s backed off until %s" % (self.blocked[0], utc_iso(self.blocked[2]))
        else:
            key = min(self.stored)
            out["why"] = "entry waiting after a failed call: %s" % key
        return out


class Health:
    """The stages that ran, in order, and collector_up."""

    def __init__(self):
        self.stages = {}
        self.order = []

    def stage(self, name):
        if name not in self.stages:
            self.stages[name] = Stage(name)
            self.order.append(name)
        return self.stages[name]

    def ran(self):
        return [self.stages[n] for n in self.order]

    def failed(self):
        return [(s.name, s.code) for s in self.ran() if not s.ok]

    def collector_up(self, applicable, unpriced):
        """1 when every applicable stage ran and is ok, and nothing is
        unpriced for a reason other than capacity_block or dedicated_host."""
        for name in applicable:
            st = self.stages.get(name)
            if st is None or not st.ok:
                return 0
        for (kind, reason), count in unpriced.items():
            if count and reason not in NON_DEGRADING_REASONS:
                return 0
        return 1


def applicable_stages(config, dry_run):
    """The stages that every complete run has, in run order."""
    names = ["discovery", "ebs", "slurm", "on_demand_prices", "spot_prices"]
    if config.platform == "pcs":
        names.append("pcs")
    if not dry_run:
        names.append("state")
    return names


# =============================================================================
# Files
# =============================================================================
def fsync_dir(path):
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_all(fd, data):
    view = memoryview(data)
    while view:
        n = os.write(fd, view)
        view = view[n:]


def _unlink_quietly(path):
    try:
        os.unlink(path)
    except OSError:
        pass


def write_new_file(path, data, mode):
    """Create path afresh (a leftover is removed first, and a symlink is never
    followed), write data, set mode and fsync."""
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    try:
        try:
            _write_all(fd, data)
            os.fchmod(fd, mode)
            os.fsync(fd)
        finally:
            os.close(fd)
    except BaseException:
        _unlink_quietly(path)
        raise


def write_file_atomic(directory, name, tmp_name, data, mode):
    """Write data to directory/name by temporary file, fsync and rename."""
    tmp = os.path.join(directory, tmp_name)
    write_new_file(tmp, data, mode)
    try:
        os.replace(tmp, os.path.join(directory, name))
    except BaseException:
        _unlink_quietly(tmp)
        raise
    fsync_dir(directory)


def write_json_atomic(directory, name, obj, mode=PRIVATE_FILE_MODE):
    data = (json.dumps(obj, sort_keys=True, indent=1, allow_nan=False) + "\n").encode("utf-8", "replace")
    write_file_atomic(directory, name, name + ".tmp", data, mode)


def acquire_lock(state_dir):
    """A non-blocking flock on <state dir>/.lock: the fd, or None when held."""
    fd = os.open(os.path.join(state_dir, LOCK_FILE), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, PRIVATE_FILE_MODE)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


def release_lock(fd):
    if fd is not None:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


# =============================================================================
# Price cache (prices.json)
# =============================================================================
PRICE_KEY_RES = (
    re.compile(r"^od\|[a-z0-9-]+\.[a-z0-9-]+\|RunInstances(:[0-9a-z]{4})?\|(Shared|Dedicated)$"),
    re.compile(r"^spot\|[a-z0-9-]+\.[a-z0-9-]+\|[a-z0-9-]+\|[A-Za-z/ ]+$"),
    re.compile(r"^ebs\|storage\|[a-z0-9]+$"),
    re.compile(r"^ebs\|iops\|(gp3|io1|io2\|[123])$"),
    re.compile(r"^ebs\|throughput\|gp3$"),
    re.compile(r"^pcs\|(controller|accounting)\|(SMALL|MEDIUM|LARGE)$"),
    re.compile(r"^pcs\|node\|(std|adv)$"),
)
CACHE_SOURCES = ("pricing", "spot", "pcs")


def _finite(x, minimum=None):
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return False
    if not math.isfinite(x):
        return False
    return minimum is None or x >= minimum


def _int_ge(x, minimum):
    return isinstance(x, int) and not isinstance(x, bool) and x >= minimum


def _opt_time(x):
    return x is None or _finite(x)


def valid_volume_record(v):
    if not isinstance(v, dict):
        return False
    if not isinstance(v.get("VolumeId"), str) or not VOLUME_ID_RE.match(v["VolumeId"]):
        return False
    if not isinstance(v.get("VolumeType"), str) or not VOLUME_TYPE_RE.match(v["VolumeType"]):
        return False
    if not _int_ge(v.get("Size"), 1):
        return False
    for k in ("Iops", "Throughput"):
        if v.get(k) is not None and not _int_ge(v.get(k), 0):
            return False
    return True


def _valid_value(key, value):
    if value is None:
        return True
    if key == "spot|refresh":
        return False
    if key == "pcs|cluster":
        return (isinstance(value, dict)
                and all(isinstance(value.get(k), str) for k in ("size", "status", "accounting_mode"))
                and (value.get("scheduler_version") is None or isinstance(value.get("scheduler_version"), str)))
    if key == "pcs|node_groups":
        return isinstance(value, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in value.items())
    if key == "volumes":
        if not isinstance(value, dict):
            return False
        for iid, rec in value.items():
            if not isinstance(rec, dict) or not _finite(rec.get("fetched_at")):
                return False
            if not isinstance(rec.get("volumes"), list) or not all(valid_volume_record(v) for v in rec["volumes"]):
                return False
        return True
    if any(r.match(key) for r in PRICE_KEY_RES):
        return _finite(value, 0)
    return False


def _valid_entry(key, e):
    if not isinstance(key, str) or not isinstance(e, dict):
        return False
    if key not in ("spot|refresh", "pcs|cluster", "pcs|node_groups", "volumes") and \
            not any(r.match(key) for r in PRICE_KEY_RES):
        return False
    if not _valid_value(key, e.get("value")) or not isinstance(e.get("unit", ""), str):
        return False
    if not _opt_time(e.get("fetched_at")) or not _opt_time(e.get("last_attempt_at")):
        return False
    err = e.get("last_error")
    if err is not None and err not in AWS_CODES and err not in NOT_FOUND_REASONS:
        return False
    return e.get("detail") is None or isinstance(e.get("detail"), dict)


def _valid_backoff(source, b):
    return (source in CACHE_SOURCES and isinstance(b, dict) and _finite(b.get("until"))
            and _opt_time(b.get("since")) and b.get("code") in AWS_CODES)


class PriceCache:
    """prices.json: cached prices, PCS details, volume records and backoffs."""

    SCHEMA = 1

    def __init__(self):
        self.entries = {}
        self.backoff = {}

    @classmethod
    def load(cls, path):
        cache = cls()
        try:
            with open(path, "rb") as f:
                raw = f.read()
        except FileNotFoundError:
            return cache
        except OSError as e:
            LOG.warning("price cache %s unreadable (%s): starting with an empty cache", path, e.strerror)
            return cache
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            LOG.warning("price cache %s doesn't parse: discarded", path)
            return cache
        if not isinstance(data, dict) or data.get("schema") != cls.SCHEMA or isinstance(data.get("schema"), bool):
            LOG.warning("price cache %s has another schema: discarded", path)
            return cache
        entries = data.get("entries", {})
        backoff = data.get("backoff", {})
        if not isinstance(entries, dict) or not isinstance(backoff, dict) \
                or not all(_valid_entry(k, e) for k, e in entries.items()) \
                or not all(_valid_backoff(s, b) for s, b in backoff.items()):
            LOG.warning("price cache %s holds a value of the wrong type: discarded", path)
            return cache
        cache.entries = entries
        cache.backoff = backoff
        return cache

    def to_json(self):
        return {"schema": self.SCHEMA, "entries": self.entries, "backoff": self.backoff}

    def get(self, key):
        return self.entries.get(key)

    def value(self, key):
        e = self.entries.get(key)
        return None if e is None else e.get("value")

    def due(self, key, intervals, now):
        """Due when missing, or when the interval of its state has passed
        since its last attempt."""
        e = self.entries.get(key)
        if e is None or e.get("last_attempt_at") is None:
            return True
        success, not_found, failed = intervals
        err = e.get("last_error")
        if err in AWS_CODES:
            interval = failed
        elif err is not None:
            interval = not_found
        else:
            interval = success
        return now - e["last_attempt_at"] >= interval

    def failed(self, key):
        e = self.entries.get(key)
        return e is not None and e.get("last_error") in AWS_CODES

    def backed_off(self, source, now):
        b = self.backoff.get(source)
        return b is not None and now < b["until"]

    def set_backoff(self, source, code, now):
        self.backoff[source] = {"until": now + BACKOFF_SECONDS[source], "code": code, "since": now}

    def _entry(self, key, unit):
        e = self.entries.get(key)
        if e is None:
            e = {"value": None, "unit": unit, "fetched_at": None, "last_attempt_at": None, "last_error": None}
            self.entries[key] = e
        e["unit"] = unit
        return e

    def record_success(self, key, value, unit, now, detail=None):
        e = self._entry(key, unit)
        e.update(value=value, fetched_at=now, last_attempt_at=now, last_error=None)
        if detail is not None:
            e["detail"] = detail
        else:
            e.pop("detail", None)

    def record_not_found(self, key, reason, unit, now, detail=None):
        e = self._entry(key, unit)
        e.update(value=None, fetched_at=now, last_attempt_at=now, last_error=reason)
        if detail is not None:
            e["detail"] = detail
        else:
            e.pop("detail", None)

    def record_failure(self, key, code, unit, now):
        """A failed call: the cached value stays in use."""
        e = self._entry(key, unit)
        e.update(last_attempt_at=now, last_error=code)


# =============================================================================
# The run
# =============================================================================
class Deps:
    """Everything that tests replace: the command runner, the clock, the
    expected owner of the output directories, the file-system root (for the
    Slurm lookups), the caller's environment, stdout (for --dry-run) and
    the log stream."""

    def __init__(self, runner=None, clock=None, owner_uid=0, root="/", environ=None, stdout=None,
                 stderr=None):
        self.runner = runner or run_command
        self.clock = clock or Clock()
        self.owner_uid = owner_uid
        self.root = root
        self.environ = environ
        self.stdout = stdout
        self.stderr = stderr


class Run:
    """The state of one run: configuration, time budget, cache and health."""

    def __init__(self, config, deps, dry_run=False):
        self.config = config
        self.deps = deps
        self.dry_run = dry_run
        self.now = deps.clock.wall()              # "now" for every cache and backoff rule
        self.deadline = Deadline(deps.clock)
        environ = deps.environ if deps.environ is not None else os.environ
        self.env = aws_env(environ)
        self.pricing_calls = 0
        self.health = Health()
        self.cache = PriceCache()
        self.ages = {}                            # price source -> oldest fetched_at used
        self.calls = []                           # for last-run.json
        self.deferred = []
        self._logged_backoff = set()
        self.state = State(cluster_identity(config))   # the accumulators (state.json)
        self.state_load = StateLoad()
        self.slurm = None                         # what the slurm stage read, for last-run.json

    # --- AWS ------------------------------------------------------------------
    def aws(self, args, needed=None, required=False):
        """One aws call. Pricing goes to us-east-1, everything else to REGION."""
        what = " ".join(args[:2])
        region = PRICING_REGION if args[0] == "pricing" else self.config.region
        argv = ["aws"] + list(args) + ["--region", region, "--output", "json"] + list(AWS_TIMEOUT_ARGS)
        if required:
            timeout = self.deadline.required_timeout()
            if timeout is None:
                LOG.warning("%s skipped: run deadline", what)
                self.calls.append({"call": what, "code": "Timeout", "skipped": True})
                return AwsResult(code="Timeout", message="%s skipped: run deadline" % what)
        else:
            timeout = CALL_TIMEOUT
        if args[0] == "pricing":
            self.pricing_calls += 1
        result = self.deps.runner(argv, timeout, self.env)
        code = classify_aws_error(result, needed)
        self.calls.append({"call": what, "code": code, "seconds": round(result.duration, 3)})
        if code is None:
            return AwsResult(data=json.loads(result.stdout))
        stderr = " ".join((result.stderr or "").split())[:200]
        if result.timed_out:
            stderr = "killed after %.0f s" % timeout
        elif code == "InvalidResponse":
            stderr = "the response lacks what the call needs"
        LOG.warning("%s failed: %s: %s", what, code, stderr)
        return AwsResult(code=code, message="%s: %s" % (what, stderr))

    def optional_gate(self, source, stage, what):
        """None when an optional call to source can start now. Otherwise
        'backoff' (the stage is blocked), 'budget' or 'time' (deferred)."""
        if self.cache.backed_off(source, self.now):
            b = self.cache.backoff[source]
            stage.source_blocked(source, b["code"], b["until"])
            if source not in self._logged_backoff:
                self._logged_backoff.add(source)
                LOG.info("source %s backed off until %s (%s)", source, utc_iso(b["until"]), b["code"])
            return "backoff"
        why = None
        if source == "pricing" and self.pricing_calls >= PRICING_CALL_BUDGET:
            why = "budget"
        elif not self.deadline.optional_allowed():
            why = "time"
        if why is not None:
            self.deferred.append({"call": what, "why": why})
            LOG.info("%s deferred: %s", what,
                     "Pricing call budget used up" if why == "budget" else "less than 30 s left")
        return why

    def call_failed(self, source, code):
        if code in SOURCE_CODES:
            self.cache.set_backoff(source, code, self.now)

    def used(self, source, fetched_at):
        """Remember the oldest cached value used, per price source."""
        if fetched_at is None:
            return
        old = self.ages.get(source)
        if old is None or fetched_at < old:
            self.ages[source] = fetched_at


def run_stage(run, name, func, *args):
    """Run one stage. An unexpected exception fails it with InternalError."""
    stage = run.health.stage(name)
    try:
        return func(run, stage, *args)
    except Exception:
        LOG.exception("stage %s: internal error", name)
        stage.internal_error()
        return None


# =============================================================================
# Discovery and classification
# =============================================================================
DISCOVERY_QUERY = (
    "Reservations[].Instances[].{InstanceId:InstanceId,InstanceType:InstanceType,"
    "Az:Placement.AvailabilityZone,Tenancy:Placement.Tenancy,Lifecycle:InstanceLifecycle,"
    "UsageOperation:UsageOperation,PlatformDetails:PlatformDetails,PrivateIp:PrivateIpAddress,"
    "PrivateDns:PrivateDnsName,Tags:Tags}"
)
PC_ROLES = {"HeadNode": "head", "LoginNode": "login", "Compute": "compute"}
PCS_GROUP_TAG = "aws:pcs:compute-node-group-id"


class Instance:
    """A running cluster instance that passed validation."""

    def __init__(self, instance_id, instance_type, az, tenancy, usage_operation, purchase_option,
                 role, queue=None, node_group_id=None, pcs_managed=False, private_ip=None,
                 private_dns=None, platform_details=None):
        self.instance_id = instance_id
        self.instance_type = instance_type
        self.az = az
        self.tenancy = tenancy
        self.usage_operation = usage_operation
        self.purchase_option = purchase_option
        self.role = role
        self.queue = queue
        self.node_group_id = node_group_id
        self.pcs_managed = pcs_managed
        self.private_ip = private_ip
        self.private_dns = private_dns
        self.platform_details = platform_details

    def __repr__(self):
        return "Instance(%s, %s, %s, %s)" % (self.instance_id, self.role, self.instance_type, self.purchase_option)


class Discovery:
    def __init__(self, instances, invalid):
        self.instances = instances      # sorted by instance ID
        self.invalid = invalid          # records counted as unpriced invalid_record


def discovery_tag_keys(config):
    if config.platform == "parallelcluster":
        return ["parallelcluster:cluster-name"]
    return ["aws:pcs:cluster-id", "pcs-cluster-id"]


def parse_tags(raw):
    tags = {}
    if isinstance(raw, list):
        for t in raw:
            if isinstance(t, dict) and isinstance(t.get("Key"), str) and isinstance(t.get("Value"), str):
                tags[t["Key"]] = t["Value"]
    return tags


def parse_instance(rec, tags, platform):
    """(Instance, None), (None, 'invalid') or (None, 'unknown node type')."""
    iid, itype, az = rec.get("InstanceId"), rec.get("InstanceType"), rec.get("Az")
    if not isinstance(iid, str) or not INSTANCE_ID_RE.match(iid):
        return None, "invalid"
    if not isinstance(itype, str) or not INSTANCE_TYPE_RE.match(itype):
        return None, "invalid"
    if not isinstance(az, str) or not AZ_RE.match(az):
        return None, "invalid"
    op = rec.get("UsageOperation")
    if op is None:
        op = "RunInstances"
    elif not isinstance(op, str) or not USAGE_OPERATION_RE.match(op):
        return None, "invalid"
    tenancy = rec.get("Tenancy")
    if tenancy is None:
        tenancy = "default"
    elif tenancy not in ("default", "dedicated", "host"):
        return None, "invalid"
    lifecycle = rec.get("Lifecycle")
    option = {"spot": "spot", "capacity-block": "capacity_block"}.get(lifecycle, "on_demand")
    ip = rec.get("PrivateIp") if isinstance(rec.get("PrivateIp"), str) else None
    dns = rec.get("PrivateDns") if isinstance(rec.get("PrivateDns"), str) else None
    details = rec.get("PlatformDetails") if isinstance(rec.get("PlatformDetails"), str) else None
    common = dict(instance_id=iid, instance_type=itype, az=az, tenancy=tenancy, usage_operation=op,
                  purchase_option=option, private_ip=ip, private_dns=dns, platform_details=details)
    if platform == "parallelcluster":
        role = PC_ROLES.get(tags.get("parallelcluster:node-type"))
        if role is None:
            return None, "unknown node type"
        queue = (tags.get("parallelcluster:queue-name") or "unknown") if role == "compute" else None
        return Instance(role=role, queue=queue, **common), None
    group = tags.get(PCS_GROUP_TAG) or None
    role = "login" if tags.get("monitoring-role") == "login" or group is None else "compute"
    return Instance(role=role, node_group_id=group, pcs_managed=group is not None, **common), None


def discover(run, stage):
    """Step 3: the running cluster instances, classified by role.

    One call on ParallelCluster, two on PCS (merged by InstanceId). If a
    call fails, the stage fails with its code and no partial fleet is used.
    """
    seen = set()
    instances = {}
    invalid = 0
    tagged = 0
    for tag_key in discovery_tag_keys(run.config):
        res = run.aws(["ec2", "describe-instances", "--filters",
                       "Name=tag:%s,Values=%s" % (tag_key, run.config.cluster_id),
                       "Name=instance-state-name,Values=running", "--query", DISCOVERY_QUERY],
                      needed=_is_list, required=True)
        if not res.ok:
            stage.call_failed(res.code, res.message)
            LOG.error("discovery failed: %s", res.message)
            return None
        for rec in res.data:
            if not isinstance(rec, dict):
                tagged += 1
                invalid += 1
                LOG.warning("describe-instances returned a record that isn't an object: skipped")
                continue
            tags = parse_tags(rec.get("Tags"))
            iid = rec.get("InstanceId")
            if tags.get(tag_key) != run.config.cluster_id:
                LOG.warning("instance %s doesn't carry the tag %s=%s: skipped",
                            str(iid)[:40], tag_key, run.config.cluster_id)
                continue
            if isinstance(iid, str):
                if iid in seen:
                    continue
                seen.add(iid)
            tagged += 1
            inst, why = parse_instance(rec, tags, run.config.platform)
            if inst is None:
                if why == "invalid":
                    invalid += 1
                    LOG.warning("instance record %s fails validation: skipped (invalid_record)", str(iid)[:40])
                else:
                    LOG.warning("instance %s has an unknown node type %r: skipped", iid,
                                tags.get("parallelcluster:node-type", "")[:64])
                continue
            instances[inst.instance_id] = inst
    if tagged == 0:
        stage.fail("NoInstancesFound", "no running instance carries the cluster's tag")
        LOG.error("discovery failed: no running instance carries the cluster's tag (NoInstancesFound)")
        return None
    return Discovery([instances[k] for k in sorted(instances)], invalid)


# =============================================================================
# EBS volumes (the required part of stage ebs)
# =============================================================================
class Volumes:
    def __init__(self, records, unavailable, invalid):
        self.records = records          # instance ID -> {"fetched_at", "volumes"}
        self.unavailable = unavailable  # running instances without usable records
        self.invalid = invalid          # volume records counted as unpriced invalid_record


def _slim_volume(v):
    return {"VolumeId": v["VolumeId"], "VolumeType": v["VolumeType"], "Size": v["Size"],
            "Iops": v.get("Iops"), "Throughput": v.get("Throughput")}


def list_volumes(run, stage, instances):
    """Step 4: the volumes attached to the running instances, one call per
    100 instances. Each instance keeps its own records: this run's when its
    chunk's call succeeded, otherwise its cached ones while at most one hour
    old. Instances that aren't running are dropped from the cache."""
    entry = run.cache.get("volumes") or {}
    cached = entry.get("value") or {}
    ids = sorted(i.instance_id for i in instances)
    discovered = set(ids)
    records, unavailable, invalid = {}, [], 0
    first_code = None
    for start in range(0, len(ids), VOLUME_CHUNK):
        chunk = ids[start:start + VOLUME_CHUNK]
        res = run.aws(["ec2", "describe-volumes", "--filters",
                       "Name=attachment.instance-id,Values=" + ",".join(chunk)],
                      needed=_has_list("Volumes"), required=True)
        if res.ok:
            found = {iid: [] for iid in chunk}
            seen = set()
            for v in res.data["Volumes"]:
                if not valid_volume_record(v) or v.get("State") != "in-use":
                    invalid += 1
                    LOG.warning("volume record %s fails validation: skipped (invalid_record)",
                                str(v.get("VolumeId") if isinstance(v, dict) else v)[:40])
                    continue
                if v["VolumeId"] in seen:
                    continue
                seen.add(v["VolumeId"])
                owner = None
                for a in v.get("Attachments") or []:
                    if isinstance(a, dict) and a.get("InstanceId") in discovered:
                        owner = a["InstanceId"]
                        break
                if owner in found:
                    found[owner].append(_slim_volume(v))
            for iid in chunk:
                records[iid] = {"fetched_at": run.now, "volumes": found[iid]}
        else:
            first_code = first_code or res.code
            stage.call_failed(res.code, res.message)
            for iid in chunk:
                rec = cached.get(iid)
                if rec is not None and run.now - rec["fetched_at"] <= VOLUME_RECORD_MAX_AGE:
                    records[iid] = rec
                else:
                    unavailable.append(iid)
    new = {"value": records, "unit": "", "last_attempt_at": run.now, "last_error": first_code,
           "fetched_at": run.now if first_code is None else entry.get("fetched_at")}
    run.cache.entries["volumes"] = new
    for rec in records.values():
        run.used("volumes", rec["fetched_at"])
    if unavailable:
        LOG.warning("volumes of %d running instance(s) couldn't be listed: left out (volume_list_unavailable)",
                    len(unavailable))
    return Volumes(records, unavailable, invalid)


# =============================================================================
# Slurm (stage slurm)
# =============================================================================
def unwrap_number(value):
    """A Slurm number: plain, or the number of a {"set", "infinite", "number"}
    object, which is None when unset or infinite."""
    if isinstance(value, dict) and "number" in value:
        if value.get("infinite") is True or value.get("set") is False:
            return None
        return value.get("number")
    return value


def slurm_int(value, minimum):
    """An integer >= minimum (unwrapped), or None."""
    value = unwrap_number(value)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        return None
    return value


def state_flags(value):
    """A Slurm state, a string or a list of flags, as a set of flags."""
    if isinstance(value, str):
        return frozenset([value])
    if isinstance(value, list):
        return frozenset(v for v in value if isinstance(v, str))
    return frozenset()


def memory_is_consumable(select_type):
    # v0.0.42-44 (CR_TYPE): ["CORE", "MEMORY"]; v0.0.45 (SELECT_TYPE_PARAM): ["CR_CORE_MEMORY"]
    flags = [select_type] if isinstance(select_type, str) else select_type if isinstance(select_type, list) else []
    return any(isinstance(f, str) and (f == "MEMORY" or f.endswith("_MEMORY")) for f in flags)


def split_gres(text):
    """A GRES string split on the commas outside parentheses."""
    parts, current, depth = [], [], 0
    for ch in text:
        if ch == "(":
            depth += 1
        elif ch == ")" and depth:
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    parts.append("".join(current))
    return [p.strip() for p in parts if p.strip()]


def gpu_count(text):
    """The GPUs in a node's gres or a job's gres_detail entry: gpu:T4:1(S:0)
    is 1, gpu:a100:8(S:0-1) is 8, gpu:a100:2(IDX:0,2),gpu:1g.10gb:2(IDX:5-6) is 4."""
    if not isinstance(text, str):
        return 0
    total = 0
    for item in split_gres(text):
        fields = [f.strip() for f in item.split("(", 1)[0].split(":")]
        if fields[0] != "gpu":
            continue
        if len(fields) > 1 and COUNT_RE.match(fields[-1]):
            total += int(fields[-1])
        elif len(fields) <= 2:
            total += 1                  # "gpu" or "gpu:<type>": one GPU
    return total


def tres_gpus(text):
    """N of gres/gpu=N in a job's tres_alloc_str, or 0."""
    if isinstance(text, str):
        for item in text.split(","):
            name, _, value = item.partition("=")
            if name.strip() == "gres/gpu" and COUNT_RE.match(value.strip()):
                return int(value.strip())
    return 0


def _nested(obj, *path):
    for name in path:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(name)
    return obj


def _text(value):
    return value if isinstance(value, str) and value else None


def _slurm_message(entry):
    text = entry.get("description") if isinstance(entry, dict) else entry
    return " ".join(str(text).split())[:200]


class SlurmNode:
    """A Slurm node: the CPUs (C), memory in MiB (M) and GPUs (G) that jobs can
    use, its mapping keys and its state flags."""

    def __init__(self, name, cpus, memory, gpus, address=None, hostname=None, instance_id=None,
                 states=frozenset()):
        self.name = name
        self.cpus = cpus
        self.memory = memory
        self.gpus = gpus
        self.address = address
        self.hostname = hostname
        self.instance_id = instance_id
        self.states = states

    def __repr__(self):
        return "SlurmNode(%s, C=%d, M=%d, G=%d)" % (self.name, self.cpus, self.memory, self.gpus)


class SlurmShare:
    """One job's allocation on one node (one entry of job_resources.nodes.allocation)."""

    def __init__(self, job_id, username, account, node, cpus, memory, gpus, whole=False, memory_counts=False):
        self.job_id = job_id
        self.username = username
        self.account = account
        self.node = node
        self.cpus = cpus
        self.memory = memory
        self.gpus = gpus
        self.whole = whole
        self.memory_counts = memory_counts


class SlurmData:
    """What the slurm stage read: the client, the nodes, the job allocations
    and the mapping of nodes to instances."""

    def __init__(self, bin_dir=None, found_by=None):
        self.bin_dir = bin_dir
        self.found_by = found_by
        self.data_parser = {}
        self.nodes = {}             # name -> SlurmNode
        self.shares = []            # SlurmShare
        self.jobs = 0
        self.holding = set()        # nodes with a job in RUNNING, CONFIGURING or COMPLETING
        self.busy = set()           # nodes with a RUNNING job that is neither CONFIGURING nor COMPLETING
        self.mapping = {}           # node name -> (Instance, "instance_id" | "address" | "hostname")
        self.busy_unmapped = []


def _rooted(run, path):
    """An absolute path under the injected file-system root (tests use a scratch root)."""
    return (run.deps.root or "/").rstrip("/") + path


def _has_slurm_client(directory):
    for name in SLURM_COMMANDS:
        path = os.path.join(directory, name)
        if not (os.path.isfile(path) and os.access(path, os.X_OK)):
            return False
    return True


def _pcs_slurm_candidates(run):
    """PCS, in order: the node's configured version (the symlink), the
    cluster's scheduler.version from the cached get-cluster result, and the
    only Slurm tree. Values that fail their checks are skipped (DEBUG)."""
    out = []
    link = _rooted(run, PCS_SLURM_LINK)
    if os.path.islink(link):
        try:
            name = os.path.basename(os.readlink(link).rstrip("/"))
        except OSError:
            name = ""
        if SLURM_TREE_RE.match(name):
            out.append(("symlink " + PCS_SLURM_LINK, _rooted(run, "%s/%s/bin" % (PCS_SCHEDULER_DIR, name))))
        else:
            LOG.debug("%s points to %r: skipped", PCS_SLURM_LINK, name[:64])
    value = run.cache.value("pcs|cluster")
    version = value.get("scheduler_version") if isinstance(value, dict) else None
    if isinstance(version, str) and SCHEDULER_VERSION_RE.match(version):
        out.append(("pcs scheduler.version", _rooted(run, "%s/slurm-%s/bin" % (PCS_SCHEDULER_DIR, version))))
    elif version is not None:
        LOG.debug("cached PCS scheduler version %r: skipped", str(version)[:32])
    base = _rooted(run, PCS_SCHEDULER_DIR)
    try:
        trees = sorted(n for n in os.listdir(base) if SLURM_TREE_RE.match(n) and os.path.isdir(os.path.join(base, n)))
    except OSError:
        trees = []
    if len(trees) == 1:
        out.append(("only tree under " + PCS_SCHEDULER_DIR, os.path.join(base, trees[0], "bin")))
    elif trees:
        LOG.debug("%d Slurm trees under %s (%s): none picked", len(trees), PCS_SCHEDULER_DIR, ", ".join(trees))
    return out


def find_slurm_bin(run):
    """(directory, how it was found) of the Slurm client, or (None, None): the
    first candidate of design "Slurm binaries" that holds executable squeue
    and scontrol. No shell is run, and of several PCS trees none is guessed."""
    override = run.config.slurm_bin_dir
    if override is not None:
        if os.path.isabs(override) and _has_slurm_client(override):
            return override, "CLUSTER_COST_SLURM_BIN_DIR"
        LOG.debug("CLUSTER_COST_SLURM_BIN_DIR %s holds no executable squeue and scontrol", override[:200])
        return None, None
    if run.config.platform == "parallelcluster":
        candidates = [(PC_SLURM_BIN, _rooted(run, PC_SLURM_BIN))]
    else:
        candidates = _pcs_slurm_candidates(run)
    environ = run.deps.environ if run.deps.environ is not None else os.environ
    candidates += [("PATH", d) for d in (environ.get("PATH") or "").split(os.pathsep) if os.path.isabs(d)]
    for how, directory in candidates:
        if _has_slurm_client(directory):
            return directory, how
        LOG.debug("no executable squeue and scontrol in %s (%s)", directory, how)
    return None, None


def slurm_command(run, stage, directory, name, args, list_key):
    """Run one Slurm command with the timeout of a required call. The parsed
    JSON object, or None after failing the stage with its code."""
    timeout = run.deadline.required_timeout()
    if timeout is None:
        LOG.warning("%s skipped: run deadline", name)
        run.calls.append({"call": name, "code": "Timeout", "skipped": True})
        stage.fail("Timeout", "%s skipped: run deadline" % name)
        return None
    result = run.deps.runner([os.path.join(directory, name)] + list(args), timeout, run.env)
    stderr = " ".join((result.stderr or "").split())[:200]
    data, code, message = None, None, ""
    if result.not_found:
        code, message = "SlurmNotFound", "%s couldn't be executed: %s" % (name, stderr)
    elif result.timed_out:
        code, message = "Timeout", "%s killed after %.0f s" % (name, timeout)
    elif result.rc != 0:
        code, message = "SlurmCommandFailed", "%s exited with %d: %s" % (name, result.rc, stderr)
    else:
        data = _parse_json(result.stdout)
        if not isinstance(data, dict):
            code, message = "SlurmJsonInvalid", "%s printed no JSON object" % name
        else:
            warnings = data.get("warnings")
            for w in warnings if isinstance(warnings, list) else []:
                LOG.debug("%s warning: %s", name, _slurm_message(w))
            errors = data.get("errors")
            if errors is not None and not isinstance(errors, list):
                code, message = "SlurmJsonInvalid", "%s printed an errors field that isn't a list" % name
            elif errors:
                code, message = "SlurmCommandFailed", "%s reported an error: %s" % (name, _slurm_message(errors[0]))
            elif not isinstance(data.get(list_key), list):
                code, message = "SlurmJsonInvalid", "%s printed no %s list" % (name, list_key)
    run.calls.append({"call": name, "code": code, "seconds": round(result.duration, 3)})
    if code is not None:
        LOG.warning("%s failed: %s: %s", name, code, message)
        stage.fail(code, message)
        return None
    return data


def _data_parser(doc):
    value = _nested(doc, "meta", "plugin", "data_parser")
    return value[:64] if isinstance(value, str) else None


def parse_slurm_node(rec):
    """Allocation step 1 for one record of scontrol show nodes --json: a
    SlurmNode, or None for a record that fails validation (DEBUG)."""
    if not isinstance(rec, dict) or not isinstance(rec.get("name"), str) or not rec["name"]:
        LOG.debug("Slurm node record without a name: skipped")
        return None
    name = rec["name"]
    cpus = slurm_int(rec.get("cpus"), 1)
    effective = rec.get("effective_cpus")
    effective = cpus if unwrap_number(effective) is None else slurm_int(effective, 1)
    real = slurm_int(rec.get("real_memory"), 0)
    special = rec.get("specialized_memory")
    special = 0 if unwrap_number(special) is None else slurm_int(special, 0)
    if cpus is None or effective is None or real is None or special is None:
        LOG.debug("Slurm node %s: invalid CPU or memory figures: skipped", name[:64])
        return None
    return SlurmNode(name, effective, real - special, gpu_count(rec.get("gres")), _text(rec.get("address")),
                     _text(rec.get("hostname")), _text(rec.get("instance_id")), state_flags(rec.get("state")))


def parse_slurm_nodes(records):
    nodes = {}
    for rec in records:
        node = parse_slurm_node(rec)
        if node is not None and node.name not in nodes:
            nodes[node.name] = node
    return nodes


def parse_slurm_jobs(slurm, records):
    """Allocation step 3: the allocation entries of the jobs that hold
    resources, and the nodes that the tie-break and the mapping check need."""
    for job in records:
        if not isinstance(job, dict):
            continue
        states = state_flags(job.get("job_state"))
        if not states & HOLDING_JOB_STATES:
            continue
        resources = job.get("job_resources")
        node_res = resources.get("nodes") if isinstance(resources, dict) else None
        entries = node_res.get("allocation") if isinstance(node_res, dict) else None
        job_id = slurm_int(job.get("job_id"), 0)
        if not isinstance(entries, list):
            LOG.debug("job %s has no node allocation: skipped", job_id)
            continue
        names = [e["name"] for e in entries if isinstance(e, dict) and isinstance(e.get("name"), str)]
        slurm.holding.update(names)
        if "RUNNING" in states and not states & NOT_BUSY_JOB_STATES:
            slurm.busy.update(names)
        user = job.get("user_name")
        account = job.get("account")
        account = "" if account is None else account
        if not isinstance(user, str) or not isinstance(account, str):
            LOG.debug("job %s: user_name or account isn't a string: skipped", job_id)
            continue
        if not user:
            uid = slurm_int(job.get("user_id"), 0)
            if uid is None:
                LOG.debug("job %s: no user name and no valid user ID: skipped", job_id)
                continue
            user = "uid:%d" % uid            # the UID doesn't resolve here; different users don't merge
        username, account = truncate(user), truncate(account or "none")
        whole = node_res.get("whole") is True
        memory_counts = memory_is_consumable(resources.get("select_type"))
        details = job.get("gres_detail")
        per_entry = None
        slurm.jobs += 1
        for position, e in enumerate(entries):
            if not isinstance(e, dict) or not isinstance(e.get("name"), str) or e["name"] not in slurm.nodes:
                LOG.debug("job %s: allocation entry on an unknown node: skipped", job_id)
                continue
            cpus = slurm_int(_nested(e, "cpus", "count"), 0)
            memory = slurm_int(_nested(e, "memory", "allocated"), 0)
            if cpus is None or memory is None:
                LOG.debug("job %s: invalid allocation on %s: skipped", job_id, e["name"][:64])
                continue
            index = slurm_int(e.get("index"), 0)
            index = position if index is None else index
            if isinstance(details, list) and index < len(details):
                gpus = gpu_count(details[index])
            else:
                if per_entry is None:       # no gres_detail: gres/gpu=N of tres_alloc_str per node
                    per_entry = tres_gpus(job.get("tres_alloc_str")) / float(len(entries))
                gpus = per_entry
            slurm.shares.append(SlurmShare(job_id, username, account, e["name"], cpus, memory, gpus, whole,
                                           memory_counts))


def map_slurm_nodes(nodes, instances, holding):
    """D6 and Allocation step 2: each Slurm node's instance by instance_id,
    then address (the private IP), then hostname (the private DNS name or
    its first label). When several nodes map to one instance, the one
    matched by instance_id is kept; among equal matches, the one with jobs;
    then the lowest name. The others are unmapped."""
    by_id, by_ip, by_host = {}, {}, {}
    for inst in instances:
        by_id[inst.instance_id] = inst
        if inst.private_ip:
            by_ip.setdefault(inst.private_ip, inst)
        if inst.private_dns:
            by_host.setdefault(inst.private_dns, inst)
            by_host.setdefault(inst.private_dns.split(".", 1)[0], inst)
    claims = {}
    for name in sorted(nodes):
        node = nodes[name]
        if node.instance_id and node.instance_id in by_id:
            inst, how = by_id[node.instance_id], "instance_id"
        elif node.address and node.address in by_ip:
            inst, how = by_ip[node.address], "address"
        elif node.hostname and node.hostname in by_host:
            inst, how = by_host[node.hostname], "hostname"
        else:
            continue
        claims.setdefault(inst.instance_id, []).append((name, inst, how))
    mapping = {}
    for iid in sorted(claims):
        pool = claims[iid]
        if len(pool) > 1:
            pool = [c for c in pool if c[2] == "instance_id"] or pool
            with_jobs = [c for c in pool if c[0] in holding]
            if len(with_jobs) > 1:
                LOG.warning("Slurm nodes %s map to instance %s and all hold jobs: %s kept, the others unmapped",
                            ", ".join(c[0] for c in with_jobs), iid, with_jobs[0][0])
            pool = with_jobs or pool
        name, inst, how = pool[0]           # the lowest name: claims are in name order
        mapping[name] = (inst, how)
    return mapping


def read_slurm(run, stage, instances):
    """Step 5: resolve the Slurm client, read nodes and jobs, map the nodes
    to the running instances and run the mapping check (Allocation step 8)."""
    directory, how = find_slurm_bin(run)
    slurm = SlurmData(directory, how)
    run.slurm = slurm
    if directory is None:
        message = "no directory with executable squeue and scontrol"
        LOG.warning("slurm failed: SlurmNotFound: %s", message)
        stage.fail("SlurmNotFound", message)
        return None
    nodes_doc = slurm_command(run, stage, directory, "scontrol", SCONTROL_ARGS, "nodes")
    if nodes_doc is None:
        return None
    jobs_doc = slurm_command(run, stage, directory, "squeue", SQUEUE_ARGS, "jobs")
    if jobs_doc is None:
        return None
    slurm.data_parser = {"scontrol": _data_parser(nodes_doc), "squeue": _data_parser(jobs_doc)}
    slurm.nodes = parse_slurm_nodes(nodes_doc["nodes"])
    parse_slurm_jobs(slurm, jobs_doc["jobs"])
    slurm.mapping = map_slurm_nodes(slurm.nodes, instances, slurm.holding)
    slurm.busy_unmapped = sorted(n for n in slurm.busy if n in slurm.nodes and n not in slurm.mapping
                                 and not slurm.nodes[n].states & NOT_BUSY_NODE_STATES)
    if slurm.busy_unmapped:
        names = ", ".join(slurm.busy_unmapped[:10])
        if len(slurm.busy_unmapped) > 10:
            names += " and %d more" % (len(slurm.busy_unmapped) - 10)
        message = "busy Slurm nodes map to no running cluster instance: " + names
        LOG.warning("slurm failed: NodeMappingIncomplete: %s", message)
        stage.fail("NodeMappingIncomplete", message)
    return slurm


# =============================================================================
# Pricing
# =============================================================================
class PriceItem:
    """One product of a get-products response, with its usable dimensions."""

    def __init__(self, sku, family, attributes, dimensions):
        self.sku = sku
        self.family = family
        self.attributes = attributes
        self.dimensions = dimensions    # [(unit, price)]


def parse_price(value):
    """A price as a finite float >= 0, or None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        x = float(value)
    elif isinstance(value, str):
        try:
            x = float(value)
        except ValueError:
            return None
    else:
        return None
    if not math.isfinite(x) or x < 0:
        return None
    return x


def parse_price_list(data):
    """The PriceList of a get-products response. Each entry is a JSON string.
    Dimensions whose price isn't a finite number >= 0 are ignored."""
    items = []
    for raw in data.get("PriceList") or []:
        doc = raw
        if isinstance(raw, str):
            try:
                doc = json.loads(raw)
            except ValueError:
                continue
        if not isinstance(doc, dict):
            continue
        product = doc.get("product") if isinstance(doc.get("product"), dict) else {}
        attrs = product.get("attributes") if isinstance(product.get("attributes"), dict) else {}
        sku = product.get("sku") if isinstance(product.get("sku"), str) else ""
        dims = []
        terms = doc.get("terms") if isinstance(doc.get("terms"), dict) else {}
        on_demand = terms.get("OnDemand") if isinstance(terms.get("OnDemand"), dict) else {}
        for term in on_demand.values():
            pds = term.get("priceDimensions") if isinstance(term, dict) else None
            if not isinstance(pds, dict):
                continue
            for dim in pds.values():
                if not isinstance(dim, dict):
                    continue
                ppu = dim.get("pricePerUnit")
                price = parse_price(ppu.get("USD") if isinstance(ppu, dict) else None)
                if isinstance(dim.get("unit"), str) and price is not None:
                    dims.append((dim["unit"], price))
        items.append(PriceItem(sku, product.get("productFamily"), attrs, dims))
    return items


def select_price(items, units):
    """Apply the selection rule: (price, None, skus) for one distinct price,
    (None, 'not_in_price_list', []) for none, (None, 'ambiguous_price', skus)
    for several. `units` maps the expected units, in lower case, to the
    factor that converts their price to the item's unit."""
    prices = {}
    for it in items:
        for unit, price in it.dimensions:
            factor = units.get(unit.lower())
            if factor is None:
                continue
            prices.setdefault(round(price * factor, 12), set()).add(it.sku)
    if not prices:
        return None, "not_in_price_list", []
    skus = sorted({s for group in prices.values() for s in group})
    if len(prices) > 1:
        return None, "ambiguous_price", skus
    return next(iter(prices)), None, skus


def pricing_call(run, service_code, filters):
    args = ["pricing", "get-products", "--service-code", service_code, "--filters"]
    args += ["Type=TERM_MATCH,Field=%s,Value=%s" % (field, value) for field, value in filters]
    return run.aws(args, needed=_has_list("PriceList"))


def record_selection(run, key, unit, price, reason, skus, warn=True):
    if reason is None:
        run.cache.record_success(key, price, unit, run.now, detail={"sku": skus[0] if len(skus) == 1 else skus})
        return
    run.cache.record_not_found(key, reason, unit, run.now, detail={"skus": skus} if skus else None)
    if warn:
        if reason == "ambiguous_price":
            LOG.warning("%s: several distinct prices in the price list (SKUs %s): unpriced (ambiguous_price)",
                        key, ", ".join(skus))
        else:
            LOG.warning("%s: no product in the price list: unpriced (%s)", key, reason)


def note_stored(run, stage, keys):
    """Needed entries whose last attempt failed keep their stage failed."""
    for key in keys:
        e = run.cache.get(key)
        if e is not None and e.get("last_error") in AWS_CODES:
            stage.stored_failure(key, e["last_error"])


def unpriced_reason(run, entry, source):
    """Why a key priced from the Pricing API has no usable value."""
    err = entry.get("last_error") if entry is not None else None
    if err in AWS_CODES:
        return "price_api_error"
    if err in NOT_FOUND_REASONS:
        return err
    if run.cache.backed_off(source, run.now):
        return "price_api_error"
    return "price_lookup_pending"


def cached_price(run, key, age_source):
    """(value, None) or (None, reason). Pricing API values are used at any age."""
    e = run.cache.get(key)
    if e is not None and e.get("value") is not None:
        run.used(age_source, e.get("fetched_at"))
        return e["value"], None
    return None, unpriced_reason(run, e, "pricing")


# --- On-Demand ---------------------------------------------------------------
OD_UNITS = {"hrs": 1.0}
SPOT_DESCRIPTIONS = {
    "RunInstances": "Linux/UNIX",
    "RunInstances:0010": "Red Hat Enterprise Linux",
    "RunInstances:000g": "SUSE Linux",
}


def price_kind(inst):
    if inst.tenancy == "host":
        return "dedicated_host"
    if inst.purchase_option == "capacity_block":
        return "capacity_block"
    if inst.purchase_option == "spot":
        return "spot"
    return "on_demand"


def od_key(inst):
    tenancy = "Dedicated" if inst.tenancy == "dedicated" else "Shared"
    return "od|%s|%s|%s" % (inst.instance_type, inst.usage_operation, tenancy)


def refresh_on_demand(run, stage, instances):
    """Step 6.1: missing keys first, then due refreshes, within the budget."""
    keys = sorted({od_key(i) for i in instances if price_kind(i) == "on_demand"})
    for key in sorted(keys, key=lambda k: (k in run.cache.entries, k)):
        if not run.cache.due(key, OD_INTERVALS, run.now):
            continue
        if run.optional_gate("pricing", stage, "pricing get-products " + key) is not None:
            continue
        _, itype, operation, tenancy = key.split("|")
        res = pricing_call(run, "AmazonEC2", [
            ("regionCode", run.config.region), ("instanceType", itype), ("operation", operation),
            ("tenancy", tenancy), ("capacitystatus", "Used")])
        if not res.ok:
            run.cache.record_failure(key, res.code, "USD/h", run.now)
            stage.call_failed(res.code, res.message)
            run.call_failed("pricing", res.code)
            continue
        price, reason, skus = select_price(parse_price_list(res.data), OD_UNITS)
        record_selection(run, key, "USD/h", price, reason, skus)
    note_stored(run, stage, keys)


# --- Spot ----------------------------------------------------------------------
def spot_key(inst):
    desc = SPOT_DESCRIPTIONS.get(inst.usage_operation)
    if desc is None:
        return None
    return "spot|%s|%s|%s" % (inst.instance_type, inst.az, desc)


def refresh_spot(run, stage, instances):
    """Step 6.2: at most one Spot call per run, covering every running pair.

    Due 5 minutes after the last successful call, or at once when a running
    pair has no entry; after a failed call, 5 minutes after it, new pairs
    included.
    """
    keys = sorted({k for k in (spot_key(i) for i in instances if price_kind(i) == "spot") if k})
    if not keys:
        return
    cache = run.cache
    refresh = cache.get("spot|refresh")
    if refresh is not None and refresh.get("last_error") in AWS_CODES:
        due = run.now - refresh["last_attempt_at"] >= SPOT_REFRESH
    else:
        last_ok = refresh.get("fetched_at") if refresh is not None else None
        due = (last_ok is None or run.now - last_ok >= SPOT_REFRESH
               or any(k not in cache.entries for k in keys))
    if due and run.optional_gate("spot", stage, "ec2 describe-spot-price-history") is None:
        query_time = utc_iso(run.now)
        parts = [k.split("|") for k in keys]
        types = sorted({p[1] for p in parts})
        azs = sorted({p[2] for p in parts})
        descs = sorted({p[3] for p in parts})
        res = run.aws(["ec2", "describe-spot-price-history", "--start-time", query_time,
                       "--end-time", query_time, "--instance-types"] + types
                      + ["--product-descriptions"] + descs
                      + ["--filters", "Name=availability-zone,Values=" + ",".join(azs)],
                      needed=_has_list("SpotPriceHistory"))
        if res.ok:
            latest = {}
            for e in res.data["SpotPriceHistory"]:
                if not isinstance(e, dict):
                    continue
                fields = [e.get(f) for f in ("InstanceType", "AvailabilityZone", "ProductDescription")]
                ts = parse_iso8601(e.get("Timestamp"))
                price = parse_price(e.get("SpotPrice"))
                if not all(isinstance(f, str) for f in fields) or ts is None or price is None:
                    continue
                k = "spot|%s|%s|%s" % tuple(fields)
                if k not in latest or ts > latest[k][0]:
                    latest[k] = (ts, price, e["Timestamp"])
            for k in keys:
                if k in latest:
                    cache.record_success(k, latest[k][1], "USD/h", run.now,
                                         detail={"price_timestamp": latest[k][2], "query_time": query_time})
                else:
                    cache.record_not_found(k, "spot_price_unavailable", "USD/h", run.now,
                                           detail={"query_time": query_time})
                    LOG.warning("%s: no Spot price in the response: unpriced (spot_price_unavailable)", k)
            cache.record_success("spot|refresh", None, "", run.now)
        else:
            for k in keys:
                cache.record_failure(k, res.code, "USD/h", run.now)
            cache.record_failure("spot|refresh", res.code, "", run.now)
            stage.call_failed(res.code, res.message)
            run.call_failed("spot", res.code)
    note_stored(run, stage, keys + ["spot|refresh"])


def spot_price(run, key):
    """(value, None) or (None, reason). Spot values are used while under 6 h old."""
    cache = run.cache
    e = cache.get(key)
    if e is not None and e.get("value") is not None and e.get("fetched_at") is not None \
            and run.now - e["fetched_at"] < SPOT_MAX_AGE:
        run.used("spot", e["fetched_at"])
        return e["value"], None
    if e is not None or cache.backed_off("spot", run.now) or cache.failed("spot|refresh"):
        return None, "spot_price_unavailable"
    return None, "price_lookup_pending"


def instance_price(run, inst):
    """(rate, reason, key) for one instance."""
    kind = price_kind(inst)
    if kind in ("dedicated_host", "capacity_block"):
        return None, kind, None
    if kind == "spot":
        key = spot_key(inst)
        if key is None:
            return None, "spot_platform_unsupported", None
        rate, reason = spot_price(run, key)
        return rate, reason, key
    key = od_key(inst)
    rate, reason = cached_price(run, key, "on_demand")
    return rate, reason, key


# --- EBS -----------------------------------------------------------------------
EBS_STORAGE_TYPES = ("gp2", "gp3", "io1", "io2", "st1", "sc1", "standard")
EBS_IOPS_ITEMS = (
    ("ebs|iops|gp3", "gp3", "EBS IOPS"),
    ("ebs|iops|io1", "io1", "EBS IOPS"),
    ("ebs|iops|io2|1", "io2", "EBS IOPS"),
    ("ebs|iops|io2|2", "io2", "EBS IOPS Tier 2"),
    ("ebs|iops|io2|3", "io2", "EBS IOPS Tier 3"),
)
EBS_FAMILIES = (("storage", "Storage"), ("iops", "System Operation"), ("throughput", "Provisioned Throughput"))
EBS_UNITS = {
    "storage": ({"gb-mo": 1.0, "gb-month": 1.0}, "USD/GB-month"),
    "iops": ({"iops-mo": 1.0}, "USD/IOPS-month"),
    "throughput": ({"gibps-mo": 1.0 / 1024, "mibps-mo": 1.0}, "USD/MiBps-month"),
}


def volume_items(v):
    """The EBS price items a volume's rate needs: storage, then the provisioned
    IOPS and throughput above the free baseline."""
    vtype = v["VolumeType"]
    iops = v.get("Iops") or 0
    throughput = v.get("Throughput") or 0
    items = ["ebs|storage|" + vtype]
    if vtype == "gp3":
        if iops > 3000:
            items.append("ebs|iops|gp3")
        if throughput > 125:
            items.append("ebs|throughput|gp3")
    elif vtype == "io1" and iops > 0:
        items.append("ebs|iops|io1")
    elif vtype == "io2":
        if iops > 0:
            items.append("ebs|iops|io2|1")
        if iops > 32000:
            items.append("ebs|iops|io2|2")
        if iops > 64000:
            items.append("ebs|iops|io2|3")
    return items


def _ebs_selection(items, family, key):
    """Select the products of one EBS item from its family's response."""
    family_name = dict(EBS_FAMILIES)[family]
    in_family = [it for it in items if it.family == family_name]
    if family == "storage":
        vtype = key.split("|")[2]
        if vtype not in EBS_STORAGE_TYPES:
            return []
        chosen = [it for it in in_family if it.attributes.get("volumeApiName") == vtype]
        single = [it for it in chosen if it.attributes.get("ebsAzConfiguration") == "single-az"]
        return single or chosen
    if family == "iops":
        _, vtype, group = [x for x in EBS_IOPS_ITEMS if x[0] == key][0]
        return [it for it in in_family
                if it.attributes.get("volumeApiName") == vtype and it.attributes.get("group") == group]
    return [it for it in in_family if it.attributes.get("volumeApiName") == "gp3"]


def _ebs_family_keys(family, needed):
    if family == "storage":
        known = ["ebs|storage|" + t for t in EBS_STORAGE_TYPES]
        return sorted(set(known) | {k for k in needed if k.startswith("ebs|storage|")})
    if family == "iops":
        return [x[0] for x in EBS_IOPS_ITEMS]
    return ["ebs|throughput|gp3"]


def refresh_ebs_prices(run, stage, volumes):
    """Step 6.3: EBS prices, one call per product family when it is due."""
    needed = sorted({k for v in volumes for k in volume_items(v)})
    for family, family_name in EBS_FAMILIES:
        fam_needed = [k for k in needed if k.split("|")[1] == family]
        if not any(run.cache.due(k, EBS_INTERVALS, run.now) for k in fam_needed):
            continue
        if run.optional_gate("pricing", stage, "pricing get-products EBS " + family_name) is not None:
            continue
        units, unit_name = EBS_UNITS[family]
        res = pricing_call(run, "AmazonEC2", [("regionCode", run.config.region), ("productFamily", family_name)])
        if not res.ok:
            for k in fam_needed:
                run.cache.record_failure(k, res.code, unit_name, run.now)
            stage.call_failed(res.code, res.message)
            run.call_failed("pricing", res.code)
            continue
        items = parse_price_list(res.data)
        for k in _ebs_family_keys(family, needed):
            price, reason, skus = select_price(_ebs_selection(items, family, k), units)
            record_selection(run, k, unit_name, price, reason, skus, warn=k in fam_needed)
    note_stored(run, stage, needed)


def volume_rate(run, v, hours):
    """(USD per hour, None) or (None, reason) for one volume."""
    prices = {}
    for k in volume_items(v):
        price, reason = cached_price(run, k, "ebs")
        if price is None:
            if reason == "not_in_price_list" and k.startswith("ebs|storage|"):
                reason = "unknown_volume_type"
            return None, reason
        prices[k] = price
    vtype = v["VolumeType"]
    iops = v.get("Iops") or 0
    throughput = v.get("Throughput") or 0
    monthly = v["Size"] * prices["ebs|storage|" + vtype]
    if vtype == "gp3":
        monthly += max(0, iops - 3000) * prices.get("ebs|iops|gp3", 0.0)
        monthly += max(0, throughput - 125) * prices.get("ebs|throughput|gp3", 0.0)
    elif vtype == "io1":
        monthly += iops * prices.get("ebs|iops|io1", 0.0)
    elif vtype == "io2":
        monthly += (min(iops, 32000) * prices.get("ebs|iops|io2|1", 0.0)
                    + min(max(iops - 32000, 0), 32000) * prices.get("ebs|iops|io2|2", 0.0)
                    + max(iops - 64000, 0) * prices.get("ebs|iops|io2|3", 0.0))
    return monthly / hours, None


# --- PCS -----------------------------------------------------------------------
PCS_SIZES = ("SMALL", "MEDIUM", "LARGE")
PCS_FEE_KEYS = (["pcs|controller|" + s for s in PCS_SIZES] + ["pcs|accounting|" + s for s in PCS_SIZES]
                + ["pcs|node|std", "pcs|node|adv"])
PCS_FEE_UNITS = {"hrs": 1.0, "hour": 1.0, "hours": 1.0}
PCS_ACTIVE_STATES = ("ACTIVE", "UPDATING", "UPDATE_FAILED")


def node_tier(instance_type):
    """advanced for the P and Trn instance families, standard otherwise."""
    return "adv" if ADVANCED_TIER_RE.match(instance_type.split(".")[0]) else "std"


def pcs_fee_key(attrs):
    """The fee a product of the AWSPCS price list is, from its attributes."""
    size = attrs.get("controllersize")
    if isinstance(size, str) and size.upper() in PCS_SIZES:
        if attrs.get("controllerstate") == "Running":
            return "pcs|controller|" + size.upper()
        if attrs.get("dbstate") == "Running":
            return "pcs|accounting|" + size.upper()
    nodetype = attrs.get("nodetype")
    if nodetype == "SlurmStd":
        return "pcs|node|std"
    if nodetype == "SlurmAdv":
        return "pcs|node|adv"
    return None


def parse_pcs_cluster(c):
    """(value, None) or (None, problem) for the cluster object of get-cluster."""
    size = c.get("size")
    size = size.upper() if isinstance(size, str) else None
    if size not in PCS_SIZES:
        return None, "size %r" % (c.get("size"),)
    if not isinstance(c.get("status"), str):
        return None, "status %r" % (c.get("status"),)
    slurm = c.get("slurmConfiguration") if isinstance(c.get("slurmConfiguration"), dict) else {}
    accounting = slurm.get("accounting") if isinstance(slurm.get("accounting"), dict) else {}
    mode = accounting.get("mode", "NONE")
    if mode is None:
        mode = "NONE"
    if mode not in ("STANDARD", "NONE"):
        return None, "accounting mode %r" % (mode,)
    scheduler = c.get("scheduler") if isinstance(c.get("scheduler"), dict) else {}
    version = scheduler.get("version")
    if not isinstance(version, str) or not SCHEDULER_VERSION_RE.match(version):
        if version is not None:
            LOG.warning("pcs get-cluster: scheduler version %r not used", str(version)[:32])
        version = None
    return {"size": size, "status": c["status"], "accounting_mode": mode, "scheduler_version": version}, None


def pcs_cluster(run):
    """(value, usable): the last get-cluster result, usable while under 24 h old."""
    e = run.cache.get("pcs|cluster")
    if e is None or e.get("value") is None:
        return None, False
    usable = e.get("fetched_at") is not None and run.now - e["fetched_at"] < PCS_CLUSTER_MAX_AGE
    return e["value"], usable


def pcs_needed_fees(run, instances):
    value, usable = pcs_cluster(run)
    keys = set()
    if usable and value["status"] in PCS_ACTIVE_STATES:
        keys.add("pcs|controller|" + value["size"])
        if value["accounting_mode"] == "STANDARD":
            keys.add("pcs|accounting|" + value["size"])
    keys.update("pcs|node|" + node_tier(i.instance_type) for i in instances if i.pcs_managed)
    return sorted(keys)


def refresh_pcs(run, stage, instances):
    """Step 6.4 (PCS only): get-cluster, node group names, the AWSPCS fees."""
    cache, cid = run.cache, run.config.cluster_id
    if cache.due("pcs|cluster", PCS_CLUSTER_INTERVALS, run.now) \
            and run.optional_gate("pcs", stage, "pcs get-cluster") is None:
        res = run.aws(["pcs", "get-cluster", "--cluster-identifier", cid], needed=_has_dict("cluster"))
        if res.ok:
            value, problem = parse_pcs_cluster(res.data["cluster"])
            if value is None:
                LOG.warning("pcs get-cluster failed: InvalidResponse: %s", problem)
                res = AwsResult(code="InvalidResponse", message="pcs get-cluster: " + problem)
            else:
                cache.record_success("pcs|cluster", value, "", run.now)
        if not res.ok:
            cache.record_failure("pcs|cluster", res.code, "", run.now)
            stage.call_failed(res.code, res.message)
            run.call_failed("pcs", res.code)

    groups = cache.get("pcs|node_groups")
    names = (groups or {}).get("value") or {}
    due = cache.due("pcs|node_groups", PCS_NODE_GROUPS_INTERVALS, run.now)
    if not due and groups is not None and groups.get("last_error") is None \
            and any(i.node_group_id and i.node_group_id not in names for i in instances):
        due = run.now - groups["last_attempt_at"] >= PCS_NODE_GROUPS_UNKNOWN_ID
    if due and run.optional_gate("pcs", stage, "pcs list-compute-node-groups") is None:
        res = run.aws(["pcs", "list-compute-node-groups", "--cluster-identifier", cid],
                      needed=_has_list("computeNodeGroups"))
        if res.ok:
            names = {g["id"]: g["name"] for g in res.data["computeNodeGroups"]
                     if isinstance(g, dict) and isinstance(g.get("id"), str) and isinstance(g.get("name"), str)}
            cache.record_success("pcs|node_groups", names, "", run.now)
        else:
            cache.record_failure("pcs|node_groups", res.code, "", run.now)
            stage.call_failed(res.code, res.message)
            run.call_failed("pcs", res.code)

    needed = pcs_needed_fees(run, instances)
    if any(cache.due(k, PCS_FEE_INTERVALS, run.now) for k in needed) \
            and run.optional_gate("pricing", stage, "pricing get-products AWSPCS") is None:
        res = pricing_call(run, "AWSPCS", [("regionCode", run.config.region)])
        if res.ok:
            by_fee = {}
            for it in parse_price_list(res.data):
                k = pcs_fee_key(it.attributes)
                if k is not None:
                    by_fee.setdefault(k, []).append(it)
            for k in PCS_FEE_KEYS:
                price, reason, skus = select_price(by_fee.get(k, []), PCS_FEE_UNITS)
                record_selection(run, k, "USD/h", price, reason, skus, warn=k in needed)
        else:
            for k in needed:
                cache.record_failure(k, res.code, "USD/h", run.now)
            stage.call_failed(res.code, res.message)
            run.call_failed("pricing", res.code)
    note_stored(run, stage, ["pcs|cluster", "pcs|node_groups"] + needed)


# =============================================================================
# Cost lines
# =============================================================================
def truncate(value):
    return value[:LABEL_MAX]


def label_key(labels):
    """A series key: (name, value) pairs sorted by name, values truncated to
    128 characters, absent labels left out. Equal keys are one series."""
    return tuple(sorted((k, truncate(str(v))) for k, v in labels.items() if v is not None))


def instance_labels(inst):
    labels = {"node_role": inst.role, "ec2_instance_type": inst.instance_type,
              "purchase_option": inst.purchase_option}
    if inst.role == "compute":
        labels["queue"] = inst.queue
    return labels


class CostResult:
    """Step 7: cost lines, node costs, unpriced counts and node counts."""

    def __init__(self):
        self.lines = {}        # label key -> USD per hour
        self.nodes = {}        # label key -> running instances
        self.unpriced = {}     # (kind, reason) -> count
        self.node_cost = {}    # compute instance ID -> USD per hour, None when unknown
        self.instances = []    # rows of last-run.json
        self.volumes = []
        self.fees = []

    def add_line(self, labels, rate):
        k = label_key(labels)
        self.lines[k] = self.lines.get(k, 0.0) + rate

    def add_unpriced(self, kind, reason, count=1):
        if count:
            self.unpriced[(kind, reason)] = self.unpriced.get((kind, reason), 0) + count

    @property
    def total(self):
        return sum(self.lines.values())


def _fetched(run, key):
    e = run.cache.get(key) if key else None
    return None if e is None or e.get("fetched_at") is None else utc_iso(e["fetched_at"])


def build_cost_lines(run, discovery, volumes):
    cost = CostResult()
    instances = discovery.instances
    if run.config.platform == "pcs":
        groups = run.cache.get("pcs|node_groups") or {}
        names = groups.get("value") or {}
        for inst in instances:
            if inst.role == "compute":
                inst.queue = names.get(inst.node_group_id) or inst.node_group_id
                if inst.node_group_id in names:
                    run.used("pcs", groups.get("fetched_at"))
    by_id = {i.instance_id: i for i in instances}

    for inst in instances:
        labels = instance_labels(inst)
        k = label_key(labels)
        cost.nodes[k] = cost.nodes.get(k, 0) + 1
        rate, reason, key = instance_price(run, inst)
        if rate is None:
            cost.add_unpriced("instance", reason)
        else:
            cost.add_line(dict(labels, component="instance"), rate)
        if inst.role == "compute":
            cost.node_cost[inst.instance_id] = rate
        row = {"instance_id": inst.instance_id, "role": inst.role, "queue": inst.queue,
               "ec2_instance_type": inst.instance_type, "purchase_option": inst.purchase_option,
               "az": inst.az, "tenancy": inst.tenancy, "usage_operation": inst.usage_operation,
               "private_ip": inst.private_ip, "usd_per_hour": rate, "price_key": key,
               "price_source": {"od": "on_demand", "spot": "spot"}.get((key or "").split("|")[0]),
               "price_fetched_at": _fetched(run, key), "unpriced_reason": reason}
        if run.config.platform == "pcs":
            row["node_group_id"] = inst.node_group_id
        if key and key.startswith("spot|"):
            detail = (run.cache.get(key) or {}).get("detail") or {}
            row["query_time"] = detail.get("query_time")
            row["price_timestamp"] = detail.get("price_timestamp")
        cost.instances.append(row)

    hours = hours_in_month(run.now)
    seen = set()
    for iid in sorted(volumes.records):
        inst = by_id.get(iid)
        if inst is None:
            continue
        for v in volumes.records[iid]["volumes"]:
            if v["VolumeId"] in seen:
                continue
            seen.add(v["VolumeId"])
            rate, reason = volume_rate(run, v, hours)
            if rate is None:
                cost.add_unpriced("volume", reason)
            else:
                cost.add_line(dict(instance_labels(inst), component="ebs"), rate)
                if cost.node_cost.get(iid) is not None:
                    cost.node_cost[iid] += rate
            cost.volumes.append({"volume_id": v["VolumeId"], "instance_id": iid, "volume_type": v["VolumeType"],
                                 "size": v["Size"], "iops": v.get("Iops"), "throughput": v.get("Throughput"),
                                 "usd_per_hour": rate, "unpriced_reason": reason,
                                 "records_fetched_at": utc_iso(volumes.records[iid]["fetched_at"])})
    # Finding 60: an instance whose volumes couldn't be listed counts once.
    cost.add_unpriced("volume", "volume_list_unavailable", len(volumes.unavailable))
    cost.add_unpriced("volume", "invalid_record", volumes.invalid)
    cost.add_unpriced("instance", "invalid_record", discovery.invalid)

    if run.config.platform == "pcs":
        _pcs_fee_lines(run, cost, instances)
    return cost


def _pcs_fee_lines(run, cost, instances):
    for inst in instances:
        if not inst.pcs_managed:
            continue
        key = "pcs|node|" + node_tier(inst.instance_type)
        rate, reason = cached_price(run, key, "pcs")
        if rate is None:
            cost.add_unpriced("fee", reason)
        else:
            cost.add_line(dict(instance_labels(inst), component="pcs_node_management"), rate)
            if cost.node_cost.get(inst.instance_id) is not None:
                cost.node_cost[inst.instance_id] += rate
        cost.fees.append({"component": "pcs_node_management", "instance_id": inst.instance_id,
                          "price_key": key, "usd_per_hour": rate, "unpriced_reason": reason})

    value, usable = pcs_cluster(run)
    if usable:
        run.used("pcs", run.cache.get("pcs|cluster").get("fetched_at"))
        if value["status"] not in PCS_ACTIVE_STATES:
            LOG.info("PCS cluster status %s: no controller or accounting fee", value["status"][:32])
            return
        fees = [("pcs_controller", "pcs|controller|" + value["size"])]
        if value["accounting_mode"] == "STANDARD":
            fees.append(("pcs_accounting", "pcs|accounting|" + value["size"]))
        for component, key in fees:
            rate, reason = cached_price(run, key, "pcs")
            if rate is None:
                cost.add_unpriced("fee", reason)
            else:
                cost.add_line({"component": component}, rate)
            cost.fees.append({"component": component, "price_key": key, "usd_per_hour": rate,
                              "unpriced_reason": reason})
        return
    # No usable cluster details (none, or older than 24 h): the controller line
    # is unpriced. The accounting line is unpriced too when the last details
    # had accounting STANDARD, and left out when there are no details at all.
    failing = run.cache.failed("pcs|cluster") or run.cache.backed_off("pcs", run.now)
    reason = "price_api_error" if failing else "price_lookup_pending"
    components = ["pcs_controller"]
    if value is not None and value.get("accounting_mode") == "STANDARD":
        components.append("pcs_accounting")
    for component in components:
        cost.add_unpriced("fee", reason)
        cost.fees.append({"component": component, "price_key": None, "usd_per_hour": None,
                          "unpriced_reason": reason})


# =============================================================================
# Allocation (users, accounts, idle)
# =============================================================================
def share_of(entry, node):
    """D4: the largest of the job's CPU, GPU and (when Slurm schedules it)
    memory share of the node, 1 for whole nodes, clamped to [0, 1]."""
    if entry.whole:
        return 1.0
    share = entry.cpus / float(node.cpus)
    if node.gpus > 0:
        share = max(share, entry.gpus / float(node.gpus))
    if entry.memory_counts and node.memory > 0:
        share = max(share, entry.memory / float(node.memory))
    return min(1.0, max(0.0, share))


def group_labels(inst):
    return {"queue": inst.queue, "ec2_instance_type": inst.instance_type, "purchase_option": inst.purchase_option}


class Allocation:
    """Step 7: the node cost of the priced compute instances, split among
    (username, account) and the idle and unallocated rest."""

    def __init__(self):
        self.users = {}         # label key (account, username) -> USD per hour
        self.unallocated = {}   # label key (reason, queue, ec2_instance_type, purchase_option) -> USD per hour
        self.idle_nodes = {}    # label key (queue, ec2_instance_type, purchase_option) -> count
        self.nodes = []         # per-node shares, for last-run.json

    @staticmethod
    def add(table, labels, value):
        k = label_key(labels)
        table[k] = table.get(k, 0) + value


def allocate(run, stage, slurm, discovery, cost):
    """Allocation steps 4-7. Every priced compute instance contributes its
    node cost K exactly once: to its jobs' (username, account) keys and the
    partial-node rest, or to idle_node."""
    alloc = Allocation()
    by_node = {}
    for entry in slurm.shares:
        by_node.setdefault(entry.node, []).append(entry)
    mapped = set()
    for name in sorted(slurm.mapping):
        inst, how = slurm.mapping[name]
        if inst.role != "compute":
            continue                    # head and login nodes are shared cost
        mapped.add(inst.instance_id)
        k = cost.node_cost.get(inst.instance_id)
        node = slurm.nodes[name]
        shares = [(e, share_of(e, node)) for e in by_node.get(name, [])]
        total = sum(s for _, s in shares)
        scale = total if total > 1 else 1.0
        alloc.nodes.append({"node": name, "instance_id": inst.instance_id, "matched_by": how, "usd_per_hour": k,
                            "share_sum": round(total, 9),
                            "shares": [{"job_id": e.job_id, "username": e.username, "account": e.account,
                                        "share": round(s / scale, 9)} for e, s in shares]})
        if k is None:
            continue                    # unpriced instance: left out of allocation
        group = group_labels(inst)
        for e, s in shares:
            alloc.add(alloc.users, {"username": e.username, "account": e.account}, k * s / scale)
        if total == 0:
            alloc.add(alloc.unallocated, dict(group, reason="idle_node"), k)
            alloc.add(alloc.idle_nodes, group, 1)
        else:
            alloc.add(alloc.unallocated, dict(group, reason="partial_node"), k * (1.0 - min(total, 1.0)))
    for inst in discovery.instances:
        k = cost.node_cost.get(inst.instance_id)
        if inst.role != "compute" or k is None:
            continue
        group = group_labels(inst)
        if inst.instance_id not in mapped:      # step 6: costs money, runs no job
            alloc.add(alloc.unallocated, dict(group, reason="idle_node"), k)
            alloc.add(alloc.idle_nodes, group, 1)
            alloc.nodes.append({"node": None, "instance_id": inst.instance_id, "matched_by": None,
                                "usd_per_hour": k, "share_sum": 0.0, "shares": []})
        for reason in UNALLOCATED_REASONS:      # step 7: known zeros
            alloc.add(alloc.unallocated, dict(group, reason=reason), 0.0)
        alloc.add(alloc.idle_nodes, group, 0)
    return alloc


# =============================================================================
# Accumulation and state (state.json)
# =============================================================================
def cluster_identity(config):
    return "%s/%s/%s" % (config.platform, config.cluster_id, config.region)


def state_key(key):
    """The state.json key of a label key: the JSON of its sorted pairs."""
    return json.dumps([list(pair) for pair in key], separators=(",", ":"))


def key_labels(skey):
    return tuple(tuple(pair) for pair in json.loads(skey))


OTHER_USER_KEY = state_key(label_key({"username": OTHER_USER, "account": OTHER_USER}))


def _valid_state_key(skey):
    try:
        pairs = json.loads(skey)
    except (TypeError, ValueError):
        return False
    if not isinstance(pairs, list):
        return False
    names = []
    for pair in pairs:
        if not (isinstance(pair, list) and len(pair) == 2 and all(isinstance(x, str) for x in pair)):
            return False
        if not LABEL_NAME_RE.match(pair[0]) or len(pair[1]) > LABEL_MAX:
            return False
        names.append(pair[0])
    return names == sorted(set(names))


def utc_month(t):
    d = datetime.datetime.fromtimestamp(t, datetime.timezone.utc)
    return "%04d-%02d" % (d.year, d.month)


def month_start(t):
    d = datetime.datetime.fromtimestamp(t, datetime.timezone.utc)
    return datetime.datetime(d.year, d.month, 1, tzinfo=datetime.timezone.utc).timestamp()


def next_month_start(t):
    d = datetime.datetime.fromtimestamp(t, datetime.timezone.utc)
    year, month = (d.year + 1, 1) if d.month == 12 else (d.year, d.month + 1)
    return datetime.datetime(year, month, 1, tzinfo=datetime.timezone.utc).timestamp()


def month_pieces(a, b):
    """[a, b] split at every UTC month boundary inside it."""
    pieces, t = [], a
    while t < b:
        end = min(b, next_month_start(t))
        pieces.append((t, end))
        t = end
    return pieces


class StateInvalid(ValueError):
    pass


def _need(condition, what):
    if not condition:
        raise StateInvalid(what)


class State:
    """The accumulators of state.json (schema 1)."""

    SCHEMA = 1

    def __init__(self, cluster):
        self.cluster = cluster
        self.started_at = None
        self.last_success_at = None
        self.month = None
        self.month_started_at = None
        self.families = {"lines": {"last_at": None, "last_rates": {}, "last_unpriced": {}},
                         "alloc": {"last_at": None, "last_rates": {}}}
        self.accumulated = {p: {b: {} for b in ACC_BUCKETS} for p in PERIODS}
        self.unaccounted = {p: {f: 0.0 for f in ACC_FAMILIES} for p in PERIODS}

    def to_json(self):
        return {"schema": self.SCHEMA, "cluster": self.cluster, "started_at": self.started_at,
                "last_success_at": self.last_success_at, "month": self.month,
                "month_started_at": self.month_started_at, "families": self.families,
                "accumulated": self.accumulated, "unaccounted_seconds": self.unaccounted}

    @classmethod
    def from_json(cls, data):
        """A State, or StateInvalid naming the first problem."""
        _need(isinstance(data, dict), "not an object")
        _need(data.get("schema") == cls.SCHEMA and not isinstance(data.get("schema"), bool), "schema")
        _need(isinstance(data.get("cluster"), str), "cluster")
        st = cls(data["cluster"])
        for name in ("started_at", "last_success_at", "month_started_at"):
            _need(data.get(name) is None or _finite(data.get(name)), name)
            setattr(st, name, data.get(name))
        month = data.get("month")
        _need(month is None or (isinstance(month, str) and MONTH_RE.match(month)), "month")
        _need((month is None) == (st.month_started_at is None), "month_started_at")
        _need(st.started_at is None or month is not None, "month")
        st.month = month
        families = data.get("families")
        _need(isinstance(families, dict), "families")
        for f in ACC_FAMILIES:
            fam = families.get(f)
            _need(isinstance(fam, dict), "families." + f)
            _need(fam.get("last_at") is None or _finite(fam.get("last_at")), "families.%s.last_at" % f)
            st.families[f]["last_at"] = fam.get("last_at")
            for rates in st.families[f]:
                if rates != "last_at":
                    st.families[f][rates] = _state_rates(fam.get(rates), "families.%s.%s" % (f, rates))
        accumulated = data.get("accumulated")
        _need(isinstance(accumulated, dict), "accumulated")
        unaccounted = data.get("unaccounted_seconds")
        _need(isinstance(unaccounted, dict), "unaccounted_seconds")
        for p in PERIODS:
            _need(isinstance(accumulated.get(p), dict), "accumulated." + p)
            for b in ACC_BUCKETS:
                st.accumulated[p][b] = _state_rates(accumulated[p].get(b), "accumulated.%s.%s" % (p, b))
            _need(isinstance(unaccounted.get(p), dict), "unaccounted_seconds." + p)
            for f in ACC_FAMILIES:
                _need(_finite(unaccounted[p].get(f), 0), "unaccounted_seconds.%s.%s" % (p, f))
                st.unaccounted[p][f] = float(unaccounted[p][f])
        return st


def _state_rates(obj, what):
    _need(isinstance(obj, dict), what)
    for k, v in obj.items():
        _need(_valid_state_key(k) and _finite(v, 0), what)
    return dict(obj)


class StateLoad:
    """How this run loaded state.json."""

    def __init__(self):
        self.source = None          # STATE_FILE or STATE_BACKUP, None for a fresh state
        self.valid_main = False     # a valid state.json was loaded: the save rotates it into the backup
        self.unreadable = False     # files existed but none was usable: StateUnreadable for this run


def _read_state(path):
    """(State, None), (None, "missing") or (None, problem)."""
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except FileNotFoundError:
        return None, "missing"
    except OSError as e:
        return None, e.strerror or str(e)
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, RecursionError):
        return None, "doesn't parse"
    try:
        return State.from_json(data), None
    except StateInvalid as e:
        return None, "invalid %s" % e


def load_state(config):
    """Step 2: state.json, else state.json.bak (ERROR), else a fresh state.
    A fresh state replacing files that exist is StateUnreadable. A state of
    another cluster starts fresh (WARNING)."""
    identity = cluster_identity(config)
    load = StateLoad()
    state, problem = _read_state(os.path.join(config.state_dir, STATE_FILE))
    if state is not None:
        load.source, load.valid_main = STATE_FILE, True
    else:
        if problem != "missing":
            LOG.error("%s unreadable (%s): trying %s", STATE_FILE, problem, STATE_BACKUP)
        state, backup_problem = _read_state(os.path.join(config.state_dir, STATE_BACKUP))
        if state is not None:
            load.source = STATE_BACKUP
            LOG.error("accumulators loaded from %s (%s %s)", STATE_BACKUP, STATE_FILE,
                      "missing" if problem == "missing" else "unreadable")
        elif problem == "missing" and backup_problem == "missing":
            LOG.info("no %s yet: first run", STATE_FILE)
        else:
            load.unreadable = True
            LOG.error("%s (%s) and %s (%s) are unusable: accumulation restarts (StateUnreadable)",
                      STATE_FILE, problem, STATE_BACKUP, backup_problem)
    if state is not None and state.cluster != identity:
        LOG.warning("%s holds the accumulators of %s, not %s: starting a fresh state", load.source,
                    state.cluster[:200], identity)
        state = None
    return (state if state is not None else State(identity)), load


def write_state_file(directory, obj, rotate):
    """design "Saving and loading": write state.json.tmp with fsync, rename
    state.json to state.json.bak, rename the temporary file to state.json and
    fsync the directory. When this run didn't load a valid state.json
    (rotate False), the unreadable state.json is renamed to
    state.json.corrupt first, and the backup is left as it is."""
    data = (json.dumps(obj, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
    path = os.path.join(directory, STATE_FILE)
    if not rotate and os.path.lexists(path):
        os.replace(path, os.path.join(directory, STATE_CORRUPT))
    tmp = os.path.join(directory, STATE_TMP)
    write_new_file(tmp, data, PRIVATE_FILE_MODE)
    try:
        if rotate and os.path.lexists(path):
            os.replace(path, os.path.join(directory, STATE_BACKUP))
        os.replace(tmp, path)
    except BaseException:
        _unlink_quietly(tmp)
        raise
    fsync_dir(directory)


class _Adder:
    """Adds increments to the accumulators of both periods: a piece goes to
    since_start, and to month only when it lies in state.month. New
    (username, account) keys beyond USER_KEY_CAP per period go to (other)."""

    def __init__(self, state):
        self.state = state
        self.capped = {}

    def route(self, period, bucket, key):
        acc = self.state.accumulated[period][bucket]
        if bucket != "users" or key in acc:
            return key
        if len(acc) - (1 if OTHER_USER_KEY in acc else 0) < USER_KEY_CAP:
            return key
        self.capped.setdefault(period, set()).add(key)
        return OTHER_USER_KEY

    def add(self, bucket, key, increment, in_month):
        for period in PERIODS:
            if period == "month" and not in_month:
                continue
            k = self.route(period, bucket, key)
            acc = self.state.accumulated[period][bucket]
            acc[k] = acc.get(k, 0.0) + increment

    def keep(self, bucket, key):
        """A current key is exported, with 0 if nothing was added yet."""
        for period in PERIODS:
            self.state.accumulated[period][bucket].setdefault(self.route(period, bucket, key), 0.0)

    def unaccounted(self, family, start, end):
        for t0, t1 in month_pieces(start, end):
            self.state.unaccounted["since_start"][family] += t1 - t0
            if utc_month(t0) == self.state.month:
                self.state.unaccounted["month"][family] += t1 - t0

    def warn_cap(self):
        for period in sorted(self.capped):
            LOG.warning("%d new (username, account) keys over the cap of %d for %s: added to (other)",
                        len(self.capped[period]), USER_KEY_CAP, period)


def integrate_family(state, family, buckets, now):
    """D8 for one family. buckets: (bucket, previous rates, current rates,
    seconds per unit: 3600 for USD per hour, 1 for resource counts)."""
    fam = state.families[family]
    a = fam["last_at"]
    adder = _Adder(state)
    if a is None:
        # The family's first good run integrates nothing. For alloc, the
        # time since tracking started is unaccounted.
        if family == "alloc" and state.started_at is not None:
            adder.unaccounted(family, state.started_at, now)
    elif now <= a:
        LOG.warning("%s accumulators: the clock moved backwards (%s, last run %s): nothing added",
                    family, utc_iso(now), utc_iso(a))
    elif now - a <= GAP_CAP:
        dt = now - a
        for t0, t1 in month_pieces(a, now):
            in_month = utc_month(t0) == state.month
            f0, f1 = (t0 - a) / dt, (t1 - a) / dt
            for bucket, previous, current, unit in buckets:
                for key in sorted(set(previous) | set(current)):
                    p, r = previous.get(key, 0.0), current.get(key, 0.0)
                    v0, v1 = p + (r - p) * f0, p + (r - p) * f1     # rate interpolated at a boundary
                    adder.add(bucket, key, (v0 + v1) / 2.0 * (t1 - t0) / unit, in_month)
    else:
        end = a + GAP_CAP
        for t0, t1 in month_pieces(a, end):
            in_month = utc_month(t0) == state.month
            for bucket, previous, current, unit in buckets:
                for key in sorted(previous):
                    adder.add(bucket, key, previous[key] * (t1 - t0) / unit, in_month)
        adder.unaccounted(family, end, now)
        LOG.info("%s accumulators: a gap of %.0f s: one hour added at the last rates, %.0f s unaccounted",
                 family, now - a, now - end)
    for bucket, previous, current, unit in buckets:
        for key in sorted(current):
            adder.keep(bucket, key)
    adder.warn_cap()
    fam["last_at"] = now


def _is_user_key(skey):
    return any(name == "username" for name, _ in key_labels(skey))


def accumulate(run, cost, alloc):
    """Step 8. The month rollover runs in every run. The lines family (cost
    lines and unpriced counts) integrates when discovery succeeded, the
    alloc family (users and unallocated) when the slurm stage did too."""
    st, now = run.state, run.now
    if cost is not None and st.started_at is None:
        st.started_at, st.month, st.month_started_at = now, utc_month(now), now
        LOG.info("tracking starts at %s", utc_iso(now))
    if st.month is not None and utc_month(now) > st.month:
        LOG.info("month rollover: %s to %s", st.month, utc_month(now))
        st.month, st.month_started_at = utc_month(now), month_start(now)
        st.accumulated["month"] = {b: {} for b in ACC_BUCKETS}
        st.unaccounted["month"] = {f: 0.0 for f in ACC_FAMILIES}
    if cost is None:
        return
    lines = {state_key(k): v for k, v in cost.lines.items()}
    unpriced = {state_key(label_key({"kind": kind, "reason": reason})): float(n)
                for (kind, reason), n in cost.unpriced.items() if n}
    fam = st.families["lines"]
    integrate_family(st, "lines", [("lines", fam["last_rates"], lines, HOUR),
                                   ("unpriced", fam["last_unpriced"], unpriced, 1.0)], now)
    fam["last_rates"], fam["last_unpriced"] = lines, unpriced
    if alloc is None:
        return
    users = {state_key(k): v for k, v in alloc.users.items()}
    unallocated = {state_key(k): v for k, v in alloc.unallocated.items()}
    fam = st.families["alloc"]
    previous_users = {k: v for k, v in fam["last_rates"].items() if _is_user_key(k)}
    previous_unallocated = {k: v for k, v in fam["last_rates"].items() if k not in previous_users}
    integrate_family(st, "alloc", [("users", previous_users, users, HOUR),
                                   ("unallocated", previous_unallocated, unallocated, HOUR)], now)
    merged = dict(users)
    merged.update(unallocated)
    fam["last_rates"] = merged


ACC_EXPORTS = (("lines", "cluster_cost_accumulated_usd"), ("users", "cluster_cost_user_accumulated_usd"),
               ("unallocated", "cluster_cost_unallocated_accumulated_usd"),
               ("unpriced", "cluster_cost_unpriced_resource_seconds"))


def export_accumulated(state, fam):
    """The accumulated series, the period starts and the unaccounted seconds
    (always), whatever this run integrated."""
    for period in PERIODS:
        for bucket, family in ACC_EXPORTS:
            for skey, value in state.accumulated[period][bucket].items():
                labels = dict(key_labels(skey))
                labels["period"] = period
                fam[family][label_key(labels)] = value
        for f in ACC_FAMILIES:
            fam["cluster_cost_unaccounted_seconds"][label_key({"period": period, "family": f})] = \
                state.unaccounted[period][f]
    if state.started_at is not None:
        starts = fam["cluster_cost_period_start_timestamp_seconds"]
        starts[label_key({"period": "since_start"})] = state.started_at
        starts[label_key({"period": "month"})] = max(state.month_started_at, state.started_at)


# =============================================================================
# Exposition and write
# =============================================================================
def escape_label(value):
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def format_value(value, fmt):
    if fmt == "usd":
        return "%.9f" % value
    if fmt == "count":
        return "%d" % value
    return "%.3f" % value


def render_exposition(families):
    """Prometheus text format 0.0.4: each family in one block (HELP, TYPE,
    then its series sorted by labels), in the fixed order, no timestamps."""
    out = []
    for name, fmt, help_text in FAMILIES:
        series = families.get(name)
        if not series:
            continue
        out.append("# HELP %s %s\n" % (name, help_text))
        out.append("# TYPE %s gauge\n" % name)
        for key in sorted(series):
            labels = ",".join('%s="%s"' % (k, escape_label(truncate(v))) for k, v in key)
            out.append("%s%s %s\n" % (name, "{%s}" % labels if labels else "", format_value(series[key], fmt)))
    return "".join(out)


def build_families(run, cost, applicable, alloc=None, previous_success=None):
    """The series of this run, per family. Returns (families, collector_up).
    alloc is the allocation of a successful slurm stage, or None: a failed
    slurm stage exports no user, idle-node or unallocated series."""
    fam = {name: {} for name in FAMILY_NAMES}
    unpriced = {}
    if cost is not None:
        fam["cluster_cost_usd_per_hour"] = dict(cost.lines)
        fam["cluster_cost_nodes"] = dict(cost.nodes)
        unpriced = cost.unpriced
        for (kind, reason), count in unpriced.items():
            if count:
                fam["cluster_cost_unpriced_resources"][label_key({"kind": kind, "reason": reason})] = count
        for source, fetched_at in run.ages.items():
            fam["cluster_cost_price_age_seconds"][label_key({"source": source})] = max(0.0, run.now - fetched_at)
        if alloc is not None:
            fam["cluster_cost_user_usd_per_hour"] = dict(alloc.users)
            fam["cluster_cost_unallocated_usd_per_hour"] = dict(alloc.unallocated)
            fam["cluster_cost_idle_nodes"] = dict(alloc.idle_nodes)
    export_accumulated(run.state, fam)
    up = run.health.collector_up(applicable, unpriced)
    fam["cluster_cost_collector_up"][()] = up
    for st in run.health.ran():
        fam["cluster_cost_collector_stage_ok"][label_key({"stage": st.name})] = 1 if st.ok else 0
        if not st.ok:
            fam["cluster_cost_collector_stage_error"][label_key({"stage": st.name, "code": st.code})] = 1
    last_success = run.now if up else previous_success
    if last_success is not None:
        fam["cluster_cost_collector_last_success_timestamp_seconds"][()] = last_success
    fam["cluster_cost_collector_run_duration_seconds"][()] = run.deadline.elapsed()
    fam["cluster_cost_collector_info"][label_key({
        "platform": run.config.platform, "cluster": run.config.cluster_id, "region": run.config.region})] = 1
    return fam, up


def write_textfile(directory, text):
    """Replace <dir>/cluster_cost.prom atomically, mode 0644."""
    write_file_atomic(directory, TEXTFILE_NAME, TEXTFILE_TMP, text.encode("utf-8", "replace"), TEXTFILE_MODE)


def write_run_report(directory, report):
    """<state dir>/last-run.json, mode 0640. Root-only; not exported."""
    write_json_atomic(directory, REPORT_FILE, report)


def slurm_report(run, alloc):
    """The part-2 fields of last-run.json: the Slurm client directory, the
    data_parser versions, the node mapping, the busy unmapped nodes and the
    per-node shares (kept also when the mapping check failed)."""
    slurm = run.slurm
    if slurm is None:
        return None
    return {
        "bin_dir": slurm.bin_dir,
        "found_by": slurm.found_by,
        "data_parser": slurm.data_parser,
        "nodes": len(slurm.nodes),
        "jobs": slurm.jobs,
        "mapping": [{"node": name, "instance_id": inst.instance_id, "role": inst.role, "matched_by": how}
                    for name, (inst, how) in sorted(slurm.mapping.items())],
        "unmapped_nodes": sorted(set(slurm.nodes) - set(slurm.mapping)),
        "busy_unmapped_nodes": slurm.busy_unmapped,
        "shares": alloc.nodes if alloc is not None else [],
    }


def build_report(run, cost, up, exit_code, alloc=None):
    return {
        "schema": 1,
        "started_at": utc_iso(run.now),
        "duration_seconds": round(run.deadline.elapsed(), 3),
        "platform": run.config.platform,
        "cluster": run.config.cluster_id,
        "region": run.config.region,
        "collector_up": up,
        "exit_code": exit_code,
        "stages": {s.name: s.describe() for s in run.health.ran()},
        "calls": run.calls,
        "pricing_calls": run.pricing_calls,
        "deferred": run.deferred,
        "backoff": {s: {"until": utc_iso(b["until"]), "code": b["code"]}
                    for s, b in sorted(run.cache.backoff.items()) if run.now < b["until"]},
        "total_usd_per_hour": round(cost.total, 9) if cost is not None else None,
        "unpriced": [{"kind": k, "reason": r, "count": n} for (k, r), n in sorted(cost.unpriced.items())]
        if cost is not None else [],
        "instances": cost.instances if cost is not None else [],
        "volumes": cost.volumes if cost is not None else [],
        "fees": cost.fees if cost is not None else [],
        "slurm": slurm_report(run, alloc),
    }


# =============================================================================
# Run sequence
# =============================================================================
def _state_write_failed(stage, name, error):
    # A failed save is the condition to fix, so it replaces a StateUnreadable
    # from the load: the next runs would otherwise keep showing that.
    if stage.direct is None or stage.direct[0] != "StateWriteFailed":
        stage.direct = ("StateWriteFailed", "%s: %s" % (name, getattr(error, "strerror", None) or error))
    LOG.error("couldn't save %s: %s", name, error)


def save_state(run, stage):
    """Step 9: save state.json, then prices.json."""
    if run.state_load.unreadable:
        stage.fail("StateUnreadable", "%s and %s were unusable: accumulation restarted" % (STATE_FILE, STATE_BACKUP))
    try:
        write_state_file(run.config.state_dir, run.state.to_json(), rotate=run.state_load.valid_main)
    except (OSError, ValueError) as e:
        _state_write_failed(stage, STATE_FILE, e)
    try:
        write_json_atomic(run.config.state_dir, PRICES_FILE, run.cache.to_json())
    except OSError as e:
        _state_write_failed(stage, PRICES_FILE, e)


def _unique_volumes(volumes):
    seen, out = set(), []
    for iid in sorted(volumes.records):
        for v in volumes.records[iid]["volumes"]:
            if v["VolumeId"] not in seen:
                seen.add(v["VolumeId"])
                out.append(v)
    return out


def _execute(config, deps, dry_run):
    run = Run(config, deps, dry_run)
    run.state, run.state_load = load_state(config)
    run.cache = PriceCache.load(os.path.join(config.state_dir, PRICES_FILE))
    applicable = applicable_stages(config, dry_run)
    discovery = run_stage(run, "discovery", discover)
    cost = alloc = None
    if discovery is not None:
        volumes = run_stage(run, "ebs", list_volumes, discovery.instances)
        if volumes is None:
            volumes = Volumes({}, [i.instance_id for i in discovery.instances], 0)
        slurm = run_stage(run, "slurm", read_slurm, discovery.instances)
        run_stage(run, "on_demand_prices", refresh_on_demand, discovery.instances)
        run_stage(run, "spot_prices", refresh_spot, discovery.instances)
        run_stage(run, "ebs", refresh_ebs_prices, _unique_volumes(volumes))
        if config.platform == "pcs":
            run_stage(run, "pcs", refresh_pcs, discovery.instances)
        cost = build_cost_lines(run, discovery, volumes)
        if slurm is not None:
            alloc = run_stage(run, "slurm", allocate, slurm, discovery, cost)
    slurm_stage = run.health.stages.get("slurm")
    good_alloc = alloc if alloc is not None and slurm_stage is not None and slurm_stage.ok else None
    accumulate(run, cost, good_alloc)
    previous_success = run.state.last_success_at
    unpriced = cost.unpriced if cost is not None else {}
    if not run.state_load.unreadable and \
            run.health.collector_up([s for s in applicable if s != "state"], unpriced):
        run.state.last_success_at = run.now         # saved with the state; exported only if the save works
    if not dry_run:
        run_stage(run, "state", save_state)

    families, up = build_families(run, cost, applicable, good_alloc, previous_success)
    text = render_exposition(families)
    exit_code = 0
    if dry_run:
        out = deps.stdout or sys.stdout
        out.write(text)
        out.flush()
    else:
        try:
            write_textfile(config.textfile_dir, text)
        except OSError as e:
            LOG.error("couldn't write %s: %s", os.path.join(config.textfile_dir, TEXTFILE_NAME), e)
            exit_code = 1
        state = run.health.stages.get("state")
        if state is not None and state.code == "StateWriteFailed":
            exit_code = 1
        try:
            write_run_report(config.state_dir, build_report(run, cost, up, exit_code, alloc))
        except (OSError, ValueError) as e:
            LOG.warning("couldn't write %s: %s", REPORT_FILE, e)

    failed = ", ".join("%s=%s" % f for f in run.health.failed()) or "none"
    if cost is None:
        LOG.info("run done: no estimate, %.1f s, failed stages: %s", run.deadline.elapsed(), failed)
    else:
        LOG.info("run done: %.9f USD/h, %d instances, %d volumes, %d users, %.1f s, failed stages: %s",
                 cost.total, len(discovery.instances), len(cost.volumes),
                 len({dict(k)["username"] for k in good_alloc.users}) if good_alloc is not None else 0,
                 run.deadline.elapsed(), failed)
    return exit_code


def run_once(config, deps, dry_run=False):
    """One run of the run sequence. Returns the exit code (0 or 1)."""
    lock = None
    if not dry_run:
        lock = acquire_lock(config.state_dir)
        if lock is None:
            LOG.info("another run holds the lock: nothing to do")
            return 0
    try:
        return _execute(config, deps, dry_run)
    finally:
        release_lock(lock)


def setup_logging(debug, stream):
    for h in list(LOG.handlers):
        if getattr(h, "cluster_cost", False):
            LOG.removeHandler(h)
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    handler.cluster_cost = True
    LOG.addHandler(handler)
    LOG.setLevel(logging.DEBUG if debug else logging.INFO)
    LOG.propagate = False


def main(argv=None, environ=None, deps=None):
    parser = argparse.ArgumentParser(
        prog="cluster-cost-collector",
        description="Estimate the cluster's cost for the Cluster Costs dashboard.")
    parser.add_argument("--dry-run", action="store_true",
                        help="run every stage and print the exposition; take no lock and write nothing")
    try:
        args = parser.parse_args(argv)
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else 2
    environ = os.environ if environ is None else environ
    deps = deps or Deps()
    if deps.environ is None:
        deps.environ = dict(environ)
    setup_logging(environ.get("CLUSTER_COST_DEBUG") == "1", deps.stderr or sys.stderr)
    try:
        config = load_config(environ)
        check_private_dir(config.state_dir, deps.owner_uid)
        check_private_dir(config.textfile_dir, deps.owner_uid)
    except ConfigError as e:
        LOG.error("invalid configuration: %s", e)
        return 2
    try:
        return run_once(config, deps, dry_run=args.dry_run)
    except Exception:
        LOG.exception("unexpected error outside a stage: nothing written")
        return 1


if __name__ == "__main__":
    sys.exit(main())
