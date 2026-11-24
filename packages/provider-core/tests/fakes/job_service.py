"""Scripted public API ports for concurrent service and native-host tests."""

import asyncio
from dataclasses import asdict, replace
import errno
from hashlib import sha256
import json
from urllib.parse import parse_qs, unquote, urlsplit

from tests.fakes.api import Transport, api, io_failure, reply
from tests.fakes.attempts import observation, progress_response, start_response
from tests.fakes.hello import receipt
from tests.fakes.job_input import facts, response


class JobApi:
    def __init__(self):
        self.jobs = {
            "job:runtime": ("runtime", b'{"value":0}'),
            "job:calculate": ("calculate", b'{"value":2}'),
            "job:held": ("calculate", b'{"value":-1}'),
            "job:healthy": ("healthy", b'{"value":3}'),
        }

        self.attempts = {}
        self.started = []
        self.announcements = []
        self.unavailable = False
        self.feed_requests = asyncio.Queue()

    def new_api(self):
        fixture = self

        class Replies(Transport):
            async def exchange(self, request, *, maximum_bytes, owner=None):
                url = urlsplit(str(request.url))
                path = unquote(url.path)

                if path.endswith("/hello"):
                    fixture.announcements.append(json.loads(request.content)["analysis_offerings"])

                    return reply(receipt())

                if path.endswith("/jobs"):
                    kind = parse_qs(url.query)["analysis_kind_ref"][0]

                    fixture.feed_requests.put_nowait(kind)

                    if fixture.unavailable:
                        raise io_failure(
                            "The shared API transport is unavailable",
                            primary=ConnectionRefusedError(errno.ECONNREFUSED, "API fixture is offline"),
                            stage="connect",
                        )

                    jobs = [
                        {
                            **asdict(replace(facts(payload), job_ref=job)),
                            "analysis_kind_ref": kind,
                            "created_at": "2026-10-18T00:00:00.123456Z",
                        }
                        for (job, (selected, payload)) in fixture.jobs.items()
                        if (selected == kind) and (job not in fixture.started)
                    ]

                    return reply(
                        json.dumps(
                            {
                                "schema_id": "nmr.provider.jobs.list.response.v1",
                                "analysis_kind_ref": kind,
                                "has_provider_execution_attempt": False,
                                "jobs": jobs,
                                "next_cursor": None,
                            }
                        )
                        .encode()
                    )

                if path.endswith("/input"):
                    job = path.split("/")[-2]

                    return reply(response(fixture.jobs[job][1], job_ref=job))

                if path.endswith("/start"):
                    job = json.loads(request.content)["job_ref"]
                    reference = "execution_attempt:sha256:" + sha256(job.encode()).hexdigest()
                    fixture.attempts[reference] = job

                    fixture.started.append(job)

                    return reply(
                        start_response(
                            execution_attempt_ref=reference,
                            job_ref=job,
                            analysis_kind_ref=fixture.jobs[job][0],
                        )
                    )

                if path.endswith("/progress"):
                    reference = path.split("/")[-2]
                    progress = json.loads(request.content)

                    return reply(
                        progress_response(
                            execution_attempt_ref=reference,
                            phase=progress["phase"],
                            condition_code=progress["condition_code"],
                        )
                    )

                reference = path.split("/")[-1]

                return reply(observation(execution_attempt_ref=reference, job_ref=fixture.attempts[reference]))

        return api(Replies(), "https://api.example")
