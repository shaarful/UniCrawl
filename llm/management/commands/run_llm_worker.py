"""Run the LLM extraction worker as its own process, separate from the crawler."""
from __future__ import annotations

import signal

import redis
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.management.base import BaseCommand, CommandError

from llm.gemini import GeminiExtractor, ServiceError
from llm.worker import ExtractionWorker


class Command(BaseCommand):
    help = "Extract disease events from matched articles on the Redis stream using Gemini."

    def add_arguments(self, parser):
        parser.add_argument(
            "--once",
            action="store_true",
            help="Exit when no messages are ready instead of waiting for more.",
        )

    def handle(self, *args, **options):
        try:
            extractor = GeminiExtractor()
        except (ImproperlyConfigured, OSError, ValueError) as exc:
            raise CommandError(f"Cannot start the LLM client: {exc}") from exc

        client = redis.Redis.from_url(
            settings.REDIS_URL,
            decode_responses=True,
            socket_timeout=30,  # well above the worker's 5 s blocking read
        )
        worker = ExtractionWorker(client, extractor, once=options["once"])
        signal.signal(signal.SIGINT, worker.stop)
        signal.signal(signal.SIGTERM, worker.stop)

        self.stdout.write(f"LLM worker {worker.consumer} reading {worker.stream}")
        try:
            worker.run()
        except (ServiceError, redis.RedisError) as exc:
            raise CommandError(f"Stopped by an outage: {exc}") from exc
        self.stdout.write(f"LLM worker stopped: {dict(worker.stats)}")
