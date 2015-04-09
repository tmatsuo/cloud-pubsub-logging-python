# -*- coding: utf-8 -*-
# Copyright 2015 Google Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Python logging handler implementation for Cloud Pub/Sub.

This module has logging.Handler implementation which sends the logs to
Cloud Pub/Sub[1] asynchronously. The logs are kept in an internal
queue and child workers will pick them up and send them in
background.

[1]: https://cloud.google.com/pubsub/docs

"""

import logging

try:
    from queue import Empty
    from queue import Queue  # pragma: NO COVER
except ImportError:
    from Queue import Empty
    from Queue import Queue

from threading import Thread

from pubsub_logging.utils import compat_urlsafe_b64encode
from pubsub_logging.utils import get_pubsub_client
from pubsub_logging.utils import publish_body


DEFAULT_WORKER_SIZE = 1
DEFAULT_RETRY_COUNT = 10
MAX_BATCH_SIZE = 1000


class AsyncPubsubHandler(logging.Handler):
    """A logging handler to publish logs to Cloud Pub/Sub in background."""
    def __init__(self, topic, worker_size=DEFAULT_WORKER_SIZE,
                 retry=DEFAULT_RETRY_COUNT, client=None):
        """The constructor of the handler.

        Args:
          topic: Cloud Pub/Sub topic name to send the logs.
          worker_size: The initial worker size.
          retry: How many times to retry upon Cloud Pub/Sub API failure,
                 defaults to 5.
          client: An optional Cloud Pub/Sub client to use. If not set, one is
                  built automatically, defaults to None.
        """
        super(AsyncPubsubHandler, self).__init__()
        self._topic = topic
        self._retry = retry
        self._worker_size = worker_size
        self._client = client
        self._q = Queue()
        self._should_exit = False
        self._children = []
        for i in range(self._worker_size):
            t = Thread(target=self.send_loop)
            self._children.append(t)
            t.start()

    def send_loop(self):
        """Thread loop for indefinitely sending logs to Cloud Pub/Sub."""
        if self._client:
            client = self._client
        else:  # pragma: NO COVER
            client = get_pubsub_client()
        while not self._should_exit:
            logs = []
            num = 0
            for i in range(MAX_BATCH_SIZE):
                try:
                    item = self._q.get(True, 0.1)
                    logs.append(item)
                    num += 1
                except Empty:
                    break
            if not logs:
                continue
            try:
                body = {'messages':
                        [{'data': compat_urlsafe_b64encode(self.format(r))}
                            for r in logs]}
                publish_body(client, body, self._topic, self._retry)
            except Exception:
                pass
            for i in range(num):
                self._q.task_done()

    def emit(self, record):
        """Puts the record to the internal queue."""
        self._q.put(record)

    def flush(self):
        """Blocks until the queue becomes empty."""
        self._q.join()

    def close(self):
        """Joins the child threads and call the superclass's close."""
        self._should_exit = True
        for t in self._children:
            t.join()
        super(AsyncPubsubHandler, self).close()