"""Enqueue a steady random mix of tasks so every dashboard page has data."""

import random
import time

from django.core.management.base import BaseCommand

from tasks_app.tasks import (
    charge_card,
    import_customers,
    rebuild_search_index,
    send_welcome_email,
    tail_event_stream,
)


class Command(BaseCommand):
    help = "Loop forever, enqueueing a random mix of demo tasks every few seconds."

    def add_arguments(self, parser):
        parser.add_argument("--min-sleep", type=float, default=2.0)
        parser.add_argument("--max-sleep", type=float, default=6.0)
        parser.add_argument("--burst", action="store_true", help="enqueue one burst for the reports queue and exit")

    def handle(self, *args, min_sleep, max_sleep, burst, **options):
        if burst:
            return self.burst()
        self.stdout.write("demo traffic running (Ctrl+C to stop)")
        while True:
            self.tick()
            time.sleep(random.uniform(min_sleep, max_sleep))

    def tick(self):
        roll = random.random()
        if roll < 0.45:
            who = f"user{random.randint(1, 999)}@example.com"
            send_welcome_email.delay(who)
            self.stdout.write(f"send_welcome_email {who}")
        elif roll < 0.90:
            # About a third are over the 500 limit and fail: dead letters.
            amount = random.choice([20, 45, 99, 150, 320, 480, 650, 900, 1200])
            customer = random.randint(1, 500)
            charge_card.delay(customer, amount)
            self.stdout.write(f"charge_card {customer} {amount}")
        elif roll < 0.94:
            import_customers.delay(random.choice([20, 30, 40]))
            self.stdout.write("import_customers")
        elif roll < 0.995:
            tail_event_stream.delay(random.choice([10, 20]))
            self.stdout.write("tail_event_stream")
        else:
            self.burst()

    def burst(self):
        # The worker has a few slots (prefetch 1), so most of a burst waits in
        # the "reports" queue: depth goes over the alert's 20, then drains in
        # about a minute. Rare on its own (0.5% of ticks); `--burst` fires one.
        n = random.randint(26, 30)
        for _ in range(n):
            rebuild_search_index.delay(random.choice([8, 10, 12]))
        self.stdout.write(f"rebuild_search_index x{n} (backlog on 'reports')")
