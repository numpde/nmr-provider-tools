"""Stop a real HTTP child while it returns its otherwise valid model reply."""

import json
from pathlib import Path
import signal
import sys
from unittest.mock import patch

from provider_core.chat_wire import framed, message_bytes
from provider_core.chat_worker import main


def run():
    mode, marker = sys.argv[1:3]

    def held_reply(stream, document):
        if document["kind"] != "response":
            raise AssertionError("The reply fault requires a successful real HTTP exchange")
        frame = framed(message_bytes(document))
        if mode == "header":
            count = 2
        elif mode == "payload":
            count = 4 + (len(frame) - 4) // 2
        elif mode == "exit":
            count = len(frame)
        else:
            raise AssertionError("Unknown child reply fault")
        stream.write(frame[:count])
        stream.flush()
        if mode == "exit":
            stream.close()
        Path(marker).write_text(json.dumps({"written": count, "length": len(frame)}))
        # The parent must reject partial bytes or an unretired complete reply.
        # This synchronous fault cannot finish through asyncio cancellation.
        while True:
            signal.pause()

    with patch("provider_core.chat_worker.write_message", held_reply):
        return main()


if __name__ == "__main__":
    raise SystemExit(run())
