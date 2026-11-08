"""One accounting route: decisions here, SQL reservation in storage/state."""

import threading


class BudgetController:
    def __init__(self, state, policy, clock):
        self.state, self.policy, self.clock = state, policy, clock
        self.locks = {p: threading.Lock() for p in ("traffic", "weather")}
        self.next = {p: 0.0 for p in self.locks}
        self.semaphores = {
            "traffic": threading.Semaphore(policy.traffic_workers),
            "weather": threading.Semaphore(1),
        }

    def reserve(self, resource, snapshot, number, deadline):
        if not 1<=number<=self.policy.max_attempts:
            raise RuntimeError('RESOURCE_ATTEMPT_LIMIT_EXCEEDED')
        provider = resource.provider
        with self.locks[provider]:
            delay = max(0, self.next[provider] - self.clock.monotonic())
            if self.clock.monotonic() + delay >= deadline:
                raise RuntimeError("DEADLINE_EXCEEDED")
            if delay:
                self.clock.sleep(delay)
            if self.clock.monotonic() >= deadline:
                raise RuntimeError("DEADLINE_EXCEEDED")
            reservation = self.state.reserve(
                self.policy.scope,
                provider,
                snapshot,
                resource.resource_id,
                number,
                getattr(self.policy, provider + "_attempt_cap"),
                getattr(self.policy, provider + "_remaining"),
                daily_cap=getattr(self.policy,provider+'_daily_attempt_cap'),
                accounting_timezone=self.policy.accounting_timezone,
                clock=self.clock.now_utc,
            )
            if self.clock.monotonic()>=deadline:
                self.state.finish(reservation,'not_dispatched_deadline')
                raise RuntimeError('DEADLINE_EXCEEDED')
            self.next[provider] = self.clock.monotonic() + 1 / self.policy.rate_per_second
            return reservation

    def usage(self,provider):
        return self.state.daily_counts(self.clock.now_utc(),self.policy.accounting_timezone)[provider]
