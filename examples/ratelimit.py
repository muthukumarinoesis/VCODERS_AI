"""Per-project rate limits and quotas in Redis (spec 9.5).

Fixed windows: per minute, and per UTC day (reset at midnight UTC). A
refused request gives its count back, so only accepted work uses quota.
"""

import time
from dataclasses import dataclass

GLOBAL_JOBS_PER_DAY = 20_000


class RateLimited(Exception):
    code = "rate_limited"

    def __init__(self, limit: str, retry_after: int):
        self.limit, self.retry_after = limit, max(1, int(retry_after))
        self.message = f"Too many requests ({limit}). Try again in {self.retry_after} seconds."
        super().__init__(self.message)


@dataclass
class _Window:
    key: str
    ttl: int
    retry_after: int


class RateLimiter:
    def __init__(
        self, redis, projects: dict, global_jobs_per_day: int = GLOBAL_JOBS_PER_DAY, clock=time.time
    ):
        self.redis, self.projects = redis, projects
        self.global_jobs_per_day = global_jobs_per_day
        self.clock = clock

    def _minute(self, name: str) -> _Window:
        now = self.clock()
        return _Window(f"rl:{name}:m{int(now // 60)}", 120, 60 - int(now % 60))

    def _day(self, name: str) -> _Window:
        now = self.clock()
        return _Window(f"rl:{name}:d{int(now // 86400)}", 2 * 86400, 86400 - int(now % 86400))

    def _take(self, taken: list, window: _Window, limit: int, label: str, amount: int = 1) -> None:
        count = self.redis.incrby(window.key, amount)
        if count == amount:
            self.redis.expire(window.key, window.ttl)
        taken.append((window.key, amount))
        if count > limit:
            for key, n in taken:  # give back everything taken by this request
                self.redis.decrby(key, n)
            raise RateLimited(label, window.retry_after)

    def check(self, project, kind: str, active_jobs: int = 0) -> None:
        """kind "route" or "job". active_jobs: the project's queued + running jobs."""
        limits, key, taken = project.limits, project.key, []
        if kind == "route":
            self._take(
                taken, self._minute(f"{key}:route"), limits.route_per_minute, "route requests per minute"
            )
            pages = self.redis.get(self._day(f"{key}:pages").key)
            if pages is not None and int(pages) >= limits.pages_per_day:
                self._give_back(taken)
                raise RateLimited("pages inspected per day", self._day("x").retry_after)
        elif kind == "job":
            if active_jobs >= limits.jobs_at_once:
                raise RateLimited("jobs queued or running at once", 30)
            self._take(taken, self._minute(f"{key}:jobs"), limits.jobs_per_minute, "jobs per minute")
            self._take(taken, self._day(f"{key}:jobs"), limits.jobs_per_day, "jobs per day")
            self._take(taken, self._day("global:jobs"), self.global_jobs_per_day, "service jobs per day")
        else:
            raise ValueError(kind)

    def add_pages(self, project, pages: int) -> None:
        window = self._day(f"{project.key}:pages")
        if self.redis.incrby(window.key, pages) == pages:
            self.redis.expire(window.key, window.ttl)

    def _give_back(self, taken) -> None:
        for key, n in taken:
            self.redis.decrby(key, n)
