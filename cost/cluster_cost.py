#!/usr/bin/python3 -I
#
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
"""Cluster cost collector for the Cluster Costs dashboard.

Runs as root from cluster-cost.timer every 60 seconds on the monitoring node
(the ParallelCluster head node or a PCS login node). Each run finds the
cluster's running instances, lists their EBS volumes, refreshes the cached
prices that are due, prices every instance, volume and PCS fee, and writes
the rates and the health of the run to node_exporter's textfile directory:

    /var/lib/prometheus/node-exporter/cluster_cost.prom

Configuration comes from the environment (the unit's EnvironmentFile,
/etc/aws-parallelcluster-monitoring/cluster-cost.env): PLATFORM, CLUSTER_ID
and REGION. The price cache and the run report live in the state directory,
/var/lib/aws-parallelcluster-monitoring/cost.

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
    names = ["discovery", "ebs", "on_demand_prices", "spot_prices"]
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


def write_file_atomic(directory, name, tmp_name, data, mode):
    """Write data to directory/name by temporary file, fsync and rename."""
    tmp = os.path.join(directory, tmp_name)
    final = os.path.join(directory, name)
    try:
        os.unlink(tmp)
    except FileNotFoundError:
        pass
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    try:
        try:
            _write_all(fd, data)
            os.fchmod(fd, mode)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, final)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
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


def build_families(run, cost, applicable):
    """The series of this run, per family. Returns (families, collector_up)."""
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
    up = run.health.collector_up(applicable, unpriced)
    fam["cluster_cost_collector_up"][()] = up
    for st in run.health.ran():
        fam["cluster_cost_collector_stage_ok"][label_key({"stage": st.name})] = 1 if st.ok else 0
        if not st.ok:
            fam["cluster_cost_collector_stage_error"][label_key({"stage": st.name, "code": st.code})] = 1
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


def build_report(run, cost, up, exit_code):
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
    }


# =============================================================================
# Run sequence
# =============================================================================
def save_state(run, stage):
    """Step 9: save prices.json."""
    try:
        write_json_atomic(run.config.state_dir, PRICES_FILE, run.cache.to_json())
    except OSError as e:
        stage.fail("StateWriteFailed", "%s: %s" % (PRICES_FILE, e.strerror or e))
        LOG.error("couldn't save %s: %s", PRICES_FILE, e)


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
    run.cache = PriceCache.load(os.path.join(config.state_dir, PRICES_FILE))
    discovery = run_stage(run, "discovery", discover)
    cost = None
    if discovery is not None:
        volumes = run_stage(run, "ebs", list_volumes, discovery.instances)
        if volumes is None:
            volumes = Volumes({}, [i.instance_id for i in discovery.instances], 0)
        run_stage(run, "on_demand_prices", refresh_on_demand, discovery.instances)
        run_stage(run, "spot_prices", refresh_spot, discovery.instances)
        run_stage(run, "ebs", refresh_ebs_prices, _unique_volumes(volumes))
        if config.platform == "pcs":
            run_stage(run, "pcs", refresh_pcs, discovery.instances)
        cost = build_cost_lines(run, discovery, volumes)
    if not dry_run:
        run_stage(run, "state", save_state)

    families, up = build_families(run, cost, applicable_stages(config, dry_run))
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
            write_run_report(config.state_dir, build_report(run, cost, up, exit_code))
        except (OSError, ValueError) as e:
            LOG.warning("couldn't write %s: %s", REPORT_FILE, e)

    failed = ", ".join("%s=%s" % f for f in run.health.failed()) or "none"
    if cost is None:
        LOG.info("run done: no estimate, %.1f s, failed stages: %s", run.deadline.elapsed(), failed)
    else:
        LOG.info("run done: %.9f USD/h, %d instances, %d volumes, %.1f s, failed stages: %s",
                 cost.total, len(discovery.instances), len(cost.volumes), run.deadline.elapsed(), failed)
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
