"""A deterministic engine/host pair for lifecycle tests, not Docker qualification."""

from dataclasses import replace

from provider_deploy.docker import Container, EngineIdentity
from provider_deploy.linux import IdMap, IdRange, ProcessObservation
from provider_deploy.model import Bounds, ProbeRequest
from provider_deploy.spec import OWNER, _RESTRICTIONS

CID = "c" * 64
IMAGE = "sha256:" + "a" * 64
OBSERVATION = ProcessObservation(42, 123, 7, 99,
    IdMap((IdRange(0, 100000, 65536),)), IdMap((IdRange(0, 200000, 65536),)))


def request():
    return ProbeRequest("unix:///test.sock", "approved:local", "/usr/bin/python3",
                        1000, 2000, bounds=Bounds(1, .2, .1, 30))


def container_for(spec):
    isolation = {field: expected for _flag, _arg, field, expected in _RESTRICTIONS}
    isolation.update(Privileged=False, PidMode="", CapAdd=None, Devices=None,
                     DeviceRequests=None, RestartPolicy={"Name": "no"})
    # silent-defaults: allow-contract -- The fixture models Docker inspect encoding of explicit default versus host user namespaces; tests/test_operation.py::OperationTests.
    return Container(CID, spec.name, IMAGE, spec.token, OWNER, 0, False, "created", "not-started",
                     f"{spec.request.user_uid}:{spec.request.user_gid}", "" if spec.request.userns_mode == "default" else "host",
                     (spec.request.interpreter,), spec.command, [], isolation)


class Engine:
    identity = EngineIdentity("unix:///test.sock", "engine", 1, 2, (), "/var/lib/docker", "overlay2", "linux")

    def __init__(self):
        self.events = []
        self.container = None
        self.on_create = self.on_start = self.on_remove = self.on_inspect = None
        self.create_failure = self.remove_failure = None
        self.hide_creation = False

    def image_id(self, image, **kwargs):
        self.events.append("image")
        return IMAGE

    def create(self, spec, **kwargs):
        self.events.append("create")
        self.spec = spec
        if not self.hide_creation:
            self.container = container_for(spec)
        if self.on_create:
            self.on_create(self)
        if self.create_failure:
            raise self.create_failure
        return CID

    def start(self, cid, **kwargs):
        self.events.append("start")
        self.container = replace(self.container, running=True, pid=42, status="running", started_at="started")
        if self.on_start:
            self.on_start(self)

    def inspect(self, cid, **kwargs):
        self.events.append("inspect")
        if self.on_inspect:
            self.on_inspect(self)
        return self.container

    def candidates(self, token, **kwargs):
        self.events.append("candidates")
        # silent-defaults: allow-contract -- The deterministic engine inventory contains its one extant container or is empty; this is modeled resource absence.
        return (CID,) if self.container else ()

    def remove(self, cid, **kwargs):
        self.events.append("remove")
        assert cid == CID and kwargs.get("cancel") is None
        if self.on_remove:
            self.on_remove(self)
        if self.remove_failure:
            raise self.remove_failure
        self.container = None

    def assert_unchanged(self, **kwargs):
        self.events.append("engine-check")

    def close(self):
        self.events.append("close")


class Guard:
    def __init__(self):
        self.observation = OBSERVATION
        self.on_observe = None
        self.retired = True

    def host_evidence(self):
        return "host-evidence"

    def observe(self, pid, token):
        if self.on_observe:
            self.on_observe(self)
        return self.observation

    def identify(self, pid, token):
        return self.observation

    def pid_absent(self, pid):
        return self.retired

    def process_retired(self, observation):
        return self.retired
