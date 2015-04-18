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

This module provides a logging.Handler implementation which sends the
logs to Cloud Pub/Sub[1] asynchronously. The logs are kept in an
internal queue and child workers will pick them up and send them in
background.

[1]: https://cloud.google.com/pubsub/docs

"""

import logging
import multiprocessing as mp

# For Python 2 and Python 3 compatibility.
try:
    from queue import Empty
except ImportError:  # pragma: NO COVER
    from Queue import Empty

from pubsub_logging import errors

from pubsub_logging.utils import compat_urlsafe_b64encode
from pubsub_logging.utils import get_or_create_topic
from pubsub_logging.utils import get_pubsub_client
from pubsub_logging.utils import publish_body


BATCH_SIZE = 1000
DEFAULT_POOL_SIZE = 1
DEFAULT_RETRY_COUNT = 10


class PubsubWorker(object):
    """A worker for publishing logs in child processes."""
    def __init__(self, client):
        self.should_exit = mp.Value('i', 0)
        self._client = client

    def send_loop(self, q, topic, retry, logger, fmt, publish_body):
        """Process loop for indefinitely sending logs to Cloud Pub/Sub.
        Args:
          q: mp.JoinableQueue instance to get the message from.
          topic: Cloud Pub/Sub topic name to send the logs.
          retry: How many times to retry upon Cloud Pub/Sub API failure.
          logger: A logger for informing failures within this function.
          fmt: A callable for formatting the logs.
          publish_body: A callable for sending the logs.
        """
        if not self._client:  # pragma: NO COVER
            self._client = get_pubsub_client()
        while not self.should_exit.value:
            try:
                logs = q.get(block=True, timeout=1)
            except Empty:
                continue
            try:
                body = {'messages':
                        [{'data': compat_urlsafe_b64encode(fmt(r))}
                            for r in logs]}
                publish_body(self._client, body, topic, retry)
            except errors.RecoverableError as e:
                # Records the exception and puts the logs back to the deque
                # and prints the exception to stderr.
                q.put(logs)
                logger.exception(e)
            except Exception as e:  # pragma: NO COVER
                logger.exception(e)
                logger.warn('There was a non recoverable error, exiting.')
                return
            q.task_done()


class AsyncPubsubHandler(logging.Handler):
    """A logging handler to publish logs to Cloud Pub/Sub in background."""
    def __init__(self, topic, worker_num=DEFAULT_POOL_SIZE,
                 retry=DEFAULT_RETRY_COUNT, client=None,
                 publish_body=publish_body, stderr_logger=None):
        """The constructor of the handler.

        Args:
          topic: Cloud Pub/Sub topic name to send the logs.
          worker_num: The number of workers, defaults to 1.
          retry: How many times to retry upon Cloud Pub/Sub API failure,
                 defaults to 5.
          client: An optional Cloud Pub/Sub client to use. If not set, one is
                  built automatically, defaults to None.
          publish_body: A callable for publishing the Pub/Sub message,
                        just for testing and benchmarking purposes.
          stderr_logger: A logger for informing failures with this
                         logger, defaults to None and if not specified, a last
                         resort logger will be used.
        """
        super(AsyncPubsubHandler, self).__init__()
        self._q = mp.JoinableQueue()
        self._batch_size = BATCH_SIZE
        self._buf = []
        self._workers = []
        if client:
            _client = client
        else:
            _client = get_pubsub_client()
        self._worker = PubsubWorker(client)
        get_or_create_topic(_client, topic, retry)
        if not stderr_logger:
            stderr_logger = logging.Logger('last_resort')
            stderr_logger.addHandler(logging.StreamHandler())
        for _ in range(worker_num):
            p = mp.Process(target=self._worker.send_loop,
                           args=(self._q, topic, retry, stderr_logger,
                                 self.format, publish_body))
            p.daemon = True
            self._workers.append(p)
            p.start()

    def emit(self, record):
        """Puts the record to the internal queue."""
        self._buf.append(record)
        if len(self._buf) == self._batch_size:
            self._q.put(self._buf)
            self._buf = []

    def flush(self):
        """Blocks until the queue becomes empty."""
        with self.lock:
            if self._buf:
                self._q.put(self._buf)
                self._buf = []
            self._q.join()

    def close(self):
        """Joins the child processes and call the superclass's close."""
        with self.lock:
            self.flush()
            self._worker.should_exit.value = 1
            for p in self._workers:
                p.join()
        super(AsyncPubsubHandler, self).close()
