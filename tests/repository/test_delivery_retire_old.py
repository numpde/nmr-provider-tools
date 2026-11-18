"""Exercise old delivery socket retirement without a Docker daemon."""

from tools.container_guard import require_container

require_container()

import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest

from tests.repository.quality.sources import ROOT


class DeliveryRetireOldTests(unittest.TestCase):
    def run_retirement(self, scenario, names):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            docker = root / 'operations/docker'

            docker.mkdir(parents=True)
            retirement_script = root / 'operations/delivery/retire_old.sh'
            retirement_script.parent.mkdir(parents=True)
            shutil.copyfile(ROOT / 'operations/delivery/retire_old.sh', retirement_script)
            (root / 'volumes').write_text(''.join(name + '\n' for name in names))

            (docker / 'container_operation.sh').write_text(
                '''
operation_begin() {
    operation_directory=$PROBE_ROOT/operation
    mkdir "$operation_directory"
    docker_client_directory=$PROBE_ROOT/client
    operation_diagnostic=
    engine=(docker)
    operation_success=
    trap 'rm -f "$PROBE_ROOT/reservation"; printf "%s" "$operation_success"' EXIT
}
operation_create() { touch "$PROBE_ROOT/reservation"; }
operation_metadata() {
    local receipt=$1; shift
    case "$1 $2" in
        'image inspect') printf '%s delivery %064d\\n' "$PROBE_IMAGE" 0 > "$receipt" ;;
        'volume ls')
            : > "$receipt"
            while IFS= read -r name; do
                if [[ $4 == *"-ipc-"* && $name == "$PROBE_PROJECT-ipc-"* ]] ||
                   [[ $4 == *"-route-"* && $name == "$PROBE_PROJECT-route-"* ]]; then
                    printf '%s\\n' "$name" >> "$receipt"
                fi
            done < "$PROBE_ROOT/volumes"
            if [[ $PROBE_SCENARIO == malformed-final && -e $PROBE_ROOT/removed && $4 == *"-ipc-"* ]]; then printf '%s' "$PROBE_PROJECT-ipc-$PROBE_DIGEST" >> "$receipt"; fi
            ;;
        'volume inspect')
            local name=${!#} owner=$PROBE_PROJECT created=first driver=local
            if [[ $PROBE_SCENARIO == foreign ]]; then owner=other; fi
            if [[ $PROBE_SCENARIO == bad-driver ]]; then driver=other; fi
            if [[ $PROBE_SCENARIO == changed && -e $PROBE_ROOT/seen-$name ]]; then created=second; fi
            touch "$PROBE_ROOT/seen-$name"
            printf '%s|%s|%s|%s|null|%s\\n' "$name" "$owner" "$name" "$driver" "$created" > "$receipt"
            ;;
        'ps --all')
            : > "$receipt"
            if [[ $PROBE_SCENARIO == borrowed || $PROBE_SCENARIO == later-borrowed && ${!#} == *-route-* ]]; then printf '%064d\\n' 0 > "$receipt"; fi
            ;;
        *) return 22 ;;
    esac
}
operation_call() {
    local name=${!#}
    printf '%s\\n' "$name" >> "$PROBE_ROOT/removed"
    grep -Fxv "$name" "$PROBE_ROOT/volumes" > "$PROBE_ROOT/new" || :
    mv "$PROBE_ROOT/new" "$PROBE_ROOT/volumes"
    if [[ $PROBE_SCENARIO == interrupted ]]; then operation_diagnostic='native control receipt retained'; return 130; fi
}
'''
            )

            environment = os.environ | {
                'PROBE_ROOT': str(root),
                'PROBE_PROJECT': 'delivery-test',
                'PROBE_IMAGE': 'sha256:' + 'a' * 64,
                'PROBE_DIGEST': 'b' * 64,
                'PROBE_SCENARIO': scenario,
            }

            completed_process = subprocess.run(
                ['bash', str(retirement_script), environment['PROBE_PROJECT'], environment['PROBE_IMAGE']],
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )

            remaining = (root / 'volumes').read_text().splitlines()
            removed = []

            if (root / 'removed').exists():
                removed = (root / 'removed').read_text().splitlines()

            completed_process.reservation_retained = (root / 'reservation').exists()

            return (completed_process, remaining, removed)

    def test_removes_only_owned_old_sockets(self):
        (project, digest) = ('delivery-test', 'b' * 64)
        old = [f'{project}-ipc-{digest}', f'{project}-route-{digest}']
        protected = [f'{project}-queue', f'{project}-proxy-socket', f'{project}-outbox-socket']
        (retirement_process, remaining, removed) = self.run_retirement('normal', old + protected)

        self.assertEqual(0, retirement_process.returncode, msg=retirement_process.stderr)
        self.assertEqual(old, removed)
        self.assertEqual(protected, remaining)

    def test_uncertain_ownership_borrowers_and_changes_refuse_before_removal(self):
        name = 'delivery-test-ipc-' + 'b' * 64

        for scenario in ('foreign', 'bad-driver', 'borrowed', 'changed'):
            with self.subTest(scenario=scenario):
                (retirement_process, remaining, removed) = self.run_retirement(scenario, [name])

                self.assertNotEqual(0, retirement_process.returncode)
                self.assertEqual([name], remaining)
                self.assertEqual([], removed)

    def test_later_borrower_prevents_all_removals(self):
        digest = 'b' * 64
        old = [f'delivery-test-ipc-{digest}', f'delivery-test-route-{digest}']
        (retirement_process, remaining, removed) = self.run_retirement('later-borrowed', old)

        self.assertNotEqual(0, retirement_process.returncode)
        self.assertEqual(old, remaining)
        self.assertEqual([], removed)

    def test_interrupted_removal_reports_possible_effect_and_preserves_status(self):
        name = 'delivery-test-ipc-' + 'b' * 64
        (retirement_process, remaining, removed) = self.run_retirement('interrupted', [name])

        self.assertEqual(130, retirement_process.returncode)
        self.assertEqual([], remaining)
        self.assertEqual([name], removed)
        self.assertIn('may already be removed', retirement_process.stderr)
        self.assertIn('including its project reservation', retirement_process.stderr)
        self.assertIn('native control receipt retained', retirement_process.stderr)
        self.assertTrue(retirement_process.reservation_retained)

    def test_malformed_final_inventory_cannot_claim_success(self):
        name = 'delivery-test-ipc-' + 'b' * 64
        (retirement_process, _, _) = self.run_retirement('malformed-final', [name])

        self.assertNotEqual(0, retirement_process.returncode)
        self.assertIn('final volume inventory could not be confirmed', retirement_process.stderr)

    def test_no_old_volumes_is_idempotent(self):
        (retirement_process, remaining, removed) = self.run_retirement('normal', ['delivery-test-queue'])

        self.assertEqual(0, retirement_process.returncode, msg=retirement_process.stderr)
        self.assertEqual(['delivery-test-queue'], remaining)
        self.assertEqual([], removed)
