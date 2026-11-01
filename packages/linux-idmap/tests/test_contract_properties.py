"""Deterministic map properties and projected-evidence contracts, not engine qualification."""

from dataclasses import asdict, replace
from itertools import permutations
import json
import random
import unittest

from helpers import CID, IMAGE, container_for, request
from linux_idmap.addons.docker import Container, DockerError, INSPECT_FORMAT
from linux_idmap.core import IdRange, parse_map as parse_id_map
from linux_idmap.addons.host import HostMap
from linux_idmap.addons.model import Bounds, StepError
from linux_idmap.addons.spec import ProbeSpec


class MapContractTests(unittest.TestCase):
    def test_gapped_permuted_maps_match_independent_integer_oracle(self):
        randomizer = random.Random(1729)
        for case in range(40):
            inside = randomizer.randrange(4)
            outside = randomizer.randrange(100, 120)
            rows = []
            oracle = {}
            for _ in range(3):
                length = randomizer.randrange(1, 7)
                rows.append(IdRange(inside, outside, length))
                # Enumerate corresponding IDs rather than repeat translate's interval formula.
                oracle.update(zip(range(inside, inside + length), range(outside, outside + length)))
                inside += length + randomizer.randrange(1, 5)
                outside += length + randomizer.randrange(1, 5)
            for ordering in permutations(rows):
                raw = "\n".join(f" {row.inside}\t{row.outside} {row.length} " for row in ordering).encode()
                parsed = parse_id_map(raw + b"\n")
                direct = ordering
                with self.subTest(case=case, rows=ordering):
                    self.assertEqual(direct, parsed)
                    for identifier in range(inside + 1):
                        # Synthetic coordinate-qualified fixtures test affine arithmetic.
                        for mapping in (HostMap(direct), HostMap(parsed)):
                            if identifier in oracle:
                                self.assertEqual(oracle[identifier], mapping.translate(identifier))
                            else:
                                with self.assertRaises(ValueError):
                                    mapping.translate(identifier)

    def test_assignable_32_bit_boundary_maps_without_wrapping(self):
        maximum = 2**32 - 2
        rows = (IdRange(0, maximum - 2, 3), IdRange(maximum - 2, 0, 3))
        oracle = {0: maximum - 2, 1: maximum - 1, 2: maximum,
                  maximum - 2: 0, maximum - 1: 1, maximum: 2}
        for ordering in permutations(rows):
            mapping = HostMap(ordering)
            for identifier, expected in oracle.items():
                self.assertEqual(expected, mapping.translate(identifier))
            for identifier in (-1, 3, maximum - 3, maximum + 1, maximum + 2, True, False, 1.0, "1", None):
                with self.subTest(identifier=identifier), self.assertRaises(ValueError):
                    mapping.translate(identifier)
        full = HostMap(parse_id_map(b"0 0 4294967295\n"))
        self.assertEqual(maximum, full.translate(maximum))



class IsolationEvidenceContractTests(unittest.TestCase):
    def test_inspect_projection_requests_the_complete_evidence_contract(self):
        self.assertEqual(
            '{"cid":{{json .Id}},"name":{{json .Name}},"image_id":{{json .Image}},'
            '"token":{{json (index .Config.Labels "linux-idmap.token")}},'
            '"owner":{{json (index .Config.Labels "linux-idmap.operation")}},'
            '"pid":{{json .State.Pid}},"running":{{json .State.Running}},'
            '"started_at":{{json .State.StartedAt}},'
            '"user":{{json .Config.User}},"userns_mode":{{json .HostConfig.UsernsMode}},'
            '"entrypoint":{{json .Config.Entrypoint}},"command":{{json .Config.Cmd}},'
            '"mounts":{{json .Mounts}},"isolation":{'
            '"AutoRemove":{{json .HostConfig.AutoRemove}},'
            '"RestartPolicy":{{json .HostConfig.RestartPolicy}},'
            '"NetworkMode":{{json .HostConfig.NetworkMode}},'
            '"ReadonlyRootfs":{{json .HostConfig.ReadonlyRootfs}},'
            '"CapDrop":{{json .HostConfig.CapDrop}},'
            '"SecurityOpt":{{json .HostConfig.SecurityOpt}},'
            '"Privileged":{{json .HostConfig.Privileged}},'
            '"PidMode":{{json .HostConfig.PidMode}},'
            '"CapAdd":{{json .HostConfig.CapAdd}},'
            '"Devices":{{json .HostConfig.Devices}},'
            '"DeviceRequests":{{json .HostConfig.DeviceRequests}}}}',
            INSPECT_FORMAT,
        )

    def setUp(self):
        self.spec = ProbeSpec("b" * 32, IMAGE, replace(request(), userns_mode="host"))

    def test_spec_retains_the_validated_caller_policy_without_copying_fields(self):
        policy = replace(request(), user_uid=7, user_gid=11, interpreter="/opt/python",
                         userns_mode="host", bounds=Bounds(lifetime_seconds=19))
        spec = ProbeSpec("b" * 32, IMAGE, policy)
        self.assertIs(policy, spec.request)
        arguments = spec.create_arguments()
        self.assertEqual("7:11", arguments[arguments.index("--user") + 1])
        self.assertEqual("/opt/python", arguments[arguments.index("--entrypoint") + 1])
        self.assertIn("--userns=host", arguments)
        self.assertEqual(("19", spec.token), spec.command[-2:])
        spec.verify(container_for(spec), CID)

    def test_spec_uses_the_same_policy_validation_as_the_public_request(self):
        for changes in ({"user_uid": True}, {"user_gid": -1}, {"interpreter": "python"},
                        {"userns_mode": "unrecognized"}, {"bounds": Bounds(lifetime_seconds=0)}):
            policy = replace(request(), **changes)
            with self.subTest(changes=changes):
                with self.assertRaises(StepError) as direct:
                    policy.validate()
                with self.assertRaises(StepError) as owned:
                    ProbeSpec("b" * 32, IMAGE, policy)
                self.assertEqual((direct.exception.category, str(direct.exception)),
                                 (owned.exception.category, str(owned.exception)))

    def test_duplicate_json_keys_are_rejected_at_each_object_depth(self):
        source = json.dumps(asdict(container_for(self.spec)))
        for key, value in (("cid", CID), ("AutoRemove", True)):
            member = json.dumps(key) + ": " + json.dumps(value)
            duplicate = source.replace(member, member + ", " + member, 1)
            with self.subTest(key=key), self.assertRaises(DockerError) as caught:
                Container.from_json(duplicate, CID)
            self.assertEqual("invalid_response", caught.exception.category)

    def test_unused_status_is_not_part_of_the_private_evidence_projection(self):
        self.assertNotIn(".State.Status", INSPECT_FORMAT)
        value = asdict(container_for(self.spec))
        value["status"] = "running"
        with self.assertRaises(DockerError) as caught:
            Container.from_json(json.dumps(value), CID)
        self.assertEqual("invalid_response", caught.exception.category)

    def test_every_projected_container_field_must_be_present(self):
        complete = asdict(container_for(self.spec))
        for field in complete:
            value = {key: item for key, item in complete.items() if key != field}
            with self.subTest(field=field):
                with self.assertRaises(DockerError) as caught:
                    Container.from_json(json.dumps(value), CID)
                self.assertEqual("invalid_response", caught.exception.category)

    def test_container_fields_reject_wrong_types_and_oversized_argument_vectors(self):
        invalid = (
            ("pid", True), ("pid", -1), ("running", 1), ("started_at", False),
            ("entrypoint", [False]), ("command", "not-an-argument-vector"),
            ("entrypoint", ["arg"] * 17), ("command", ["arg"] * 17),
        )
        for field, invalid_value in invalid:
            value = asdict(container_for(self.spec))
            value[field] = invalid_value
            with self.subTest(field=field, value=invalid_value):
                with self.assertRaises(DockerError) as caught:
                    Container.from_json(json.dumps(value), CID)
                self.assertEqual("invalid_response", caught.exception.category)

    def test_container_parser_accepts_zero_pid_and_argument_vector_boundaries(self):
        for count in (0, 16):
            value = asdict(container_for(self.spec))
            value.update(pid=0, running=False, entrypoint=["arg"] * count, command=["arg"] * count)
            with self.subTest(argument_count=count):
                container = Container.from_json(json.dumps(value), CID)
                self.assertEqual(0, container.pid)
                self.assertIs(False, container.running)
                self.assertEqual(("arg",) * count, container.entrypoint)
                self.assertEqual(("arg",) * count, container.command)

    def test_changed_isolation_values_are_refused(self):
        changed = {
            "AutoRemove": (False, 1), "NetworkMode": ("host", None),
            "ReadonlyRootfs": (False, 1), "CapDrop": ([], "ALL"),
            "SecurityOpt": ([], ["no-new-privileges:false"]),
            "Privileged": (True, 0), "PidMode": ("host",),
            "CapAdd": (["SYS_ADMIN"],), "Devices": ([{"PathOnHost": "/dev/sda"}],),
            "DeviceRequests": ([{"Count": -1}],),
            "RestartPolicy": ({"Name": "always"}, None),
        }
        for field, values in changed.items():
            for value in values:
                container = container_for(self.spec)
                container.isolation[field] = value
                with self.subTest(field=field, value=value):
                    with self.assertRaises(StepError) as caught:
                        self.spec.verify(container, CID)
                    self.assertIn(caught.exception.category, ("ownership", "confinement"))

    def test_nonempty_mounts_are_refused(self):
        container = replace(container_for(self.spec), mounts=[{"Destination": "/probe"}])
        with self.assertRaises(StepError) as caught:
            self.spec.verify(container, CID)
        self.assertIn(caught.exception.category, ("ownership", "confinement"))

    def test_every_projected_isolation_field_must_be_present(self):
        for field in container_for(self.spec).isolation:
            value = asdict(container_for(self.spec))
            del value["isolation"][field]
            with self.subTest(field=field):
                with self.assertRaises(DockerError) as caught:
                    Container.from_json(json.dumps(value), CID)
                self.assertEqual("invalid_response", caught.exception.category)

    def test_unexpected_projected_isolation_field_is_rejected(self):
        value = asdict(container_for(self.spec))
        value["isolation"]["Unexpected"] = None
        with self.assertRaises(DockerError) as caught:
            Container.from_json(json.dumps(value), CID)
        self.assertEqual("invalid_response", caught.exception.category)

    def test_present_optional_nulls_and_empty_values_remain_compatible(self):
        for pid_mode in (None, ""):
            for optional_list in (None, []):
                value = asdict(container_for(self.spec))
                value["isolation"].update(PidMode=pid_mode, CapAdd=optional_list,
                                          Devices=optional_list, DeviceRequests=optional_list)
                with self.subTest(pid_mode=pid_mode, optional_list=optional_list):
                    container = Container.from_json(json.dumps(value), CID)
                    self.spec.verify(container, CID)
